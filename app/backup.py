import io
import os
import json
import zipfile
import tempfile
import traceback
import hashlib
from urllib.parse import urlparse
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Form, File, UploadFile, Response, RedirectResponse, StreamingResponse, FileResponse

from .core import (db, scheduler, POSTERS_DIR, STATIC_DIR, get_proxy_url,
                   SETTINGS_TABLES, CARD_TABLES, TORRENT_TABLES, BACKUP_VERSION, get_refresh_hours)
from .notify import schedule_telegram_job
from .jobs import (schedule_distribution_job, schedule_transmission_poll_job,
                   schedule_auto_clean_job)

router_backup = APIRouter()

_ALLOWED_IMG_HOSTS = {"image.tmdb.org", "m.media-amazon.com", "ia.media-imdb.com", "upload.wikimedia.org"}


def _build_backup_zip(a, b, c, d):
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)
    try:
        src = sqlite3_connect()
        dst = sqlite3_connect(tmp_path)
        src.backup(dst)
        src.close()
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


def sqlite3_connect(path=None):
    import sqlite3
    return sqlite3.connect(path or DB_PATH_REF())


def DB_PATH_REF():
    from .core import DB_PATH
    return DB_PATH


@router_backup.post("/backup/create")
async def create_backup(include_settings: str = Form("off"), include_cards: str = Form("off"),
                        include_images: str = Form("off"), include_torrents: str = Form("off")):
    inc = [include_settings == "on", include_cards == "on", include_images == "on", include_torrents == "on"]
    if not any(inc):
        return RedirectResponse("/settings?msg=backup-empty", status_code=303)
    buf = _build_backup_zip(*inc)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename=movie-radar-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"})


@router_backup.post("/backup/restore")
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
            with open(os.path.join(os.path.dirname(DB_PATH_REF()), "auto-backup-latest.zip"), "wb") as f:
                f.write(_build_backup_zip(True, True, True, True).read())
        except Exception as e:
            print(f"[backup] Auto-backup failed: {e}")
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(tmp_fd)
        with zf.open("backup.db") as s, open(tmp_path, "wb") as d:
            d.write(s.read())
        import sqlite3
        conn = sqlite3.connect(DB_PATH_REF())
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
            from .core import scheduler as sch
            sch.reschedule_job("refresh", trigger="interval", hours=get_refresh_hours())
            schedule_telegram_job(); schedule_distribution_job(); schedule_transmission_poll_job(); schedule_auto_clean_job()
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


@router_backup.get("/img-proxy")
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


@router_backup.get("/manifest.json")
async def get_manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@router_backup.get("/sw.js")
async def get_sw():
    return Response(content=(STATIC_DIR / "sw.js").read_text(encoding="utf-8"),
                    media_type="application/javascript")