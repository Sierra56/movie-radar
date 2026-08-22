import asyncio
import json
import uuid
import traceback
from datetime import date, timedelta

import httpx
from fastapi import APIRouter, Form, Request, BackgroundTasks
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse, Response)

from .core import (templates, db, get_setting, set_setting, get_refresh_hours,
                   refresh_period_label, get_telegram_settings, save_telegram_settings,
                   get_transmission_settings, human_date, short_date, plural,
                   progress_percent, format_size, parse_torrent_url, encrypt_value,
                   decrypt_value, refresh_progress, scheduler, parse_tmdb_id,
                   get_proxy_url, parse_episode)
from .sources import SOURCES, ensure_proxied, download_card_poster
from .catalog import (get_season_count, get_next_season, get_next_episode_date,
                      get_show_progress, get_season_progress, get_watched_set,
                      toggle_watched, toggle_season_watched, refresh_catalog, refresh_single,
                      build_ics, get_pattern, save_seasons, save_episodes,
                      update_next_episode_air_date, ensure_pattern, recompute_pattern,
                      maybe_notify_season_completed)
from .notify import (notify_new_card, check_and_notify, notify_date_changes,
                     notify_new_season, notify_new_episodes, notify_torrent_started,
                     notify_torrent_completed, notify_season_completed,
                     schedule_telegram_job, send_telegram)
from .trackers import (get_distribution, check_distribution_now, build_transmission_client,
                       build_tracker_client, get_tracker_credentials, load_tracker_cookies,
                       _resolve_download_dir)
from .jobs import (schedule_distribution_job, schedule_transmission_poll_job,
                   schedule_auto_clean_job, check_transmission_job)
from .rutracker import (RuTrackerCaptchaError, RuTrackerAuthError, RuTrackerForbiddenError)
from .kinozal import KinozalAuthError, KinozalForbiddenError

router_pages = APIRouter()
router_settings = APIRouter()
router_dist = APIRouter()


# ═══════════════ СТРАНИЦЫ ═══════════════
@router_pages.get("/", response_class=HTMLResponse)
async def index(request: Request, sort: str = "date", err: str | None = None, msg: str | None = None):
    order = {"date": "release_date IS NULL, release_date", "title": "title COLLATE NOCASE",
             "genre": "genres IS NULL OR genres = '', genres COLLATE NOCASE, title COLLATE NOCASE"}.get(sort, "release_date IS NULL, release_date")
    rows = db(f"SELECT * FROM titles ORDER BY {order}")
    today = date.today()
    cards, patterns = [], {}
    for r in rows:
        c = dict(r)
        c["notify_enabled"] = c.get("notify_enabled") in (None, 1)
        c["badge"], c["released"] = None, False
        if c["release_date"]:
            c["year"] = c["release_date"][:4]
            try:
                delta = (date.fromisoformat(c["release_date"]) - today).days
                if delta < 0:
                    c["badge"], c["released"] = "уже вышло", True
                elif delta == 0:
                    c["badge"] = "сегодня!"
                else:
                    c["badge"] = f"{delta} {plural(delta, ('день', 'дня', 'дней'))}"
            except ValueError:
                pass
        else:
            c["year"] = None
        c["genres_list"] = [g.strip() for g in (c["genres"] or "").split(",") if g.strip()][:2]
        if c["type"] == "series":
            c["season_count"] = get_season_count(c["external_id"])
            w, t = get_show_progress(c["external_id"])
            c["watch_label"] = f"{w}/{t}" if t > 0 else None
            c["watch_percent"] = progress_percent(w, t)
            c["watch_total"] = t
            next_ep = get_next_episode_date(c["external_id"])
            c["next_episode_short"] = short_date(next_ep) if next_ep else None
            ns = get_next_season(c["external_id"])
            c["next_season_date_human"] = human_date(ns["release_date"]) if ns else None
        else:
            c["watch_total"] = 0
            c["next_episode_short"] = None
        c["display_poster"] = ensure_proxied(c.get("poster_url"))
        dist = get_distribution(c["external_id"])
        c["has_distribution"] = dist is not None
        c["distribution_status"] = dist["status"] if dist else None
        c["new_count"] = dist["new_files_count"] if dist else 0
        c["show_dot"] = bool(dist and (dist["new_files_count"] or 0) > 0 and not (dist["dot_ack"] or 0))
        c["show_badge"] = bool(dist and (dist["new_files_count"] or 0) > 0 and not (dist["dl_ack"] or 0))
        if dist:
            c.update(distribution_url=dist["url"], distribution_download_path=dist["download_path"],
                     distribution_mode=dist["mode"],
                     distribution_check_interval_hours=dist["check_interval_hours"],
                     distribution_tracker=dist["tracker_name"])
            pat = get_pattern(dist["id"])
            if pat:
                samples = json.loads(pat["samples_json"] or "[]")
                patterns[c["external_id"]] = {"median": pat["median_delay_hours"], "count": pat["samples_count"],
                                              "confidence": pat["confidence"], "min_samples": pat["min_samples"] or 3,
                                              "samples": [{"s": s.get("season"), "e": s.get("episode"), "d": s.get("delay_hours")} for s in samples[-5:]]}
        cards.append(c)
    success_messages = {"refresh-started": "Обновление запущено в фоне.", "card-updated": "Карточка обновлена.",
                        "dist-added": "Раздача добавлена.", "dist-updated": "Раздача обновлена. Нажмите ⟳ после сохранения.",
                        "dist-removed": "Раздача удалена.", "dist-checked": get_setting("last_dist_check") or "Проверка выполнена.",
                        "dist-downloaded": get_setting("last_dist_download") or "Торрент отправлен в клиент.",
                        "pattern-saved": "Настройки обучения сохранены.", "pattern-reset": "Обучение сброшено."}
    error_messages = {"dist-exists": "Раздача уже добавлена.", "dist-invalid-url": "Неверная ссылка на раздачу.",
                      "dist-check-fail": get_setting("last_dist_check") or "Ошибка проверки раздачи.",
                      "dist-download-fail": get_setting("last_dist_download") or "Ошибка скачивания."}
    return templates.TemplateResponse(request, "index.html", {
        "cards": cards, "sort": sort, "patterns_json": json.dumps(patterns, ensure_ascii=False),
        "error": "Ничего не нашлось — уточните название." if err else None,
        "message": success_messages.get(msg), "error_message": error_messages.get(msg)})


