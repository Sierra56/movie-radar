import asyncio
import json
import statistics
from datetime import date, datetime, timedelta

from .core import db, scheduler, get_transmission_settings
from .catalog import get_pattern, is_watched
from .trackers import check_distribution_now, build_transmission_client
from .notify import notify_torrent_completed


def effective_interval_hours(dist):
    trans = get_transmission_settings() or {}
    base = float(dist.get("check_interval_hours") or trans.get("default_check_interval") or 6)
    if dist.get("mode") == "fixed":
        return base
    interval = base
    if dist.get("last_new_files_at"):
        try:
            days = (datetime.now() - datetime.fromisoformat(dist["last_new_files_at"])).days
            if days <= 7:
                interval *= 0.5
            elif days >= 180:
                interval *= 4
            elif days >= 60:
                interval *= 2
        except ValueError:
            pass
    now = datetime.now()
    pat = get_pattern(dist["id"])
    if (pat and (pat.get("samples_count") or 0) >= (pat.get("min_samples") or 3)
            and pat.get("median_delay_hours") and dist.get("next_episode_air_date")):
        samples = json.loads(pat["samples_json"] or "[]")
        hours = [s.get("air_hour") for s in samples if "air_hour" in s]
        median_hour = int(statistics.median(hours)) if hours else 0
        try:
            air = date.fromisoformat(dist["next_episode_air_date"])
            predicted = datetime(air.year, air.month, air.day, median_hour, 0) + timedelta(hours=pat["median_delay_hours"])
            if predicted - timedelta(hours=6) <= now <= predicted + timedelta(hours=36):
                interval = min(interval, 1.0)
        except ValueError:
            pass
    if dist.get("next_episode_air_date"):
        try:
            delta = (date.fromisoformat(dist["next_episode_air_date"]) - date.today()).days
            if -1 <= delta <= 1:
                interval = min(interval, 1.0)
        except ValueError:
            pass
    return max(0.5, interval)


async def check_distributions_job():
    trans = get_transmission_settings() or {}
    if not trans.get("enabled") or not trans.get("auto_check_enabled", 1):
        return
    rows = db("SELECT * FROM distributions")
    if not rows:
        return
    now = datetime.now()
    checked = 0
    for r in rows:
        dist = dict(r)  # sqlite3.Row не имеет .get() — конвертируем в dict
        try:
            interval = effective_interval_hours(dist)
            due = True
            if dist["last_checked_at"]:
                try:
                    due = (now - datetime.fromisoformat(dist["last_checked_at"])) >= timedelta(hours=interval)
                except ValueError:
                    due = True
            if not due:
                continue
            ok, msg = await check_distribution_now(dist["title_external_id"])
            checked += 1
            print(f"[auto-check] {dist['title_external_id']}: {msg}")
            await asyncio.sleep(10)
        except Exception as e:
            print(f"[auto-check] error {dist['title_external_id']}: {e}")
    if checked:
        print(f"[auto-check] checked {checked}")


def schedule_distribution_job():
    trans = get_transmission_settings() or {}
    tick = max(5, min(60, int(trans.get("auto_check_tick_minutes") or 10)))
    scheduler.add_job(check_distributions_job, "interval", minutes=tick,
                      id="distribution_check", replace_existing=True, max_instances=1, coalesce=True)


async def check_transmission_job():
    trans = get_transmission_settings() or {}
    if not trans.get("enabled"):
        return
    rows = db("""SELECT h.id, h.transmission_hash, h.file_name, h.new_files_json, t.title as card_title
                 FROM download_history h
                 LEFT JOIN distributions d ON h.distribution_id = d.id
                 LEFT JOIN titles t ON d.title_external_id = t.external_id
                 WHERE h.completed_at IS NULL""")
    if not rows:
        return
    try:
        client = build_transmission_client()
    except Exception:
        return
    for r in rows:
        try:
            st = client.get_torrent_status(r["transmission_hash"])
        except Exception:
            st = None
        if not st:
            continue
        if (st.get("progress") or 0) >= 100 or st.get("is_finished"):
            db("UPDATE download_history SET completed_at=datetime('now') WHERE id=?", (r["id"],), write=True)
            title = r["card_title"] or r["file_name"]
            try:
                nf = json.loads(r["new_files_json"] or "[]")
            except Exception:
                nf = []
            await notify_torrent_completed(title, r["file_name"], st.get("size") or 0, nf)
            print(f"[transmission-poll] completed: {r['file_name']}")


def schedule_transmission_poll_job():
    trans = get_transmission_settings() or {}
    minutes = max(1, min(60, int(trans.get("transmission_poll_minutes") or 3)))
    scheduler.add_job(check_transmission_job, "interval", minutes=minutes,
                      id="transmission_poll", replace_existing=True, max_instances=1, coalesce=True)


async def auto_clean_job():
    trans = get_transmission_settings() or {}
    if not trans.get("enabled") or not trans.get("auto_clean_enabled"):
        return
    days = int(trans.get("auto_clean_days") or 30)
    on_watch = trans.get("auto_clean_on_watch")
    now = datetime.now()
    cutoff = now - timedelta(days=days)
    try:
        client = build_transmission_client()
    except Exception:
        return
    if days > 0:
        rows = db("""SELECT h.id, h.transmission_hash FROM download_history h
                     WHERE h.completed_at IS NOT NULL AND h.completed_at < ?""",
                  (cutoff.isoformat(),))
        for r in rows:
            try:
                client.remove_torrent(r["transmission_hash"], delete_data=True)
                db("DELETE FROM download_history WHERE id=?", (r["id"],), write=True)
                print(f"[auto-clean] removed (age): {r['transmission_hash']}")
            except Exception as e:
                print(f"[auto-clean] remove error: {e}")
    if on_watch:
        rows = db("""SELECT h.id, h.transmission_hash, d.title_external_id,
                            h.episode_season, h.episode_number
                     FROM download_history h
                     JOIN distributions d ON h.distribution_id = d.id
                     WHERE h.episode_season IS NOT NULL""")
        for r in rows:
            if is_watched(r["title_external_id"], r["episode_season"], r["episode_number"]):
                try:
                    client.remove_torrent(r["transmission_hash"], delete_data=True)
                    db("DELETE FROM download_history WHERE id=?", (r["id"],), write=True)
                    print(f"[auto-clean] removed (watched): S{r['episode_season']}E{r['episode_number']}")
                except Exception as e:
                    print(f"[auto-clean] remove error: {e}")


def schedule_auto_clean_job():
    scheduler.add_job(auto_clean_job, "interval", hours=6,
                      id="auto_clean", replace_existing=True, max_instances=1, coalesce=True)