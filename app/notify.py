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


def notify_date_change(title: str, old_date: str, new_date: str, external_id: str):
    if not _is_enabled("date_change"):
        return
    text = (
        f"📅 <b>Дата выхода изменилась</b>\n\n"
        f"<b>{html.escape(title)}</b>\n"
        f"Было: {old_date}\n"
        f"Стало: {new_date}\n\n"
        f"🔗 <a href='{external_id}'>Подробнее</a>"
    )
    _send_message(text)


def notify_new_title(title: str, year: Optional[int], external_id: str):
    if not _is_enabled("new_title"):
        return
    year_str = f" ({year})" if year else ""
    text = (
        f"➕ <b>Добавлена новая карточка</b>\n\n"
        f"<b>{html.escape(title)}</b>{year_str}\n\n"
        f"🔗 <a href='{external_id}'>Открыть</a>"
    )
    _send_message(text)


def notify_new_season(title: str, season_number: int, external_id: str):
    if not _is_enabled("new_season"):
        return
    text = (
        f"📺 <b>Новый сезон</b>\n\n"
        f"<b>{html.escape(title)}</b>\n"
        f"Сезон {season_number}\n\n"
        f"🔗 <a href='{external_id}'>Открыть</a>"
    )
    _send_message(text)


def notify_new_episode(title: str, season: int, episode: int, episode_name: str, external_id: str):
    if not _is_enabled("new_episode"):
        return
    text = (
        f"🎬 <b>Новый эпизод</b>\n\n"
        f"<b>{html.escape(title)}</b>\n"
        f"S{season:02d}E{episode:02d} — {html.escape(episode_name)}\n\n"
        f"🔗 <a href='{external_id}'>Открыть</a>"
    )
    _send_message(text)


def notify_download_started(title: str, external_id: str):
    if not _is_enabled("download_started"):
        return
    text = (
        f"⬇ <b>Скачивание начато</b>\n\n"
        f"<b>{html.escape(title)}</b>\n\n"
        f"🔗 <a href='{external_id}'>Открыть</a>"
    )
    _send_message(text)


def notify_download_finished(title: str, external_id: str):
    if not _is_enabled("download_finished"):
        return
    text = (
        f"✅ <b>Скачивание завершено</b>\n\n"
        f"<b>{html.escape(title)}</b>\n\n"
        f"🔗 <a href='{external_id}'>Открыть</a>"
    )
    _send_message(text)


def notify_season_finished(title: str, season: int, external_id: str):
    if not _is_enabled("season_finished"):
        return
    text = (
        f"🏁 <b>Сезон завершён</b>\n\n"
        f"<b>{html.escape(title)}</b>\n"
        f"Сезон {season}\n\n"
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


_TRACKER_DISPLAY = {
    "rutracker": "rutracker.org",
    "kinozal": "kinozal.me",
    "rutor": "rutor.info",
}


def notify_expired_cookies(tracker_name: str):
    """Уведомление о том, что cookies трекера протухли (403 / HTML вместо торрента)."""
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


async def notify_date_change_async(*args, **kwargs):
    await asyncio.to_thread(notify_date_change, *args, **kwargs)


async def notify_new_title_async(*args, **kwargs):
    await asyncio.to_thread(notify_new_title, *args, **kwargs)


async def notify_new_season_async(*args, **kwargs):
    await asyncio.to_thread(notify_new_season, *args, **kwargs)


async def notify_new_episode_async(*args, **kwargs):
    await asyncio.to_thread(notify_new_episode, *args, **kwargs)


async def notify_download_started_async(*args, **kwargs):
    await asyncio.to_thread(notify_download_started, *args, **kwargs)


async def notify_download_finished_async(*args, **kwargs):
    await asyncio.to_thread(notify_download_finished, *args, **kwargs)


async def notify_season_finished_async(*args, **kwargs):
    await asyncio.to_thread(notify_season_finished, *args, **kwargs)


async def notify_expired_cookies_async(tracker_name: str):
    await asyncio.to_thread(notify_expired_cookies, tracker_name)