@router_pages.get("/new", response_class=HTMLResponse)
async def new_card_page(request: Request, msg: str | None = None):
    return templates.TemplateResponse(request, "add.html", {
        "sources": list(SOURCES.keys()), "default_source": request.cookies.get("source", "tmdb"),
        "message": {"added-local": "Карточка добавлена локально.", "added": "Карточка добавлена."}.get(msg),
        "error_message": {"search-fail": "Не удалось получить данные по выбранной карточке."}.get(msg)})


@router_pages.post("/search")
async def search(query: str = Form(...), source: str = Form("tmdb")):
    src = SOURCES.get(source, SOURCES["tmdb"])
    try:
        candidates = await src.search_candidates(query)
    except Exception as e:
        print(f"[search] Error: {e}")
        candidates = []
    for c in candidates:
        if c.get("poster_url"):
            c["poster_url"] = ensure_proxied(c["poster_url"])
    return JSONResponse({"candidates": candidates})


@router_pages.post("/add")
async def add_local(title: str = Form(...), release_date: str | None = Form(None)):
    local_id = f"local:{uuid.uuid4().hex[:12]}"
    db("INSERT INTO titles (external_id, title, type, release_date, poster_url, genres, source, updated_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
       (local_id, title.strip(), None, release_date or None, None, "", "local"), write=True)
    await notify_new_card(title.strip(), release_date, "local", None)
    return RedirectResponse("/new?msg=added-local", status_code=303)


@router_pages.post("/add-select")
async def add_select(external_id: str = Form(...), source: str = Form("tmdb"), release_date: str | None = Form(None)):
    src = SOURCES.get(source)
    if not src:
        return RedirectResponse("/new?msg=search-fail", status_code=303)
    try:
        info = await src.fetch(external_id)
    except Exception as e:
        print(f"[add-select] Error: {e}")
        info = None
    if not info:
        return RedirectResponse("/new?msg=search-fail", status_code=303)
    rd = info["release_date"] or release_date or None
    db("INSERT OR REPLACE INTO titles (external_id, title, type, release_date, poster_url, genres, source, updated_at, tmdb_status) VALUES (?,?,?,?,?,?,?,datetime('now'),?)",
       (info["external_id"], info["title"], info["type"], rd, await download_card_poster(info), info["genres"], src.name, info.get("status")), write=True)
    if info["type"] == "series" and src.name == "tmdb":
        tmdb_id = parse_tmdb_id(info["external_id"])
        if tmdb_id is not None:
            try:
                seasons = await src.fetch_seasons(tmdb_id)
                await save_seasons(info["external_id"], seasons)
                for season in seasons:
                    try:
                        await save_episodes(info["external_id"], season["season_number"],
                                            await src.fetch_episodes(tmdb_id, season["season_number"]))
                        await asyncio.sleep(0.3)
                    except Exception:
                        pass
                update_next_episode_air_date(info["external_id"])
            except Exception as e:
                print(f"[add-select] Error seasons: {e}")
    await notify_new_card(info["title"], rd, src.name, info["type"])
    resp = RedirectResponse("/new?msg=added", status_code=303)
    resp.set_cookie("source", src.name, max_age=60 * 60 * 24 * 365)
    return resp


@router_pages.post("/refresh")
async def refresh(background: BackgroundTasks):
    background.add_task(refresh_catalog)
    return RedirectResponse("/?msg=refresh-started", status_code=303)


@router_pages.get("/refresh-status")
async def refresh_status():
    return refresh_progress


@router_pages.post("/refresh/{external_id}")
async def refresh_card(external_id: str, sort: str = "date"):
    await refresh_single(external_id)
    return RedirectResponse(f"/?sort={sort}&msg=card-updated", status_code=303)


