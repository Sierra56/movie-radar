import hashlib
import json

from .core import (db, encrypt_value, decrypt_value, get_proxy_url, get_transmission_settings)
from .rutracker import (RuTrackerClient, RuTrackerError, RuTrackerCaptchaError,
                        RuTrackerAuthError, RuTrackerForbiddenError, BaseTracker)
from .kinozal import (KinozalClient, KinozalError, KinozalAuthError, KinozalForbiddenError)
from .rutor import RutorClient, RutorError
from .transmission import TransmissionClient
from .catalog import (record_learning_samples, parse_episode, maybe_notify_season_completed)
from .notify import notify_torrent_started


def get_tracker_credentials(tracker_name):
    rows = db("SELECT * FROM tracker_credentials WHERE tracker_name=?", (tracker_name,))
    return dict(rows[0]) if rows else None


def get_distribution(title_external_id):
    rows = db("SELECT * FROM distributions WHERE title_external_id=?", (title_external_id,))
    return dict(rows[0]) if rows else None


def load_tracker_cookies(tracker_name):
    creds = get_tracker_credentials(tracker_name)
    if not creds or not creds.get("encrypted_cookies"):
        return None
    try:
        raw = decrypt_value(creds["encrypted_cookies"])
        return json.loads(raw) if raw else None
    except Exception as e:
        print(f"[tracker] Failed to load cookies: {e}")
        return None


def build_tracker_client(tracker_name: str = "rutracker", cookies=None) -> BaseTracker:
    creds = get_tracker_credentials(tracker_name) or {}
    username = creds.get("username", "")
    password = decrypt_value(creds.get("encrypted_password", "")) if creds.get("encrypted_password") else ""
    user_agent = creds.get("user_agent", "")
    effective_cookies = cookies if cookies is not None else load_tracker_cookies(tracker_name)
    if tracker_name == "kinozal":
        return KinozalClient(username=username, password=password, proxy=get_proxy_url(),
                             cookies=effective_cookies, user_agent=user_agent)
    if tracker_name == "rutor":
        return RutorClient(proxy=get_proxy_url(), user_agent=user_agent)
    return RuTrackerClient(username=username, password=password, proxy=get_proxy_url(),
                           cookies=effective_cookies, user_agent=user_agent)


def build_transmission_client():
    """Возвращает активный клиент загрузок: Transmission или Deluge."""
    trans = get_transmission_settings() or {}
    ct = trans.get("client_type") or "transmission"
    if ct == "deluge":
        from .deluge import DelugeClient
        return DelugeClient(url=trans.get("deluge_url") or "http://localhost:8115",
                            password=decrypt_value(trans.get("deluge_password", "")) if trans.get("deluge_password") else "")
    return TransmissionClient(host=trans.get("host", "localhost"), port=trans.get("port", 9091),
                              username=trans.get("username", ""),
                              password=decrypt_value(trans.get("encrypted_password", "")) if trans.get("encrypted_password") else "")


def _resolve_download_dir(dist, trans):
    if trans.get("default_download_behavior", "use_distribution_path") == "use_distribution_path" and dist.get("download_path"):
        return dist["download_path"]
    return trans.get("base_download_dir") or None


