import json
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import httpx
from apscheduler.triggers.cron import CronTrigger

from .core import (db, get_proxy_url, get_telegram_settings, human_date, is_today,
                   scheduler, format_size)


def was_notified_today(ntype, external_id, details=""):
    rows = db("SELECT 1 FROM sent_notifications WHERE notification_type=? AND external_id=? AND details=? AND sent_date=?",
              (ntype, external_id, details, date.today().isoformat()))
    return bool(rows)


def mark_notified_today(ntype, external_id, details=""):
    db("INSERT INTO sent_notifications (notification_type, external_id, details, sent_date) VALUES (?,?,?,?)",
       (ntype, external_id, details, date.today().isoformat()), write=True)


def cleanup_old_notifications(days_to_keep=7):
    db("DELETE FROM sent_notifications WHERE sent_date < ?",
       ((date.today() - timedelta(days=days_to_keep)).isoformat(),), write=True)


async def send_telegram(text):
    s = get_telegram_settings()
    if not s or not s.get("bot_token") or not s.get("chat_id"):
        return False
    try:
        kw = {"timeout": 10}
        if get_proxy_url():
            kw["proxy"] = get_proxy_url()
        async with httpx.AsyncClient(**kw) as client:
            r = await client.post(f"https://api.telegram.org/bot{s['bot_token']}/sendMessage",
                                  json={"chat_id": s["chat_id"], "text": text, "parse_mode": "HTML"})
            return r.status_code == 200
    except Exception as e:
        print(f"[telegram] Send error: {e}")
        return False


async def notify_date_changes(changes, force=False):
    s = get_telegram_settings()
    if not force and (not s or not s.get("enabled") or not s.get("notify_date_changes")):
        return
    if not s or not s.get("bot_token") or not s.get("chat_id") or not changes:
        return
    lines = ["📅 <b>Перенос даты релиза</b>", ""] if len(changes) == 1 else ["📅 <b>Переносы дат релизов</b>", ""]
    for c in changes:
        old_h, new_h = human_date(c["old_date"]) or "не указана", human_date(c["new_date"]) or "не указана"
        direction = ""
        if c["old_date"] and c["new_date"]:
            try:
                delta = (date.fromisoformat(c["new_date"]) - date.fromisoformat(c["old_date"])).days
                direction = (f"⬇️ на {delta} позже" if delta > 0 else f"⬆️ на {abs(delta)} раньше")
            except ValueError:
                pass
        elif not c["old_date"] and c["new_date"]:
            direction = "✅ дата стала известна"
        elif c["old_date"] and not c["new_date"]:
            direction = "⚠️ дата больше не указана"
        lines += [f"🎬 <b>{c['title']}</b>", f"Было: {old_h}", f"Стало: {new_h}"]
        if direction:
            lines.append(direction)
        lines.append("")
    await send_telegram("\n".join(lines))


async def notify_new_card(title, release_date, source, card_type, force=False):
    s = get_telegram_settings()
    if not force and (not s or not s.get("enabled") or not s.get("notify_new_cards")):
        return
    if not s or not s.get("bot_token") or not s.get("chat_id"):
        return
    lines = ["🆕 <b>Новая карточка в каталоге</b>", ""]
    tl = {"movie": "Фильм", "series": "Сериал"}.get(card_type, "")
    if tl:
        lines.append(tl)
    lines += [f"🎬 <b>{title}</b>", f"📅 {human_date(release_date)}" if release_date else "📅 дата неизвестна", f"Источник: {source}"]
    await send_telegram("\n".join(lines))


async def notify_new_season(show_title, season_number, release_date, force=False):
    s = get_telegram_settings()
    if not force and (not s or not s.get("enabled") or not s.get("notify_new_seasons")):
        return
    if not s or not s.get("bot_token") or not s.get("chat_id"):
        return
    details = json.dumps({"season": season_number})
    if not force and was_notified_today("new_season", show_title, details):
        return
    mark_notified_today("new_season", show_title, details)
    lines = ["🆕 <b>Новый сезон в каталоге</b>", "", f"📺 <b>{show_title}</b> — Сезон {season_number}",
             f"📅 {human_date(release_date)}" if release_date else "📅 дата неизвестна"]
    await send_telegram("\n".join(lines))


