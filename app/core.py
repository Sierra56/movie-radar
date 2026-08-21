import os
import json
import sqlite3
import re as _re
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler

OMDB_KEY = os.getenv("OMDB_API_KEY", "")
TMDB_KEY = os.getenv("TMDB_API_KEY", "")
DB_PATH = os.getenv("DB_PATH", "/data/catalog.db")
REFRESH_HOURS_DEFAULT = int(os.getenv("REFRESH_HOURS", "12"))

POSTERS_DIR = os.path.join(os.path.dirname(DB_PATH), "posters")
os.makedirs(POSTERS_DIR, exist_ok=True)

STATIC_DIR = Path(__file__).parent / "static"
os.makedirs(STATIC_DIR, exist_ok=True)

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]
MONTHS_RU_SHORT = ["янв", "фев", "мар", "апр", "май", "июн",
                   "июл", "авг", "сен", "окт", "ноя", "дек"]

refresh_progress = {"running": False, "done": 0, "total": 0}

BACKUP_VERSION = "1.0.1"
SETTINGS_TABLES = ["settings", "telegram_settings"]
CARD_TABLES = ["titles", "seasons", "episodes", "watched_episodes", "updates_log"]
TORRENT_TABLES = ["tracker_credentials", "transmission_settings",
                  "distributions", "download_history", "distribution_patterns"]

ENCRYPTION_KEY_PATH = os.path.join(os.path.dirname(DB_PATH), "encryption.key")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
scheduler = AsyncIOScheduler()


# ── Шифрование ────────────────────────────────────────
def get_encryption_key() -> bytes:
    if os.path.exists(ENCRYPTION_KEY_PATH):
        with open(ENCRYPTION_KEY_PATH, "rb") as f:
            return f.read()
    key = Fernet.generate_key()
    os.makedirs(os.path.dirname(ENCRYPTION_KEY_PATH), exist_ok=True)
    with open(ENCRYPTION_KEY_PATH, "wb") as f:
        f.write(key)
    os.chmod(ENCRYPTION_KEY_PATH, 0o600)
    return key


def encrypt_value(value: str) -> str:
    if not value:
        return ""
    try:
        return Fernet(get_encryption_key()).encrypt(value.encode()).decode()
    except Exception as e:
        print(f"[encrypt] Error: {e}")
        return ""


def decrypt_value(value: str) -> str:
    if not value:
        return ""
    try:
        return Fernet(get_encryption_key()).decrypt(value.encode()).decode()
    except Exception:
        return ""


# ── БД ────────────────────────────────────────────────
def db(sql, params=(), write=False):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, params)
        if write:
            conn.commit()
        return cur.fetchall()


