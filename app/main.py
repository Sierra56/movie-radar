import os
import io
import json
import sqlite3
import asyncio
import uuid
import hashlib
import zipfile
import tempfile
import statistics
import traceback
import re as _re
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
        kw = {"timeout": 15, "follow_redirects": True}
        if proxy:
            kw["proxy"] = proxy
        async with httpx.AsyncClient(**kw) as client:
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
    return await download_image(url, f"{sanitize_id(info['external_id'])}.jpg") or url


def ensure_proxied(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("/posters/") or url.startswith("/img-proxy"):
        return url
    return f"/img-proxy?url={quote(url, safe='')}"


# ── Sources ───────────────────────────────────────────
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
        kw = {"timeout": 10}
        if get_proxy_url():
            kw["proxy"] = get_proxy_url()
        async with httpx.AsyncClient(**kw) as client:
            r = await client.get("https://www.omdbapi.com/", params={**params, "apikey": OMDB_KEY})
            r.raise_for_status()
            return r.json()

    def _parse_date(self, s):
        if not s or s == "N/A":
            return None
        try:
            return datetime.strptime(s, "%d %b %Y").date().isoformat()
        except ValueError:
            return None

    def _to_card(self, d):
        poster = d.get("Poster")
        return {"external_id": d["imdbID"], "title": d["Title"], "type": d.get("Type"),
                "release_date": self._parse_date(d.get("Released")),
                "poster_url": poster if poster != "N/A" else None,
                "genres": d.get("Genre", "") or ""}

    async def search(self, query):
        data = await self._get({"s": query})
        if data.get("Response") != "True":
            return []
        q = query.strip().lower()

        def score(it):
            year = it.get("Year", "")[:4]
            return (it["Title"].strip().lower() == q, it.get("Type") in ("movie", "series"),
                    year if year.isdigit() else "0000")

        best = max(data["Search"], key=score)
        detail = await self._get({"i": best["imdbID"]})
        return [self._to_card(detail)] if detail.get("Response") == "True" else []

    async def fetch(self, external_id):
        detail = await self._get({"i": external_id})
        return self._to_card(detail) if detail.get("Response") == "True" else None

    async def search_candidates(self, query):
        data = await self._get({"s": query})
        if data.get("Response") != "True":
            return []
        out = []
        for r in data["Search"]:
            poster = r.get("Poster")
            out.append({"external_id": r["imdbID"], "title": r["Title"], "year": r.get("Year", ""),
                        "type": r.get("Type"), "poster_url": poster if poster and poster != "N/A" else None,
                        "source": "omdb"})
        return out


class TmdbSource(Source):
    name = "tmdb"
    _POSTER = "https://image.tmdb.org/t/p/w342"
    _POSTER_SMALL = "https://image.tmdb.org/t/p/w154"

    async def _get(self, path, params=None):
        params = {"api_key": TMDB_KEY, "language": "ru-RU", **(params or {})}
        kw = {"timeout": 10}
        if get_proxy_url():
            kw["proxy"] = get_proxy_url()
        async with httpx.AsyncClient(**kw) as client:
            r = await client.get(f"https://api.themoviedb.org/3{path}", params=params)
            r.raise_for_status()
            return r.json()

    def _parse_date(self, s):
        if not s:
            return None
        try:
            return date.fromisoformat(s).isoformat()
        except ValueError:
            return None

    async def _details(self, media_type, tmdb_id):
        d = await self._get(f"/{media_type}/{tmdb_id}")
        if "title" in d:
            title = d.get("title") or d.get("original_title"); type_ = "movie"; release = d.get("release_date")
        else:
            title = d.get("name") or d.get("original_name"); type_ = "series"; release = d.get("first_air_date")
        return {"external_id": f"tmdb:{media_type}:{tmdb_id}", "title": title, "type": type_,
                "release_date": self._parse_date(release),
                "poster_url": f"{self._POSTER}{d['poster_path']}" if d.get("poster_path") else None,
                "genres": ", ".join(g["name"] for g in d.get("genres", []))}

    async def search(self, query):
        data = await self._get("/search/multi", {"query": query})
        valid = [r for r in data.get("results", []) if r.get("media_type") in ("movie", "tv")]
        if not valid:
            return []
        q = query.strip().lower()

        def score(r):
            name = (r.get("name") or r.get("title") or "").strip().lower()
            return (name == q, r.get("media_type") in ("movie", "tv"), r.get("popularity", 0) or 0)

        best = max(valid, key=score)
        return [await self._details("movie" if best["media_type"] == "movie" else "tv", best["id"])]

    async def fetch(self, external_id):
        tmdb_id = parse_tmdb_id(external_id)
        if tmdb_id is None:
            return None
        t = parse_tmdb_type(external_id)
        if t:
            try:
                return await self._details(t, tmdb_id)
            except httpx.HTTPStatusError:
                return None
        for t in ("movie", "tv"):
            try:
                return await self._details(t, tmdb_id)
            except httpx.HTTPStatusError:
                continue
        return None

    async def search_candidates(self, query):
        data = await self._get("/search/multi", {"query": query})
        out = []
        for r in data.get("results", []):
            mt = r.get("media_type")
            if mt not in ("movie", "tv"):
                continue
            rel = r.get("release_date") or r.get("first_air_date") or ""
            out.append({"external_id": f"tmdb:{mt}:{r['id']}", "title": r.get("title") or r.get("name") or "",
                        "year": rel[:4], "type": "movie" if mt == "movie" else "series",
                        "poster_url": f"{self._POSTER_SMALL}{r['poster_path']}" if r.get("poster_path") else None,
                        "source": "tmdb"})
        return out

    async def fetch_seasons(self, tmdb_id):
        d = await self._get(f"/tv/{tmdb_id}")
        return [{"season_number": s.get("season_number"), "name": s.get("name"),
                 "release_date": s.get("air_date"), "episodes": s.get("episode_count"),
                 "poster_path": s.get("poster_path")} for s in d.get("seasons", [])]

    async def fetch_episodes(self, tmdb_id, season_number):
        d = await self._get(f"/tv/{tmdb_id}/season/{season_number}")
        return [{"episode_number": e.get("episode_number"), "name": e.get("name"),
                 "release_date": e.get("air_date"), "runtime": e.get("runtime"),
                 "overview": e.get("overview", ""), "still_path": e.get("still_path")}
                for e in d.get("episodes", [])]


SOURCES = {"omdb": OmdbSource(), "tmdb": TmdbSource()}


# ── SQLite ────────────────────────────────────────────
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
    for col in ("genres", "source", "updated_at", "notify_enabled"):
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
            last_sent TEXT)""", write=True)
    db("INSERT OR IGNORE INTO telegram_settings (id) VALUES (1)", write=True)
    for col in ("notify_date_changes", "notify_new_cards", "notify_new_seasons",
                "notify_new_episodes", "notify_torrent_started", "notify_torrent_completed"):
        try:
            db(f"ALTER TABLE telegram_settings ADD COLUMN {col} INTEGER DEFAULT 1", write=True)
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
            auto_check_tick_minutes INTEGER DEFAULT 10, transmission_poll_minutes INTEGER DEFAULT 3)""", write=True)
    db("INSERT OR IGNORE INTO transmission_settings (id) VALUES (1)", write=True)
    for col in ("default_download_behavior TEXT DEFAULT 'use_distribution_path'",
                "auto_download_new_files INTEGER DEFAULT 0", "auto_check_enabled INTEGER DEFAULT 1",
                "auto_check_tick_minutes INTEGER DEFAULT 10", "transmission_poll_minutes INTEGER DEFAULT 3"):
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
    for col in ("new_files_count INTEGER DEFAULT 0", "last_new_files_at TEXT"):
        try:
            db(f"ALTER TABLE distributions ADD COLUMN {col}", write=True)
        except sqlite3.OperationalError:
            pass

    db("""CREATE TABLE IF NOT EXISTS download_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, distribution_id INTEGER, file_name TEXT,
            file_size INTEGER, transmission_hash TEXT, sent_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT,
            FOREIGN KEY (distribution_id) REFERENCES distributions(id) ON DELETE CASCADE)""", write=True)
    try:
        db("ALTER TABLE download_history ADD COLUMN completed_at TEXT", write=True)
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