async def notify_new_episodes(show_title, new_eps, force=False):
    s = get_telegram_settings()
    if not force and (not s or not s.get("enabled") or not s.get("notify_new_episodes")):
        return
    if not s or not s.get("bot_token") or not s.get("chat_id") or not new_eps:
        return
    sorted_eps = sorted(new_eps, key=lambda x: (x["season_number"], x["episode_number"]))
    to_notify = []
    for ep in sorted_eps:
        details = json.dumps({"season": ep["season_number"], "episode": ep["episode_number"]})
        if force or not was_notified_today("new_episode", show_title, details):
            to_notify.append(ep)
            mark_notified_today("new_episode", show_title, details)
    if not to_notify:
        return
    if len(to_notify) == 1:
        ep = to_notify[0]
        lines = ["🆕 <b>Новый эпизод</b>", "", f"📺 <b>{show_title}</b> — S{ep['season_number']}E{ep['episode_number']}"]
        if ep["name"]:
            lines.append(f"«{ep['name']}»")
        if ep["release_date"]:
            lines.append("🔴 <b>Вышел сегодня!</b>" if is_today(ep["release_date"]) else f"📅 {human_date(ep['release_date'])}")
    else:
        lines = [f"🆕 <b>Новые эпизоды ({len(to_notify)})</b>", "", f"📺 <b>{show_title}</b>"]
        for ep in to_notify:
            ns = f": {ep['name']}" if ep["name"] else ""
            lines.append(f"• 🔴 S{ep['season_number']}E{ep['episode_number']}{ns} — вышел сегодня!" if is_today(ep["release_date"])
                         else f"• S{ep['season_number']}E{ep['episode_number']}{ns}" + (f" · {human_date(ep['release_date'])}" if ep["release_date"] else ""))
    await send_telegram("\n".join(lines))


async def notify_torrent_started(title, torrent_name, download_dir=None, force=False):
    s = get_telegram_settings()
    if not force and (not s or not s.get("enabled") or not s.get("notify_torrent_started")):
        return
    if not s or not s.get("bot_token") or not s.get("chat_id"):
        return
    lines = ["📥 <b>Начато скачивание</b>", "", f"🎬 <b>{title}</b>", f"📦 {torrent_name}"]
    if download_dir:
        lines.append(f"📂 {download_dir}")
    lines.append("⏳ Загрузка запущена в Transmission")
    await send_telegram("\n".join(lines))


async def notify_torrent_completed(title, torrent_name, size_bytes, force=False):
    s = get_telegram_settings()
    if not force and (not s or not s.get("enabled") or not s.get("notify_torrent_completed")):
        return
    if not s or not s.get("bot_token") or not s.get("chat_id"):
        return
    lines = ["✅ <b>Скачивание завершено</b>", "", f"🎬 <b>{title}</b>", f"📦 {torrent_name}", f"📏 {format_size(size_bytes)}"]
    await send_telegram("\n".join(lines))


async def notify_season_completed(show_title, season_number, force=False):
    s = get_telegram_settings()
    if not force and (not s or not s.get("enabled") or not s.get("notify_season_completed")):
        return
    if not s or not s.get("bot_token") or not s.get("chat_id"):
        return
    details = json.dumps({"season": season_number, "event": "completed"})
    if not force and was_notified_today("season_completed", show_title, details):
        return
    mark_notified_today("season_completed", show_title, details)
    lines = ["🏁 <b>Сезон завершён</b>", "", f"📺 <b>{show_title}</b> — Сезон {season_number}",
             "Все эпизоды скачаны. Приятного просмотра!"]
    await send_telegram("\n".join(lines))