def ensure_schema():
    db("""CREATE TABLE IF NOT EXISTS titles (
            external_id TEXT PRIMARY KEY, title TEXT NOT NULL, type TEXT, release_date TEXT,
            poster_url TEXT, genres TEXT, source TEXT, added_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT, notify_enabled INTEGER DEFAULT 1)""", write=True)
    for col in ("genres", "source", "updated_at", "notify_enabled", "tmdb_status"):
        try:
            db(f"ALTER TABLE titles ADD COLUMN {col} TEXT", write=True)
        except sqlite3.OperationalError:
            pass

    db("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)", write=True)

    db("""CREATE TABLE IF NOT EXISTS telegram_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1), bot_token TEXT DEFAULT '', chat_id TEXT DEFAULT '',
            enabled INTEGER DEFAULT 0, send_time TEXT DEFAULT '09:00', notify_days INTEGER DEFAULT 1,
            notify_date_changes INTEGER DEFAULT 1, notify_new_cards INTEGER DEFAULT 1,
            notify_new_seasons INTEGER DEFAULT 1, notify_new_episodes INTEGER DEFAULT 1,
            notify_torrent_started INTEGER DEFAULT 1, notify_torrent_completed INTEGER DEFAULT 1,
            notify_season_completed INTEGER DEFAULT 1, last_sent TEXT)""", write=True)
    db("INSERT OR IGNORE INTO telegram_settings (id) VALUES (1)", write=True)
    for col in ("notify_date_changes", "notify_new_cards", "notify_new_seasons",
                "notify_new_episodes", "notify_torrent_started", "notify_torrent_completed",
                "notify_season_completed"):
        try:
            db(f"ALTER TABLE telegram_settings ADD COLUMN {col} INTEGER DEFAULT 1", write=True)
        except sqlite3.OperationalError:
            pass
    try:
        db("ALTER TABLE telegram_settings ADD COLUMN timezone TEXT DEFAULT 'Europe/Moscow'", write=True)
    except sqlite3.OperationalError:
        pass

    db("""CREATE TABLE IF NOT EXISTS updates_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, external_id TEXT, title TEXT, field TEXT,
            old_value TEXT, new_value TEXT, created_at TEXT DEFAULT (datetime('now')))""", write=True)

    db("""CREATE TABLE IF NOT EXISTS seasons (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title_external_id TEXT NOT NULL,
            season_number INTEGER NOT NULL, name TEXT, release_date TEXT, episodes INTEGER,
            poster_url TEXT, UNIQUE(title_external_id, season_number))""", write=True)

    db("""CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, season_id INTEGER NOT NULL,
            episode_number INTEGER NOT NULL, name TEXT, release_date TEXT, runtime INTEGER,
            overview TEXT, poster_url TEXT, UNIQUE(season_id, episode_number),
            FOREIGN KEY (season_id) REFERENCES seasons(id))""", write=True)

    db("""CREATE TABLE IF NOT EXISTS watched_episodes (
            title_external_id TEXT NOT NULL, season_number INTEGER NOT NULL,
            episode_number INTEGER NOT NULL, watched_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (title_external_id, season_number, episode_number))""", write=True)

    db("""CREATE TABLE IF NOT EXISTS tracker_credentials (
            tracker_name TEXT PRIMARY KEY, username TEXT DEFAULT '', encrypted_password TEXT DEFAULT '',
            encrypted_cookies TEXT DEFAULT '', cookies_expires_at TEXT, user_agent TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1, last_login_at TEXT, last_error TEXT, error_count INTEGER DEFAULT 0)""", write=True)
    try:
        db("ALTER TABLE tracker_credentials ADD COLUMN user_agent TEXT DEFAULT ''", write=True)
    except sqlite3.OperationalError:
        pass

    db("""CREATE TABLE IF NOT EXISTS transmission_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1), host TEXT DEFAULT 'localhost', port INTEGER DEFAULT 9091,
            username TEXT DEFAULT '', encrypted_password TEXT DEFAULT '', enabled INTEGER DEFAULT 0,
            base_download_dir TEXT DEFAULT '', action_on_new TEXT DEFAULT 'download',
            filter_recent_only INTEGER DEFAULT 1, min_file_size_mb INTEGER DEFAULT 500,
            default_check_interval INTEGER DEFAULT 6,
            default_download_behavior TEXT DEFAULT 'use_distribution_path',
            auto_download_new_files INTEGER DEFAULT 0, auto_check_enabled INTEGER DEFAULT 1,
            auto_check_tick_minutes INTEGER DEFAULT 10, transmission_poll_minutes INTEGER DEFAULT 3,
            auto_clean_enabled INTEGER DEFAULT 0, auto_clean_days INTEGER DEFAULT 30,
            auto_clean_on_watch INTEGER DEFAULT 0)""", write=True)
    db("INSERT OR IGNORE INTO transmission_settings (id) VALUES (1)", write=True)
    for col in ("default_download_behavior TEXT DEFAULT 'use_distribution_path'",
                "auto_download_new_files INTEGER DEFAULT 0", "auto_check_enabled INTEGER DEFAULT 1",
                "auto_check_tick_minutes INTEGER DEFAULT 10", "transmission_poll_minutes INTEGER DEFAULT 3",
                "auto_clean_enabled INTEGER DEFAULT 0", "auto_clean_days INTEGER DEFAULT 30",
                "auto_clean_on_watch INTEGER DEFAULT 0"):
        try:
            db(f"ALTER TABLE transmission_settings ADD COLUMN {col}", write=True)
        except sqlite3.OperationalError:
            pass

    db("""CREATE TABLE IF NOT EXISTS distributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title_external_id TEXT UNIQUE,
            tracker_name TEXT DEFAULT 'rutracker', torrent_id TEXT, url TEXT, download_path TEXT,
            mode TEXT DEFAULT 'smart', check_interval_hours INTEGER DEFAULT 6, status TEXT DEFAULT 'idle',
            last_checked_at TEXT, last_files_hash TEXT, last_files_json TEXT, last_episode_detected TEXT,
            next_episode_air_date TEXT, last_new_files_at TEXT, new_files_count INTEGER DEFAULT 0,
            error_message TEXT, error_count INTEGER DEFAULT 0, added_at TEXT DEFAULT (datetime('now')))""", write=True)
    for col in ("new_files_count INTEGER DEFAULT 0", "last_new_files_at TEXT",
                "dot_ack INTEGER DEFAULT 0", "dl_ack INTEGER DEFAULT 0"):
        try:
            db(f"ALTER TABLE distributions ADD COLUMN {col}", write=True)
        except sqlite3.OperationalError:
            pass

    db("""CREATE TABLE IF NOT EXISTS download_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, distribution_id INTEGER, file_name TEXT,
            file_size INTEGER, transmission_hash TEXT, sent_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT, episode_season INTEGER, episode_number INTEGER,
            FOREIGN KEY (distribution_id) REFERENCES distributions(id) ON DELETE CASCADE)""", write=True)
    for col in ("completed_at TEXT", "episode_season INTEGER", "episode_number INTEGER"):
        try:
            db(f"ALTER TABLE download_history ADD COLUMN {col}", write=True)
        except sqlite3.OperationalError:
            pass

    db("""CREATE TABLE IF NOT EXISTS distribution_patterns (
            distribution_id INTEGER PRIMARY KEY, samples_json TEXT DEFAULT '[]',
            median_delay_hours INTEGER, samples_count INTEGER DEFAULT 0, confidence TEXT DEFAULT 'low',
            min_samples INTEGER DEFAULT 3, last_updated_at TEXT,
            FOREIGN KEY (distribution_id) REFERENCES distributions(id) ON DELETE CASCADE)""", write=True)
    try:
        db("ALTER TABLE distribution_patterns ADD COLUMN min_samples INTEGER DEFAULT 3", write=True)
    except sqlite3.OperationalError:
        pass

    db("""CREATE TABLE IF NOT EXISTS sent_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, notification_type TEXT NOT NULL, external_id TEXT NOT NULL,
            details TEXT DEFAULT '', sent_date TEXT NOT NULL, sent_at TEXT DEFAULT (datetime('now')))""", write=True)
    db("""CREATE INDEX IF NOT EXISTS idx_sent_notifications_lookup
          ON sent_notifications(notification_type, external_id, details, sent_date)""", write=True)