# ── Settings helpers ──────────────────────────────────
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
                           notify_torrent_completed=False):
    db("""UPDATE telegram_settings SET bot_token=?, chat_id=?, enabled=?, send_time=?, notify_days=?,
            notify_date_changes=?, notify_new_cards=?, notify_new_seasons=?, notify_new_episodes=?,
            notify_torrent_started=?, notify_torrent_completed=? WHERE id=1""",
       (bot_token, chat_id, 1 if enabled else 0, send_time, notify_days,
        1 if notify_date_changes else 0, 1 if notify_new_cards else 0,
        1 if notify_new_seasons else 0, 1 if notify_new_episodes else 0,
        1 if notify_torrent_started else 0, 1 if notify_torrent_completed else 0), write=True)


def get_transmission_settings():
    rows = db("SELECT * FROM transmission_settings WHERE id=1")
    return dict(rows[0]) if rows else {}


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


def build_tracker_client(tracker_name="rutracker", cookies=None):
    creds = get_tracker_credentials(tracker_name) or {}
    return RuTrackerClient(
        username=creds.get("username", ""),
        password=decrypt_value(creds.get("encrypted_password", "")) if creds.get("encrypted_password") else "",
        proxy=get_proxy_url(),
        cookies=cookies if cookies is not None else load_tracker_cookies(tracker_name),
        user_agent=creds.get("user_agent", ""))


def build_transmission_client():
    trans = get_transmission_settings() or {}
    return TransmissionClient(host=trans.get("host", "localhost"), port=trans.get("port", 9091),
                              username=trans.get("username", ""),
                              password=decrypt_value(trans.get("encrypted_password", "")) if trans.get("encrypted_password") else "")


# ── Notification dedup ────────────────────────────────
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


templates.env.globals["get_theme"] = get_theme


def log_update(external_id, title, field, old_value, new_value):
    db("INSERT INTO updates_log (external_id, title, field, old_value, new_value) VALUES (?,?,?,?,?)",
       (external_id, title, field, old_value, new_value), write=True)


# ── Helpers ───────────────────────────────────────────
def human_date(iso):
    if not iso:
        return None
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return None
    return f"{d.day} {MONTHS_RU[d.month - 1]} {d.year}"


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


def parse_torrent_id(url):
    url = url.strip()
    if "viewtopic.php" in url:
        for pair in urlparse(url).query.split("&"):
            if pair.startswith("t="):
                return pair[2:]
    m = _re.search(r"t=(\d+)", url)
    return m.group(1) if m else None


def format_size(size_bytes):
    if not size_bytes:
        return "?"
    mb = size_bytes / (1024 * 1024)
    return f"{mb / 1024:.2f} ГБ" if mb > 1024 else f"{mb:.1f} МБ"


# ── Stage 5: episode parsing & learning ──────────────
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


def lookup_episode_air_date(title_external_id, season, episode):
    rows = db("""SELECT e.release_date FROM episodes e JOIN seasons s ON e.season_id=s.id
                 WHERE s.title_external_id=? AND s.season_number=? AND e.episode_number=?""",
              (title_external_id, season, episode))
    return rows[0]["release_date"] if rows else None


def get_pattern(distribution_id):
    rows = db("SELECT * FROM distribution_patterns WHERE distribution_id=?", (distribution_id,))
    return dict(rows[0]) if rows else None


def ensure_pattern(distribution_id):
    db("INSERT OR IGNORE INTO distribution_patterns (distribution_id) VALUES (?)", (distribution_id,), write=True)
    return get_pattern(distribution_id)


def recompute_pattern(distribution_id):
    pat = get_pattern(distribution_id)
    if not pat:
        return
    delays = [s["delay_hours"] for s in json.loads(pat["samples_json"] or "[]") if "delay_hours" in s]
    count = len(delays)
    median = round(statistics.median(delays)) if delays else None
    min_samples = pat.get("min_samples") or 3
    confidence = "low" if count < min_samples else ("medium" if count < min_samples + 3 else "high")
    db("UPDATE distribution_patterns SET median_delay_hours=?, samples_count=?, confidence=?, last_updated_at=datetime('now') WHERE distribution_id=?",
       (median, count, confidence, distribution_id), write=True)


def record_learning_samples(dist, new_files):
    now = datetime.now()
    pat = ensure_pattern(dist["id"])
    samples = json.loads(pat["samples_json"] or "[]")
    existing = {(s.get("season"), s.get("episode")) for s in samples}
    added = 0
    for name, size in new_files:
        ep = parse_episode(name)
        if not ep or ep in existing:
            continue
        air = lookup_episode_air_date(dist["title_external_id"], ep[0], ep[1])
        if not air:
            continue
        try:
            air_d = date.fromisoformat(air)
        except ValueError:
            continue
        delay = (now - datetime(air_d.year, air_d.month, air_d.day)).total_seconds() / 3600
        if delay < 0 or delay > 2000:
            continue
        samples.append({"season": ep[0], "episode": ep[1], "air_date": air,
                        "detected_at": now.isoformat(), "delay_hours": round(delay, 1)})
        existing.add(ep)
        added += 1
    if not added:
        return
    db("UPDATE distribution_patterns SET samples_json=? WHERE distribution_id=?",
       (json.dumps(samples[-20:], ensure_ascii=False), dist["id"]), write=True)
    recompute_pattern(dist["id"])
    print(f"[learn] {dist['title_external_id']}: +{added} samples")


# ── Seasons/episodes helpers ──────────────────────────
async def save_seasons(title_external_id, seasons):
    existing = db("SELECT season_number, id FROM seasons WHERE title_external_id=?", (title_external_id,))
    emap = {r["season_number"]: r["id"] for r in existing}
    new_seasons = []
    safe_id = sanitize_id(title_external_id)
    for s in seasons:
        poster_local = None
        if s.get("poster_path"):
            poster_local = await download_image(f"https://image.tmdb.org/t/p/w342{s['poster_path']}",
                                                f"{safe_id}_s{s['season_number']}.jpg")
        if s["season_number"] in emap:
            if poster_local:
                db("UPDATE seasons SET name=?, release_date=?, episodes=?, poster_url=? WHERE id=?",
                   (s["name"], s["release_date"], s["episodes"], poster_local, emap[s["season_number"]]), write=True)
            else:
                db("UPDATE seasons SET name=?, release_date=?, episodes=? WHERE id=?",
                   (s["name"], s["release_date"], s["episodes"], emap[s["season_number"]]), write=True)
        else:
            new_seasons.append(s)
            db("INSERT INTO seasons (title_external_id, season_number, name, release_date, episodes, poster_url) VALUES (?,?,?,?,?,?)",
               (title_external_id, s["season_number"], s["name"], s["release_date"], s["episodes"], poster_local), write=True)
    return new_seasons


