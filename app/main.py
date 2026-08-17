import os
import io
import json
import sqlite3
import asyncio
import uuid
import hashlib
import zipfile
import tempfile
import traceback
from abc import ABC, abstractmethod
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, Form, Request, Response, BackgroundTasks, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .rutracker import (RuTrackerClient, RuTrackerError,
                        RuTrackerCaptchaError, RuTrackerAuthError,
                        RuTrackerForbiddenError)
from .transmission import TransmissionClient

OMDB_KEY = os.getenv("OMDB_API_KEY", "")
TMDB_KEY = os.getenv("TMDB_API_KEY", "")
DB_PATH = os.getenv("DB_PATH", "/data/catalog.db")
REFRESH_HOURS_DEFAULT = int(os.getenv("REFRESH_HOURS", "12"))

app = FastAPI()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

POSTERS_DIR = os.path.join(os.path.dirname(DB_PATH), "posters")
os.makedirs(POSTERS_DIR, exist_ok=True)
app.mount("/posters", StaticFiles(directory=POSTERS_DIR), name="posters")

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]

refresh_progress = {"running": False, "done": 0, "total": 0}

BACKUP_VERSION = "1.0.1"
SETTINGS_TABLES = ["settings", "telegram_settings"]
CARD_TABLES = ["titles", "seasons", "episodes", "watched_episodes", "updates_log"]
TORRENT_TABLES = ["tracker_credentials", "transmission_settings",
                  "distributions", "download_history", "distribution_patterns"]

ENCRYPTION_KEY_PATH = os.path.join(os.path.dirname(DB_PATH), "encryption.key")


# ── Encryption ────────────────────────────────────────
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


# ── Image helpers ─────────────────────────────────────
def sanitize_id(external_id: str) -> str:
    return external_id.replace(":", "_").replace("/", "_")


def parse_tmdb_id(external_id: str) -> int | None:
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


def parse_tmdb_type(external_id: str) -> str | None:
    if not external_id or not external_id.startswith("tmdb:"):
        return None
    parts = external_id.split(":")
    if len(parts) == 3 and parts[1] in ("movie", "tv"):
        return parts[1]
    return None


async def download_image(url: str, filename: str) -> str | None:
    if not url:
        return None
    os.makedirs(POSTERS_DIR, exist_ok=True)
    local_path = os.path.join(POSTERS_DIR, filename)
    if os.path.exists(local_path):
        return f"/posters/{filename}"
    try:
        proxy = get_proxy_url()
        client_kwargs = {"timeout": 15, "follow_redirects": True}
        if proxy:
            client_kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**client_kwargs) as client:
            r = await client.get(url)
            r.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(r.content)
        return f"/posters/{filename}"
    except Exception as e:
        print(f"[download] Error downloading {url}: {e}")
        return None


async def download_card_poster(info: dict) -> str | None:
    url = info.get("poster_url")
    if not url:
        return None
    filename = f"{sanitize_id(info['external_id'])}.jpg"
    local = await download_image(url, filename)
    return local or url


def ensure_proxied(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("/posters/") or url.startswith("/img-proxy"):
        return url
    return f"/img-proxy?url={quote(url, safe='')}"


# ── Source abstraction ────────────────────────────────
class Source(ABC):
    name: str = ""

    @abstractmethod
    async def search(self, query: str) -> list[dict]: ...

    @abstractmethod
    async def fetch(self, external_id: str) -> dict | None: ...

    @abstractmethod
    async def search_candidates(self, query: str) -> list[dict]: ...


class OmdbSource(Source):
    name = "omdb"

    async def _get(self, params: dict) -> dict:
        proxy = get_proxy_url()
        client_kwargs = {"timeout": 10}
        if proxy:
            client_kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**client_kwargs) as client:
            r = await client.get("https://www.omdbapi.com/",
                                 params={**params, "apikey": OMDB_KEY})
            r.raise_for_status()
            return r.json()

    def _parse_date(self, s: str | None) -> str | None:
        if not s or s == "N/A":
            return None
        try:
            return datetime.strptime(s, "%d %b %Y").date().isoformat()
        except ValueError:
            return None

    def _to_card(self, d: dict) -> dict:
        poster = d.get("Poster")
        return {
            "external_id": d["imdbID"],
            "title": d["Title"],
            "type": d.get("Type"),
            "release_date": self._parse_date(d.get("Released")),
            "poster_url": poster if poster != "N/A" else None,
            "genres": d.get("Genre", "") or "",
        }

    async def search(self, query: str) -> list[dict]:
        data = await self._get({"s": query})
        if data.get("Response") != "True":
            return []
        q = query.strip().lower()
        hits = data["Search"]

        def score(it):
            year = it.get("Year", "")[:4]
            return (it["Title"].strip().lower() == q,
                    it.get("Type") in ("movie", "series"),
                    year if year.isdigit() else "0000")

        best = max(hits, key=score)
        detail = await self._get({"i": best["imdbID"]})
        if detail.get("Response") != "True":
            return []
        return [self._to_card(detail)]

    async def fetch(self, external_id: str) -> dict | None:
        detail = await self._get({"i": external_id})
        if detail.get("Response") != "True":
            return None
        return self._to_card(detail)

    async def search_candidates(self, query: str) -> list[dict]:
        data = await self._get({"s": query})
        if data.get("Response") != "True":
            return []
        candidates = []
        for r in data["Search"]:
            poster = r.get("Poster")
            candidates.append({
                "external_id": r["imdbID"],
                "title": r["Title"],
                "year": r.get("Year", ""),
                "type": r.get("Type"),
                "poster_url": poster if poster and poster != "N/A" else None,
                "source": "omdb",
            })
        return candidates


class TmdbSource(Source):
    name = "tmdb"
    _POSTER = "https://image.tmdb.org/t/p/w342"
    _POSTER_SMALL = "https://image.tmdb.org/t/p/w154"

    async def _get(self, path: str, params: dict | None = None) -> dict:
        params = {"api_key": TMDB_KEY, "language": "ru-RU", **(params or {})}
        proxy = get_proxy_url()
        client_kwargs = {"timeout": 10}
        if proxy:
            client_kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**client_kwargs) as client:
            r = await client.get(f"https://api.themoviedb.org/3{path}", params=params)
            r.raise_for_status()
            return r.json()

    def _parse_date(self, s: str | None) -> str | None:
        if not s:
            return None
        try:
            return date.fromisoformat(s).isoformat()
        except ValueError:
            return None

    async def _details(self, media_type: str, tmdb_id: int) -> dict:
        path = f"/{media_type}/{tmdb_id}"
        d = await self._get(path)
        if "title" in d:
            title = d.get("title") or d.get("original_title")
            type_ = "movie"
            release = d.get("release_date")
        else:
            title = d.get("name") or d.get("original_name")
            type_ = "series"
            release = d.get("first_air_date")
        genres = ", ".join(g["name"] for g in d.get("genres", []))
        poster = f"{self._POSTER}{d['poster_path']}" if d.get("poster_path") else None
        return {
            "external_id": f"tmdb:{media_type}:{tmdb_id}",
            "title": title,
            "type": type_,
            "release_date": self._parse_date(release),
            "poster_url": poster,
            "genres": genres,
        }

    async def search(self, query: str) -> list[dict]:
        data = await self._get("/search/multi", {"query": query})
        results = data.get("results") or []
        q = query.strip().lower()

        def score(r):
            name = (r.get("name") or r.get("title") or "").strip().lower()
            mt = r.get("media_type")
            return (name == q, mt in ("movie", "tv"), r.get("popularity", 0) or 0)

        valid = [r for r in results if r.get("media_type") in ("movie", "tv")]
        if not valid:
            return []
        best = max(valid, key=score)
        mt = "movie" if best["media_type"] == "movie" else "tv"
        return [await self._details(mt, best["id"])]

    async def fetch(self, external_id: str) -> dict | None:
        tmdb_id = parse_tmdb_id(external_id)
        if tmdb_id is None:
            return None
        tmdb_type = parse_tmdb_type(external_id)
        if tmdb_type:
            try:
                return await self._details(tmdb_type, tmdb_id)
            except httpx.HTTPStatusError:
                return None
        try:
            return await self._details("movie", tmdb_id)
        except httpx.HTTPStatusError:
            pass
        try:
            return await self._details("tv", tmdb_id)
        except httpx.HTTPStatusError:
            return None

    async def search_candidates(self, query: str) -> list[dict]:
        data = await self._get("/search/multi", {"query": query})
        results = data.get("results") or []
        candidates = []
        for r in results:
            mt = r.get("media_type")
            if mt not in ("movie", "tv"):
                continue
            title = r.get("title") or r.get("name") or ""
            rel = r.get("release_date") or r.get("first_air_date") or ""
            year = rel[:4] if rel else ""
            poster = f"{self._POSTER_SMALL}{r['poster_path']}" if r.get("poster_path") else None
            candidates.append({
                "external_id": f"tmdb:{mt}:{r['id']}",
                "title": title,
                "year": year,
                "type": "movie" if mt == "movie" else "series",
                "poster_url": poster,
                "source": "tmdb",
            })
        return candidates

    async def fetch_seasons(self, tmdb_id: int) -> list[dict]:
        d = await self._get(f"/tv/{tmdb_id}")
        seasons = []
        for s in d.get("seasons", []):
            seasons.append({
                "season_number": s.get("season_number"),
                "name": s.get("name"),
                "release_date": s.get("air_date"),
                "episodes": s.get("episode_count"),
                "poster_path": s.get("poster_path"),
            })
        return seasons

    async def fetch_episodes(self, tmdb_id: int, season_number: int) -> list[dict]:
        d = await self._get(f"/tv/{tmdb_id}/season/{season_number}")
        episodes = []
        for e in d.get("episodes", []):
            episodes.append({
                "episode_number": e.get("episode_number"),
                "name": e.get("name"),
                "release_date": e.get("air_date"),
                "runtime": e.get("runtime"),
                "overview": e.get("overview", ""),
                "still_path": e.get("still_path"),
            })
        return episodes


SOURCES = {"omdb": OmdbSource(), "tmdb": TmdbSource()}


# ── SQLite ────────────────────────────────────────────
def db(sql: str, params: tuple = (), write: bool = False):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, params)
        if write:
            conn.commit()
        return cur.fetchall()


