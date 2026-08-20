import os
from abc import ABC, abstractmethod
from datetime import datetime, date
from urllib.parse import quote

import httpx

from .core import (TMDB_KEY, OMDB_KEY, POSTERS_DIR, get_proxy_url,
                   sanitize_id, parse_tmdb_id, parse_tmdb_type)


async def download_image(url: str, filename: str):
    if not url:
        return None
    os.makedirs(POSTERS_DIR, exist_ok=True)
    local_path = os.path.join(POSTERS_DIR, filename)
    if os.path.exists(local_path):
        return f"/posters/{filename}"
    try:
        kw = {"timeout": 15, "follow_redirects": True}
        if get_proxy_url():
            kw["proxy"] = get_proxy_url()
        async with httpx.AsyncClient(**kw) as client:
            r = await client.get(url)
            r.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(r.content)
        return f"/posters/{filename}"
    except Exception as e:
        print(f"[download] Error downloading {url}: {e}")
        return None


async def download_card_poster(info: dict):
    url = info.get("poster_url")
    if not url:
        return None
    return await download_image(url, f"{sanitize_id(info['external_id'])}.jpg") or url


def ensure_proxied(url):
    if not url:
        return None
    if url.startswith("/posters/") or url.startswith("/img-proxy"):
        return url
    return f"/img-proxy?url={quote(url, safe='')}"


class Source(ABC):
    name: str = ""

    @abstractmethod
    async def search(self, query: str) -> list: ...

    @abstractmethod
    async def fetch(self, external_id: str): ...

    @abstractmethod
    async def search_candidates(self, query: str) -> list: ...


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
                "genres": ", ".join(g["name"] for g in d.get("genres", [])),
                "status": d.get("status")}

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