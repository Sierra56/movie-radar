"""Rutor.info tracker client."""
import re
import httpx


class RutorError(Exception):
    pass


class RutorClient:
    BASE_URL = "http://rutor.info"

    def __init__(self, proxy: str | None = None, user_agent: str = ""):
        self.proxy = proxy
        self.user_agent = user_agent or (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
        )

    def _client_kwargs(self):
        kw = {"timeout": 20, "follow_redirects": True}
        if self.proxy:
            kw["proxy"] = self.proxy
        return kw

    def _headers(self):
        return {"User-Agent": self.user_agent}

    async def validate_cookies(self, cookies: dict) -> tuple[bool, str]:
        """Rutor не требует авторизации — всегда валиден."""
        return True, "rutor не требует авторизации"

    async def fetch_files(self, torrent_id: str, cookies: dict) -> list[dict]:
        url = f"{self.BASE_URL}/download/{torrent_id}"
        kw = self._client_kwargs()
        async with httpx.AsyncClient(**kw) as client:
            r = await client.get(url, headers=self._headers())
            if r.status_code != 200:
                raise RutorError(f"HTTP {r.status_code}")
            html = r.text
            try:
                with open("/data/debug_rutor.html", "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception:
                pass

            files = []
            rows = re.findall(r'<tr class="gai">(.*?)</tr>', html, re.DOTALL)
            for row in rows:
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                if len(cells) < 2:
                    continue
                name = re.sub(r'<[^>]+>', '', cells[0]).strip()
                size_raw = re.sub(r'<[^>]+>', '', cells[1]).strip()
                if not name or len(name) < 3:
                    continue
                size_bytes = 0
                m = re.search(r'([\d.,]+)\s*([A-Za-z]{1,2})', size_raw)
                if m:
                    try:
                        size_bytes = int(float(m.group(1).replace(",", ".")) *
                                         {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}.get(m.group(2).upper(), 1))
                    except ValueError:
                        pass
                files.append({"name": name, "size": size_bytes})
            if not files:
                t = re.search(r'<title>([^<]+)</title>', html)
                if t:
                    files.append({"name": t.group(1).strip(), "size": 0})
            return files

    async def download_torrent(self, torrent_id: str, cookies: dict) -> bytes:
        url = f"{self.BASE_URL}/download/{torrent_id}"
        kw = self._client_kwargs()
        async with httpx.AsyncClient(**kw) as client:
            r = await client.get(url, headers=self._headers())
            if r.status_code != 200:
                raise RutorError(f"download HTTP {r.status_code}")
            return r.content

    async def login(self) -> dict:
        """Rutor не требует логина."""
        return {}