@router_pages.get("/export.ics")
async def export_ics():
    return Response(content=build_ics([dict(r) for r in db("SELECT * FROM titles")]),
                    media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=movie-radar.ics"})


@router_pages.post("/delete/{external_id}")
async def delete(external_id: str, sort: str = "date"):
    for q in ("DELETE FROM titles WHERE external_id=?", "DELETE FROM seasons WHERE title_external_id=?",
              "DELETE FROM watched_episodes WHERE title_external_id=?", "DELETE FROM distributions WHERE title_external_id=?"):
        db(q, (external_id,), write=True)
    return RedirectResponse(f"/?sort={sort}", status_code=303)


@router_pages.post("/toggle-notify/{external_id}")
async def toggle_notify(external_id: str, sort: str = "date"):
    db("UPDATE titles SET notify_enabled = CASE WHEN notify_enabled=1 THEN 0 ELSE 1 END WHERE external_id=?", (external_id,), write=True)
    return RedirectResponse(f"/?sort={sort}", status_code=303)


@router_pages.post("/notify-all/{state}")
async def notify_all(state: str, sort: str = "date"):
    db("UPDATE titles SET notify_enabled=?", (1 if state == "on" else 0,), write=True)
    return RedirectResponse(f"/?sort={sort}", status_code=303)


@router_pages.get("/title/{external_id}", response_class=HTMLResponse)
async def title_page(request: Request, external_id: str):
    rows = db("SELECT * FROM titles WHERE external_id=?", (external_id,))
    if not rows:
        return RedirectResponse("/", status_code=303)
    card = dict(rows[0])
    card["date_human"] = human_date(card["release_date"])
    season_list = []
    for s in db("SELECT * FROM seasons WHERE title_external_id=? ORDER BY season_number", (external_id,)):
        sd = dict(s)
        sd["date_human"] = human_date(sd["release_date"])
        w, t = get_season_progress(external_id, sd["season_number"])
        sd.update(watched_count=w, total_count=t, percent=progress_percent(w, t))
        season_list.append(sd)
    card["poster_url"] = ensure_proxied(card.get("poster_url"))
    for sd in season_list:
        sd["poster_url"] = ensure_proxied(sd.get("poster_url"))
    w, t = get_show_progress(external_id)
    return templates.TemplateResponse(request, "title.html", {"card": card, "seasons": season_list,
                                                             "show_watched": w, "show_total": t, "show_percent": progress_percent(w, t)})


@router_pages.post("/title/{external_id}/refresh-seasons")
async def refresh_seasons(external_id: str):
    rows = db("SELECT * FROM titles WHERE external_id=?", (external_id,))
    if not rows:
        return RedirectResponse("/", status_code=303)
    card = dict(rows[0])
    src = SOURCES.get(card["source"])
    if src and src.name == "tmdb" and card["type"] == "series":
        tmdb_id = parse_tmdb_id(external_id)
        if tmdb_id is not None:
            try:
                seasons = await src.fetch_seasons(tmdb_id)
                ns = await save_seasons(external_id, seasons)
                for n in ns:
                    await notify_new_season(card["title"], n["season_number"], n["release_date"])
                all_new = []
                for season in seasons:
                    try:
                        all_new.extend(await save_episodes(external_id, season["season_number"],
                                                           await src.fetch_episodes(tmdb_id, season["season_number"])))
                    except Exception:
                        pass
                if all_new:
                    await notify_new_episodes(card["title"], all_new)
                update_next_episode_air_date(external_id)
            except Exception as e:
                print(f"[seasons] Error: {e}")
    return RedirectResponse(f"/title/{external_id}", status_code=303)


@router_pages.get("/title/{external_id}/season/{season_number}", response_class=HTMLResponse)
async def season_page(request: Request, external_id: str, season_number: int):
    rows = db("SELECT * FROM titles WHERE external_id=?", (external_id,))
    if not rows:
        return RedirectResponse("/", status_code=303)
    card = dict(rows[0])
    srows = db("SELECT * FROM seasons WHERE title_external_id=? AND season_number=?", (external_id, season_number))
    if not srows:
        return RedirectResponse(f"/title/{external_id}", status_code=303)
    season = dict(srows[0])
    season["date_human"] = human_date(season["release_date"])
    ws = get_watched_set(external_id, season_number)
    episodes = []
    for e in db("SELECT * FROM episodes WHERE season_id=? ORDER BY episode_number", (season["id"],)):
        ed = dict(e)
        ed["date_human"] = human_date(ed["release_date"])
        ed["watched"] = ed["episode_number"] in ws
        ed["poster_url"] = ensure_proxied(ed.get("poster_url"))
        episodes.append(ed)
    w, t = get_season_progress(external_id, season_number)
    return templates.TemplateResponse(request, "season.html", {"card": card, "season": season, "episodes": episodes,
                                                              "watched_count": w, "total_count": t,
                                                              "percent": progress_percent(w, t), "all_watched": t > 0 and w == t})


@router_pages.post("/watch/{external_id}/{season_number}/{episode_number}")
async def watch_episode(external_id: str, season_number: int, episode_number: int):
    toggle_watched(external_id, season_number, episode_number)
    return RedirectResponse(f"/title/{external_id}/season/{season_number}", status_code=303)


@router_pages.post("/watch-season/{external_id}/{season_number}")
async def watch_season(external_id: str, season_number: int):
    toggle_season_watched(external_id, season_number)
    return RedirectResponse(f"/title/{external_id}", status_code=303)


@router_pages.get("/downloads", response_class=HTMLResponse)
async def downloads_page(request: Request, msg: str | None = None):
    rows = db("""SELECT h.*, t.title as card_title, d.status as dist_status
                 FROM download_history h
                 LEFT JOIN distributions d ON h.distribution_id = d.id
                 LEFT JOIN titles t ON d.title_external_id = t.external_id
                 ORDER BY h.sent_at DESC LIMIT 200""")
    messages = {"downloads-refreshed": "Статусы загрузок обновлены.",
                "downloads-removed-all": "Все торренты удалены из клиента, журнал очищен.",
                "download-removed": "Загрузка удалена."}
    return templates.TemplateResponse(request, "downloads.html",
                                      {"rows": [dict(r) for r in rows], "message": messages.get(msg)})


@router_pages.post("/downloads/refresh")
async def downloads_refresh():
    await check_transmission_job()
    return RedirectResponse("/downloads?msg=downloads-refreshed", status_code=303)


@router_pages.post("/downloads/remove-all")
async def downloads_remove_all():
    rows = db("SELECT * FROM download_history")
    try:
        client = build_transmission_client()
    except Exception as e:
        print(f"[downloads] cannot connect to client: {e}")
        client = None
    for r in rows:
        if client:
            try:
                client.remove_torrent(r["transmission_hash"], delete_data=False)
            except Exception as e:
                print(f"[downloads] remove failed: {e}")
    db("DELETE FROM download_history", write=True)
    return RedirectResponse("/downloads?msg=downloads-removed-all", status_code=303)


@router_pages.post("/downloads/remove/{history_id}")
async def downloads_remove(history_id: int):
    rows = db("SELECT * FROM download_history WHERE id=?", (history_id,))
    if rows:
        try:
            build_transmission_client().remove_torrent(rows[0]["transmission_hash"], delete_data=False)
        except Exception as e:
            print(f"[downloads] remove from client failed: {e}")
        db("DELETE FROM download_history WHERE id=?", (history_id,), write=True)
    return RedirectResponse("/downloads?msg=download-removed", status_code=303)


# ═══════════════ НАСТРОЙКИ ═══════════════
@router_settings.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, msg: str | None = None):
    tracker_rt = get_tracker_credentials("rutracker")
    tracker_kz = get_tracker_credentials("kinozal")
    tracker_ru = get_tracker_credentials("rutor")
    return templates.TemplateResponse(request, "settings.html", {
        "tg": get_telegram_settings(), "refresh_hours": get_refresh_hours(),
        "refresh_label": refresh_period_label(get_refresh_hours()),
        "log_rows": [dict(r) for r in db("SELECT * FROM updates_log ORDER BY created_at DESC LIMIT 200")],
        "proxy_url": get_setting("proxy_url", "") or "", "theme": get_setting("theme", "dark"),
        "trans": get_transmission_settings(),
        "tracker_rt": tracker_rt, "tracker_kz": tracker_kz, "tracker_ru": tracker_ru,
        "tracker_rt_has_cookies": bool(tracker_rt and tracker_rt.get("encrypted_cookies") and decrypt_value(tracker_rt["encrypted_cookies"])),
        "tracker_kz_has_cookies": bool(tracker_kz and tracker_kz.get("encrypted_cookies") and decrypt_value(tracker_kz["encrypted_cookies"])),
        "message": {"refresh-saved": "Период обновления сохранён.", "telegram-saved": "Настройки Telegram сохранены.",
                    "proxy-saved": "Настройки прокси сохранены.", "proxy-ok": "Прокси работает!",
                    "theme-saved": "Тема сохранена.", "test-ok": "Тестовое сообщение отправлено!",
                    "restore-ok": "Восстановление завершено.", "transmission-saved": "Настройки клиента сохранены.",
                    "transmission-test-ok": get_setting("last_trans_test") or "Клиент подключён.",
                    "tracker-saved": "Настройки трекера сохранены.", "tracker-cookies-saved": "Cookies сохранены и зашифрованы.",
                    "tracker-test-ok": "Подключение к трекеру успешно!"}.get(msg),
        "error_message": {"proxy-fail": "Не удалось подключиться через прокси.", "proxy-not-set": "Прокси не настроен.",
                          "test-fail": "Не удалось отправить. Проверьте токен и chat_id.",
                          "backup-empty": "Выберите хотя бы один компонент.", "restore-invalid": "Неверный файл бэкапа.",
                          "restore-error": "Ошибка при восстановлении.",
                          "transmission-test-fail": get_setting("last_trans_test") or "Не удалось подключиться к клиенту.",
                          "tracker-test-fail": "Не удалось войти на трекер.", "tracker-captcha": "Трекер требует капчу.",
                          "tracker-forbidden": "Rutracker заблокировал запрос (403).",
                          "tracker-cookies-invalid": "Cookies невалидны."}.get(msg)})