async def save_episodes(title_external_id, season_number, episodes):
    rows = db("SELECT id FROM seasons WHERE title_external_id=? AND season_number=?", (title_external_id, season_number))
    if not rows:
        return []
    season_id = rows[0]["id"]
    existing = db("SELECT episode_number FROM episodes WHERE season_id=?", (season_id,))
    enums = {r["episode_number"] for r in existing}
    new_episodes = []
    safe_id = sanitize_id(title_external_id)
    for e in episodes:
        if e["episode_number"] not in enums:
            new_episodes.append({"season_number": season_number, "episode_number": e["episode_number"],
                                 "name": e["name"], "release_date": e["release_date"]})
        poster_local = None
        if e.get("still_path"):
            poster_local = await download_image(f"https://image.tmdb.org/t/p/w300{e['still_path']}",
                                                f"{safe_id}_s{season_number}e{e['episode_number']}.jpg")
        db("INSERT OR REPLACE INTO episodes (season_id, episode_number, name, release_date, runtime, overview, poster_url) VALUES (?,?,?,?,?,?,?)",
           (season_id, e["episode_number"], e["name"], e["release_date"], e["runtime"], e.get("overview", ""), poster_local), write=True)
    return new_episodes


def get_season_count(external_id):
    return db("SELECT COUNT(*) as cnt FROM seasons WHERE title_external_id=?", (external_id,))[0]["cnt"]


def get_next_season(external_id):
    rows = db("SELECT * FROM seasons WHERE title_external_id=? AND release_date >= date('now') ORDER BY release_date LIMIT 1", (external_id,))
    return dict(rows[0]) if rows else None


def update_next_episode_air_date(external_id):
    dist = get_distribution(external_id)
    if not dist:
        return
    rows = db("""SELECT e.release_date FROM episodes e JOIN seasons s ON e.season_id=s.id
                 WHERE s.title_external_id=? AND e.release_date >= date('now') ORDER BY e.release_date LIMIT 1""", (external_id,))
    new_date = rows[0]["release_date"] if rows else None
    if new_date != dist.get("next_episode_air_date"):
        db("UPDATE distributions SET next_episode_air_date=? WHERE id=?", (new_date, dist["id"]), write=True)


# ── Watched helpers ───────────────────────────────────
def is_watched(t, s, e):
    return bool(db("SELECT 1 FROM watched_episodes WHERE title_external_id=? AND season_number=? AND episode_number=?", (t, s, e)))


def get_watched_set(t, s):
    return {r["episode_number"] for r in db("SELECT episode_number FROM watched_episodes WHERE title_external_id=? AND season_number=?", (t, s))}


def toggle_watched(t, s, e):
    if is_watched(t, s, e):
        db("DELETE FROM watched_episodes WHERE title_external_id=? AND season_number=? AND episode_number=?", (t, s, e), write=True)
    else:
        db("INSERT INTO watched_episodes (title_external_id, season_number, episode_number) VALUES (?,?,?)", (t, s, e), write=True)


def toggle_season_watched(t, s):
    eps = [r["episode_number"] for r in db("""SELECT e.episode_number FROM episodes e JOIN seasons s ON e.season_id=s.id
             WHERE s.title_external_id=? AND s.season_number=?""", (t, s))]
    if not eps:
        return
    ws = get_watched_set(t, s)
    if all(n in ws for n in eps):
        db("DELETE FROM watched_episodes WHERE title_external_id=? AND season_number=?", (t, s), write=True)
    else:
        for n in eps:
            if n not in ws:
                db("INSERT OR IGNORE INTO watched_episodes (title_external_id, season_number, episode_number) VALUES (?,?,?)", (t, s, n), write=True)


def get_season_progress(t, s):
    rows = db("""SELECT COUNT(e.id) as total, SUM(CASE WHEN w.title_external_id IS NOT NULL THEN 1 ELSE 0 END) as watched
                 FROM episodes e JOIN seasons s ON e.season_id=s.id
                 LEFT JOIN watched_episodes w ON w.title_external_id=s.title_external_id AND w.season_number=s.season_number AND w.episode_number=e.episode_number
                 WHERE s.title_external_id=? AND s.season_number=?""", (t, s))
    return (rows[0]["watched"] or 0, rows[0]["total"] or 0) if rows else (0, 0)


def get_show_progress(t):
    rows = db("""SELECT COUNT(e.id) as total, SUM(CASE WHEN w.title_external_id IS NOT NULL THEN 1 ELSE 0 END) as watched
                 FROM episodes e JOIN seasons s ON e.season_id=s.id
                 LEFT JOIN watched_episodes w ON w.title_external_id=s.title_external_id AND w.season_number=s.season_number AND w.episode_number=e.episode_number
                 WHERE s.title_external_id=?""", (t,))
    return (rows[0]["watched"] or 0, rows[0]["total"] or 0) if rows else (0, 0)


# ── Telegram ──────────────────────────────────────────
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
    try:
        h, m = map(int, st.split(":"))
    except (ValueError, AttributeError):
        h, m = 9, 0
    scheduler.add_job(check_and_notify, CronTrigger(hour=h, minute=m), id="telegram_notify", replace_existing=True)


# ── Background refresh ────────────────────────────────
async def refresh_catalog():
    global refresh_progress
    rows = db("SELECT * FROM titles")
    today = date.today()
    updated = skipped = 0
    date_changes = []
    refresh_progress = {"running": True, "done": 0, "total": len(rows)}
    for i, r in enumerate(rows):
        row = dict(r)
        if row["source"] == "local":
            refresh_progress["done"] = i + 1
            continue
        if row["release_date"]:
            try:
                if date.fromisoformat(row["release_date"]) < today - timedelta(days=30):
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
        nt, nrd, ng = fresh["title"] or row["title"], fresh["release_date"] or row["release_date"], fresh["genres"] or row["genres"]
        if nt != row["title"]:
            log_update(row["external_id"], nt, "title", row["title"], nt); changed = True
        if nrd != row["release_date"]:
            log_update(row["external_id"], nt, "release_date", row["release_date"], nrd)
            if row.get("notify_enabled") in (None, 1):
                date_changes.append({"title": nt, "old_date": row["release_date"], "new_date": nrd})
            changed = True
        if ng != row["genres"]:
            log_update(row["external_id"], nt, "genres", row["genres"], ng); changed = True
        poster_local = row["poster_url"]
        if fresh.get("poster_url"):
            dl = await download_image(fresh["poster_url"], f"{sanitize_id(row['external_id'])}.jpg")
            if dl:
                poster_local = dl
        if changed or poster_local != row["poster_url"]:
            db("UPDATE titles SET title=?, release_date=?, poster_url=?, genres=?, updated_at=datetime('now') WHERE external_id=?",
               (nt, nrd, poster_local, ng, row["external_id"]), write=True)
            if changed:
                updated += 1
        if row["type"] == "series" and src.name == "tmdb":
            tmdb_id = parse_tmdb_id(row["external_id"])
            if tmdb_id is not None:
                try:
                    seasons = await src.fetch_seasons(tmdb_id)
                    ns = await save_seasons(row["external_id"], seasons)
                    await asyncio.sleep(0.5)
                    for n in ns:
                        await notify_new_season(nt, n["season_number"], n["release_date"])
                    all_new = []
                    for season in seasons:
                        try:
                            eps = await src.fetch_episodes(tmdb_id, season["season_number"])
                            all_new.extend(await save_episodes(row["external_id"], season["season_number"], eps))
                            await asyncio.sleep(0.5)
                        except Exception as e:
                            print(f"[refresh] Error episodes S{season['season_number']}: {e}")
                    if all_new:
                        await notify_new_episodes(nt, all_new)
                    update_next_episode_air_date(row["external_id"])
                except Exception as e:
                    print(f"[refresh] Error seasons {row['external_id']}: {e}")
        refresh_progress["done"] = i + 1
    refresh_progress["running"] = False
    print(f"[refresh] Done. Updated: {updated}, Skipped: {skipped}")
    if date_changes:
        await notify_date_changes(date_changes)


