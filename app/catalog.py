import asyncio
import json
import statistics
from datetime import date, datetime, timedelta, timezone

from . import core
from .core import (db, log_update, human_date, parse_episode, sanitize_id, parse_tmdb_id)
from .sources import SOURCES, download_image
from .notify import (notify_new_season, notify_new_episodes, notify_date_changes,
                     notify_season_completed)


# ── Эпизоды / даты ────────────────────────────────────
def lookup_episode_air_date(title_external_id, season, episode):
    rows = db("""SELECT e.release_date FROM episodes e JOIN seasons s ON e.season_id=s.id
                 WHERE s.title_external_id=? AND s.season_number=? AND e.episode_number=?""",
              (title_external_id, season, episode))
    return rows[0]["release_date"] if rows else None


# ── Паттерны обучения ─────────────────────────────────
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
                        "detected_at": now.isoformat(), "delay_hours": round(delay, 1),
                        "air_hour": now.hour})
        existing.add(ep)
        added += 1
    if not added:
        return
    db("UPDATE distribution_patterns SET samples_json=? WHERE distribution_id=?",
       (json.dumps(samples[-20:], ensure_ascii=False), dist["id"]), write=True)
    recompute_pattern(dist["id"])
    print(f"[learn] {dist['title_external_id']}: +{added} samples")


# ── Сезоны / эпизоды ──────────────────────────────────
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


def get_next_episode_date(external_id):
    rows = db("""SELECT e.release_date FROM episodes e JOIN seasons s ON e.season_id=s.id
                 WHERE s.title_external_id=? AND e.release_date >= date('now')
                 ORDER BY e.release_date LIMIT 1""", (external_id,))
    return rows[0]["release_date"] if rows else None


def update_next_episode_air_date(external_id):
    from .trackers import get_distribution
    dist = get_distribution(external_id)
    if not dist:
        return
    rows = db("""SELECT e.release_date FROM episodes e JOIN seasons s ON e.season_id=s.id
                 WHERE s.title_external_id=? AND e.release_date >= date('now') ORDER BY e.release_date LIMIT 1""", (external_id,))
    new_date = rows[0]["release_date"] if rows else None
    if new_date != dist.get("next_episode_air_date"):
        db("UPDATE distributions SET next_episode_air_date=? WHERE id=?", (new_date, dist["id"]), write=True)


# ── Просмотрено ───────────────────────────────────────
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


# ── «Сезон завершён» ──────────────────────────────────
def get_season_episode_count(title_external_id, season_number):
    rows = db("SELECT episodes FROM seasons WHERE title_external_id=? AND season_number=?",
              (title_external_id, season_number))
    return (rows[0]["episodes"] or 0) if rows else 0


def check_season_completed(title_external_id, season_number):
    expected = get_season_episode_count(title_external_id, season_number)
    if not expected:
        return False, 0, 0
    rows = db("""SELECT COUNT(DISTINCT h.episode_number) as c FROM download_history h
                 WHERE h.distribution_id IN (
                     SELECT id FROM distributions WHERE title_external_id=?
                 ) AND h.episode_season=?""", (title_external_id, season_number))
    actual = rows[0]["c"] if rows else 0
    return actual >= expected, expected, actual


async def maybe_notify_season_completed(title_external_id, file_name):
    ep = parse_episode(file_name)
    if not ep:
        return
    season, episode = ep
    expected = get_season_episode_count(title_external_id, season)
    if not expected or episode < expected:
        return
    done, exp, act = check_season_completed(title_external_id, season)
    if not done:
        return
    rows = db("SELECT title FROM titles WHERE external_id=?", (title_external_id,))
    title = rows[0]["title"] if rows else title_external_id
    await notify_season_completed(title, season)


# ── Обновление каталога ───────────────────────────────
async def refresh_catalog():
    rows = db("SELECT * FROM titles")
    today = date.today()
    updated = skipped = 0
    date_changes = []
    core.refresh_progress.update({"running": True, "done": 0, "total": len(rows)})
    for i, r in enumerate(rows):
        row = dict(r)
        if row["source"] == "local":
            core.refresh_progress["done"] = i + 1
            continue
        if row["release_date"]:
            try:
                if date.fromisoformat(row["release_date"]) < today - timedelta(days=30) and row["type"] != "series":
                    skipped += 1
                    core.refresh_progress["done"] = i + 1
                    continue
            except ValueError:
                pass
        src = SOURCES.get(row["source"])
        if not src:
            core.refresh_progress["done"] = i + 1
            continue
        try:
            fresh = await src.fetch(row["external_id"])
            await asyncio.sleep(1)
        except Exception as e:
            print(f"[refresh] Error fetching {row['external_id']}: {e}")
            core.refresh_progress["done"] = i + 1
            continue
        if not fresh:
            core.refresh_progress["done"] = i + 1
            continue
        if src.name == "tmdb" and fresh.get("status"):
            db("UPDATE titles SET tmdb_status=? WHERE external_id=?", (fresh["status"], row["external_id"]), write=True)
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
        core.refresh_progress["done"] = i + 1
    core.refresh_progress["running"] = False
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
    if src.name == "tmdb" and fresh.get("status"):
        db("UPDATE titles SET tmdb_status=? WHERE external_id=?", (fresh["status"], external_id), write=True)
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