@router_settings.post("/settings/refresh")
async def set_refresh_interval(hours: int = Form(...)):
    hours = max(1, min(168, hours))
    set_setting("refresh_hours", str(hours))
    scheduler.reschedule_job("refresh", trigger="interval", hours=hours)
    return RedirectResponse("/settings?msg=refresh-saved", status_code=303)


@router_settings.post("/settings/theme")
async def save_theme(theme: str = Form("dark")):
    set_setting("theme", theme if theme in ("dark", "light") else "dark")
    return RedirectResponse("/settings?msg=theme-saved", status_code=303)


@router_settings.post("/settings/proxy")
async def save_proxy(proxy_url: str = Form("")):
    set_setting("proxy_url", proxy_url.strip())
    return RedirectResponse("/settings?msg=proxy-saved", status_code=303)


@router_settings.post("/settings/proxy/test")
async def test_proxy():
    proxy = get_proxy_url()
    if not proxy:
        return RedirectResponse("/settings?msg=proxy-not-set", status_code=303)
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=10) as client:
            await client.get("https://www.omdbapi.com/", params={"apikey": "test", "t": "test"})
        return RedirectResponse("/settings?msg=proxy-ok", status_code=303)
    except Exception as e:
        print(f"[proxy] Test failed: {e}")
        return RedirectResponse("/settings?msg=proxy-fail", status_code=303)