ensure_schema()


# ── Настройки ─────────────────────────────────────────
def get_setting(key, default=None):
    rows = db("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0]["value"] if rows else default


def set_setting(key, value):
    db("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value), write=True)


def get_refresh_hours():
    try:
        return max(1, min(168, int(get_setting("refresh_hours", str(REFRESH_HOURS_DEFAULT)))))
    except ValueError:
        return REFRESH_HOURS_DEFAULT


def get_proxy_url():
    v = get_setting("proxy_url", "")
    return v.strip() if v and v.strip() else None


def get_theme():
    return get_setting("theme", "dark")


def get_telegram_settings():
    rows = db("SELECT * FROM telegram_settings WHERE id=1")
    return dict(rows[0]) if rows else {}


def save_telegram_settings(bot_token, chat_id, enabled, send_time, notify_days,
                           notify_date_changes, notify_new_cards, notify_new_seasons,
                           notify_new_episodes, notify_torrent_started=False,
                           notify_torrent_completed=False, notify_season_completed=False,
                           timezone="Europe/Moscow"):
    db("""UPDATE telegram_settings SET bot_token=?, chat_id=?, enabled=?, send_time=?, notify_days=?,
            notify_date_changes=?, notify_new_cards=?, notify_new_seasons=?, notify_new_episodes=?,
            notify_torrent_started=?, notify_torrent_completed=?, notify_season_completed=?,
            timezone=? WHERE id=1""",
       (bot_token, chat_id, 1 if enabled else 0, send_time, notify_days,
        1 if notify_date_changes else 0, 1 if notify_new_cards else 0,
        1 if notify_new_seasons else 0, 1 if notify_new_episodes else 0,
        1 if notify_torrent_started else 0, 1 if notify_torrent_completed else 0,
        1 if notify_season_completed else 0, timezone), write=True)


def get_transmission_settings():
    rows = db("SELECT * FROM transmission_settings WHERE id=1")
    return dict(rows[0]) if rows else {}


# ── Утилиты ───────────────────────────────────────────
def sanitize_id(external_id: str) -> str:
    return external_id.replace(":", "_").replace("/", "_")


def parse_tmdb_id(external_id: str):
    if not external_id or not external_id.startswith("tmdb:"):
        return None
    parts = external_id.split(":")
    if len(parts) == 3 and parts[1] in ("movie", "tv"):
        try:
            return int(parts[2])
        except ValueError:
            return None
    if len(parts) == 2:
        try:
            return int(parts[1])
        except ValueError:
            return None
    return None


def parse_tmdb_type(external_id: str):
    if not external_id or not external_id.startswith("tmdb:"):
        return None
    parts = external_id.split(":")
    if len(parts) == 3 and parts[1] in ("movie", "tv"):
        return parts[1]
    return None


def human_date(iso):
    if not iso:
        return None
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return None
    return f"{d.day} {MONTHS_RU[d.month - 1]} {d.year}"


def short_date(iso):
    if not iso:
        return None
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return None
    return f"{d.day} {MONTHS_RU_SHORT[d.month - 1]}"


def plural(n, forms):
    n = abs(n) % 100
    if 10 < n < 15:
        return forms[2]
    n %= 10
    return forms[0] if n == 1 else forms[1] if 1 < n < 5 else forms[2]


def refresh_period_label(hours):
    if hours == 1:
        return "каждый час"
    if hours < 24:
        return f"каждые {hours} ч."
    if hours == 24:
        return "раз в день"
    if hours % 24 == 0:
        d = hours // 24
        return f"раз в {d} {plural(d, ('день', 'дня', 'дней'))}"
    return f"каждые {hours} ч."


def is_today(iso_date):
    if not iso_date:
        return False
    try:
        return date.fromisoformat(iso_date) == date.today()
    except ValueError:
        return False


def progress_percent(w, t):
    return round(w / t * 100) if t > 0 else 0


def format_size(size_bytes):
    if not size_bytes:
        return "?"
    mb = size_bytes / (1024 * 1024)
    return f"{mb / 1024:.2f} ГБ" if mb > 1024 else f"{mb:.1f} МБ"


def parse_torrent_url(url: str) -> tuple:
    url = url.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if "rutracker" in host:
        for pair in parsed.query.split("&"):
            if pair.startswith("t="):
                return "rutracker", pair[2:]
        m = _re.search(r"t=(\d+)", url)
        return ("rutracker", m.group(1)) if m else (None, None)
    if "kinozal" in host:
        for pair in parsed.query.split("&"):
            if pair.startswith("id="):
                return "kinozal", pair[3:]
        m = _re.search(r"id=(\d+)", url)
        return ("kinozal", m.group(1)) if m else (None, None)
    if "rutor" in host:
        m = _re.search(r"/(?:download/|torrent/)(\d+)", url)
        return ("rutor", m.group(1)) if m else (None, None)
    return (None, None)


_EP_PATTERNS = [
    _re.compile(r"[Ss](\d{1,2})\s*[Ee](\d{1,3})"),
    _re.compile(r"(\d{1,2})\s*[xх]\s*(\d{1,3})"),
    _re.compile(r"[Сс]езон\s*(\d{1,2}).*?[Сс]ери[ия]\s*(\d{1,3})"),
]


def parse_episode(filename):
    for pat in _EP_PATTERNS:
        m = pat.search(filename)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def log_update(external_id, title, field, old_value, new_value):
    db("INSERT INTO updates_log (external_id, title, field, old_value, new_value) VALUES (?,?,?,?,?)",
       (external_id, title, field, old_value, new_value), write=True)


templates.env.globals["get_theme"] = get_theme
templates.env.globals["format_size"] = format_size