async def refresh_single(external_id):
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
        print(f"[refresh-single] Error: {e}")
        return False
    if not fresh:
        return False
    changed = False
    nt, nrd, ng = fresh["title"] or row["title"], fresh["release_date"] or row["release_date"], fresh["genres"] or row["genres"]
    date_change = None
    if nt != row["title"]:
        log_update(external_id, nt, "title", row["title"], nt); changed = True
    if nrd != row["release_date"]:
        log_update(external_id, nt, "release_date", row["release_date"], nrd)
        if row.get("notify_enabled") in (None, 1):
            date_change = {"title": nt, "old_date": row["release_date"], "new_date": nrd}
        changed = True
    if ng != row["genres"]:
        log_update(external_id, nt, "genres", row["genres"], ng); changed = True
    poster_local = row["poster_url"]
    if fresh.get("poster_url"):
        dl = await download_image(fresh["poster_url"], f"{sanitize_id(external_id)}.jpg")
        if dl:
            poster_local = dl
    if changed or poster_local != row["poster_url"]:
        db("UPDATE titles SET title=?, release_date=?, poster_url=?, genres=?, updated_at=datetime('now') WHERE external_id=?",
           (nt, nrd, poster_local, ng, external_id), write=True)
    if row["type"] == "series" and src.name == "tmdb":
        tmdb_id = parse_tmdb_id(external_id)
        if tmdb_id is not None:
            try:
                seasons = await src.fetch_seasons(tmdb_id)
                ns = await save_seasons(external_id, seasons)
                for n in ns:
                    await notify_new_season(nt, n["season_number"], n["release_date"])
                all_new = []
                for season in seasons:
                    try:
                        eps = await src.fetch_episodes(tmdb_id, season["season_number"])
                        all_new.extend(await save_episodes(external_id, season["season_number"], eps))
                        await asyncio.sleep(0.3)
                    except Exception:
                        pass
                if all_new:
                    await notify_new_episodes(nt, all_new)
                update_next_episode_air_date(external_id)
            except Exception as e:
                print(f"[refresh-single] Error seasons: {e}")
    if date_change:
        await notify_date_changes([date_change])
    return changed


scheduler = AsyncIOScheduler()
scheduler.add_job(refresh_catalog, "interval", hours=get_refresh_hours(), id="refresh", next_run_time=None)


# ── Stage 4: auto-check ──────────────────────────────
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
        try:
            air = date.fromisoformat(dist["next_episode_air_date"])
            predicted = datetime(air.year, air.month, air.day) + timedelta(hours=pat["median_delay_hours"])
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
    for dist in rows:
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