@router_settings.post("/settings/telegram")
async def save_telegram(bot_token: str = Form(""), chat_id: str = Form(""), enabled: str = Form("off"),
                        send_time: str = Form("09:00"), notify_days: int = Form(1),
                        notify_date_changes: str = Form("off"), notify_new_cards: str = Form("off"),
                        notify_new_seasons: str = Form("off"), notify_new_episodes: str = Form("off"),
                        notify_torrent_started: str = Form("off"), notify_torrent_completed: str = Form("off"),
                        notify_season_completed: str = Form("off"),
                        timezone: str = Form("Europe/Moscow")):
    save_telegram_settings(bot_token.strip(), chat_id.strip(), enabled == "on", send_time, notify_days,
                           notify_date_changes == "on", notify_new_cards == "on", notify_new_seasons == "on",
                           notify_new_episodes == "on", notify_torrent_started == "on",
                           notify_torrent_completed == "on", notify_season_completed == "on",
                           timezone.strip() or "Europe/Moscow")
    schedule_telegram_job()
    return RedirectResponse("/settings?msg=telegram-saved", status_code=303)


@router_settings.post("/settings/telegram/test/{test_type}")
async def telegram_test(test_type: str):
    s = get_telegram_settings()
    if not s.get("bot_token") or not s.get("chat_id"):
        return RedirectResponse("/settings?msg=test-fail", status_code=303)
    today = date.today()
    ok = True
    if test_type == "simple":
        ok = await send_telegram("🎬 <b>Тестовое сообщение</b>\nВсё работает!")
    elif test_type == "date-change":
        await notify_date_changes([{"title": "Тест", "old_date": (today + timedelta(days=10)).isoformat(),
                                    "new_date": (today + timedelta(days=15)).isoformat()}], force=True)
    elif test_type == "new-card":
        await notify_new_card("Тест", (today + timedelta(days=30)).isoformat(), "tmdb", "movie", force=True)
    elif test_type == "new-season":
        await notify_new_season("Тест", 2, (today + timedelta(days=20)).isoformat(), force=True)
    elif test_type == "new-episodes":
        await notify_new_episodes("Тест", [{"season_number": 1, "episode_number": 5, "name": "Эпизод",
                                             "release_date": today.isoformat()}], force=True)
    elif test_type == "torrent-started":
        await notify_torrent_started("Тест", "Test.S01E01.mkv", "/media", force=True)
    elif test_type == "torrent-completed":
        await notify_torrent_completed("Тест", "Test.S01E01.mkv", 2 * 1024 ** 3, force=True)
    elif test_type == "season-completed":
        await notify_season_completed("Тестовый сериал", 1, force=True)
    elif test_type == "daily":
        await check_and_notify(force=True)
    else:
        return RedirectResponse("/settings?msg=test-fail", status_code=303)
    return RedirectResponse(f"/settings?msg={'test-ok' if ok else 'test-fail'}", status_code=303)


