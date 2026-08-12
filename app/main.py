import os
import sqlite3
import asyncio
from abc import ABC, abstractmethod
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, Request, Response, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

OMDB_KEY = os.getenv("OMDB_API_KEY", "")
TMDB_KEY = os.getenv("TMDB_API_KEY", "")
DB_PATH = os.getenv("DB_PATH", "/data/catalog.db")
REFRESH_HOURS_DEFAULT = int(os.getenv("REFRESH_HOURS", "12"))

app = FastAPI()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]

# Global refresh progress (visible to /refresh-status endpoint)
refresh_progress = {"running": False, "done": 0, "total": 0}


# ── Source abstraction ────────────────────────────────
class Source(ABC):
    name: str = ""

    @abstractmethod
    async def search(self, query: str) -> list[dict]: ...

    @abstractmethod
    async def fetch(self, external_id: str) -> dict | None: ...


class OmdbSource(Source):
    name = "omdb"

    async def _get(self, params: dict) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
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


class TmdbSource(Source):
    name = "tmdb"
    _POSTER = "https://image.tmdb.org/t/p/w342"

    async def _get(self, path: str, params: dict | None = None) -> dict:
        params = {"api_key": TMDB_KEY, "language": "ru-RU", **(params or {})}
        async with httpx.AsyncClient(timeout=10) as client:
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
            "external_id": f"tmdb:{tmdb_id}",
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
        return [await self._details("movie" if best["media_type"] == "movie" else "tv",
                                    best["id"])]

    async def fetch(self, external_id: str) -> dict | None:
        if not external_id.startswith("tmdb:"):
            return None
        try:
            tmdb_id = int(external_id.split(":", 1)[1])
        except (ValueError, IndexError):
            return None
        try:
            return await self._details("movie", tmdb_id)
        except httpx.HTTPStatusError:
            pass
        try:
            return await self._details("tv", tmdb_id)
        except httpx.HTTPStatusError:
            return None

    async def fetch_seasons(self, tmdb_id: int) -> list[dict]:
        """Fetch all seasons for a TV show."""
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
        """Fetch all episodes for a specific season."""
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
            last_sent TEXT
         )""", write=True)
    db("INSERT OR IGNORE INTO telegram_settings (id) VALUES (1)", write=True)
    for col in ("notify_date_changes", "notify_new_cards",
                "notify_new_seasons", "notify_new_episodes"):
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


def get_telegram_settings() -> dict:
    rows = db("SELECT * FROM telegram_settings WHERE id=1")
    return dict(rows[0]) if rows else {}


def save_telegram_settings(bot_token: str, chat_id: str, enabled: bool,
                           send_time: str, notify_days: int,
                           notify_date_changes: bool, notify_new_cards: bool,
                           notify_new_seasons: bool, notify_new_episodes: bool):
    db("""UPDATE telegram_settings SET
            bot_token=?, chat_id=?, enabled=?, send_time=?,
            notify_days=?, notify_date_changes=?, notify_new_cards=?,
            notify_new_seasons=?, notify_new_episodes=?
          WHERE id=1""",
       (bot_token, chat_id, 1 if enabled else 0, send_time,
        notify_days, 1 if notify_date_changes else 0,
        1 if notify_new_cards else 0, 1 if notify_new_seasons else 0,
        1 if notify_new_episodes else 0),
       write=True)


# ── Update log ────────────────────────────────────────
def log_update(external_id: str, title: str, field: str,
               old_value: str | None, new_value: str | None):
    db("""INSERT INTO updates_log (external_id, title, field, old_value, new_value)
          VALUES (?,?,?,?,?)""",
       (external_id, title, field, old_value, new_value), write=True)


# ── Helpers ───────────────────────────────────────────
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


# ── Seasons & episodes helpers ────────────────────────
def save_seasons(title_external_id: str, seasons: list[dict]) -> list[dict]:
    """Save or update seasons. Returns list of newly added seasons."""
    existing = db("SELECT season_number FROM seasons WHERE title_external_id=?",
                  (title_external_id,))
    existing_numbers = {r["season_number"] for r in existing}

    new_seasons = []
    for s in seasons:
        if s["season_number"] not in existing_numbers:
            new_seasons.append(s)

        poster = (f"https://image.tmdb.org/t/p/w342{s['poster_path']}"
                  if s.get("poster_path") else None)
        db("""INSERT OR REPLACE INTO seasons
              (title_external_id, season_number, name, release_date, episodes, poster_url)
              VALUES (?,?,?,?,?,?)""",
           (title_external_id, s["season_number"], s["name"],
            s["release_date"], s["episodes"], poster), write=True)

    return new_seasons


def save_episodes(title_external_id: str, season_number: int,
                  episodes: list[dict]) -> list[dict]:
    """Save or update episodes. Returns list of newly added episodes."""
    rows = db("SELECT id FROM seasons WHERE title_external_id=? AND season_number=?",
              (title_external_id, season_number))
    if not rows:
        return []
    season_id = rows[0]["id"]

    existing = db("SELECT episode_number FROM episodes WHERE season_id=?",
                  (season_id,))
    existing_numbers = {r["episode_number"] for r in existing}

    new_episodes = []
    for e in episodes:
        if e["episode_number"] not in existing_numbers:
            new_episodes.append({
                "season_number": season_number,
                "episode_number": e["episode_number"],
                "name": e["name"],
                "release_date": e["release_date"],
            })

        poster = (f"https://image.tmdb.org/t/p/w342{e['still_path']}"
                  if e.get("still_path") else None)
        db("""INSERT OR REPLACE INTO episodes
              (season_id, episode_number, name, release_date, runtime, overview, poster_url)
              VALUES (?,?,?,?,?,?,?)""",
           (season_id, e["episode_number"], e["name"], e["release_date"],
            e["runtime"], e.get("overview", ""), poster), write=True)

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


def get_season_progress(title_external_id: str, season_number: int) -> tuple[int, int]:
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


def get_show_progress(title_external_id: str) -> tuple[int, int]:
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
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json={
                "chat_id": s["chat_id"],
                "text": text,
                "parse_mode": "HTML",
            })
            return r.status_code == 200
    except Exception as e:
        print(f"[telegram] Send error: {e}")
        return False


async def notify_date_changes(changes: list[dict]):
    s = get_telegram_settings()
    if not s or not s.get("enabled") or not s.get("notify_date_changes"):
        return
    if not s.get("bot_token") or not s.get("chat_id"):
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


async def notify_new_card(title: str, release_date: str | None,
                          source: str, card_type: str | None):
    s = get_telegram_settings()
    if not s or not s.get("enabled") or not s.get("notify_new_cards"):
        return
    if not s.get("bot_token") or not s.get("chat_id"):
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
                            release_date: str | None):
    s = get_telegram_settings()
    if not s or not s.get("enabled") or not s.get("notify_new_seasons"):
        return
    if not s.get("bot_token") or not s.get("chat_id"):
        return

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


async def notify_new_episodes(show_title: str, new_eps: list[dict]):
    s = get_telegram_settings()
    if not s or not s.get("enabled") or not s.get("notify_new_episodes"):
        return
    if not s.get("bot_token") or not s.get("chat_id"):
        return
    if not new_eps:
        return

    sorted_eps = sorted(new_eps, key=lambda x: (x["season_number"], x["episode_number"]))

    if len(sorted_eps) == 1:
        ep = sorted_eps[0]
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
            f"🆕 <b>Новые эпизоды ({len(sorted_eps)})</b>",
            "",
            f"📺 <b>{show_title}</b>",
        ]
        for ep in sorted_eps:
            name_str = f": {ep['name']}" if ep["name"] else ""
            if is_today(ep["release_date"]):
                lines.append(f"• 🔴 S{ep['season_number']}E{ep['episode_number']}"
                             f"{name_str} — вышел сегодня!")
            else:
                date_str = f" · {human_date(ep['release_date'])}" if ep["release_date"] else ""
                lines.append(f"• S{ep['season_number']}E{ep['episode_number']}"
                             f"{name_str}{date_str}")

    await send_telegram("\n".join(lines))
    print(f"[telegram] New episodes notification: {show_title} ({len(sorted_eps)} eps)")


async def check_and_notify():
    """Scheduled job: sends upcoming releases (titles, seasons, episodes) to Telegram."""
    s = get_telegram_settings()
    if not s or not s.get("enabled"):
        return
    today = date.today()
    notify_days = s.get("notify_days", 1)

    upcoming = []

    # Upcoming title releases
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

    # Upcoming season releases
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

    # Upcoming episode releases
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
    """Fetches updated data for every active title."""
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
        new_poster = fresh["poster_url"] or row["poster_url"]
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
        if new_poster != row["poster_url"]:
            log_update(row["external_id"], new_title, "poster_url",
                       row["poster_url"], new_poster)
            changed = True
        if new_genres != row["genres"]:
            log_update(row["external_id"], new_title, "genres",
                       row["genres"], new_genres)
            changed = True

        if changed:
            db("""UPDATE titles SET
                    title=?, release_date=?, poster_url=?, genres=?,
                    updated_at=datetime('now')
                  WHERE external_id=?""",
               (new_title, new_rd, new_poster, new_genres, row["external_id"]),
               write=True)
            updated += 1

        # Auto-refresh seasons and episodes for TMDB series
        if row["type"] == "series" and src.name == "tmdb":
            try:
                tmdb_id = int(row["external_id"].split(":")[1])
                seasons = await src.fetch_seasons(tmdb_id)
                new_seasons = save_seasons(row["external_id"], seasons)
                await asyncio.sleep(0.5)

                for ns in new_seasons:
                    await notify_new_season(new_title, ns["season_number"],
                                            ns["release_date"])

                new_episodes_all = []
                for season in seasons:
                    try:
                        episodes = await src.fetch_episodes(tmdb_id,
                                                            season["season_number"])
                        new_eps = save_episodes(row["external_id"],
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
    new_poster = fresh["poster_url"] or row["poster_url"]
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
    if new_poster != row["poster_url"]:
        log_update(external_id, new_title, "poster_url",
                   row["poster_url"], new_poster)
        changed = True
    if new_genres != row["genres"]:
        log_update(external_id, new_title, "genres", row["genres"], new_genres)
        changed = True

    if changed:
        db("""UPDATE titles SET
                title=?, release_date=?, poster_url=?, genres=?,
                updated_at=datetime('now')
              WHERE external_id=?""",
           (new_title, new_rd, new_poster, new_genres, external_id), write=True)

    if date_change:
        await notify_date_changes([date_change])

    return changed


scheduler = AsyncIOScheduler()
scheduler.add_job(refresh_catalog, "interval", hours=get_refresh_hours(),
                  id="refresh", next_run_time=None)


@app.on_event("startup")
async def on_startup():
    scheduler.start()
    scheduler.reschedule_job("refresh", trigger="interval",
                             hours=get_refresh_hours(),
                             next_run_time=datetime.now() + timedelta(minutes=5))
    schedule_telegram_job()


@app.on_event("shutdown")
async def on_shutdown():
    scheduler.shutdown()


# ── iCalendar export ──────────────────────────────────
def escape_ics(s: str | None) -> str:
    if not s:
        return ""
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_ics(cards: list[dict]) -> str:
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

        cards.append(c)

    messages = {
        "refresh-started": "Обновление запущено в фоне.",
        "settings-saved": "Настройки сохранены.",
        "card-updated": "Карточка обновлена.",
        "telegram-saved": "Настройки Telegram сохранены.",
    }

    return templates.TemplateResponse(
        "index.html", {
            "request": request, "cards": cards, "sort": sort,
            "sources": list(SOURCES.keys()),
            "default_source": request.cookies.get("source", "omdb"),
            "refresh_hours": get_refresh_hours(),
            "refresh_label": refresh_period_label(get_refresh_hours()),
            "error": "Ничего не нашлось — уточните название." if err else None,
            "message": messages.get(msg),
        })


@app.post("/add")
async def add(title: str = Form(...),
              release_date: str | None = Form(None),
              source: str = Form("omdb")):
    src = SOURCES.get(source, SOURCES["omdb"])
    try:
        results = await src.search(title)
    except Exception:
        return RedirectResponse("/?err=1", status_code=303)
    if not results:
        return RedirectResponse("/?err=1", status_code=303)

    info = results[0]
    rd = info["release_date"] or release_date or None

    db("""INSERT OR REPLACE INTO titles
          (external_id, title, type, release_date, poster_url, genres, source, updated_at)
          VALUES (?,?,?,?,?,?,?,datetime('now'))""",
       (info["external_id"], info["title"], info["type"], rd,
        info["poster_url"], info["genres"], src.name), write=True)

    # Fetch seasons and episodes for series via TMDB (no notifications on initial add)
    if info["type"] == "series" and src.name == "tmdb":
        try:
            tmdb_id = int(info["external_id"].split(":")[1])
            seasons = await src.fetch_seasons(tmdb_id)
            save_seasons(info["external_id"], seasons)
            for season in seasons:
                try:
                    episodes = await src.fetch_episodes(tmdb_id, season["season_number"])
                    save_episodes(info["external_id"], season["season_number"], episodes)
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
        except Exception as e:
            print(f"[add] Error fetching seasons: {e}")

    await notify_new_card(info["title"], rd, src.name, info["type"])

    resp = RedirectResponse("/", status_code=303)
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


@app.post("/settings/refresh")
async def set_refresh_interval(hours: int = Form(...)):
    hours = max(1, min(168, hours))
    set_setting("refresh_hours", str(hours))
    scheduler.reschedule_job("refresh", trigger="interval", hours=hours)
    print(f"[settings] Refresh interval changed to {hours}h")
    return RedirectResponse("/?msg=settings-saved", status_code=303)


@app.get("/settings/telegram", response_class=HTMLResponse)
async def telegram_settings_page(request: Request, msg: str | None = None):
    s = get_telegram_settings()
    messages = {
        "saved": "Настройки сохранены.",
        "test-ok": "Тестовое сообщение отправлено!",
        "test-fail": "Не удалось отправить. Проверьте токен и chat_id.",
    }
    return templates.TemplateResponse(
        "settings.html", {
            "request": request,
            "tg": s,
            "message": messages.get(msg),
        })


@app.post("/settings/telegram")
async def save_telegram(bot_token: str = Form(""),
                        chat_id: str = Form(""),
                        enabled: str = Form("off"),
                        send_time: str = Form("09:00"),
                        notify_days: int = Form(1),
                        notify_date_changes: str = Form("off"),
                        notify_new_cards: str = Form("off"),
                        notify_new_seasons: str = Form("off"),
                        notify_new_episodes: str = Form("off")):
    save_telegram_settings(bot_token.strip(), chat_id.strip(),
                           enabled == "on", send_time, notify_days,
                           notify_date_changes == "on",
                           notify_new_cards == "on",
                           notify_new_seasons == "on",
                           notify_new_episodes == "on")
    schedule_telegram_job()
    return RedirectResponse("/settings/telegram?msg=saved", status_code=303)


@app.post("/settings/telegram/test")
async def telegram_test():
    s = get_telegram_settings()
    if not s.get("bot_token") or not s.get("chat_id"):
        return RedirectResponse("/settings/telegram?msg=test-fail", status_code=303)
    ok = await send_telegram("🎬 <b>Тестовое сообщение</b>\nВсё работает!")
    msg = "test-ok" if ok else "test-fail"
    return RedirectResponse(f"/settings/telegram?msg={msg}", status_code=303)


@app.get("/log", response_class=HTMLResponse)
async def log_page(request: Request):
    rows = db("SELECT * FROM updates_log ORDER BY created_at DESC LIMIT 200")
    return templates.TemplateResponse(
        "log.html", {"request": request, "rows": [dict(r) for r in rows]})


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

    show_watched, show_total = get_show_progress(external_id)

    return templates.TemplateResponse("title.html", {
        "request": request,
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
        try:
            tmdb_id = int(external_id.split(":")[1])
            seasons = await src.fetch_seasons(tmdb_id)
            new_seasons = save_seasons(external_id, seasons)
            await asyncio.sleep(0.5)

            for ns in new_seasons:
                await notify_new_season(card["title"], ns["season_number"],
                                        ns["release_date"])

            new_episodes_all = []
            for season in seasons:
                try:
                    episodes = await src.fetch_episodes(tmdb_id, season["season_number"])
                    new_eps = save_episodes(external_id, season["season_number"], episodes)
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
        episodes.append(ed)

    watched_count, total_count = get_season_progress(external_id, season_number)

    return templates.TemplateResponse("season.html", {
        "request": request,
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