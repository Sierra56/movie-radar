import re

import httpx
from bs4 import BeautifulSoup

from .core import get_proxy_url
from .notify import notify_expired_cookies


class RutrackerError(Exception):
    pass


class RutrackerAuthError(RutrackerError):
    pass


class RutrackerForbiddenError(RutrackerError):
    pass


class RutrackerClient:
    BASE_URL = "https://rutracker.org/forum"

    def __init__(self, username="", password="", proxy=None, cookies=None, user_agent=""):
        self.username = username or ""
        self.password = password or ""
        self.proxy = proxy
        self.cookies = cookies or {}
        self.user_agent = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    def _headers(self):
        h = {"User-Agent": self.user_agent}
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return h

    async def login(self) -> dict:
        if not self.username or not self.password:
            raise RutrackerAuthError("Не указаны логин/пароль для rutracker.org")
        async with httpx.AsyncClient(proxy=get_proxy_url(), timeout=30, follow_redirects=True) as client:
            r = await client.post(f"{self.BASE_URL}/login.php", data={
                "login_username": self.username,
                "login_password": self.password,
                "login": "Вход"
            }, headers=self._headers())
            if r.status_code != 200:
                raise RutrackerAuthError(f"HTTP {r.status_code} при входе")
            cookies = dict(r.cookies)
            if "bb_sessionhash" not in cookies:
                raise RutrackerAuthError("Не удалось получить cookies")
            return cookies

    async def validate_cookies(self, cookies: dict) -> tuple:
        async with httpx.AsyncClient(proxy=get_proxy_url(), timeout=30) as client:
            r = await client.get(f"{self.BASE_URL}/index.php", headers=self._headers(), cookies=cookies)
            if r.status_code == 200 and "login.php" not in str(r.url):
                return True, None
            return False, f"HTTP {r.status_code}, url={r.url}"

    async def fetch_files(self, torrent_id: str, cookies: dict = None) -> list:
        effective_cookies = cookies if cookies is not None else self.cookies
        async with httpx.AsyncClient(proxy=get_proxy_url(), timeout=30) as client:
            r = await client.get(f"{self.BASE_URL}/viewtopic.php?t={torrent_id}",
                                 headers=self._headers(), cookies=effective_cookies)
            if r.status_code == 403:
                await notify_expired_cookies("rutracker")
                raise RutrackerForbiddenError("403 Forbidden — обновите cookies rutracker.org")
            if r.status_code != 200:
                raise RutrackerError(f"HTTP {r.status_code}")

            if "login.php" in str(r.url) or "Вход" in r.text[:2000]:
                await notify_expired_cookies("rutracker")
                raise RutrackerForbiddenError("Сессия истекла — обновите cookies rutracker.org")

            soup = BeautifulSoup(r.text, "html.parser")
            files = []
            for a in soup.find_all("a"):
                href = a.get("href", "")
                text = a.get_text(strip=True)
                if text and any(text.lower().endswith(ext) for ext in ('.mkv', '.mp4', '.avi', '.ts', '.m2ts', '.srt')):
                    files.append({"name": text, "size": 0, "url": href})

            if not files:
                for table in soup.find_all("table", class_="file-list"):
                    for tr in table.find_all("tr"):
                        cells = tr.find_all("td")
                        if len(cells) >= 2:
                            name = cells[0].get_text(strip=True)
                            size_text = cells[1].get_text(strip=True)
                            if name:
                                files.append({"name": name, "size": self._parse_size(size_text), "url": ""})
            return files

    async def download_torrent(self, torrent_id: str, cookies: dict = None) -> bytes:
        effective_cookies = cookies if cookies is not None else self.cookies
        async with httpx.AsyncClient(proxy=get_proxy_url(), timeout=60) as client:
            r = await client.get(f"{self.BASE_URL}/dl.php?t={torrent_id}",
                                 headers=self._headers(), cookies=effective_cookies, follow_redirects=True)

            content_type = r.headers.get("content-type", "")
            if "text/html" in content_type or "login" in str(r.url).lower():
                if self.username and self.password:
                    try:
                        new_cookies = await self.login()
                        r = await client.get(f"{self.BASE_URL}/dl.php?t={torrent_id}",
                                             headers=self._headers(), cookies=new_cookies, follow_redirects=True)
                        content_type = r.headers.get("content-type", "")
                        if "text/html" in content_type or "login" in str(r.url).lower():
                            await notify_expired_cookies("rutracker")
                            raise RutrackerForbiddenError(
                                "rutracker.org вернул HTML вместо торрента. Сессия не восстановилась. "
                                "Обновите cookies вручную."
                            )
                    except RutrackerAuthError as e:
                        await notify_expired_cookies("rutracker")
                        raise RutrackerForbiddenError(f"Не удалось перелогиниться: {e}")
                else:
                    await notify_expired_cookies("rutracker")
                    raise RutrackerForbiddenError(
                        "rutracker.org вернул HTML вместо торрента. Сессия истекла. "
                        "Настройте логин/пароль или обновите cookies."
                    )

            if r.status_code == 403:
                await notify_expired_cookies("rutracker")
                raise RutrackerForbiddenError("403 Forbidden при скачивании торрента")
            if r.status_code != 200:
                raise RutrackerError(f"HTTP {r.status_code} при скачивании торрента")

            return r.content

    def _parse_size(self, size_str: str) -> int:
        s = size_str.replace(",", ".").replace(" ", "")
        m = re.match(r"([\d.]+)\s*([KMGT])B?", s, re.IGNORECASE)
        if not m:
            return 0
        num, unit = float(m.group(1)), m.group(2).upper()
        if unit == "K":
            return int(num * 1024)
        if unit == "M":
            return int(num * 1024 * 1024)
        if unit == "G":
            return int(num * 1024 * 1024 * 1024)
        if unit == "T":
            return int(num * 1024 * 1024 * 1024 * 1024)
        return 0