@router_settings.post("/settings/transmission")
async def save_transmission(host: str = Form("localhost"), port: int = Form(9091), username: str = Form(""),
                            password: str = Form(""), enabled: str = Form("off"), base_download_dir: str = Form(""),
                            action_on_new: str = Form("download"), filter_recent_only: str = Form("off"),
                            min_file_size_mb: int = Form(500), default_check_interval: int = Form(6),
                            default_download_behavior: str = Form("use_distribution_path"),
                            auto_download_new_files: str = Form("off"), auto_check_enabled: str = Form("off"),
                            auto_check_tick_minutes: int = Form(10), transmission_poll_minutes: int = Form(3),
                            auto_clean_enabled: str = Form("off"), auto_clean_days: int = Form(30),
                            auto_clean_on_watch: str = Form("off"),
                            client_type: str = Form("transmission"),
                            deluge_url: str = Form(""), deluge_password: str = Form("")):
    if action_on_new not in ("download", "pause", "notify_only"):
        action_on_new = "download"
    if default_download_behavior not in ("use_distribution_path", "use_base_dir"):
        default_download_behavior = "use_distribution_path"
    if client_type not in ("transmission", "deluge"):
        client_type = "transmission"
    db("""UPDATE transmission_settings SET host=?, port=?, username=?, encrypted_password=?, enabled=?,
            base_download_dir=?, action_on_new=?, filter_recent_only=?, min_file_size_mb=?, default_check_interval=?,
            default_download_behavior=?, auto_download_new_files=?, auto_check_enabled=?, auto_check_tick_minutes=?,
            transmission_poll_minutes=?, auto_clean_enabled=?, auto_clean_days=?, auto_clean_on_watch=?,
            client_type=?, deluge_url=?, deluge_password=?
          WHERE id=1""",
       (host.strip(), port, username.strip(), encrypt_value(password.strip()), 1 if enabled == "on" else 0,
        base_download_dir.strip(), action_on_new, 1 if filter_recent_only == "on" else 0, max(1, min_file_size_mb),
        max(1, min(168, default_check_interval)), default_download_behavior,
        1 if auto_download_new_files == "on" else 0, 1 if auto_check_enabled == "on" else 0,
        max(5, min(60, auto_check_tick_minutes)), max(1, min(60, transmission_poll_minutes)),
        1 if auto_clean_enabled == "on" else 0, max(1, min(365, auto_clean_days)),
        1 if auto_clean_on_watch == "on" else 0, client_type, deluge_url.strip(),
        encrypt_value(deluge_password.strip())), write=True)
    schedule_distribution_job()
    schedule_transmission_poll_job()
    schedule_auto_clean_job()
    return RedirectResponse("/settings?msg=transmission-saved", status_code=303)


@router_settings.post("/settings/transmission/test")
async def test_transmission():
    trans = get_transmission_settings()
    if not trans or not trans.get("enabled"):
        return RedirectResponse("/settings?msg=transmission-test-fail", status_code=303)
    try:
        ok, message = build_transmission_client().test_connection()
        set_setting("last_trans_test", message)
        return RedirectResponse("/settings?msg=" + ("transmission-test-ok" if ok else "transmission-test-fail"), status_code=303)
    except Exception as e:
        set_setting("last_trans_test", str(e))
        return RedirectResponse("/settings?msg=transmission-test-fail", status_code=303)


@router_settings.post("/settings/tracker")
async def save_tracker(tracker_name: str = Form("rutracker"), username: str = Form(""), password: str = Form(""),
                       cookies_manual: str = Form(""), user_agent: str = Form(""), enabled: str = Form("off")):
    ev = 1 if enabled == "on" else 0
    ep = encrypt_value(password.strip()) if password else ""
    if cookies_manual.strip():
        try:
            cd = {}
            for pair in cookies_manual.split(";"):
                pair = pair.strip()
                if "=" in pair:
                    n, v = pair.split("=", 1)
                    cd[n.strip()] = v.strip()
            if not cd:
                return RedirectResponse("/settings?msg=tracker-cookies-invalid", status_code=303)
            db("""INSERT OR REPLACE INTO tracker_credentials
                  (tracker_name, username, encrypted_password, encrypted_cookies, user_agent, enabled)
                  VALUES (?,?,?,?,?,?)""",
               (tracker_name, username.strip(), ep, encrypt_value(json.dumps(cd)), user_agent.strip(), ev), write=True)
            client = build_tracker_client(tracker_name, cookies=cd)
            ok, reason = await client.validate_cookies(cd)
            if ok:
                db("UPDATE tracker_credentials SET last_login_at=datetime('now'), last_error=NULL WHERE tracker_name=?",
                   (tracker_name,), write=True)
                return RedirectResponse("/settings?msg=tracker-cookies-saved", status_code=303)
            db("UPDATE tracker_credentials SET last_error=? WHERE tracker_name=?", (reason,), write=True)
            return RedirectResponse("/settings?msg=tracker-cookies-invalid", status_code=303)
        except Exception as e:
            print(f"[tracker] Error: {e}")
            return RedirectResponse("/settings?msg=tracker-cookies-invalid", status_code=303)
    ex = get_tracker_credentials(tracker_name)
    existing_cookies = ex.get("encrypted_cookies", "") if ex else ""
    db("""INSERT OR REPLACE INTO tracker_credentials
          (tracker_name, username, encrypted_password, encrypted_cookies, user_agent, enabled)
          VALUES (?,?,?,?,?,?)""",
       (tracker_name, username.strip(), ep, existing_cookies, user_agent.strip(), ev), write=True)
    if password.strip() and username.strip() and not existing_cookies:
        try:
            client = build_tracker_client(tracker_name)
            new_cookies = await client.login()
            db("""UPDATE tracker_credentials SET encrypted_cookies=?, last_login_at=datetime('now'),
                  last_error=NULL, error_count=0 WHERE tracker_name=?""",
               (encrypt_value(json.dumps(new_cookies)), tracker_name), write=True)
            print(f"[tracker] Auto-login succeeded for {tracker_name}, cookies saved")
            return RedirectResponse("/settings?msg=tracker-saved", status_code=303)
        except (RuTrackerCaptchaError, KinozalAuthError):
            print(f"[tracker] Auto-login failed for {tracker_name}: captcha")
        except Exception as e:
            print(f"[tracker] Auto-login failed for {tracker_name}: {e}")
    return RedirectResponse("/settings?msg=tracker-saved", status_code=303)