def ensure_schema():
    db("""CREATE TABLE IF NOT EXISTS titles (
            external_id  TEXT PRIMARY KEY,
            title        TEXT NOT NULL,
            type         TEXT,
            release_date TEXT,
            poster_url   TEXT,
            genres       TEXT,
            source       TEXT,
            added_at     TEXT DEFAULT (datetime('now')),
            updated_at   TEXT,
            notify_enabled INTEGER DEFAULT 1
         )""", write=True)
    for col in ("genres", "source", "updated_at", "notify_enabled"):
        try:
            db(f"ALTER TABLE titles ADD COLUMN {col} TEXT", write=True)
        except sqlite3.OperationalError:
            pass

    db("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
         )""", write=True)

    db("""CREATE TABLE IF NOT EXISTS telegram_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            bot_token TEXT DEFAULT '',
            chat_id TEXT DEFAULT '',
            enabled INTEGER DEFAULT 0,
            send_time TEXT DEFAULT '09:00',
            notify_days INTEGER DEFAULT 1,
            notify_date_changes INTEGER DEFAULT 1,
            notify_new_cards INTEGER DEFAULT 1,
            notify_new_seasons INTEGER DEFAULT 1,
            notify_new_episodes INTEGER DEFAULT 1,
            notify_torrent_started INTEGER DEFAULT 1,
            notify_torrent_completed INTEGER DEFAULT 1,
            last_sent TEXT
         )""", write=True)
    db("INSERT OR IGNORE INTO telegram_settings (id) VALUES (1)", write=True)
    for col in ("notify_date_changes", "notify_new_cards",
                "notify_new_seasons", "notify_new_episodes",
                "notify_torrent_started", "notify_torrent_completed"):
        try:
            db(f"ALTER TABLE telegram_settings ADD COLUMN {col} INTEGER DEFAULT 1",
               write=True)
        except sqlite3.OperationalError:
            pass

    db("""CREATE TABLE IF NOT EXISTS updates_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id TEXT,
            title TEXT,
            field TEXT,
            old_value TEXT,
            new_value TEXT,
            created_at TEXT DEFAULT (datetime('now'))
         )""", write=True)

    db("""CREATE TABLE IF NOT EXISTS seasons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_external_id TEXT NOT NULL,
            season_number INTEGER NOT NULL,
            name TEXT,
            release_date TEXT,
            episodes INTEGER,
            poster_url TEXT,
            UNIQUE(title_external_id, season_number)
         )""", write=True)

    db("""CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            season_id INTEGER NOT NULL,
            episode_number INTEGER NOT NULL,
            name TEXT,
            release_date TEXT,
            runtime INTEGER,
            overview TEXT,
            poster_url TEXT,
            UNIQUE(season_id, episode_number),
            FOREIGN KEY (season_id) REFERENCES seasons(id)
         )""", write=True)

    db("""CREATE TABLE IF NOT EXISTS watched_episodes (
            title_external_id TEXT NOT NULL,
            season_number INTEGER NOT NULL,
            episode_number INTEGER NOT NULL,
            watched_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (title_external_id, season_number, episode_number)
         )""", write=True)

    db("""CREATE TABLE IF NOT EXISTS tracker_credentials (
            tracker_name TEXT PRIMARY KEY,
            username TEXT DEFAULT '',
            encrypted_password TEXT DEFAULT '',
            encrypted_cookies TEXT DEFAULT '',
            cookies_expires_at TEXT,
            user_agent TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            last_login_at TEXT,
            last_error TEXT,
            error_count INTEGER DEFAULT 0
         )""", write=True)
    try:
        db("ALTER TABLE tracker_credentials ADD COLUMN user_agent TEXT DEFAULT ''",
           write=True)
    except sqlite3.OperationalError:
        pass

    db("""CREATE TABLE IF NOT EXISTS transmission_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            host TEXT DEFAULT 'localhost',
            port INTEGER DEFAULT 9091,
            username TEXT DEFAULT '',
            encrypted_password TEXT DEFAULT '',
            enabled INTEGER DEFAULT 0,
            base_download_dir TEXT DEFAULT '',
            action_on_new TEXT DEFAULT 'download',
            filter_recent_only INTEGER DEFAULT 1,
            min_file_size_mb INTEGER DEFAULT 500,
            default_check_interval INTEGER DEFAULT 6,
            default_download_behavior TEXT DEFAULT 'use_distribution_path',
            auto_download_new_files INTEGER DEFAULT 0
         )""", write=True)
    db("INSERT OR IGNORE INTO transmission_settings (id) VALUES (1)", write=True)
    for col in ("default_download_behavior TEXT DEFAULT 'use_distribution_path'",
                "auto_download_new_files INTEGER DEFAULT 0"):
        try:
            db(f"ALTER TABLE transmission_settings ADD COLUMN {col}", write=True)
        except sqlite3.OperationalError:
            pass

    db("""CREATE TABLE IF NOT EXISTS distributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_external_id TEXT UNIQUE,
            tracker_name TEXT DEFAULT 'rutracker',
            torrent_id TEXT,
            url TEXT,
            download_path TEXT,
            mode TEXT DEFAULT 'smart',
            check_interval_hours INTEGER DEFAULT 6,
            status TEXT DEFAULT 'idle',
            last_checked_at TEXT,
            last_files_hash TEXT,
            last_files_json TEXT,
            last_episode_detected TEXT,
            next_episode_air_date TEXT,
            new_files_count INTEGER DEFAULT 0,
            error_message TEXT,
            error_count INTEGER DEFAULT 0,
            added_at TEXT DEFAULT (datetime('now'))
         )""", write=True)
    try:
        db("ALTER TABLE distributions ADD COLUMN new_files_count INTEGER DEFAULT 0",
           write=True)
    except sqlite3.OperationalError:
        pass

    db("""CREATE TABLE IF NOT EXISTS download_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            distribution_id INTEGER,
            file_name TEXT,
            file_size INTEGER,
            transmission_hash TEXT,
            sent_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (distribution_id) REFERENCES distributions(id) ON DELETE CASCADE
         )""", write=True)

    db("""CREATE TABLE IF NOT EXISTS distribution_patterns (
            distribution_id INTEGER PRIMARY KEY,
            samples_json TEXT DEFAULT '[]',
            median_delay_hours INTEGER,
            samples_count INTEGER DEFAULT 0,
            confidence TEXT DEFAULT 'low',
            last_updated_at TEXT,
            FOREIGN KEY (distribution_id) REFERENCES distributions(id) ON DELETE CASCADE
         )""", write=True)

    db("""CREATE TABLE IF NOT EXISTS sent_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notification_type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            details TEXT DEFAULT '',
            sent_date TEXT NOT NULL,
            sent_at TEXT DEFAULT (datetime('now'))
         )""", write=True)
    db("""CREATE INDEX IF NOT EXISTS idx_sent_notifications_lookup
          ON sent_notifications(notification_type, external_id, details, sent_date)""",
       write=True)


ensure_schema()


# ── Settings helpers ──────────────────────────────────
def get_setting(key: str, default: str | None = None) -> str | None:
    rows = db("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0]["value"] if rows else default


def set_setting(key: str, value: str):
    db("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
       (key, value), write=True)


def get_refresh_hours() -> int:
    v = get_setting("refresh_hours", str(REFRESH_HOURS_DEFAULT))
    try:
        return max(1, min(168, int(v)))
    except ValueError:
        return REFRESH_HOURS_DEFAULT


def get_proxy_url() -> str | None:
    v = get_setting("proxy_url", "")
    return v.strip() if v and v.strip() else None


def get_theme() -> str:
    return get_setting("theme", "dark")


def get_telegram_settings() -> dict:
    rows = db("SELECT * FROM telegram_settings WHERE id=1")
    return dict(rows[0]) if rows else {}


def save_telegram_settings(bot_token: str, chat_id: str, enabled: bool,
                           send_time: str, notify_days: int,
                           notify_date_changes: bool, notify_new_cards: bool,
                           notify_new_seasons: bool, notify_new_episodes: bool,
                           notify_torrent_started: bool = False,
                           notify_torrent_completed: bool = False):
    db("""UPDATE telegram_settings SET
            bot_token=?, chat_id=?, enabled=?, send_time=?,
            notify_days=?, notify_date_changes=?, notify_new_cards=?,
            notify_new_seasons=?, notify_new_episodes=?,
            notify_torrent_started=?, notify_torrent_completed=?
          WHERE id=1""",
       (bot_token, chat_id, 1 if enabled else 0, send_time,
        notify_days, 1 if notify_date_changes else 0,
        1 if notify_new_cards else 0, 1 if notify_new_seasons else 0,
        1 if notify_new_episodes else 0,
        1 if notify_torrent_started else 0,
        1 if notify_torrent_completed else 0),
       write=True)


def get_transmission_settings() -> dict:
    rows = db("SELECT * FROM transmission_settings WHERE id=1")
    return dict(rows[0]) if rows else {}


def get_tracker_credentials(tracker_name: str) -> dict | None:
    rows = db("SELECT * FROM tracker_credentials WHERE tracker_name=?", (tracker_name,))
    return dict(rows[0]) if rows else None


def get_distribution(title_external_id: str) -> dict | None:
    rows = db("SELECT * FROM distributions WHERE title_external_id=?", (title_external_id,))
    return dict(rows[0]) if rows else None


def load_tracker_cookies(tracker_name: str) -> dict | None:
    creds = get_tracker_credentials(tracker_name)
    if not creds or not creds.get("encrypted_cookies"):
        return None
    try:
        raw = decrypt_value(creds["encrypted_cookies"])
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:
        print(f"[tracker] Failed to load cookies: {e}")
        return None


def build_tracker_client(tracker_name: str = "rutracker",
                         cookies: dict | None = None) -> RuTrackerClient:
    creds = get_tracker_credentials(tracker_name) or {}
    username = creds.get("username", "")
    password = decrypt_value(creds.get("encrypted_password", "")) if creds.get("encrypted_password") else ""
    user_agent = creds.get("user_agent", "")
    effective_cookies = cookies if cookies is not None else load_tracker_cookies(tracker_name)
    return RuTrackerClient(
        username=username,
        password=password,
        proxy=get_proxy_url(),
        cookies=effective_cookies,
        user_agent=user_agent)


def build_transmission_client() -> TransmissionClient:
    trans = get_transmission_settings() or {}
    password = decrypt_value(trans.get("encrypted_password", "")) if trans.get("encrypted_password") else ""
    return TransmissionClient(
        host=trans.get("host", "localhost"),
        port=trans.get("port", 9091),
        username=trans.get("username", ""),
        password=password
    )


# ── Notification dedup ────────────────────────────────
def was_notified_today(notification_type: str, external_id: str, details: str = "") -> bool:
    today = date.today().isoformat()
    rows = db("""SELECT 1 FROM sent_notifications
                 WHERE notification_type=? AND external_id=? AND details=? AND sent_date=?""",
              (notification_type, external_id, details, today))
    return bool(rows)


def mark_notified_today(notification_type: str, external_id: str, details: str = ""):
    today = date.today().isoformat()
    db("""INSERT INTO sent_notifications (notification_type, external_id, details, sent_date)
          VALUES (?,?,?,?)""",
       (notification_type, external_id, details, today), write=True)


def cleanup_old_notifications(days_to_keep: int = 7):
    cutoff = (date.today() - timedelta(days=days_to_keep)).isoformat()
    db("DELETE FROM sent_notifications WHERE sent_date < ?", (cutoff,), write=True)


templates.env.globals["get_theme"] = get_theme


# ── Update log ────────────────────────────────────────
def log_update(external_id: str, title: str, field: str,
               old_value: str | None, new_value: str | None):
    db("""INSERT INTO updates_log (external_id, title, field, old_value, new_value)
          VALUES (?,?,?,?,?)""",
       (external_id, title, field, old_value, new_value), write=True)


# ── Helpers ─────────────────────────────────────────
def human_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return None
    return f"{d.day} {MONTHS_RU[d.month - 1]} {d.year}"


def plural(n: int, forms: tuple) -> str:
    n = abs(n) % 100
    if 10 < n < 15:
        return forms[2]
    n %= 10
    return forms[0] if n == 1 else forms[1] if 1 < n < 5 else forms[2]


def refresh_period_label(hours: int) -> str:
    if hours == 1:
        return "каждый час"
    if hours < 24:
        return f"каждые {hours} ч."
    if hours == 24:
        return "раз в день"
    if hours % 24 == 0:
        days = hours // 24
        return f"раз в {days} {plural(days, ('день', 'дня', 'дней'))}"
    return f"каждые {hours} ч."


def is_today(iso_date: str | None) -> bool:
    if not iso_date:
        return False
    try:
        return date.fromisoformat(iso_date) == date.today()
    except ValueError:
        return False


def progress_percent(watched: int, total: int) -> int:
    return round(watched / total * 100) if total > 0 else 0


def parse_torrent_id(url: str) -> str | None:
    url = url.strip()
    if "viewtopic.php" in url:
        parsed = urlparse(url)
        for pair in parsed.query.split("&"):
            if pair.startswith("t="):
                return pair[2:]
    import re
    m = re.search(r"t=(\d+)", url)
    return m.group(1) if m else None


def format_size(size_bytes: int) -> str:
    if not size_bytes:
        return "?"
    size_mb = size_bytes / (1024 * 1024)
    if size_mb > 1024:
        return f"{size_mb / 1024:.2f} ГБ"
    return f"{size_mb:.1f} МБ"


# ── Seasons & episodes helpers ────────────────────────
async def save_seasons(title_external_id: str, seasons: list) -> list:
    existing = db("SELECT season_number, id FROM seasons WHERE title_external_id=?",
                  (title_external_id,))
    existing_map = {r["season_number"]: r["id"] for r in existing}

    new_seasons = []
    safe_id = sanitize_id(title_external_id)
    for s in seasons:
        poster_local = None
        if s.get("poster_path"):
            url = f"https://image.tmdb.org/t/p/w342{s['poster_path']}"
            filename = f"{safe_id}_s{s['season_number']}.jpg"
            poster_local = await download_image(url, filename) or url

        if s["season_number"] in existing_map:
            if poster_local:
                db("""UPDATE seasons SET name=?, release_date=?, episodes=?, poster_url=?
                      WHERE id=?""",
                   (s["name"], s["release_date"], s["episodes"], poster_local,
                    existing_map[s["season_number"]]), write=True)
            else:
                db("""UPDATE seasons SET name=?, release_date=?, episodes=?
                      WHERE id=?""",
                   (s["name"], s["release_date"], s["episodes"],
                    existing_map[s["season_number"]]), write=True)
        else:
            new_seasons.append(s)
            db("""INSERT INTO seasons
                  (title_external_id, season_number, name, release_date, episodes, poster_url)
                  VALUES (?,?,?,?,?,?)""",
               (title_external_id, s["season_number"], s["name"],
                s["release_date"], s["episodes"], poster_local), write=True)

    return new_seasons


async def save_episodes(title_external_id: str, season_number: int,
                        episodes: list) -> list:
    rows = db("SELECT id FROM seasons WHERE title_external_id=? AND season_number=?",
              (title_external_id, season_number))
    if not rows:
        return []
    season_id = rows[0]["id"]

    existing = db("SELECT episode_number FROM episodes WHERE season_id=?",
                  (season_id,))
    existing_numbers = {r["episode_number"] for r in existing}

    new_episodes = []
    safe_id = sanitize_id(title_external_id)
    for e in episodes:
        if e["episode_number"] not in existing_numbers:
            new_episodes.append({
                "season_number": season_number,
                "episode_number": e["episode_number"],
                "name": e["name"],
                "release_date": e["release_date"],
            })

        poster_local = None
        if e.get("still_path"):
            url = f"https://image.tmdb.org/t/p/w300{e['still_path']}"
            filename = f"{safe_id}_s{season_number}e{e['episode_number']}.jpg"
            poster_local = await download_image(url, filename) or url

        db("""INSERT OR REPLACE INTO episodes
              (season_id, episode_number, name, release_date, runtime, overview, poster_url)
              VALUES (?,?,?,?,?,?,?)""",
           (season_id, e["episode_number"], e["name"], e["release_date"],
            e["runtime"], e.get("overview", ""), poster_local), write=True)

    return new_episodes


def get_season_count(external_id: str) -> int:
    rows = db("SELECT COUNT(*) as cnt FROM seasons WHERE title_external_id=?",
              (external_id,))
    return rows[0]["cnt"] if rows else 0


def get_next_season(external_id: str) -> dict | None:
    rows = db("""SELECT * FROM seasons
                 WHERE title_external_id=? AND release_date >= date('now')
                 ORDER BY release_date LIMIT 1""", (external_id,))
    return dict(rows[0]) if rows else None


# ── Watched episodes helpers ──────────────────────────
def is_watched(title_external_id: str, season_number: int, episode_number: int) -> bool:
    rows = db("""SELECT 1 FROM watched_episodes
                 WHERE title_external_id=? AND season_number=? AND episode_number=?""",
              (title_external_id, season_number, episode_number))
    return bool(rows)


def get_watched_set(title_external_id: str, season_number: int) -> set:
    rows = db("""SELECT episode_number FROM watched_episodes
                 WHERE title_external_id=? AND season_number=?""",
              (title_external_id, season_number))
    return {r["episode_number"] for r in rows}


def toggle_watched(title_external_id: str, season_number: int, episode_number: int):
    if is_watched(title_external_id, season_number, episode_number):
        db("""DELETE FROM watched_episodes
              WHERE title_external_id=? AND season_number=? AND episode_number=?""",
           (title_external_id, season_number, episode_number), write=True)
    else:
        db("""INSERT INTO watched_episodes
              (title_external_id, season_number, episode_number) VALUES (?,?,?)""",
           (title_external_id, season_number, episode_number), write=True)


def toggle_season_watched(title_external_id: str, season_number: int):
    rows = db("""
        SELECT e.episode_number FROM episodes e
        JOIN seasons s ON e.season_id = s.id
        WHERE s.title_external_id=? AND s.season_number=?
    """, (title_external_id, season_number))
    ep_numbers = [r["episode_number"] for r in rows]
    if not ep_numbers:
        return

    watched_set = get_watched_set(title_external_id, season_number)
    all_watched = all(n in watched_set for n in ep_numbers)

    if all_watched:
        db("""DELETE FROM watched_episodes
              WHERE title_external_id=? AND season_number=?""",
           (title_external_id, season_number), write=True)
    else:
        for n in ep_numbers:
            if n not in watched_set:
                db("""INSERT OR IGNORE INTO watched_episodes
                      (title_external_id, season_number, episode_number) VALUES (?,?,?)""",
                   (title_external_id, season_number, n), write=True)


def get_season_progress(title_external_id: str, season_number: int) -> tuple:
    rows = db("""
        SELECT COUNT(e.id) as total,
               SUM(CASE WHEN w.title_external_id IS NOT NULL THEN 1 ELSE 0 END) as watched
        FROM episodes e
        JOIN seasons s ON e.season_id = s.id
        LEFT JOIN watched_episodes w
            ON w.title_external_id = s.title_external_id
            AND w.season_number = s.season_number
            AND w.episode_number = e.episode_number
        WHERE s.title_external_id=? AND s.season_number=?
    """, (title_external_id, season_number))
    if rows:
        return (rows[0]["watched"] or 0, rows[0]["total"] or 0)
    return (0, 0)


def get_show_progress(title_external_id: str) -> tuple:
    rows = db("""
        SELECT COUNT(e.id) as total,
               SUM(CASE WHEN w.title_external_id IS NOT NULL THEN 1 ELSE 0 END) as watched
        FROM episodes e
        JOIN seasons s ON e.season_id = s.id
        LEFT JOIN watched_episodes w
            ON w.title_external_id = s.title_external_id
            AND w.season_number = s.season_number
            AND w.episode_number = e.episode_number
        WHERE s.title_external_id=?
    """, (title_external_id,))
    if rows:
        return (rows[0]["watched"] or 0, rows[0]["total"] or 0)
    return (0, 0)


# ── Telegram ──────────────────────────────────────────
async def send_telegram(text: str) -> bool:
    s = get_telegram_settings()
    if not s or not s.get("bot_token") or not s.get("chat_id"):
        return False
    url = f"https://api.telegram.org/bot{s['bot_token']}/sendMessage"
    try:
        proxy = get_proxy_url()
        client_kwargs = {"timeout": 10}
        if proxy:
            client_kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**client_kwargs) as client:
            r = await client.post(url, json={
                "chat_id": s["chat_id"],
                "text": text,
                "parse_mode": "HTML",
            })
            return r.status_code == 200
    except Exception as e:
        print(f"[telegram] Send error: {e}")
        return False


async def notify_date_changes(changes: list, force: bool = False):
    s = get_telegram_settings()
    if not force and (not s or not s.get("enabled") or not s.get("notify_date_changes")):
        return
    if not s or not s.get("bot_token") or not s.get("chat_id"):
        return
    if not changes:
        return

    if len(changes) == 1:
        lines = ["📅 <b>Перенос даты релиза</b>", ""]
    else:
        lines = ["📅 <b>Переносы дат релизов</b>", ""]

    for c in changes:
        old_h = human_date(c["old_date"]) or "не указана"
        new_h = human_date(c["new_date"]) or "не указана"

        direction = ""
        if c["old_date"] and c["new_date"]:
            try:
                old_d = date.fromisoformat(c["old_date"])
                new_d = date.fromisoformat(c["new_date"])
                delta = (new_d - old_d).days
                if delta > 0:
                    direction = f"⬇️ на {delta} {plural(delta, ('день', 'дня', 'дней'))} позже"
                elif delta < 0:
                    direction = f"⬆️ на {abs(delta)} {plural(abs(delta), ('день', 'дня', 'дней'))} раньше"
            except ValueError:
                pass
        elif not c["old_date"] and c["new_date"]:
            direction = "✅ дата стала известна"
        elif c["old_date"] and not c["new_date"]:
            direction = "⚠️ дата больше не указана"

        lines.append(f"🎬 <b>{c['title']}</b>")
        lines.append(f"Было: {old_h}")
        lines.append(f"Стало: {new_h}")
        if direction:
            lines.append(direction)
        lines.append("")

    await send_telegram("\n".join(lines))
    print(f"[telegram] Date change notification sent ({len(changes)} titles)")


async def notify_new_card(title: str, release_date, source: str,
                          card_type, force: bool = False):
    s = get_telegram_settings()
    if not force and (not s or not s.get("enabled") or not s.get("notify_new_cards")):
        return
    if not s or not s.get("bot_token") or not s.get("chat_id"):
        return

    lines = ["🆕 <b>Новая карточка в каталоге</b>", ""]
    type_label = {"movie": "Фильм", "series": "Сериал"}.get(card_type, "")
    if type_label:
        lines.append(type_label)
    lines.append(f"🎬 <b>{title}</b>")
    if release_date:
        lines.append(f"📅 {human_date(release_date)}")
    else:
        lines.append("📅 дата неизвестна")
    lines.append(f"Источник: {source}")

    await send_telegram("\n".join(lines))
    print(f"[telegram] New card notification sent: {title}")


async def notify_new_season(show_title: str, season_number: int,
                            release_date, force: bool = False):
    s = get_telegram_settings()
    if not force and (not s or not s.get("enabled") or not s.get("notify_new_seasons")):
        return
    if not s or not s.get("bot_token") or not s.get("chat_id"):
        return

    details = json.dumps({"season": season_number})
    if not force and was_notified_today("new_season", show_title, details):
        print(f"[telegram] Season already notified today: {show_title} S{season_number}")
        return
    mark_notified_today("new_season", show_title, details)

    lines = [
        "🆕 <b>Новый сезон в каталоге</b>",
        "",
        f"📺 <b>{show_title}</b> — Сезон {season_number}",
    ]
    if release_date:
        lines.append(f"📅 {human_date(release_date)}")
    else:
        lines.append("📅 дата неизвестна")

    await send_telegram("\n".join(lines))
    print(f"[telegram] New season notification: {show_title} S{season_number}")


async def notify_new_episodes(show_title: str, new_eps: list, force: bool = False):
    s = get_telegram_settings()
    if not force and (not s or not s.get("enabled") or not s.get("notify_new_episodes")):
        return
    if not s or not s.get("bot_token") or not s.get("chat_id"):
        return
    if not new_eps:
        return

    sorted_eps = sorted(new_eps, key=lambda x: (x["season_number"], x["episode_number"]))

    eps_to_notify = []
    for ep in sorted_eps:
        details = json.dumps({"season": ep["season_number"], "episode": ep["episode_number"]})
        if force or not was_notified_today("new_episode", show_title, details):
            eps_to_notify.append(ep)
            mark_notified_today("new_episode", show_title, details)

    if not eps_to_notify:
        print(f"[telegram] All episodes already notified today: {show_title}")
        return

    if len(eps_to_notify) == 1:
        ep = eps_to_notify[0]
        lines = [
            "🆕 <b>Новый эпизод</b>",
            "",
            f"📺 <b>{show_title}</b> — S{ep['season_number']}E{ep['episode_number']}",
        ]
        if ep["name"]:
            lines.append(f"«{ep['name']}»")
        if ep["release_date"]:
            if is_today(ep["release_date"]):
                lines.append("🔴 <b>Вышел сегодня!</b>")
            else:
                lines.append(f"📅 {human_date(ep['release_date'])}")
    else:
        lines = [
            f"🆕 <b>Новые эпизоды ({len(eps_to_notify)})</b>",
            "",
            f"📺 <b>{show_title}</b>",
        ]
        for ep in eps_to_notify:
            name_str = f": {ep['name']}" if ep["name"] else ""
            if is_today(ep["release_date"]):
                lines.append(f"• 🔴 S{ep['season_number']}E{ep['episode_number']}"
                             f"{name_str} — вышел сегодня!")
            else:
                date_str = f" · {human_date(ep['release_date'])}" if ep["release_date"] else ""
                lines.append(f"• S{ep['season_number']}E{ep['episode_number']}"
                             f"{name_str}{date_str}")

    await send_telegram("\n".join(lines))
    print(f"[telegram] New episodes notification: {show_title} ({len(eps_to_notify)} eps)")


async def notify_torrent_started(title: str, torrent_name: str,
                                 download_dir: str = None, force: bool = False):
    s = get_telegram_settings()
    if not force and (not s or not s.get("enabled") or not s.get("notify_torrent_started")):
        return
    if not s or not s.get("bot_token") or not s.get("chat_id"):
        return

    lines = [
        "📥 <b>Начато скачивание</b>",
        "",
        f"🎬 <b>{title}</b>",
        f"📦 {torrent_name}",
    ]
    if download_dir:
        lines.append(f"📂 {download_dir}")
    lines.append("⏳ Загрузка запущена в Transmission")

    await send_telegram("\n".join(lines))
    print(f"[telegram] Torrent started notification: {title}")


async def notify_torrent_completed(title: str, torrent_name: str,
                                   size_bytes: int, force: bool = False):
    s = get_telegram_settings()
    if not force and (not s or not s.get("enabled") or not s.get("notify_torrent_completed")):
        return
    if not s or not s.get("bot_token") or not s.get("chat_id"):
        return

    lines = [
        "✅ <b>Скачивание завершено</b>",
        "",
        f"🎬 <b>{title}</b>",
        f"📦 {torrent_name}",
        f"📏 {format_size(size_bytes)}",
    ]

    await send_telegram("\n".join(lines))
    print(f"[telegram] Torrent completed notification: {title}")


async def check_and_notify(force: bool = False):
    cleanup_old_notifications(7)

    s = get_telegram_settings()
    if not force and (not s or not s.get("enabled")):
        return
    if not s or not s.get("bot_token") or not s.get("chat_id"):
        return
    today = date.today()
    notify_days = s.get("notify_days", 1)

    upcoming = []

    rows = db("SELECT * FROM titles WHERE release_date IS NOT NULL AND notify_enabled = 1")
    for r in rows:
        row = dict(r)
        try:
            rd = date.fromisoformat(row["release_date"])
        except ValueError:
            continue
        delta = (rd - today).days
        if 0 <= delta <= notify_days:
            upcoming.append((delta, row["title"], row["release_date"]))

    season_rows = db("""SELECT s.*, t.title as show_title, t.notify_enabled
                        FROM seasons s
                        JOIN titles t ON s.title_external_id = t.external_id
                        WHERE s.release_date IS NOT NULL AND t.notify_enabled = 1""")
    for r in season_rows:
        row = dict(r)
        try:
            rd = date.fromisoformat(row["release_date"])
        except ValueError:
            continue
        delta = (rd - today).days
        if 0 <= delta <= notify_days:
            label = f"{row['show_title']} — Сезон {row['season_number']}"
            upcoming.append((delta, label, row["release_date"]))

    episode_rows = db("""
        SELECT e.name, e.release_date, e.episode_number,
               s.season_number, t.title as show_title
        FROM episodes e
        JOIN seasons s ON e.season_id = s.id
        JOIN titles t ON s.title_external_id = t.external_id
        WHERE e.release_date IS NOT NULL AND t.notify_enabled = 1
    """)
    for r in episode_rows:
        row = dict(r)
        try:
            rd = date.fromisoformat(row["release_date"])
        except ValueError:
            continue
        delta = (rd - today).days
        if 0 <= delta <= notify_days:
            label = f"{row['show_title']} — S{row['season_number']}E{row['episode_number']}"
            if row["name"]:
                label += f" «{row['name']}»"
            upcoming.append((delta, label, row["release_date"]))

    if not upcoming:
        if force:
            await send_telegram(
                f"🎬 <b>Тестовая рассылка</b>\n"
                f"Предстоящих релизов в горизонте {notify_days} дн. нет.")
        else:
            print("[telegram] No upcoming releases to notify")
        return

    upcoming.sort()
    lines = ["🎬 <b>Скоро на экранах</b>", ""]
    for delta, title, rd in upcoming:
        if delta == 0:
            lines.append(f"🔴 <b>Сегодня:</b> {title}")
        elif delta == 1:
            lines.append(f"🟠 <b>Завтра:</b> {title}")
        else:
            lines.append(f"📅 {human_date(rd)}: {title}")

    ok = await send_telegram("\n".join(lines))
    if ok:
        db("UPDATE telegram_settings SET last_sent=datetime('now') WHERE id=1", write=True)
        print(f"[telegram] Notification sent ({len(upcoming)} items)")


def schedule_telegram_job():
    s = get_telegram_settings()
    send_time = s.get("send_time", "09:00") or "09:00"
    try:
        hour, minute = send_time.split(":")
        hour, minute = int(hour), int(minute)
    except (ValueError, AttributeError):
        hour, minute = 9, 0

    scheduler.add_job(check_and_notify,
                      CronTrigger(hour=hour, minute=minute),
                      id="telegram_notify",
                      replace_existing=True)
    print(f"[telegram] Scheduled at {hour:02d}:{minute:02d}")


# ── Background refresh ────────────────────────────────
async def refresh_catalog():
    global refresh_progress
    rows = db("SELECT * FROM titles")
    today = date.today()
    updated = 0
    skipped = 0
    date_changes = []

    refresh_progress = {"running": True, "done": 0, "total": len(rows)}
    print(f"[refresh] Starting refresh at {datetime.now().isoformat()}, {len(rows)} titles")

    for i, r in enumerate(rows):
        row = dict(r)

        if row["source"] == "local":
            refresh_progress["done"] = i + 1
            continue

        if row["release_date"]:
            try:
                rd = date.fromisoformat(row["release_date"])
                if rd < today - timedelta(days=30):
                    skipped += 1
                    refresh_progress["done"] = i + 1
                    continue
            except ValueError:
                pass

        src = SOURCES.get(row["source"])
        if not src:
            refresh_progress["done"] = i + 1
            continue

        try:
            fresh = await src.fetch(row["external_id"])
            await asyncio.sleep(1)
        except Exception as e:
            print(f"[refresh] Error fetching {row['external_id']}: {e}")
            refresh_progress["done"] = i + 1
            continue

        if not fresh:
            refresh_progress["done"] = i + 1
            continue

        changed = False
        new_title = fresh["title"] or row["title"]
        new_rd = fresh["release_date"] or row["release_date"]
        new_genres = fresh["genres"] or row["genres"]

        if new_title != row["title"]:
            log_update(row["external_id"], new_title, "title",
                       row["title"], new_title)
            changed = True
        if new_rd != row["release_date"]:
            log_update(row["external_id"], new_title, "release_date",
                       row["release_date"], new_rd)
            if row.get("notify_enabled") in (None, 1):
                date_changes.append({
                    "title": new_title,
                    "old_date": row["release_date"],
                    "new_date": new_rd,
                })
            changed = True
        if new_genres != row["genres"]:
            log_update(row["external_id"], new_title, "genres",
                       row["genres"], new_genres)
            changed = True

        poster_local = row["poster_url"]
        if fresh.get("poster_url"):
            filename = f"{sanitize_id(row['external_id'])}.jpg"
            downloaded = await download_image(fresh["poster_url"], filename)
            if downloaded:
                poster_local = downloaded

        if changed or poster_local != row["poster_url"]:
            db("""UPDATE titles SET
                    title=?, release_date=?, poster_url=?, genres=?,
                    updated_at=datetime('now')
                  WHERE external_id=?""",
               (new_title, new_rd, poster_local, new_genres, row["external_id"]),
               write=True)
            if changed:
                updated += 1

        if row["type"] == "series" and src.name == "tmdb":
            tmdb_id = parse_tmdb_id(row["external_id"])
            if tmdb_id is not None:
                try:
                    seasons = await src.fetch_seasons(tmdb_id)
                    new_seasons = await save_seasons(row["external_id"], seasons)
                    await asyncio.sleep(0.5)

                    for ns in new_seasons:
                        await notify_new_season(new_title, ns["season_number"],
                                                ns["release_date"])

                    new_episodes_all = []
                    for season in seasons:
                        try:
                            episodes = await src.fetch_episodes(tmdb_id,
                                                                season["season_number"])
                            new_eps = await save_episodes(row["external_id"],
                                                          season["season_number"], episodes)
                            new_episodes_all.extend(new_eps)
                            await asyncio.sleep(0.5)
                        except Exception as e:
                            print(f"[refresh] Error fetching episodes S{season['season_number']}: {e}")

                    if new_episodes_all:
                        await notify_new_episodes(new_title, new_episodes_all)
                except Exception as e:
                    print(f"[refresh] Error fetching seasons for {row['external_id']}: {e}")

        refresh_progress["done"] = i + 1

    refresh_progress["running"] = False
    print(f"[refresh] Done. Updated: {updated}, Skipped (old): {skipped}")

    if date_changes:
        await notify_date_changes(date_changes)


async def refresh_single(external_id: str) -> bool:
    rows = db("SELECT * FROM titles WHERE external_id=?", (external_id,))
    if not rows:
        return False
    row = dict(rows[0])
    if row["source"] == "local":
        return False
    src = SOURCES.get(row["source"])
    if not src:
        return False

    try:
        fresh = await src.fetch(external_id)
    except Exception as e:
        print(f"[refresh-single] Error fetching {external_id}: {e}")
        return False

    if not fresh:
        return False

    changed = False
    new_title = fresh["title"] or row["title"]
    new_rd = fresh["release_date"] or row["release_date"]
    new_genres = fresh["genres"] or row["genres"]
    date_change = None

    if new_title != row["title"]:
        log_update(external_id, new_title, "title", row["title"], new_title)
        changed = True
    if new_rd != row["release_date"]:
        log_update(external_id, new_title, "release_date",
                   row["release_date"], new_rd)
        if row.get("notify_enabled") in (None, 1):
            date_change = {
                "title": new_title,
                "old_date": row["release_date"],
                "new_date": new_rd,
            }
        changed = True
    if new_genres != row["genres"]:
        log_update(external_id, new_title, "genres", row["genres"], new_genres)
        changed = True

    poster_local = row["poster_url"]
    if fresh.get("poster_url"):
        filename = f"{sanitize_id(external_id)}.jpg"
        downloaded = await download_image(fresh["poster_url"], filename)
        if downloaded:
            poster_local = downloaded

    if changed or poster_local != row["poster_url"]:
        db("""UPDATE titles SET
                title=?, release_date=?, poster_url=?, genres=?,
                updated_at=datetime('now')
              WHERE external_id=?""",
           (new_title, new_rd, poster_local, new_genres, external_id), write=True)

    if row["type"] == "series" and src.name == "tmdb":
        tmdb_id = parse_tmdb_id(external_id)
        if tmdb_id is not None:
            try:
                seasons = await src.fetch_seasons(tmdb_id)
                new_seasons = await save_seasons(external_id, seasons)
                for ns in new_seasons:
                    await notify_new_season(new_title, ns["season_number"],
                                            ns["release_date"])

                new_episodes_all = []
                for season in seasons:
                    try:
                        episodes = await src.fetch_episodes(tmdb_id, season["season_number"])
                        new_eps = await save_episodes(external_id, season["season_number"], episodes)
                        new_episodes_all.extend(new_eps)
                        await asyncio.sleep(0.3)
                    except Exception:
                        pass

                if new_episodes_all:
                    await notify_new_episodes(new_title, new_episodes_all)
            except Exception as e:
                print(f"[refresh-single] Error refreshing seasons: {e}")

    if date_change:
        await notify_date_changes([date_change])

    return changed


scheduler = AsyncIOScheduler()
scheduler.add_job(refresh_catalog, "interval", hours=get_refresh_hours(),
                  id="refresh", next_run_time=None)


@app.on_event("startup")
async def on_startup():
    scheduler.start()
    scheduler.reschedule_job("refresh", trigger="interval", hours=get_refresh_hours())
    scheduler.modify_job("refresh", next_run_time=datetime.now() + timedelta(minutes=5))
    schedule_telegram_job()


@app.on_event("shutdown")
async def on_shutdown():
    scheduler.shutdown()


# ── iCalendar export ──────────────────────────────────
def escape_ics(s):
    if not s:
        return ""
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_ics(cards: list) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Movie Radar//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Скоро на экранах",
        "X-WR-TIMEZONE:Europe/Moscow",
        "X-APPLE-CALENDAR-COLOR:#4F8CFF",
    ]
    for c in cards:
        if not c.get("release_date"):
            continue
        uid = c["external_id"].replace(":", "_")
        desc_parts = []
        if c.get("type"):
            desc_parts.append(f"Тип: {c['type']}")
        if c.get("genres"):
            desc_parts.append(f"Жанр: {c['genres']}")
        if c.get("source"):
            desc_parts.append(f"Источник: {c['source']}")
        description = "\\n".join(desc_parts)
        dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}@movieradar",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;VALUE=DATE:{c['release_date'].replace('-', '')}",
            f"DTEND;VALUE=DATE:{c['release_date'].replace('-', '')}",
            f"SUMMARY:{escape_ics('Премьера: ' + c['title'])}",
            f"DESCRIPTION:{description}",
            "STATUS:CONFIRMED",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


# ── Backup / Restore ──────────────────────────────────
def _build_backup_zip(include_settings: bool, include_cards: bool,
                      include_images: bool, include_torrents: bool) -> io.BytesIO:
    tmp_db_fd, tmp_db_path = tempfile.mkstemp(suffix=".db")
    os.close(tmp_db_fd)
    try:
        src_conn = sqlite3.connect(DB_PATH)
        dst_conn = sqlite3.connect(tmp_db_path)
        src_conn.backup(dst_conn)
        src_conn.close()

        if not include_settings:
            for t in SETTINGS_TABLES:
                dst_conn.execute(f"DROP TABLE IF EXISTS {t}")
        if not include_cards:
            for t in CARD_TABLES:
                dst_conn.execute(f"DROP TABLE IF EXISTS {t}")
        if not include_torrents:
            for t in TORRENT_TABLES:
                dst_conn.execute(f"DROP TABLE IF EXISTS {t}")
        dst_conn.commit()
        dst_conn.close()

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            meta = {
                "app": "movie-radar",
                "backup_version": BACKUP_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "include_settings": include_settings,
                "include_cards": include_cards,
                "include_images": include_images,
                "include_torrents": include_torrents,
            }
            zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
            zf.write(tmp_db_path, "backup.db")

            if include_images and os.path.isdir(POSTERS_DIR):
                for root, _, files in os.walk(POSTERS_DIR):
                    for fn in files:
                        full = os.path.join(root, fn)
                        rel = os.path.relpath(full, POSTERS_DIR)
                        arcname = os.path.join("posters", rel)
                        zf.write(full, arcname)

        buffer.seek(0)
        return buffer
    finally:
        if os.path.exists(tmp_db_path):
            os.remove(tmp_db_path)


@app.post("/backup/create")
async def create_backup(include_settings: str = Form("off"),
                        include_cards: str = Form("off"),
                        include_images: str = Form("off"),
                        include_torrents: str = Form("off")):
    inc_settings = include_settings == "on"
    inc_cards = include_cards == "on"
    inc_images = include_images == "on"
    inc_torrents = include_torrents == "on"

    if not (inc_settings or inc_cards or inc_images or inc_torrents):
        return RedirectResponse("/settings?msg=backup-empty", status_code=303)

    buffer = _build_backup_zip(inc_settings, inc_cards, inc_images, inc_torrents)
    filename = f"movie-radar-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.post("/backup/restore")
async def restore_backup(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".zip"):
        return RedirectResponse("/settings?msg=restore-invalid", status_code=303)

    content = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return RedirectResponse("/settings?msg=restore-invalid", status_code=303)

    names = zf.namelist()
    if "meta.json" not in names or "backup.db" not in names:
        return RedirectResponse("/settings?msg=restore-invalid", status_code=303)

    try:
        meta = json.loads(zf.read("meta.json"))
    except (json.JSONDecodeError, KeyError):
        return RedirectResponse("/settings?msg=restore-invalid", status_code=303)

    tmp_path = None
    scheduler.pause()
    try:
        try:
            auto_buffer = _build_backup_zip(True, True, True, True)
            auto_path = os.path.join(os.path.dirname(DB_PATH), "auto-backup-latest.zip")
            with open(auto_path, "wb") as f:
                f.write(auto_buffer.read())
            print(f"[backup] Auto-backup saved to {auto_path}")
        except Exception as e:
            print(f"[backup] Auto-backup failed (continuing): {e}")

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(tmp_fd)
        with zf.open("backup.db") as src, open(tmp_path, "wb") as dst:
            dst.write(src.read())

        include_settings = meta.get("include_settings", False)
        include_cards = meta.get("include_cards", False)
        include_torrents = meta.get("include_torrents", False)

        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("ATTACH DATABASE ? AS backup", (tmp_path,))

            tables_to_restore = []
            if include_settings:
                tables_to_restore.extend(SETTINGS_TABLES)
            if include_cards:
                tables_to_restore.extend(CARD_TABLES)
            if include_torrents:
                tables_to_restore.extend(TORRENT_TABLES)

            has_seq_table = conn.execute(
                "SELECT name FROM backup.sqlite_master "
                "WHERE type='table' AND name='sqlite_sequence'"
            ).fetchone()

            for table in tables_to_restore:
                row = conn.execute(
                    "SELECT name FROM backup.sqlite_master WHERE type='table' AND name=?",
                    (table,)).fetchone()
                if not row:
                    continue
                conn.execute(f"DELETE FROM main.{table}")
                conn.execute(f"INSERT INTO main.{table} SELECT * FROM backup.{table}")

                if has_seq_table:
                    has_seq = conn.execute(
                        "SELECT 1 FROM backup.sqlite_sequence WHERE name=?",
                        (table,)).fetchone()
                    if has_seq:
                        max_id = conn.execute(
                            f"SELECT MAX(id) FROM main.{table}").fetchone()[0] or 0
                        conn.execute(
                            "DELETE FROM main.sqlite_sequence WHERE name=?", (table,))
                        conn.execute(
                            "INSERT INTO main.sqlite_sequence (name, seq) VALUES (?,?)",
                            (table, max_id))

            conn.commit()
            conn.execute("DETACH DATABASE backup")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.commit()
        finally:
            conn.close()

        if meta.get("include_images", False):
            os.makedirs(POSTERS_DIR, exist_ok=True)
            for name in names:
                if name.startswith("posters/") and not name.endswith("/"):
                    rel = os.path.relpath(name, "posters")
                    target = os.path.join(POSTERS_DIR, rel)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(name) as src, open(target, "wb") as dst:
                        dst.write(src.read())

        try:
            scheduler.reschedule_job("refresh", trigger="interval",
                                     hours=get_refresh_hours())
            schedule_telegram_job()
        except Exception as e:
            print(f"[backup] Error re-applying settings: {e}")

    except Exception as e:
        print(f"[backup] Restore error: {e}")
        traceback.print_exc()
        return RedirectResponse("/settings?msg=restore-error", status_code=303)
    finally:
        scheduler.resume()
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    return RedirectResponse("/settings?msg=restore-ok", status_code=303)


# ── Image proxy route ─────────────────────────────────
_ALLOWED_IMG_HOSTS = {
    "image.tmdb.org",
    "m.media-amazon.com",
    "ia.media-imdb.com",
    "upload.wikimedia.org",
}


@app.get("/img-proxy")
async def img_proxy(url: str):
    parsed = urlparse(url)
    if parsed.hostname not in _ALLOWED_IMG_HOSTS:
        return Response(status_code=403)

    url_hash = hashlib.md5(url.encode()).hexdigest()
    cache_path = os.path.join(POSTERS_DIR, f"cache_{url_hash}.img")
    meta_path = os.path.join(POSTERS_DIR, f"cache_{url_hash}.mime")

    if os.path.exists(cache_path):
        media_type = "image/jpeg"
        if os.path.exists(meta_path):
            with open(meta_path, "r") as f:
                media_type = f.read().strip() or "image/jpeg"
        with open(cache_path, "rb") as f:
            content = f.read()
        return Response(content=content, media_type=media_type)

    try:
        proxy = get_proxy_url()
        client_kwargs = {"timeout": 15, "follow_redirects": True}
        if proxy:
            client_kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**client_kwargs) as client:
            r = await client.get(url)
            r.raise_for_status()
            content = r.content
            media_type = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            with open(cache_path, "wb") as f:
                f.write(content)
            with open(meta_path, "w") as f:
                f.write(media_type)
            return Response(content=content, media_type=media_type)
    except Exception as e:
        print(f"[img-proxy] Error fetching {url}: {e}")
        return Response(status_code=502)


# ── Stage 2-3: rutracker + Transmission ──────────────
async def check_distribution_now(title_external_id: str) -> tuple:
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

    if cookies:
        print("[dist-check] Using saved cookies")
    else:
        try:
            cookies = await client.login()
        except RuTrackerCaptchaError:
            db("UPDATE tracker_credentials SET last_error='captcha' WHERE tracker_name=?",
               (dist["tracker_name"],), write=True)
            return False, "Трекер требует капчу"
        except (RuTrackerAuthError, RuTrackerForbiddenError) as e:
            db("UPDATE tracker_credentials SET last_error=? WHERE tracker_name=?",
               (str(e), dist["tracker_name"]), write=True)
            return False, str(e)
        except RuTrackerError as e:
            db("UPDATE tracker_credentials SET last_error=? WHERE tracker_name=?",
               (str(e), dist["tracker_name"]), write=True)
            return False, str(e)

        db("""UPDATE tracker_credentials SET encrypted_cookies=?, last_login_at=datetime('now'),
              last_error=NULL, error_count=0 WHERE tracker_name=?""",
           (encrypt_value(json.dumps(cookies)), dist["tracker_name"]), write=True)

    try:
        files = await client.fetch_files(dist["torrent_id"], cookies)
    except RuTrackerForbiddenError as e:
        db("UPDATE distributions SET status='error', error_message=? WHERE id=?",
           (str(e), dist["id"]), write=True)
        if username and password:
            print("[dist-check] 403 on fetch, attempting re-login")
            try:
                new_cookies = await client.login()
                db("""UPDATE tracker_credentials SET encrypted_cookies=?, last_login_at=datetime('now')
                      WHERE tracker_name=?""",
                   (encrypt_value(json.dumps(new_cookies)), dist["tracker_name"]), write=True)
                files = await client.fetch_files(dist["torrent_id"], new_cookies)
            except Exception as retry_err:
                return False, f"Ошибка: {e} (повторная попытка: {retry_err})"
        else:
            return False, f"{e} Обновите cookies в настройках."
    except RuTrackerError as e:
        db("UPDATE distributions SET status='error', error_message=? WHERE id=?",
           (str(e), dist["id"]), write=True)
        return False, str(e)

    if not files:
        db("""UPDATE distributions SET status='error',
              error_message='Не удалось распарсить список файлов (debug-дамп сохранён)'
              WHERE id=?""", (dist["id"],), write=True)
        return False, "Не удалось распарсить список файлов. Debug-дамп: /data/debug_last_topic.html"

    snapshot = sorted([(f["name"], f["size"]) for f in files])
    new_hash = hashlib.md5(json.dumps(snapshot, ensure_ascii=False).encode()).hexdigest()
    old_hash = dist["last_files_hash"]

    new_count = 0
    if old_hash and old_hash != new_hash:
        try:
            old_files = json.loads(dist["last_files_json"] or "[]")
        except json.JSONDecodeError:
            old_files = []
        old_names = {f[0] for f in old_files}
        new_count = len([f for f in snapshot if f[0] not in old_names])

    status = "has_new" if new_count else "idle"
    db("""UPDATE distributions SET last_checked_at=datetime('now'), last_files_hash=?,
          last_files_json=?, status=?, new_files_count=?, error_count=0, error_message=NULL
          WHERE id=?""",
       (new_hash, json.dumps(snapshot, ensure_ascii=False), status, new_count,
        dist["id"]), write=True)

    if new_count > 0:
        trans = get_transmission_settings()
        if (trans and trans.get("enabled") and
            trans.get("auto_download_new_files") and
            trans.get("action_on_new") != "notify_only"):
            try:
                torrent_data = await client.download_torrent(dist["torrent_id"], cookies)
                download_dir = _resolve_download_dir(dist, trans)
                paused = (trans.get("action_on_new") == "pause")

                trans_client = build_transmission_client()
                result = trans_client.add_torrent(torrent_data, download_dir, paused)

                # После отправки в Transmission возвращаем статус в idle
                db("UPDATE distributions SET status='idle' WHERE id=?",
                   (dist["id"],), write=True)
                db("""INSERT INTO download_history
                      (distribution_id, file_name, file_size, transmission_hash, sent_at)
                      VALUES (?, ?, ?, ?, datetime('now'))""",
                   (dist["id"], result["name"], result["size"], result["hash"]),
                   write=True)

                card_rows = db("SELECT title FROM titles WHERE external_id=?",
                               (title_external_id,))
                card_title = card_rows[0]["title"] if card_rows else title_external_id
                await notify_torrent_started(card_title, result["name"], download_dir)
                print(f"[dist-check] Auto-downloaded: {result['name']}")
            except Exception as e:
                print(f"[dist-check] Auto-download failed: {e}")

    if not old_hash:
        return True, f"Первая проверка: найдено {len(files)} файлов, снапшот сохранён"
    if new_count:
        return True, f"Обнаружено новых файлов: {new_count}"
    return True, "Изменений нет"


def _resolve_download_dir(dist: dict, trans: dict) -> str | None:
    behavior = trans.get("default_download_behavior", "use_distribution_path")
    if behavior == "use_distribution_path":
        if dist.get("download_path"):
            return dist["download_path"]
        return trans.get("base_download_dir") or None
    return trans.get("base_download_dir") or None


@app.post("/distribution/check/{title_external_id}")
async def check_distribution(title_external_id: str, sort: str = "date"):
    ok, message = await check_distribution_now(title_external_id)
    msg_param = "dist-checked" if ok else "dist-check-fail"
    set_setting("last_dist_check", message)
    return RedirectResponse(f"/?sort={sort}&msg={msg_parameter}", status_code=303)


@app.post("/settings/tracker/test")
async def test_tracker_login():
    creds = get_tracker_credentials("rutracker")
    if not creds:
        print("[tracker-test] No credentials record")
        return RedirectResponse("/settings?msg=tracker-test-fail", status_code=303)

    cookies = load_tracker_cookies("rutracker")
    username = creds.get("username", "")
    password = decrypt_value(creds.get("encrypted_password", "")) if creds.get("encrypted_password") else ""

    if not cookies and not (username and password):
        print("[tracker-test] No cookies and no credentials")
        return RedirectResponse("/settings?msg=tracker-test-fail", status_code=303)

    client = build_tracker_client("rutracker", cookies=cookies)

    if cookies:
        print("[tracker-test] Validating saved cookies")
        is_valid, reason = await client.validate_cookies(cookies)
        if is_valid:
            db("""UPDATE tracker_credentials SET last_login_at=datetime('now'),
                  last_error=NULL, error_count=0 WHERE tracker_name='rutracker'""",
               write=True)
            return RedirectResponse("/settings?msg=tracker-test-ok", status_code=303)
        else:
            if username and password:
                print(f"[tracker-test] Cookies invalid ({reason}), attempting login")
            else:
                db("UPDATE tracker_credentials SET last_error=? WHERE tracker_name='rutracker'",
                   (reason,), write=True)
                return RedirectResponse("/settings?msg=tracker-cookies-invalid", status_code=303)

    try:
        new_cookies = await client.login()
        db("""UPDATE tracker_credentials SET encrypted_cookies=?,
              last_login_at=datetime('now'), last_error=NULL, error_count=0
              WHERE tracker_name='rutracker'""",
           (encrypt_value(json.dumps(new_cookies)),), write=True)
        return RedirectResponse("/settings?msg=tracker-test-ok", status_code=303)
    except RuTrackerCaptchaError:
        db("UPDATE tracker_credentials SET last_error='captcha' WHERE tracker_name='rutracker'",
           write=True)
        return RedirectResponse("/settings?msg=tracker-captcha", status_code=303)
    except RuTrackerForbiddenError as e:
        db("UPDATE tracker_credentials SET last_error=? WHERE tracker_name='rutracker'",
           (str(e),), write=True)
        return RedirectResponse("/settings?msg=tracker-forbidden", status_code=303)
    except RuTrackerError as e:
        print(f"[tracker-test] Login failed: {e}")
        db("UPDATE tracker_credentials SET last_error=?, error_count=error_count+1 WHERE tracker_name='rutracker'",
           (str(e),), write=True)
        return RedirectResponse("/settings?msg=tracker-test-fail", status_code=303)
    except Exception as e:
        print(f"[tracker-test] Unexpected error: {type(e).__name__}: {e}")
        traceback.print_exc()
        db("UPDATE tracker_credentials SET last_error=? WHERE tracker_name='rutracker'",
           (f"{type(e).__name__}: {e}",), write=True)
        return RedirectResponse("/settings?msg=tracker-test-fail", status_code=303)


@app.post("/distribution/download/{title_external_id}")
async def download_distribution(title_external_id: str, sort: str = "date"):
    dist = get_distribution(title_external_id)
    if not dist:
        set_setting("last_dist_download", "Раздача не найдена")
        return RedirectResponse(f"/?sort={sort}&msg=dist-download-fail", status_code=303)

    trans = get_transmission_settings()
    if not trans or not trans.get("enabled"):
        set_setting("last_dist_download", "Transmission отключён в настройках")
        return RedirectResponse(f"/?sort={sort}&msg=dist-download-fail", status_code=303)

    creds = get_tracker_credentials(dist["tracker_name"])
    if not creds or not creds.get("enabled"):
        set_setting("last_dist_download", "Трекер отключён в настройках")
        return RedirectResponse(f"/?sort={sort}&msg=dist-download-fail", status_code=303)

    cookies = load_tracker_cookies(dist["tracker_name"])
    if not cookies:
        set_setting("last_dist_download", "Не найдены cookies трекера")
        return RedirectResponse(f"/?sort={sort}&msg=dist-download-fail", status_code=303)

    tracker_client = build_tracker_client(dist["tracker_name"], cookies=cookies)

    try:
        print(f"[download] Downloading .torrent for {dist['torrent_id']}")
        torrent_data = await tracker_client.download_torrent(dist["torrent_id"], cookies)
        print(f"[download] Downloaded {len(torrent_data)} bytes")

        download_dir = _resolve_download_dir(dist, trans)
        action = trans.get("action_on_new", "download")
        paused = (action == "pause")

        print(f"[download] Sending to Transmission: dir={download_dir}, paused={paused}")

        trans_client = build_transmission_client()
        result = trans_client.add_torrent(torrent_data, download_dir, paused)

        # После отправки в Transmission возвращаем статус в idle
        db("UPDATE distributions SET status='idle', error_count=0, error_message=NULL WHERE id=?",
           (dist["id"],), write=True)

        db("""INSERT INTO download_history
              (distribution_id, file_name, file_size, transmission_hash, sent_at)
              VALUES (?, ?, ?, ?, datetime('now'))""",
           (dist["id"], result["name"], result["size"], result["hash"]),
           write=True)

        card_rows = db("SELECT title FROM titles WHERE external_id=?",
                       (title_external_id,))
        card_title = card_rows[0]["title"] if card_rows else title_external_id

        await notify_torrent_started(card_title, result["name"], download_dir)

        set_setting("last_dist_download",
                    f"✅ Отправлено: {result['name']} ({format_size(result['size'])})")
        return RedirectResponse(f"/?sort={sort}&msg=dist-downloaded", status_code=303)

    except Exception as e:
        error_msg = str(e)
        print(f"[download] Error: {type(e).__name__}: {error_msg}")
        traceback.print_exc()
        db("UPDATE distributions SET status='error', error_message=?, error_count=error_count+1 WHERE id=?",
           (error_msg, dist["id"]), write=True)
        set_setting("last_dist_download", f"❌ Ошибка: {error_msg}")
        return RedirectResponse(f"/?sort={sort}&msg=dist-download-fail", status_code=303)


@app.post("/settings/transmission/test")
async def test_transmission():
    trans = get_transmission_settings()
    if not trans or not trans.get("host"):
        return RedirectResponse("/settings?msg=transmission-test-fail", status_code=303)
    try:
        trans_client = build_transmission_client()
        ok, message = trans_client.test_connection()
        set_setting("last_trans_test", message)
        return RedirectResponse(
            "/settings?msg=" + ("transmission-test-ok" if ok else "transmission-test-fail"),
            status_code=303)
    except Exception as e:
        print(f"[transmission-test] Error: {e}")
        set_setting("last_trans_test", str(e))
        return RedirectResponse("/settings?msg=transmission-test-fail", status_code=303)


# ── Routes ────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, sort: str = "date",
                err: str | None = None, msg: str | None = None):
    order = {
        "date":  "release_date IS NULL, release_date",
        "title": "title COLLATE NOCASE",
        "genre": "genres IS NULL OR genres = '', genres COLLATE NOCASE, title COLLATE NOCASE",
    }.get(sort, "release_date IS NULL, release_date")

    rows = db(f"SELECT * FROM titles ORDER BY {order}")

    today = date.today()
    cards = []
    for r in rows:
        c = dict(r)
        c["date_human"] = human_date(c["release_date"])
        c["notify_enabled"] = c.get("notify_enabled") in (None, 1)
        c["badge"], c["released"] = None, False
        if c["release_date"]:
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

        if c["type"] == "series":
            c["season_count"] = get_season_count(c["external_id"])
            next_s = get_next_season(c["external_id"])
            if next_s:
                c["next_season_date_human"] = human_date(next_s["release_date"])
                c["next_season_number"] = next_s["season_number"]
                c["display_poster"] = next_s.get("poster_url") or c["poster_url"]
            else:
                c["next_season_date_human"] = None
                c["next_season_number"] = None
                c["display_poster"] = c["poster_url"]

            w, t = get_show_progress(c["external_id"])
            c["watch_label"] = f"{w}/{t}" if t > 0 else None
            c["watch_percent"] = progress_percent(w, t)
        else:
            c["display_poster"] = c["poster_url"]

        c["display_poster"] = ensure_proxied(c.get("display_poster"))

        dist = get_distribution(c["external_id"])
        c["has_distribution"] = dist is not None
        c["distribution_status"] = dist["status"] if dist else None
        c["new_count"] = dist["new_files_count"] if dist else 0
        if dist:
            c["distribution_url"] = dist["url"]
            c["distribution_download_path"] = dist["download_path"]
            c["distribution_mode"] = dist["mode"]
            c["distribution_check_interval_hours"] = dist["check_interval_hours"]

        cards.append(c)

    success_messages = {
        "refresh-started": "Обновление запущено в фоне.",
        "card-updated": "Карточка обновлена.",
        "dist-added": "Раздача добавлена.",
        "dist-updated": "Раздача обновлена. Нажмите ⟳ для загрузки нового списка файлов.",
        "dist-removed": "Раздача удалена.",
        "dist-checked": get_setting("last_dist_check") or "Проверка выполнена.",
        "dist-downloaded": get_setting("last_dist_download") or "Торрент отправлен в Transmission.",
    }
    error_messages = {
        "dist-exists": "Раздача уже добавлена к этой карточке.",
        "dist-invalid-url": "Неверная ссылка на раздачу.",
        "dist-check-fail": get_setting("last_dist_check") or "Ошибка проверки раздачи.",
        "dist-download-fail": get_setting("last_dist_download") or "Ошибка скачивания.",
    }

    return templates.TemplateResponse(
        request, "index.html", {
            "cards": cards, "sort": sort,
            "error": "Ничего не нашлось — уточните название." if err else None,
            "message": success_messages.get(msg),
            "error_message": error_messages.get(msg),
        })


@app.get("/new", response_class=HTMLResponse)
async def new_card_page(request: Request, msg: str | None = None):
    success_messages = {
        "added-local": "Карточка добавлена локально.",
        "added": "Карточка добавлена.",
    }
    error_messages = {
        "search-fail": "Не удалось получить данные по выбранной карточке.",
    }
    return templates.TemplateResponse(
        request, "add.html", {
            "sources": list(SOURCES.keys()),
            "default_source": request.cookies.get("source", "tmdb"),
            "message": success_messages.get(msg),
            "error_message": error_messages.get(msg),
        })


@app.post("/search")
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


@app.post("/add")
async def add_local(title: str = Form(...),
                    release_date: str | None = Form(None)):
    local_id = f"local:{uuid.uuid4().hex[:12]}"
    db("""INSERT INTO titles
          (external_id, title, type, release_date, poster_url, genres, source, updated_at)
          VALUES (?,?,?,?,?,?,?,datetime('now'))""",
       (local_id, title.strip(), None, release_date or None, None, "", "local"),
       write=True)
    await notify_new_card(title.strip(), release_date, "local", None)
    return RedirectResponse("/new?msg=added-local", status_code=303)


@app.post("/add-select")
async def add_select(external_id: str = Form(...),
                     source: str = Form("tmdb"),
                     release_date: str | None = Form(None)):
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
    poster_local = await download_card_poster(info)

    db("""INSERT OR REPLACE INTO titles
          (external_id, title, type, release_date, poster_url, genres, source, updated_at)
          VALUES (?,?,?,?,?,?,?,datetime('now'))""",
       (info["external_id"], info["title"], info["type"], rd,
        poster_local, info["genres"], src.name), write=True)

    if info["type"] == "series" and src.name == "tmdb":
        tmdb_id = parse_tmdb_id(info["external_id"])
        if tmdb_id is not None:
            try:
                seasons = await src.fetch_seasons(tmdb_id)
                await save_seasons(info["external_id"], seasons)
                for season in seasons:
                    try:
                        episodes = await src.fetch_episodes(tmdb_id, season["season_number"])
                        await save_episodes(info["external_id"], season["season_number"], episodes)
                        await asyncio.sleep(0.3)
                    except Exception:
                        pass
            except Exception as e:
                print(f"[add-select] Error fetching seasons: {e}")

    await notify_new_card(info["title"], rd, src.name, info["type"])
    resp = RedirectResponse("/new?msg=added", status_code=303)
    resp.set_cookie("source", src.name, max_age=60 * 60 * 24 * 365)
    return resp


@app.post("/refresh")
async def refresh(background: BackgroundTasks):
    background.add_task(refresh_catalog)
    return RedirectResponse("/?msg=refresh-started", status_code=303)


@app.get("/refresh-status")
async def refresh_status():
    return refresh_progress


@app.post("/refresh/{external_id}")
async def refresh_card(external_id: str, sort: str = "date"):
    await refresh_single(external_id)
    return RedirectResponse(f"/?sort={sort}&msg=card-updated", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, msg: str | None = None):
    s = get_telegram_settings()
    log_rows = db("SELECT * FROM updates_log ORDER BY created_at DESC LIMIT 200")
    proxy_url = get_setting("proxy_url", "") or ""
    theme = get_setting("theme", "dark")
    trans = get_transmission_settings()
    tracker = get_tracker_credentials("rutracker")

    tracker_has_cookies = False
    if tracker and tracker.get("encrypted_cookies"):
        tracker_has_cookies = bool(decrypt_value(tracker["encrypted_cookies"]))

    success_messages = {
        "refresh-saved": "Период обновления сохранён.",
        "telegram-saved": "Настройки Telegram сохранены.",
        "proxy-saved": "Настройки прокси сохранены.",
        "proxy-ok": "Прокси работает! Соединение установлено.",
        "theme-saved": "Тема сохранена.",
        "test-ok": "Тестовое сообщение отправлено!",
        "restore-ok": "Восстановление завершено. Данные заменены из бэкапа.",
        "transmission-saved": "Настройки Transmission сохранены.",
        "transmission-test-ok": get_setting("last_trans_test") or "Transmission подключён.",
        "tracker-saved": "Настройки трекера сохранены.",
        "tracker-cookies-saved": "Cookies трекера сохранены и зашифрованы.",
        "tracker-test-ok": "Подключение к трекеру успешно!",
    }
    error_messages = {
        "proxy-fail": "Не удалось подключиться через прокси. Проверьте URL.",
        "proxy-not-set": "Прокси не настроен. Укажите URL прокси.",
        "test-fail": "Не удалось отправить. Проверьте токен и chat_id.",
        "backup-empty": "Выберите хотя бы один компонент для бэкапа.",
        "restore-invalid": "Неверный файл бэкапа. Ожидается архив movie-radar.",
        "restore-error": "Ошибка при восстановлении. Подробности в логах контейнера.",
        "transmission-test-fail": get_setting("last_trans_test") or "Не удалось подключиться к Transmission.",
        "tracker-test-fail": "Не удалось войти на трекер. Проверьте учётные данные.",
        "tracker-captcha": "Трекер требует капчу. Войдите вручную в браузере и обновите cookies.",
        "tracker-forbidden": "Rutracker заблокировал запрос (403). Обновите cookies из браузера.",
        "tracker-cookies-invalid": "Cookies невалидны. Скопируйте свежие из браузера.",
    }

    return templates.TemplateResponse(
        request, "settings.html", {
            "tg": s,
            "refresh_hours": get_refresh_hours(),
            "refresh_label": refresh_period_label(get_refresh_hours()),
            "log_rows": [dict(r) for r in log_rows],
            "proxy_url": proxy_url,
            "theme": theme,
            "trans": trans,
            "tracker": tracker,
            "tracker_has_cookies": tracker_has_cookies,
            "message": success_messages.get(msg),
            "error_message": error_messages.get(msg),
        })


@app.post("/settings/refresh")
async def set_refresh_interval(hours: int = Form(...)):
    hours = max(1, min(168, hours))
    set_setting("refresh_hours", str(hours))
    scheduler.reschedule_job("refresh", trigger="interval", hours=hours)
    print(f"[settings] Refresh interval changed to {hours}h")
    return RedirectResponse("/settings?msg=refresh-saved", status_code=303)


@app.post("/settings/theme")
async def save_theme(theme: str = Form("dark")):
    if theme not in ("dark", "light"):
        theme = "dark"
    set_setting("theme", theme)
    return RedirectResponse("/settings?msg=theme-saved", status_code=303)


@app.post("/settings/proxy")
async def save_proxy(proxy_url: str = Form("")):
    set_setting("proxy_url", proxy_url.strip())
    print(f"[settings] Proxy URL set: {proxy_url.strip() or '(none)'}")
    return RedirectResponse("/settings?msg=proxy-saved", status_code=303)


@app.post("/settings/proxy/test")
async def test_proxy():
    proxy = get_proxy_url()
    if not proxy:
        return RedirectResponse("/settings?msg=proxy-not-set", status_code=303)
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=10) as client:
            r = await client.get("https://www.omdbapi.com/",
                                 params={"apikey": "test", "t": "test"})
            print(f"[proxy] Test succeeded, status={r.status_code}")
            return RedirectResponse("/settings?msg=proxy-ok", status_code=303)
    except Exception as e:
        print(f"[proxy] Test failed: {e}")
        return RedirectResponse("/settings?msg=proxy-fail", status_code=303)


@app.post("/settings/telegram")
async def save_telegram(bot_token: str = Form(""),
                        chat_id: str = Form(""),
                        enabled: str = Form("off"),
                        send_time: str = Form("09:00"),
                        notify_days: int = Form(1),
                        notify_date_changes: str = Form("off"),
                        notify_new_cards: str = Form("off"),
                        notify_new_seasons: str = Form("off"),
                        notify_new_episodes: str = Form("off"),
                        notify_torrent_started: str = Form("off"),
                        notify_torrent_completed: str = Form("off")):
    save_telegram_settings(bot_token.strip(), chat_id.strip(),
                           enabled == "on", send_time, notify_days,
                           notify_date_changes == "on",
                           notify_new_cards == "on",
                           notify_new_seasons == "on",
                           notify_new_episodes == "on",
                           notify_torrent_started == "on",
                           notify_torrent_completed == "on")
    schedule_telegram_job()
    return RedirectResponse("/settings?msg=telegram-saved", status_code=303)


@app.post("/settings/telegram/test/{test_type}")
async def telegram_test(test_type: str):
    s = get_telegram_settings()
    if not s.get("bot_token") or not s.get("chat_id"):
        return RedirectResponse("/settings?msg=test-fail", status_code=303)

    today = date.today()
    ok = True

    if test_type == "simple":
        ok = await send_telegram("🎬 <b>Тестовое сообщение</b>\nВсё работает!")
    elif test_type == "date-change":
        await notify_date_changes([{
            "title": "Тестовый фильм",
            "old_date": (today + timedelta(days=10)).isoformat(),
            "new_date": (today + timedelta(days=15)).isoformat(),
        }], force=True)
    elif test_type == "new-card":
        await notify_new_card("Тестовый фильм",
                              (today + timedelta(days=30)).isoformat(),
                              "tmdb", "movie", force=True)
    elif test_type == "new-season":
        await notify_new_season("Тестовый сериал", 2,
                                (today + timedelta(days=20)).isoformat(),
                                force=True)
    elif test_type == "new-episodes":
        await notify_new_episodes("Тестовый сериал", [
            {"season_number": 1, "episode_number": 5,
             "name": "Тестовый эпизод",
             "release_date": today.isoformat()},
        ], force=True)
    elif test_type == "torrent-started":
        await notify_torrent_started("Тестовый сериал",
                                     "Тестовый.сериал.S01E01.1080p.mkv",
                                     "/media/series", force=True)
    elif test_type == "torrent-completed":
        await notify_torrent_completed("Тестовый сериал",
                                       "Тестовый.сериал.S01E01.1080p.mkv",
                                       2 * 1024 * 1024 * 1024, force=True)
    elif test_type == "daily":
        await check_and_notify(force=True)
    else:
        return RedirectResponse("/settings?msg=test-fail", status_code=303)

    msg = "test-ok" if ok else "test-fail"
    return RedirectResponse(f"/settings?msg={msg}", status_code=303)


@app.post("/settings/transmission")
async def save_transmission(host: str = Form("localhost"),
                            port: int = Form(9091),
                            username: str = Form(""),
                            password: str = Form(""),
                            enabled: str = Form("off"),
                            base_download_dir: str = Form(""),
                            action_on_new: str = Form("download"),
                            filter_recent_only: str = Form("off"),
                            min_file_size_mb: int = Form(500),
                            default_check_interval: int = Form(6),
                            default_download_behavior: str = Form("use_distribution_path"),
                            auto_download_new_files: str = Form("off")):
    if action_on_new not in ("download", "pause", "notify_only"):
        action_on_new = "download"
    if default_download_behavior not in ("use_distribution_path", "use_base_dir"):
        default_download_behavior = "use_distribution_path"

    db("""UPDATE transmission_settings SET
            host=?, port=?, username=?, encrypted_password=?, enabled=?,
            base_download_dir=?, action_on_new=?, filter_recent_only=?,
            min_file_size_mb=?, default_check_interval=?,
            default_download_behavior=?, auto_download_new_files=?
          WHERE id=1""",
       (host.strip(), port, username.strip(), encrypt_value(password.strip()),
        1 if enabled == "on" else 0, base_download_dir.strip(), action_on_new,
        1 if filter_recent_only == "on" else 0,
        max(1, min_file_size_mb), max(1, min(168, default_check_interval)),
        default_download_behavior,
        1 if auto_download_new_files == "on" else 0),
       write=True)
    return RedirectResponse("/settings?msg=transmission-saved", status_code=303)


@app.post("/settings/tracker")
async def save_tracker(tracker_name: str = Form("rutracker"),
                       username: str = Form(""),
                       password: str = Form(""),
                       cookies_manual: str = Form(""),
                       user_agent: str = Form(""),
                       enabled: str = Form("off")):
    enabled_val = 1 if enabled == "on" else 0
    encrypted_pwd = encrypt_value(password.strip()) if password else ""
    ua_val = user_agent.strip()

    if cookies_manual.strip():
        try:
            cookies_dict = {}
            for pair in cookies_manual.split(";"):
                pair = pair.strip()
                if "=" in pair:
                    name, value = pair.split("=", 1)
                    cookies_dict[name.strip()] = value.strip()

            if not cookies_dict:
                return RedirectResponse("/settings?msg=tracker-cookies-invalid", status_code=303)

            db("""INSERT OR REPLACE INTO tracker_credentials
                  (tracker_name, username, encrypted_password, encrypted_cookies, user_agent, enabled)
                  VALUES (?,?,?,?,?,?)""",
               (tracker_name, username.strip(), encrypted_pwd,
                encrypt_value(json.dumps(cookies_dict)), ua_val, enabled_val),
               write=True)

            client = RuTrackerClient(
                username=username.strip(),
                password=password.strip(),
                proxy=get_proxy_url(),
                cookies=cookies_dict,
                user_agent=ua_val)
            is_valid, reason = await client.validate_cookies(cookies_dict)
            if is_valid:
                db("UPDATE tracker_credentials SET last_login_at=datetime('now'), last_error=NULL WHERE tracker_name=?",
                   (tracker_name,), write=True)
                print(f"[tracker] Cookies saved and validated ({len(cookies_dict)} keys)")
                return RedirectResponse("/settings?msg=tracker-cookies-saved", status_code=303)
            else:
                db("UPDATE tracker_credentials SET last_error=? WHERE tracker_name=?",
                   (reason,), write=True)
                print(f"[tracker] Cookies saved but validation failed: {reason}")
                return RedirectResponse("/settings?msg=tracker-cookies-invalid", status_code=303)
        except Exception as e:
            print(f"[tracker] Error parsing cookies: {e}")
            return RedirectResponse("/settings?msg=tracker-cookies-invalid", status_code=303)

    existing = get_tracker_credentials(tracker_name)
    existing_cookies = existing.get("encrypted_cookies", "") if existing else ""
    db("""INSERT OR REPLACE INTO tracker_credentials
          (tracker_name, username, encrypted_password, encrypted_cookies, user_agent, enabled)
          VALUES (?,?,?,?,?,?)""",
       (tracker_name, username.strip(), encrypted_pwd, existing_cookies,
        ua_val, enabled_val),
       write=True)
    return RedirectResponse("/settings?msg=tracker-saved", status_code=303)


@app.post("/distribution/add")
async def add_distribution(title_external_id: str = Form(...),
                           url: str = Form(...),
                           download_path: str = Form(""),
                           mode: str = Form("smart"),
                           check_interval_hours: int = Form(6)):
    existing = get_distribution(title_external_id)
    torrent_id = parse_torrent_id(url)

    if not torrent_id:
        return RedirectResponse("/?msg=dist-invalid-url", status_code=303)

    if mode not in ("smart", "fixed"):
        mode = "smart"

    if existing:
        db("""UPDATE distributions SET
                tracker_name='rutracker', torrent_id=?, url=?, download_path=?,
                mode=?, check_interval_hours=?, status='idle',
                last_checked_at=NULL, last_files_hash=NULL, last_files_json=NULL,
                new_files_count=0, error_message=NULL, error_count=0
              WHERE title_external_id=?""",
           (torrent_id, url.strip(), download_path.strip(),
            mode, max(1, min(168, check_interval_hours)), title_external_id),
           write=True)
        return RedirectResponse("/?msg=dist-updated", status_code=303)
    else:
        db("""INSERT INTO distributions
              (title_external_id, tracker_name, torrent_id, url, download_path,
               mode, check_interval_hours, status)
              VALUES (?, 'rutracker', ?, ?, ?, ?, ?, 'idle')""",
           (title_external_id, torrent_id, url.strip(), download_path.strip(),
            mode, max(1, min(168, check_interval_hours))),
           write=True)
        return RedirectResponse("/?msg=dist-added", status_code=303)


@app.post("/distribution/remove/{title_external_id}")
async def remove_distribution(title_external_id: str, sort: str = "date"):
    db("DELETE FROM distributions WHERE title_external_id=?",
       (title_external_id,), write=True)
    return RedirectResponse(f"/?sort={sort}&msg=dist-removed", status_code=303)


@app.get("/export.ics")
async def export_ics():
    rows = db("SELECT * FROM titles")
    cards = [dict(r) for r in rows]
    content = build_ics(cards)
    return Response(
        content=content,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=movie-radar.ics"}
    )


@app.post("/delete/{external_id}")
async def delete(external_id: str, sort: str = "date"):
    db("DELETE FROM titles WHERE external_id = ?", (external_id,), write=True)
    db("DELETE FROM seasons WHERE title_external_id = ?", (external_id,), write=True)
    db("DELETE FROM watched_episodes WHERE title_external_id = ?", (external_id,), write=True)
    db("DELETE FROM distributions WHERE title_external_id = ?", (external_id,), write=True)
    return RedirectResponse(f"/?sort={sort}", status_code=303)


@app.post("/toggle-notify/{external_id}")
async def toggle_notify(external_id: str, sort: str = "date"):
    db("""UPDATE titles
          SET notify_enabled = CASE WHEN notify_enabled = 1 THEN 0 ELSE 1 END
          WHERE external_id = ?""",
       (external_id,), write=True)
    return RedirectResponse(f"/?sort={sort}", status_code=303)


@app.post("/notify-all/{state}")
async def notify_all(state: str, sort: str = "date"):
    val = 1 if state == "on" else 0
    db("UPDATE titles SET notify_enabled = ?", (val,), write=True)
    return RedirectResponse(f"/?sort={sort}", status_code=303)


@app.get("/title/{external_id}", response_class=HTMLResponse)
async def title_page(request: Request, external_id: str):
    rows = db("SELECT * FROM titles WHERE external_id=?", (external_id,))
    if not rows:
        return RedirectResponse("/", status_code=303)
    card = dict(rows[0])
    card["date_human"] = human_date(card["release_date"])

    seasons = db("""SELECT * FROM seasons WHERE title_external_id=?
                    ORDER BY season_number""", (external_id,))
    season_list = []
    for s in seasons:
        sd = dict(s)
        sd["date_human"] = human_date(sd["release_date"])
        w, t = get_season_progress(external_id, sd["season_number"])
        sd["watched_count"] = w
        sd["total_count"] = t
        sd["percent"] = progress_percent(w, t)
        season_list.append(sd)

    card["poster_url"] = ensure_proxied(card.get("poster_url"))
    for sd in season_list:
        sd["poster_url"] = ensure_proxied(sd.get("poster_url"))

    show_watched, show_total = get_show_progress(external_id)

    return templates.TemplateResponse(request, "title.html", {
        "card": card,
        "seasons": season_list,
        "show_watched": show_watched,
        "show_total": show_total,
        "show_percent": progress_percent(show_watched, show_total),
    })


@app.post("/title/{external_id}/refresh-seasons")
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
                new_seasons = await save_seasons(external_id, seasons)
                await asyncio.sleep(0.5)

                for ns in new_seasons:
                    await notify_new_season(card["title"], ns["season_number"],
                                            ns["release_date"])

                new_episodes_all = []
                for season in seasons:
                    try:
                        episodes = await src.fetch_episodes(tmdb_id, season["season_number"])
                        new_eps = await save_episodes(external_id, season["season_number"], episodes)
                        new_episodes_all.extend(new_eps)
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        print(f"[seasons] Error fetching episodes: {e}")

                if new_episodes_all:
                    await notify_new_episodes(card["title"], new_episodes_all)

                print(f"[seasons] Refreshed {len(seasons)} seasons for {card['title']}")
            except Exception as e:
                print(f"[seasons] Error: {e}")
    return RedirectResponse(f"/title/{external_id}", status_code=303)


@app.get("/title/{external_id}/season/{season_number}", response_class=HTMLResponse)
async def season_page(request: Request, external_id: str, season_number: int):
    rows = db("SELECT * FROM titles WHERE external_id=?", (external_id,))
    if not rows:
        return RedirectResponse("/", status_code=303)
    card = dict(rows[0])

    srows = db("SELECT * FROM seasons WHERE title_external_id=? AND season_number=?",
               (external_id, season_number))
    if not srows:
        return RedirectResponse(f"/title/{external_id}", status_code=303)
    season = dict(srows[0])
    season["date_human"] = human_date(season["release_date"])

    watched_set = get_watched_set(external_id, season_number)

    erows = db("SELECT * FROM episodes WHERE season_id=? ORDER BY episode_number",
               (season["id"],))
    episodes = []
    for e in erows:
        ed = dict(e)
        ed["date_human"] = human_date(ed["release_date"])
        ed["watched"] = ed["episode_number"] in watched_set
        ed["poster_url"] = ensure_proxied(ed.get("poster_url"))
        episodes.append(ed)

    watched_count, total_count = get_season_progress(external_id, season_number)

    return templates.TemplateResponse(request, "season.html", {
        "card": card,
        "season": season,
        "episodes": episodes,
        "watched_count": watched_count,
        "total_count": total_count,
        "percent": progress_percent(watched_count, total_count),
        "all_watched": total_count > 0 and watched_count == total_count,
    })


@app.post("/watch/{external_id}/{season_number}/{episode_number}")
async def watch_episode(external_id: str, season_number: int, episode_number: int):
    toggle_watched(external_id, season_number, episode_number)
    return RedirectResponse(f"/title/{external_id}/season/{season_number}",
                            status_code=303)


@app.post("/watch-season/{external_id}/{season_number}")
async def watch_season(external_id: str, season_number: int):
    toggle_season_watched(external_id, season_number)
    return RedirectResponse(f"/title/{external_id}/season/{season_number}",
                            status_code=303)