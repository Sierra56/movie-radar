import asyncio
import html
from typing import Optional

import httpx

from .core import get_telegram_settings


def _send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Синхронная отправка сообщения в Telegram."""
    settings = get_telegram_settings()
    if not settings.get("enabled") or not settings.get("bot_token") or not settings.get("chat_id"):
        return False
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(
                f"https://api.telegram.org/bot{settings['bot_token']}/sendMessage",
                json={
                    "chat_id": settings["chat_id"],
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                }
            )
            return r.status_code == 200
    except Exception as e:
        print(f"[notify] Ошибка отправки: {e}")
        return False


def _is_enabled(event_type: str) -> bool:
    settings = get_telegram_settings()
    return bool(settings.get(f"notify_{event_type}", False))


# ──────────────────────────────────────────────────────
# Функции, которые реально вызываются из catalog.py:
#   notify_new_season(title, season_number, release_date)
#   notify_new_episodes(title, episodes)
#   notify_date_changes(changes)
#   notify_season_completed(title, season)
# ──────────────────────────────────────────────────────

def notify_new_season(title: str, season_number: int, release_date: Optional[str] = None):
    """catalog.py вызывает: notify_new_season(nt, n['season_number'], n['release_date'])"""
    if not _is_enabled("new_season"):
        return
    date_str = f"\nДата: {release_date}" if release_date else ""
    text = (
        f"📺 <b>Новый сезон</b>\n\n"
        f"<b>{html.escape(title)}</b>\n"
        f"Сезон {season_number}{date_str}"
    )
    _send_message(text)


def notify_new_episodes(title: str, episodes: list):
    """catalog.py вызывает: notify_new_episodes(nt, all_new)
       где episodes — список dict с ключами season_number, episode_number, name"""
    if not _is_enabled("new_episode") or not episodes:
        return
    lines = [f"🎬 <b>Новые эпизоды</b>\n\n<b>{html.escape(title)}</b>\n"]
    for ep in episodes[:10]:
        s = ep.get("season_number") or ep.get("season") or 0
        e = ep.get("episode_number") or ep.get("episode") or 0
        name = ep.get("name", "")
        lines.append(f"• S{s:02d}E{e:02d} — {html.escape(name)}")
    _send_message("\n".join(lines))


def notify_date_changes(changes: list):
    """catalog.py вызывает: notify_date_changes(date_changes)
       где changes — список dict {title, old_date, new_date}"""
    if not _is_enabled("date_change") or not changes:
        return
    lines = ["📅 <b>Изменения дат выхода</b>\n"]
    for c in changes[:10]:
        title = c.get("title", "")
        old_date = c.get("old_date") or "—"
        new_date = c.get("new_date") or "—"
        lines.append(f"• <b>{html.escape(title)}</b>\n  Было: {old_date}\n  Стало: {new_date}")
    _send_message("\n".join(lines))


def notify_season_completed(title: str, season: int):
    """catalog.py вызывает: notify_season_completed(title, season)"""
    if not _is_enabled("season_completed"):
        return
    text = (
        f"🏁 <b>Сезон завершён</b>\n\n"
        f"<b>{html.escape(title)}</b>\n"
        f"Сезон {season} полностью скачан"
    )
    _send_message(text)


# ── Остальные уведомления (из web.py / trackers.py) ──

def notify_download_started(title: str, external_id: str = ""):
    if not _is_enabled("download_started"):
        return
    text = (
        f"⬇ <b>Скачивание начато</b>\n\n"
        f"<b>{html.escape(title)}</b>\n\n"
        f"🔗 <a href='{external_id}'>Открыть</a>"
    )
    _send_message(text)


def notify_download_finished(title: str, external_id: str = ""):
    if not _is_enabled("download_finished"):
        return
    text = (
        f"✅ <b>Скачивание завершено</b>\n\n"
        f"<b>{html.escape(title)}</b>\n\n"
        f"🔗 <a href='{external_id}'>Открыть</a>"
    )
    _send_message(text)


def notify_daily_digest(titles: list):
    if not _is_enabled("daily_digest") or not titles:
        return
    lines = ["📅 <b>Ближайшие релизы</b>\n"]
    for t in titles[:10]:
        lines.append(f"• <b>{html.escape(t['title'])}</b> — {t['date']}")
    _send_message("\n".join(lines))


# ── Уведомление о протухших cookies ──

_TRACKER_DISPLAY = {
    "rutracker": "rutracker.org",
    "kinozal": "kinozal.me",
    "rutor": "rutor.info",
}


def notify_expired_cookies(tracker_name: str):
    if not _is_enabled("expired_cookies"):
        return
    name = _TRACKER_DISPLAY.get(tracker_name, tracker_name)
    text = (
        f"🔑 <b>Cookies протухли</b>\n\n"
        f"Трекер: <b>{html.escape(name)}</b>\n\n"
        f"Автоматически перелогиниться не удалось.\n"
        f"Обновите cookies вручную:\n"
        f"<b>⚙ Настройки → Трекеры → {html.escape(name)} → 🔑 Проверить</b>"
    )
    _send_message(text)


def send_test_message() -> bool:
    return _send_message("🧪 <b>Тестовое сообщение</b>\n\nУведомления работают!")


# ── Алиасы для обратной совместимости (если где-то ещё используются старые имена) ──

notify_season_finished = notify_season_completed
notify_season_completed_async = None  # будет перезаписан ниже
notify_season_finished_async = None


# ── Асинхронные обёртки ──

async def notify_new_season_async(title, season_number, release_date=None):
    await asyncio.to_thread(notify_new_season, title, season_number, release_date)


async def notify_new_episodes_async(title, episodes):
    await asyncio.to_thread(notify_new_episodes, title, episodes)


async def notify_date_changes_async(changes):
    await asyncio.to_thread(notify_date_changes, changes)


async def notify_season_completed_async(title, season):
    await asyncio.to_thread(notify_season_completed, title, season)


async def notify_season_finished_async(title, season):
    await asyncio.to_thread(notify_season_completed, title, season)


async def notify_download_started_async(title, external_id=""):
    await asyncio.to_thread(notify_download_started, title, external_id)


async def notify_download_finished_async(title, external_id=""):
    await asyncio.to_thread(notify_download_finished, title, external_id)


async def notify_expired_cookies_async(tracker_name: str):
    await asyncio.to_thread(notify_expired_cookies, tracker_name)


# Устаревшие алиасы (на случай, если где-то используются сингулярные имена)
notify_new_episode = notify_new_episodes
notify_new_episode_async = notify_new_episodes_async
notify_date_change = notify_date_changes
notify_date_change_async = notify_date_changes_async