@router_settings.post("/settings/tracker/test")
async def test_tracker_login(tracker_name: str = Form("rutracker")):
    creds = get_tracker_credentials(tracker_name)
    if not creds:
        return RedirectResponse("/settings?msg=tracker-test-fail", status_code=303)
    cookies = load_tracker_cookies(tracker_name)
    username = creds.get("username", "")
    password = decrypt_value(creds.get("encrypted_password", "")) if creds.get("encrypted_password") else ""
    if not cookies and not (username and password):
        return RedirectResponse("/settings?msg=tracker-test-fail", status_code=303)
    client = build_tracker_client(tracker_name, cookies=cookies)
    if cookies:
        ok, reason = await client.validate_cookies(cookies)
        if ok:
            db("UPDATE tracker_credentials SET last_login_at=datetime('now'), last_error=NULL, error_count=0 WHERE tracker_name=?",
               (tracker_name,), write=True)
            return RedirectResponse("/settings?msg=tracker-test-ok", status_code=303)
        if not (username and password):
            db("UPDATE tracker_credentials SET last_error=? WHERE tracker_name=?", (reason,), write=True)
            return RedirectResponse("/settings?msg=tracker-cookies-invalid", status_code=303)
    try:
        nc = await client.login()
        db("UPDATE tracker_credentials SET encrypted_cookies=?, last_login_at=datetime('now'), last_error=NULL, error_count=0 WHERE tracker_name=?",
           (encrypt_value(json.dumps(nc)), tracker_name), write=True)
        return RedirectResponse("/settings?msg=tracker-test-ok", status_code=303)
    except Exception as e:
        traceback.print_exc()
        db("UPDATE tracker_credentials SET last_error=? WHERE tracker_name=?", (str(e), tracker_name), write=True)
        return RedirectResponse("/settings?msg=tracker-test-fail", status_code=303)


# ═══════════════ РАЗДАЧИ ═══════════════
@router_dist.post("/distribution/add")
async def add_distribution(title_external_id: str = Form(...), url: str = Form(...),
                           download_path: str = Form(""), mode: str = Form("smart"),
                           check_interval_hours: int = Form(6)):
    tracker_name, torrent_id = parse_torrent_url(url)
    if not torrent_id or not tracker_name:
        return RedirectResponse("/?msg=dist-invalid-url", status_code=303)
    existing = get_distribution(title_external_id)
    if mode not in ("smart", "fixed"):
        mode = "smart"
    if existing:
        db("""UPDATE distributions SET tracker_name=?, torrent_id=?, url=?, download_path=?, mode=?,
                check_interval_hours=?, status='idle', last_checked_at=NULL, last_files_hash=NULL,
                last_files_json=NULL, new_files_count=0, error_message=NULL, error_count=0 WHERE title_external_id=?""",
           (tracker_name, torrent_id, url.strip(), download_path.strip(), mode,
            max(1, min(168, check_interval_hours)), title_external_id), write=True)
        update_next_episode_air_date(title_external_id)
        return RedirectResponse("/?msg=dist-updated", status_code=303)
    db("""INSERT INTO distributions
          (title_external_id, tracker_name, torrent_id, url, download_path,
           mode, check_interval_hours, status) VALUES (?,?,?,?,?,?,?,'idle')""",
       (title_external_id, tracker_name, torrent_id, url.strip(),
        download_path.strip(), mode, max(1, min(168, check_interval_hours))), write=True)
    update_next_episode_air_date(title_external_id)
    return RedirectResponse("/?msg=dist-added", status_code=303)


@router_dist.post("/distribution/remove/{title_external_id}")
async def remove_distribution(title_external_id: str, sort: str = "date"):
    db("DELETE FROM distributions WHERE title_external_id=?", (title_external_id,), write=True)
    return RedirectResponse(f"/?sort={sort}&msg=dist-removed", status_code=303)