async def check_distribution_now(title_external_id):
    dist = get_distribution(title_external_id)
    if not dist:
        return False, "Раздача не найдена"
    creds = get_tracker_credentials(dist["tracker_name"])
    if not creds or not creds.get("enabled"):
        return False, "Трекер не включён в настройках"
    cookies = load_tracker_cookies(dist["tracker_name"])
    username = creds.get("username", "")
    password = decrypt_value(creds.get("encrypted_password", "")) if creds.get("encrypted_password") else ""
    if not cookies and not (username and password):
        return False, "Не настроены учётные данные трекера"
    client = build_tracker_client(dist["tracker_name"], cookies=cookies)
    if not cookies:
        try:
            cookies = await client.login()
        except (RuTrackerCaptchaError, KinozalAuthError):
            db("UPDATE tracker_credentials SET last_error='captcha' WHERE tracker_name=?", (dist["tracker_name"],), write=True)
            return False, "Трекер требует капчу"
        except (RuTrackerAuthError, RuTrackerForbiddenError, KinozalAuthError, KinozalForbiddenError) as e:
            db("UPDATE tracker_credentials SET last_error=? WHERE tracker_name=?", (str(e), dist["tracker_name"]), write=True)
            return False, str(e)
        except (RuTrackerError, RutorError, KinozalError) as e:
            db("UPDATE tracker_credentials SET last_error=? WHERE tracker_name=?", (str(e), dist["tracker_name"]), write=True)
            return False, str(e)
        db("UPDATE tracker_credentials SET encrypted_cookies=?, last_login_at=datetime('now'), last_error=NULL, error_count=0 WHERE tracker_name=?",
           (encrypt_value(json.dumps(cookies)), dist["tracker_name"]), write=True)
    try:
        files = await client.fetch_files(dist["torrent_id"], cookies)
    except (RuTrackerForbiddenError, KinozalForbiddenError) as e:
        db("UPDATE distributions SET status='error', error_message=? WHERE id=?", (str(e), dist["id"]), write=True)
        if username and password:
            try:
                nc = await client.login()
                db("UPDATE tracker_credentials SET encrypted_cookies=?, last_login_at=datetime('now') WHERE tracker_name=?",
                   (encrypt_value(json.dumps(nc)), dist["tracker_name"]), write=True)
                files = await client.fetch_files(dist["torrent_id"], nc)
            except Exception as re_:
                return False, f"Ошибка: {e} (повторная: {re_})"
        else:
            return False, f"{e} Обновите cookies в настройках."
    except (RuTrackerError, RutorError, KinozalError) as e:
        db("UPDATE distributions SET status='error', error_message=? WHERE id=?", (str(e), dist["id"]), write=True)
        return False, str(e)
    if not files:
        db("UPDATE distributions SET status='error', error_message='Не удалось распарсить список файлов (debug-дамп сохранён)' WHERE id=?", (dist["id"],), write=True)
        return False, "Не удалось распарсить список файлов. Debug-дамп сохранён."
    snapshot = sorted([(f["name"], f["size"]) for f in files])
    new_hash = hashlib.md5(json.dumps(snapshot, ensure_ascii=False).encode()).hexdigest()
    old_hash = dist["last_files_hash"]
    new_files = []
    if old_hash and old_hash != new_hash:
        try:
            old_files = json.loads(dist["last_files_json"] or "[]")
        except json.JSONDecodeError:
            old_files = []
        old_names = {f[0] for f in old_files}
        new_files = [f for f in snapshot if f[0] not in old_names]
    new_count = len(new_files)

    if new_count:
        status, nfc, da, dla = "has_new", new_count, 0, 0
    else:
        nfc = dist["new_files_count"] or 0
        da = dist["dot_ack"] or 0
        dla = dist["dl_ack"] or 0
        status = "has_new" if (nfc > 0 and not dla) else "idle"
    db("""UPDATE distributions SET last_checked_at=datetime('now'), last_files_hash=?, last_files_json=?,
          status=?, new_files_count=?, dot_ack=?, dl_ack=?, error_count=0, error_message=NULL WHERE id=?""",
       (new_hash, json.dumps(snapshot, ensure_ascii=False), status, nfc, da, dla, dist["id"]), write=True)
    if new_count and old_hash:
        db("UPDATE distributions SET last_new_files_at=datetime('now') WHERE id=?", (dist["id"],), write=True)
        record_learning_samples(dist, new_files)
    if new_count > 0:
        trans = get_transmission_settings()
        if trans and trans.get("enabled") and trans.get("auto_download_new_files") and trans.get("action_on_new") != "notify_only":
            try:
                td = await client.download_torrent(dist["torrent_id"], cookies)
                dd = _resolve_download_dir(dist, trans)
                paused = trans.get("action_on_new") == "pause"
                result = build_transmission_client().add_torrent(td, dd, paused)
                db("UPDATE distributions SET status='idle', dl_ack=1 WHERE id=?", (dist["id"],), write=True)
                ep = parse_episode(result["name"])
                ep_s = ep[0] if ep else None
                ep_n = ep[1] if ep else None
                db("""INSERT INTO download_history
                      (distribution_id, file_name, file_size, transmission_hash,
                       episode_season, episode_number, sent_at)
                      VALUES (?,?,?,?,?,?,datetime('now'))""",
                   (dist["id"], result["name"], result["size"], result["hash"], ep_s, ep_n), write=True)
                cr = db("SELECT title FROM titles WHERE external_id=?", (title_external_id,))
                await notify_torrent_started(cr[0]["title"] if cr else title_external_id, result["name"], dd)
                await maybe_notify_season_completed(title_external_id, result["name"])
            except Exception as e:
                print(f"[dist-check] Auto-download failed: {e}")
    if not old_hash:
        return True, f"Первая проверка: найдено {len(files)} файлов, снапшот сохранён"
    if new_count:
        return True, f"Обнаружено новых файлов: {new_count}"
    return True, "Изменений нет"