# ── Stage 6: Transmission completion polling ──────────
async def check_transmission_job():
    trans = get_transmission_settings() or {}
    if not trans.get("enabled"):
        return
    rows = db("""SELECT h.id, h.transmission_hash, h.file_name, t.title as card_title
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
            await notify_torrent_completed(title, r["file_name"], st.get("size") or 0)
            print(f"[transmission-poll] completed: {r['file_name']}")


def schedule_transmission_poll_job():
    trans = get_transmission_settings() or {}
    minutes = max(1, min(60, int(trans.get("transmission_poll_minutes") or 3)))
    scheduler.add_job(check_transmission_job, "interval", minutes=minutes,
                      id="transmission_poll", replace_existing=True, max_instances=1, coalesce=True)


@app.on_event("startup")
async def on_startup():
    scheduler.start()
    scheduler.reschedule_job("refresh", trigger="interval", hours=get_refresh_hours())
    scheduler.modify_job("refresh", next_run_time=datetime.now() + timedelta(minutes=5))
    schedule_telegram_job()
    schedule_distribution_job()
    schedule_transmission_poll_job()


@app.on_event("shutdown")
async def on_shutdown():
    scheduler.shutdown()


# ── iCalendar ─────────────────────────────────────────
def escape_ics(s):
    if not s:
        return ""
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_ics(cards):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Movie Radar//RU", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:Скоро на экранах", "X-WR-TIMEZONE:Europe/Moscow",
             "X-APPLE-CALENDAR-COLOR:#4F8CFF"]
    for c in cards:
        if not c.get("release_date"):
            continue
        uid = c["external_id"].replace(":", "_")
        desc = []
        if c.get("type"):
            desc.append(f"Тип: {c['type']}")
        if c.get("genres"):
            desc.append(f"Жанр: {c['genres']}")
        if c.get("source"):
            desc.append(f"Источник: {c['source']}")
        dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines += ["BEGIN:VEVENT", f"UID:{uid}@movieradar", f"DTSTAMP:{dtstamp}",
                  f"DTSTART;VALUE=DATE:{c['release_date'].replace('-', '')}",
                  f"DTEND;VALUE=DATE:{c['release_date'].replace('-', '')}",
                  f"SUMMARY:{escape_ics('Премьера: ' + c['title'])}",
                  f"DESCRIPTION:{escape_ics(chr(10).join(desc))}",
                  "STATUS:CONFIRMED", "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


# ── Backup / Restore ──────────────────────────────────
def _build_backup_zip(a, b, c, d):
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)
    try:
        src = sqlite3.connect(DB_PATH); dst = sqlite3.connect(tmp_path)
        src.backup(dst); src.close()
        if not a:
            for t in SETTINGS_TABLES:
                dst.execute(f"DROP TABLE IF EXISTS {t}")
        if not b:
            for t in CARD_TABLES:
                dst.execute(f"DROP TABLE IF EXISTS {t}")
        if not d:
            for t in TORRENT_TABLES:
                dst.execute(f"DROP TABLE IF EXISTS {t}")
        dst.commit(); dst.close()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("meta.json", json.dumps({
                "app": "movie-radar", "backup_version": BACKUP_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "include_settings": a, "include_cards": b, "include_images": c, "include_torrents": d},
                ensure_ascii=False, indent=2))
            zf.write(tmp_path, "backup.db")
            if c and os.path.isdir(POSTERS_DIR):
                for root, _, files in os.walk(POSTERS_DIR):
                    for fn in files:
                        full = os.path.join(root, fn)
                        zf.write(full, os.path.join("posters", os.path.relpath(full, POSTERS_DIR)))
        buf.seek(0)
        return buf
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.post("/backup/create")
async def create_backup(include_settings: str = Form("off"), include_cards: str = Form("off"),
                        include_images: str = Form("off"), include_torrents: str = Form("off")):
    inc = [include_settings == "on", include_cards == "on", include_images == "on", include_torrents == "on"]
    if not any(inc):
        return RedirectResponse("/settings?msg=backup-empty", status_code=303)
    buf = _build_backup_zip(*inc)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename=movie-radar-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"})


@app.post("/backup/restore")
async def restore_backup(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".zip"):
        return RedirectResponse("/settings?msg=restore-invalid", status_code=303)
    try:
        zf = zipfile.ZipFile(io.BytesIO(await file.read()))
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
            with open(os.path.join(os.path.dirname(DB_PATH), "auto-backup-latest.zip"), "wb") as f:
                f.write(_build_backup_zip(True, True, True, True).read())
        except Exception as e:
            print(f"[backup] Auto-backup failed: {e}")
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(tmp_fd)
        with zf.open("backup.db") as s, open(tmp_path, "wb") as d:
            d.write(s.read())
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("ATTACH DATABASE ? AS backup", (tmp_path,))
            tables = []
            if meta.get("include_settings"):
                tables += SETTINGS_TABLES
            if meta.get("include_cards"):
                tables += CARD_TABLES
            if meta.get("include_torrents"):
                tables += TORRENT_TABLES
            has_seq = conn.execute("SELECT name FROM backup.sqlite_master WHERE type='table' AND name='sqlite_sequence'").fetchone()
            for t in tables:
                if not conn.execute("SELECT name FROM backup.sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
                    continue
                conn.execute(f"DELETE FROM main.{t}")
                conn.execute(f"INSERT INTO main.{t} SELECT * FROM backup.{t}")
                if has_seq and conn.execute("SELECT 1 FROM backup.sqlite_sequence WHERE name=?", (t,)).fetchone():
                    mx = conn.execute(f"SELECT MAX(id) FROM main.{t}").fetchone()[0] or 0
                    conn.execute("DELETE FROM main.sqlite_sequence WHERE name=?", (t,))
                    conn.execute("INSERT INTO main.sqlite_sequence (name, seq) VALUES (?,?)", (t, mx))
            conn.commit()
            conn.execute("DETACH DATABASE backup")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.commit()
        finally:
            conn.close()
        if meta.get("include_images"):
            os.makedirs(POSTERS_DIR, exist_ok=True)
            for name in names:
                if name.startswith("posters/") and not name.endswith("/"):
                    target = os.path.join(POSTERS_DIR, os.path.relpath(name, "posters"))
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(name) as s, open(target, "wb") as d:
                        d.write(s.read())
        try:
            scheduler.reschedule_job("refresh", trigger="interval", hours=get_refresh_hours())
            schedule_telegram_job(); schedule_distribution_job(); schedule_transmission_poll_job()
        except Exception as e:
            print(f"[backup] re-apply error: {e}")
    except Exception as e:
        traceback.print_exc()
        return RedirectResponse("/settings?msg=restore-error", status_code=303)
    finally:
        scheduler.resume()
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
    return RedirectResponse("/settings?msg=restore-ok", status_code=303)


# ── Image proxy ───────────────────────────────────────
_ALLOWED_IMG_HOSTS = {"image.tmdb.org", "m.media-amazon.com", "ia.media-imdb.com", "upload.wikimedia.org"}


@app.get("/img-proxy")
async def img_proxy(url: str):
    if urlparse(url).hostname not in _ALLOWED_IMG_HOSTS:
        return Response(status_code=403)
    h = hashlib.md5(url.encode()).hexdigest()
    cp, mp = os.path.join(POSTERS_DIR, f"cache_{h}.img"), os.path.join(POSTERS_DIR, f"cache_{h}.mime")
    if os.path.exists(cp):
        mt = "image/jpeg"
        if os.path.exists(mp):
            mt = open(mp).read().strip() or "image/jpeg"
        return Response(content=open(cp, "rb").read(), media_type=mt)
    try:
        kw = {"timeout": 15, "follow_redirects": True}
        if get_proxy_url():
            kw["proxy"] = get_proxy_url()
        async with httpx.AsyncClient(**kw) as client:
            r = await client.get(url)
            r.raise_for_status()
            content = r.content
            mt = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            open(cp, "wb").write(content)
            open(mp, "w").write(mt)
            return Response(content=content, media_type=mt)
    except Exception as e:
        print(f"[img-proxy] Error: {e}")
        return Response(status_code=502)


# ── Stage 2-3 ─────────────────────────────────────────
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
        except RuTrackerCaptchaError:
            db("UPDATE tracker_credentials SET last_error='captcha' WHERE tracker_name=?", (dist["tracker_name"],), write=True)
            return False, "Трекер требует капчу"
        except (RuTrackerAuthError, RuTrackerForbiddenError) as e:
            db("UPDATE tracker_credentials SET last_error=? WHERE tracker_name=?", (str(e), dist["tracker_name"]), write=True)
            return False, str(e)
        except RuTrackerError as e:
            db("UPDATE tracker_credentials SET last_error=? WHERE tracker_name=?", (str(e), dist["tracker_name"]), write=True)
            return False, str(e)
        db("UPDATE tracker_credentials SET encrypted_cookies=?, last_login_at=datetime('now'), last_error=NULL, error_count=0 WHERE tracker_name=?",
           (encrypt_value(json.dumps(cookies)), dist["tracker_name"]), write=True)
    try:
        files = await client.fetch_files(dist["torrent_id"], cookies)
    except RuTrackerForbiddenError as e:
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
    except RuTrackerError as e:
        db("UPDATE distributions SET status='error', error_message=? WHERE id=?", (str(e), dist["id"]), write=True)
        return False, str(e)
    if not files:
        db("UPDATE distributions SET status='error', error_message='Не удалось распарсить список файлов (debug-дамп сохранён)' WHERE id=?", (dist["id"],), write=True)
        return False, "Не удалось распарсить список файлов. Debug-дамп: /data/debug_last_topic.html"
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
    status = "has_new" if new_count else "idle"
    db("UPDATE distributions SET last_checked_at=datetime('now'), last_files_hash=?, last_files_json=?, status=?, new_files_count=?, error_count=0, error_message=NULL WHERE id=?",
       (new_hash, json.dumps(snapshot, ensure_ascii=False), status, new_count, dist["id"]), write=True)
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
                db("UPDATE distributions SET status='idle' WHERE id=?", (dist["id"],), write=True)
                db("INSERT INTO download_history (distribution_id, file_name, file_size, transmission_hash, sent_at) VALUES (?,?,?,?,datetime('now'))",
                   (dist["id"], result["name"], result["size"], result["hash"]), write=True)
                cr = db("SELECT title FROM titles WHERE external_id=?", (title_external_id,))
                await notify_torrent_started(cr[0]["title"] if cr else title_external_id, result["name"], dd)
            except Exception as e:
                print(f"[dist-check] Auto-download failed: {e}")
    if not old_hash:
        return True, f"Первая проверка: найдено {len(files)} файлов, снапшот сохранён"
    if new_count:
        return True, f"Обнаружено новых файлов: {new_count}"
    return True, "Изменений нет"


def _resolve_download_dir(dist, trans):
    if trans.get("default_download_behavior", "use_distribution_path") == "use_distribution_path" and dist.get("download_path"):
        return dist["download_path"]
    return trans.get("base_download_dir") or None


@app.post("/distribution/check/{title_external_id}")
async def check_distribution(title_external_id: str, sort: str = "date"):
    ok, message = await check_distribution_now(title_external_id)
    set_setting("last_dist_check", message)
    return RedirectResponse(f"/?sort={sort}&msg={'dist-checked' if ok else 'dist-check-fail'}", status_code=303)


@app.post("/distribution/pattern/save/{title_external_id}")
async def save_pattern(title_external_id: str, min_samples: int = Form(3), sort: str = "date"):
    dist = get_distribution(title_external_id)
    if dist:
        ensure_pattern(dist["id"])
        db("UPDATE distribution_patterns SET min_samples=? WHERE distribution_id=?", (max(1, min(10, min_samples)), dist["id"]), write=True)
        recompute_pattern(dist["id"])
    return RedirectResponse(f"/?sort={sort}&msg=pattern-saved", status_code=303)


@app.post("/distribution/pattern/reset/{title_external_id}")
async def reset_pattern(title_external_id: str, sort: str = "date"):
    dist = get_distribution(title_external_id)
    if dist:
        db("UPDATE distribution_patterns SET samples_json='[]', median_delay_hours=NULL, samples_count=0, confidence='low' WHERE distribution_id=?", (dist["id"],), write=True)
    return RedirectResponse(f"/?sort={sort}&msg=pattern-reset", status_code=303)


@app.post("/settings/tracker/test")
async def test_tracker_login():
    creds = get_tracker_credentials("rutracker")
    if not creds:
        return RedirectResponse("/settings?msg=tracker-test-fail", status_code=303)
    cookies = load_tracker_cookies("rutracker")
    username = creds.get("username", "")
    password = decrypt_value(creds.get("encrypted_password", "")) if creds.get("encrypted_password") else ""
    if not cookies and not (username and password):
        return RedirectResponse("/settings?msg=tracker-test-fail", status_code=303)
    client = build_tracker_client("rutracker", cookies=cookies)
    if cookies:
        ok, reason = await client.validate_cookies(cookies)
        if ok:
            db("UPDATE tracker_credentials SET last_login_at=datetime('now'), last_error=NULL, error_count=0 WHERE tracker_name='rutracker'", write=True)
            return RedirectResponse("/settings?msg=tracker-test-ok", status_code=303)
        if not (username and password):
            db("UPDATE tracker_credentials SET last_error=? WHERE tracker_name='rutracker'", (reason,), write=True)
            return RedirectResponse("/settings?msg=tracker-cookies-invalid", status_code=303)
    try:
        nc = await client.login()
        db("UPDATE tracker_credentials SET encrypted_cookies=?, last_login_at=datetime('now'), last_error=NULL, error_count=0 WHERE tracker_name='rutracker'",
           (encrypt_value(json.dumps(nc)),), write=True)
        return RedirectResponse("/settings?msg=tracker-test-ok", status_code=303)
    except RuTrackerCaptchaError:
        db("UPDATE tracker_credentials SET last_error='captcha' WHERE tracker_name='rutracker'", write=True)
        return RedirectResponse("/settings?msg=tracker-captcha", status_code=303)
    except RuTrackerForbiddenError as e:
        db("UPDATE tracker_credentials SET last_error=? WHERE tracker_name='rutracker'", (str(e),), write=True)
        return RedirectResponse("/settings?msg=tracker-forbidden", status_code=303)
    except RuTrackerError as e:
        db("UPDATE tracker_credentials SET last_error=?, error_count=error_count+1 WHERE tracker_name='rutracker'", (str(e),), write=True)
        return RedirectResponse("/settings?msg=tracker-test-fail", status_code=303)
    except Exception as e:
        traceback.print_exc()
        db("UPDATE tracker_credentials SET last_error=? WHERE tracker_name='rutracker'", (f"{type(e).__name__}: {e}",), write=True)
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
    try:
        td = await build_tracker_client(dist["tracker_name"], cookies=cookies).download_torrent(dist["torrent_id"], cookies)
        dd = _resolve_download_dir(dist, trans)
        paused = trans.get("action_on_new") == "pause"
        result = build_transmission_client().add_torrent(td, dd, paused)
        db("UPDATE distributions SET status='idle', error_count=0, error_message=NULL WHERE id=?", (dist["id"],), write=True)
        db("INSERT INTO download_history (distribution_id, file_name, file_size, transmission_hash, sent_at) VALUES (?,?,?,?,datetime('now'))",
           (dist["id"], result["name"], result["size"], result["hash"]), write=True)
        cr = db("SELECT title FROM titles WHERE external_id=?", (title_external_id,))
        await notify_torrent_started(cr[0]["title"] if cr else title_external_id, result["name"], dd)
        set_setting("last_dist_download", f"✅ Отправлено: {result['name']} ({format_size(result['size'])})")
        return RedirectResponse(f"/?sort={sort}&msg=dist-downloaded", status_code=303)
    except Exception as e:
        traceback.print_exc()
        db("UPDATE distributions SET status='error', error_message=?, error_count=error_count+1 WHERE id=?", (str(e), dist["id"]), write=True)
        set_setting("last_dist_download", f"❌ Ошибка: {e}")
        return RedirectResponse(f"/?sort={sort}&msg=dist-download-fail", status_code=303)


@app.post("/settings/transmission/test")
async def test_transmission():
    trans = get_transmission_settings()
    if not trans or not trans.get("host"):
        return RedirectResponse("/settings?msg=transmission-test-fail", status_code=303)
    try:
        ok, message = build_transmission_client().test_connection()
        set_setting("last_trans_test", message)
        return RedirectResponse("/settings?msg=" + ("transmission-test-ok" if ok else "transmission-test-fail"), status_code=303)
    except Exception as e:
        set_setting("last_trans_test", str(e))
        return RedirectResponse("/settings?msg=transmission-test-fail", status_code=303)


# ── Stage 6: downloads journal ────────────────────────
@app.get("/downloads", response_class=HTMLResponse)
async def downloads_page(request: Request):
    rows = db("""SELECT h.*, t.title as card_title, d.status as dist_status
                 FROM download_history h
                 LEFT JOIN distributions d ON h.distribution_id = d.id
                 LEFT JOIN titles t ON d.title_external_id = t.external_id
                 ORDER BY h.sent_at DESC LIMIT 200""")
    return templates.TemplateResponse(request, "downloads.html", {"rows": [dict(r) for r in rows]})


@app.post("/downloads/remove/{history_id}")
async def downloads_remove(history_id: int):
    rows = db("SELECT * FROM download_history WHERE id=?", (history_id,))
    if rows:
        try:
            build_transmission_client().remove_torrent(rows[0]["transmission_hash"], delete_data=False)
        except Exception as e:
            print(f"[downloads] remove from transmission failed: {e}")
        db("DELETE FROM download_history WHERE id=?", (history_id,), write=True)
    return RedirectResponse("/downloads?msg=download-removed", status_code=303)


# ── Routes ────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, sort: str = "date", err: str | None = None, msg: str | None = None):
    order = {"date": "release_date IS NULL, release_date", "title": "title COLLATE NOCASE",
             "genre": "genres IS NULL OR genres = '', genres COLLATE NOCASE, title COLLATE NOCASE"}.get(sort, "release_date IS NULL, release_date")
    rows = db(f"SELECT * FROM titles ORDER BY {order}")
    today = date.today()
    cards, patterns = [], {}
    for r in rows:
        c = dict(r)
        c["date_human"] = human_date(c["release_date"])
        c["notify_enabled"] = c.get("notify_enabled") in (None, 1)
        c["badge"], c["released"] = None, False
        if c["release_date"]:
            try:
                delta = (date.fromisoformat(c["release_date"]) - today).days
                c["badge"], c["released"] = ("уже вышло", True) if delta < 0 else ("сегодня!", False) if delta == 0 else (f"{delta} {plural(delta, ('день', 'дня', 'дней'))}", False)
            except ValueError:
                pass
        if c["type"] == "series":
            c["season_count"] = get_season_count(c["external_id"])
            ns = get_next_season(c["external_id"])
            c["next_season_date_human"] = human_date(ns["release_date"]) if ns else None
            c["display_poster"] = (ns.get("poster_url") if ns else None) or c["poster_url"]
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
            c.update(distribution_url=dist["url"], distribution_download_path=dist["download_path"],
                     distribution_mode=dist["mode"], distribution_check_interval_hours=dist["check_interval_hours"])
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
                        "dist-downloaded": get_setting("last_dist_download") or "Торрент отправлен в Transmission.",
                        "pattern-saved": "Настройки обучения сохранены.", "pattern-reset": "Обучение сброшено."}
    error_messages = {"dist-exists": "Раздача уже добавлена.", "dist-invalid-url": "Неверная ссылка на раздачу.",
                      "dist-check-fail": get_setting("last_dist_check") or "Ошибка проверки раздачи.",
                      "dist-download-fail": get_setting("last_dist_download") or "Ошибка скачивания."}
    return templates.TemplateResponse(request, "index.html", {
        "cards": cards, "sort": sort, "patterns_json": json.dumps(patterns, ensure_ascii=False),
        "error": "Ничего не нашлось — уточните название." if err else None,
        "message": success_messages.get(msg), "error_message": error_messages.get(msg)})


@app.get("/new", response_class=HTMLResponse)
async def new_card_page(request: Request, msg: str | None = None):
    return templates.TemplateResponse(request, "add.html", {
        "sources": list(SOURCES.keys()), "default_source": request.cookies.get("source", "tmdb"),
        "message": {"added-local": "Карточка добавлена локально.", "added": "Карточка добавлена."}.get(msg),
        "error_message": {"search-fail": "Не удалось получить данные по выбранной карточке."}.get(msg)})


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
async def add_local(title: str = Form(...), release_date: str | None = Form(None)):
    local_id = f"local:{uuid.uuid4().hex[:12]}"
    db("INSERT INTO titles (external_id, title, type, release_date, poster_url, genres, source, updated_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
       (local_id, title.strip(), None, release_date or None, None, "", "local"), write=True)
    await notify_new_card(title.strip(), release_date, "local", None)
    return RedirectResponse("/new?msg=added-local", status_code=303)


@app.post("/add-select")
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
    db("INSERT OR REPLACE INTO titles (external_id, title, type, release_date, poster_url, genres, source, updated_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
       (info["external_id"], info["title"], info["type"], rd, await download_card_poster(info), info["genres"], src.name), write=True)
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
    tracker = get_tracker_credentials("rutracker")
    return templates.TemplateResponse(request, "settings.html", {
        "tg": get_telegram_settings(), "refresh_hours": get_refresh_hours(),
        "refresh_label": refresh_period_label(get_refresh_hours()),
        "log_rows": [dict(r) for r in db("SELECT * FROM updates_log ORDER BY created_at DESC LIMIT 200")],
        "proxy_url": get_setting("proxy_url", "") or "", "theme": get_setting("theme", "dark"),
        "trans": get_transmission_settings(), "tracker": tracker,
        "tracker_has_cookies": bool(tracker and tracker.get("encrypted_cookies") and decrypt_value(tracker["encrypted_cookies"])),
        "message": {"refresh-saved": "Период обновления сохранён.", "telegram-saved": "Настройки Telegram сохранены.",
                    "proxy-saved": "Настройки прокси сохранены.", "proxy-ok": "Прокси работает!",
                    "theme-saved": "Тема сохранена.", "test-ok": "Тестовое сообщение отправлено!",
                    "restore-ok": "Восстановление завершено.", "transmission-saved": "Настройки Transmission сохранены.",
                    "transmission-test-ok": get_setting("last_trans_test") or "Transmission подключён.",
                    "tracker-saved": "Настройки трекера сохранены.", "tracker-cookies-saved": "Cookies сохранены и зашифрованы.",
                    "tracker-test-ok": "Подключение к трекеру успешно!"}.get(msg),
        "error_message": {"proxy-fail": "Не удалось подключиться через прокси.", "proxy-not-set": "Прокси не настроен.",
                          "test-fail": "Не удалось отправить. Проверьте токен и chat_id.",
                          "backup-empty": "Выберите хотя бы один компонент.", "restore-invalid": "Неверный файл бэкапа.",
                          "restore-error": "Ошибка при восстановлении.",
                          "transmission-test-fail": get_setting("last_trans_test") or "Не удалось подключиться к Transmission.",
                          "tracker-test-fail": "Не удалось войти на трекер.", "tracker-captcha": "Трекер требует капчу.",
                          "tracker-forbidden": "Rutracker заблокировал запрос (403).",
                          "tracker-cookies-invalid": "Cookies невалидны."}.get(msg)})


@app.post("/settings/refresh")
async def set_refresh_interval(hours: int = Form(...)):
    hours = max(1, min(168, hours))
    set_setting("refresh_hours", str(hours))
    scheduler.reschedule_job("refresh", trigger="interval", hours=hours)
    return RedirectResponse("/settings?msg=refresh-saved", status_code=303)


@app.post("/settings/theme")
async def save_theme(theme: str = Form("dark")):
    set_setting("theme", theme if theme in ("dark", "light") else "dark")
    return RedirectResponse("/settings?msg=theme-saved", status_code=303)


@app.post("/settings/proxy")
async def save_proxy(proxy_url: str = Form("")):
    set_setting("proxy_url", proxy_url.strip())
    return RedirectResponse("/settings?msg=proxy-saved", status_code=303)


@app.post("/settings/proxy/test")
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


@app.post("/settings/telegram")
async def save_telegram(bot_token: str = Form(""), chat_id: str = Form(""), enabled: str = Form("off"),
                        send_time: str = Form("09:00"), notify_days: int = Form(1),
                        notify_date_changes: str = Form("off"), notify_new_cards: str = Form("off"),
                        notify_new_seasons: str = Form("off"), notify_new_episodes: str = Form("off"),
                        notify_torrent_started: str = Form("off"), notify_torrent_completed: str = Form("off")):
    save_telegram_settings(bot_token.strip(), chat_id.strip(), enabled == "on", send_time, notify_days,
                           notify_date_changes == "on", notify_new_cards == "on", notify_new_seasons == "on",
                           notify_new_episodes == "on", notify_torrent_started == "on", notify_torrent_completed == "on")
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
    elif test_type == "daily":
        await check_and_notify(force=True)
    else:
        return RedirectResponse("/settings?msg=test-fail", status_code=303)
    return RedirectResponse(f"/settings?msg={'test-ok' if ok else 'test-fail'}", status_code=303)


@app.post("/settings/transmission")
async def save_transmission(host: str = Form("localhost"), port: int = Form(9091), username: str = Form(""),
                            password: str = Form(""), enabled: str = Form("off"), base_download_dir: str = Form(""),
                            action_on_new: str = Form("download"), filter_recent_only: str = Form("off"),
                            min_file_size_mb: int = Form(500), default_check_interval: int = Form(6),
                            default_download_behavior: str = Form("use_distribution_path"),
                            auto_download_new_files: str = Form("off"), auto_check_enabled: str = Form("off"),
                            auto_check_tick_minutes: int = Form(10), transmission_poll_minutes: int = Form(3)):
    if action_on_new not in ("download", "pause", "notify_only"):
        action_on_new = "download"
    if default_download_behavior not in ("use_distribution_path", "use_base_dir"):
        default_download_behavior = "use_distribution_path"
    db("""UPDATE transmission_settings SET host=?, port=?, username=?, encrypted_password=?, enabled=?,
            base_download_dir=?, action_on_new=?, filter_recent_only=?, min_file_size_mb=?, default_check_interval=?,
            default_download_behavior=?, auto_download_new_files=?, auto_check_enabled=?, auto_check_tick_minutes=?,
            transmission_poll_minutes=? WHERE id=1""",
       (host.strip(), port, username.strip(), encrypt_value(password.strip()), 1 if enabled == "on" else 0,
        base_download_dir.strip(), action_on_new, 1 if filter_recent_only == "on" else 0, max(1, min_file_size_mb),
        max(1, min(168, default_check_interval)), default_download_behavior,
        1 if auto_download_new_files == "on" else 0, 1 if auto_check_enabled == "on" else 0,
        max(5, min(60, auto_check_tick_minutes)), max(1, min(60, transmission_poll_minutes))), write=True)
    schedule_distribution_job()
    schedule_transmission_poll_job()
    return RedirectResponse("/settings?msg=transmission-saved", status_code=303)


@app.post("/settings/tracker")
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
            db("INSERT OR REPLACE INTO tracker_credentials (tracker_name, username, encrypted_password, encrypted_cookies, user_agent, enabled) VALUES (?,?,?,?,?,?)",
               (tracker_name, username.strip(), ep, encrypt_value(json.dumps(cd)), user_agent.strip(), ev), write=True)
            ok, reason = await RuTrackerClient(username=username.strip(), password=password.strip(),
                                               proxy=get_proxy_url(), cookies=cd,
                                               user_agent=user_agent.strip()).validate_cookies(cd)
            if ok:
                db("UPDATE tracker_credentials SET last_login_at=datetime('now'), last_error=NULL WHERE tracker_name=?", (tracker_name,), write=True)
                return RedirectResponse("/settings?msg=tracker-cookies-saved", status_code=303)
            db("UPDATE tracker_credentials SET last_error=? WHERE tracker_name=?", (reason,), write=True)
            return RedirectResponse("/settings?msg=tracker-cookies-invalid", status_code=303)
        except Exception as e:
            print(f"[tracker] Error: {e}")
            return RedirectResponse("/settings?msg=tracker-cookies-invalid", status_code=303)
    ex = get_tracker_credentials(tracker_name)
    db("INSERT OR REPLACE INTO tracker_credentials (tracker_name, username, encrypted_password, encrypted_cookies, user_agent, enabled) VALUES (?,?,?,?,?,?)",
       (tracker_name, username.strip(), ep, ex.get("encrypted_cookies", "") if ex else "", user_agent.strip(), ev), write=True)
    return RedirectResponse("/settings?msg=tracker-saved", status_code=303)


@app.post("/distribution/add")
async def add_distribution(title_external_id: str = Form(...), url: str = Form(...), download_path: str = Form(""),
                           mode: str = Form("smart"), check_interval_hours: int = Form(6)):
    existing = get_distribution(title_external_id)
    torrent_id = parse_torrent_id(url)
    if not torrent_id:
        return RedirectResponse("/?msg=dist-invalid-url", status_code=303)
    if mode not in ("smart", "fixed"):
        mode = "smart"
    if existing:
        db("""UPDATE distributions SET tracker_name='rutracker', torrent_id=?, url=?, download_path=?, mode=?,
                check_interval_hours=?, status='idle', last_checked_at=NULL, last_files_hash=NULL, last_files_json=NULL,
                new_files_count=0, error_message=NULL, error_count=0 WHERE title_external_id=?""",
           (torrent_id, url.strip(), download_path.strip(), mode, max(1, min(168, check_interval_hours)), title_external_id), write=True)
        update_next_episode_air_date(title_external_id)
        return RedirectResponse("/?msg=dist-updated", status_code=303)
    db("INSERT INTO distributions (title_external_id, tracker_name, torrent_id, url, download_path, mode, check_interval_hours, status) VALUES (?,?,?,?,?,?,?,'idle')",
       (title_external_id, torrent_id, url.strip(), download_path.strip(), mode, max(1, min(168, check_interval_hours))), write=True)
    update_next_episode_air_date(title_external_id)
    return RedirectResponse("/?msg=dist-added", status_code=303)


@app.post("/distribution/remove/{title_external_id}")
async def remove_distribution(title_external_id: str, sort: str = "date"):
    db("DELETE FROM distributions WHERE title_external_id=?", (title_external_id,), write=True)
    return RedirectResponse(f"/?sort={sort}&msg=dist-removed", status_code=303)


@app.get("/export.ics")
async def export_ics():
    return Response(content=build_ics([dict(r) for r in db("SELECT * FROM titles")]),
                    media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=movie-radar.ics"})


@app.post("/delete/{external_id}")
async def delete(external_id: str, sort: str = "date"):
    for q in ("DELETE FROM titles WHERE external_id=?", "DELETE FROM seasons WHERE title_external_id=?",
              "DELETE FROM watched_episodes WHERE title_external_id=?", "DELETE FROM distributions WHERE title_external_id=?"):
        db(q, (external_id,), write=True)
    return RedirectResponse(f"/?sort={sort}", status_code=303)


@app.post("/toggle-notify/{external_id}")
async def toggle_notify(external_id: str, sort: str = "date"):
    db("UPDATE titles SET notify_enabled = CASE WHEN notify_enabled=1 THEN 0 ELSE 1 END WHERE external_id=?", (external_id,), write=True)
    return RedirectResponse(f"/?sort={sort}", status_code=303)


@app.post("/notify-all/{state}")
async def notify_all(state: str, sort: str = "date"):
    db("UPDATE titles SET notify_enabled=?", (1 if state == "on" else 0,), write=True)
    return RedirectResponse(f"/?sort={sort}", status_code=303)


@app.get("/title/{external_id}", response_class=HTMLResponse)
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


@app.get("/title/{external_id}/season/{season_number}", response_class=HTMLResponse)
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


@app.post("/watch/{external_id}/{season_number}/{episode_number}")
async def watch_episode(external_id: str, season_number: int, episode_number: int):
    toggle_watched(external_id, season_number, episode_number)
    return RedirectResponse(f"/title/{external_id}/season/{season_number}", status_code=303)


@app.post("/watch-season/{external_id}/{season_number}")
async def watch_season(external_id: str, season_number: int):
    toggle_season_watched(external_id, season_number)
    return RedirectResponse(f"/title/{external_id}/season/{season_number}", status_code=303)