@router_dist.post("/distribution/check/{title_external_id}")
async def check_distribution(title_external_id: str, sort: str = "date"):
    ok, message = await check_distribution_now(title_external_id)
    set_setting("last_dist_check", message)
    return RedirectResponse(f"/?sort={sort}&msg={'dist-checked' if ok else 'dist-check-fail'}", status_code=303)


@router_dist.post("/distribution/download/{title_external_id}")
async def download_distribution(title_external_id: str, sort: str = "date"):
    dist = get_distribution(title_external_id)
    if not dist:
        set_setting("last_dist_download", "Раздача не найдена")
        return RedirectResponse(f"/?sort={sort}&msg=dist-download-fail", status_code=303)
    trans = get_transmission_settings()
    if not trans or not trans.get("enabled"):
        set_setting("last_dist_download", "Клиент загрузок отключён в настройках")
        return RedirectResponse(f"/?sort={sort}&msg=dist-download-fail", status_code=303)
    creds = get_tracker_credentials(dist["tracker_name"])
    if not creds or not creds.get("enabled"):
        set_setting("last_dist_download", "Трекер отключён в настройках")
        return RedirectResponse(f"/?sort={sort}&msg=dist-download-fail", status_code=303)
    cookies = load_tracker_cookies(dist["tracker_name"])
    username = creds.get("username", "")
    password = decrypt_value(creds.get("encrypted_password", "")) if creds.get("encrypted_password") else ""
    if not cookies and not (username and password):
        set_setting("last_dist_download", "Не настроены учётные данные трекера")
        return RedirectResponse(f"/?sort={sort}&msg=dist-download-fail", status_code=303)
    try:
        tracker_client = build_tracker_client(dist["tracker_name"], cookies=cookies)
        if not cookies:
            try:
                cookies = await tracker_client.login()
                db("""UPDATE tracker_credentials SET encrypted_cookies=?, last_login_at=datetime('now'),
                      last_error=NULL, error_count=0 WHERE tracker_name=?""",
                   (encrypt_value(json.dumps(cookies)), dist["tracker_name"]), write=True)
                print(f"[download] Logged in and saved cookies for {dist['tracker_name']}")
            except (RuTrackerCaptchaError, KinozalAuthError):
                set_setting("last_dist_download", "Трекер требует капчу — войдите через браузер")
                return RedirectResponse(f"/?sort={sort}&msg=dist-download-fail", status_code=303)
            except (RuTrackerAuthError, RuTrackerForbiddenError, KinozalAuthError, KinozalForbiddenError) as e:
                set_setting("last_dist_download", f"Ошибка входа: {e}")
                return RedirectResponse(f"/?sort={sort}&msg=dist-download-fail", status_code=303)
        td = await tracker_client.download_torrent(dist["torrent_id"], cookies)
        dd = _resolve_download_dir(dist, trans)
        paused = trans.get("action_on_new") == "pause"
        result = build_transmission_client().add_torrent(td, dd, paused)
        db("UPDATE distributions SET status='idle', dl_ack=1, error_count=0, error_message=NULL WHERE id=?",
           (dist["id"],), write=True)
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
        set_setting("last_dist_download", f"✅ Отправлено: {result['name']} ({format_size(result['size'])})")
        return RedirectResponse(f"/?sort={sort}&msg=dist-downloaded", status_code=303)
    except Exception as e:
        traceback.print_exc()
        db("UPDATE distributions SET status='error', error_message=?, error_count=error_count+1 WHERE id=?",
           (str(e), dist["id"]), write=True)
        set_setting("last_dist_download", f"❌ Ошибка: {e}")
        return RedirectResponse(f"/?sort={sort}&msg=dist-download-fail", status_code=303)


@router_dist.post("/distribution/pattern/save/{title_external_id}")
async def save_pattern(title_external_id: str, min_samples: int = Form(3), sort: str = "date"):
    dist = get_distribution(title_external_id)
    if dist:
        ensure_pattern(dist["id"])
        db("UPDATE distribution_patterns SET min_samples=? WHERE distribution_id=?", (max(1, min(10, min_samples)), dist["id"]), write=True)
        recompute_pattern(dist["id"])
    return RedirectResponse(f"/?sort={sort}&msg=pattern-saved", status_code=303)


@router_dist.post("/distribution/pattern/reset/{title_external_id}")
async def reset_pattern(title_external_id: str, sort: str = "date"):
    dist = get_distribution(title_external_id)
    if dist:
        db("UPDATE distribution_patterns SET samples_json='[]', median_delay_hours=NULL, samples_count=0, confidence='low' WHERE distribution_id=?", (dist["id"],), write=True)
    return RedirectResponse(f"/?sort={sort}&msg=pattern-reset", status_code=303)


@router_dist.post("/distribution/seen/{title_external_id}")
async def distribution_seen(title_external_id: str):
    db("UPDATE distributions SET dot_ack=1 WHERE title_external_id=? AND new_files_count>0",
       (title_external_id,), write=True)
    return JSONResponse({"ok": True})