async def check_and_notify(force=False):
    cleanup_old_notifications(7)
    s = get_telegram_settings()
    if not force and (not s or not s.get("enabled")):
        return
    if not s or not s.get("bot_token") or not s.get("chat_id"):
        return
    today = date.today()
    nd = s.get("notify_days", 1)
    upcoming = []
    for r in db("SELECT * FROM titles WHERE release_date IS NOT NULL AND notify_enabled = 1"):
        row = dict(r)
        try:
            rd = date.fromisoformat(row["release_date"])
        except ValueError:
            continue
        if 0 <= (rd - today).days <= nd:
            upcoming.append(((rd - today).days, row["title"], row["release_date"]))
    for r in db("""SELECT s.*, t.title as show_title, t.notify_enabled FROM seasons s JOIN titles t ON s.title_external_id=t.external_id
                   WHERE s.release_date IS NOT NULL AND t.notify_enabled = 1"""):
        row = dict(r)
        try:
            rd = date.fromisoformat(row["release_date"])
        except ValueError:
            continue
        if 0 <= (rd - today).days <= nd:
            upcoming.append(((rd - today).days, f"{row['show_title']} — Сезон {row['season_number']}", row["release_date"]))
    for r in db("""SELECT e.name, e.release_date, e.episode_number, s.season_number, t.title as show_title
                   FROM episodes e JOIN seasons s ON e.season_id=s.id JOIN titles t ON s.title_external_id=t.external_id
                   WHERE e.release_date IS NOT NULL AND t.notify_enabled = 1"""):
        row = dict(r)
        try:
            rd = date.fromisoformat(row["release_date"])
        except ValueError:
            continue
        if 0 <= (rd - today).days <= nd:
            label = f"{row['show_title']} — S{row['season_number']}E{row['episode_number']}"
            if row["name"]:
                label += f" «{row['name']}»"
            upcoming.append(((rd - today).days, label, row["release_date"]))
    if not upcoming:
        if force:
            await send_telegram(f"🎬 <b>Тестовая рассылка</b>\nПредстоящих релизов в горизонте {nd} дн. нет.")
        return
    upcoming.sort()
    lines = ["🎬 <b>Скоро на экранах</b>", ""]
    for delta, title, rd in upcoming:
        lines.append("🔴 <b>Сегодня:</b> " + title if delta == 0 else "🟠 <b>Завтра:</b> " + title if delta == 1 else f"📅 {human_date(rd)}: {title}")
    if await send_telegram("\n".join(lines)):
        db("UPDATE telegram_settings SET last_sent=datetime('now') WHERE id=1", write=True)


def schedule_telegram_job():
    s = get_telegram_settings()
    st = s.get("send_time", "09:00") or "09:00"
    tz_name = s.get("timezone") or "Europe/Moscow"
    try:
        h, m = map(int, st.split(":"))
    except (ValueError, AttributeError):
        h, m = 9, 0
    try:
        tz_info = ZoneInfo(tz_name)
    except Exception:
        tz_info = ZoneInfo("Europe/Moscow")
    scheduler.add_job(check_and_notify, CronTrigger(hour=h, minute=m, timezone=tz_info),
                      id="telegram_notify", replace_existing=True)


# ── НОВОЕ: уведомление о протухших cookies трекеров ──

_TRACKER_DISPLAY = {
    "rutracker": "rutracker.org",
    "kinozal": "kinozal.me",
    "rutor": "rutor.info",
}


async def notify_expired_cookies(tracker_name: str):
    """Уведомление о том, что cookies трекера протухли (403 / HTML вместо торрента)."""
    s = get_telegram_settings()
    if not s or not s.get("enabled") or not s.get("notify_expired_cookies"):
        return
    if not s.get("bot_token") or not s.get("chat_id"):
        return
    name = _TRACKER_DISPLAY.get(tracker_name, tracker_name)
    text = (
        f"🔑 <b>Cookies протухли</b>\n\n"
        f"Трекер: <b>{name}</b>\n\n"
        f"Автоматически перелогиниться не удалось.\n"
        f"Обновите cookies вручную:\n"
        f"<b>⚙ Настройки → Трекеры → {name} → 🔑 Проверить</b>"
    )
    await send_telegram(text)