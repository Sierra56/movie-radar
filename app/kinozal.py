import json
import re
from typing import Optional

import httpx

from .core import get_proxy_url


class KinozalError(Exception):
    pass


class KinozalAuthError(KinozalError):
    pass


class KinozalForbiddenError(KinozalError):
    pass


class KinozalClient:
    BASE_URL = "https://kinozal.me"

    def __init__(self, username="", password="", proxy=None, cookies=None, user_agent=""):
        self.username = username or ""
        self.password = password or ""
        self.proxy = proxy
        self.cookies = cookies or {}
        self.user_agent = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    def _headers(self):
        h = {"User-Agent": self.user_agent, "Referer": self.BASE_URL}
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return h

    async def login(self) -> dict:
        if not self.username or not self.password:
            raise KinozalAuthError("Не указаны логин/пароль для kinozal.me")
        async with httpx.AsyncClient(proxy=get_proxy_url(), timeout=30, follow_redirects=True) as client:
            r = await client.post(f"{self.BASE_URL}/takelogin.php", data={
                "username": self.username, "password": self.password, "returnto": ""
            }, headers={"User-Agent": self.user_agent})
            if r.status_code != 200:
                raise KinozalAuthError(f"HTTP {r.status_code} при входе")
            cookies = dict(r.cookies)
            if "uid" not in cookies or "pass" not in cookies:
                raise KinozalAuthError("Не удалось получить cookies")
            return cookies

    async def validate_cookies(self, cookies: dict) -> tuple:
        async with httpx.AsyncClient(proxy=get_proxy_url(), timeout=30) as client:
            r = await client.get(f"{self.BASE_URL}/my.php", headers=self._headers(), cookies=cookies)
            if r.status_code == 200 and "my.php" in str(r.url):
                return True, None
            return False, f"HTTP {r.status_code}, redirect to {r.url}"

    async def fetch_files(self, torrent_id: str, cookies: dict = None) -> list:
        effective_cookies = cookies if cookies is not None else self.cookies
        async with httpx.AsyncClient(proxy=get_proxy_url(), timeout=30) as client:
            r = await client.get(f"{self.BASE_URL}/details.php?id={torrent_id}",
                                 headers=self._headers(), cookies=effective_cookies)
            if r.status_code == 403:
                raise KinozalForbiddenError("403 Forbidden")
            if r.status_code != 200:
                raise KinozalError(f"HTTP {r.status_code}")
            html = r.text
            files = []
            for m in re.finditer(r'<a href="/download\.php\?id=(\d+)"[^>]*>([^<]+)</a>', html):
                files.append({"name": m.group(2).strip(), "size": 0, "url": f"{self.BASE_URL}/download.php?id={m.group(1)}"})
            if not files:
                # Fallback: ищем в тексте раздачи
                for m in re.finditer(r'<td class="s">\s*([^<]+?)\s*</td>\s*<td class="s">\s*([\d.]+\s*[КMG]Б)\s*</td>', html):
                    files.append({"name": m.group(1).strip(), "size": self._parse_size(m.group(2)), "url": ""})
            return files

    async def download_torrent(self, torrent_id: str, cookies: dict = None) -> bytes:
        effective_cookies = cookies if cookies is not None else self.cookies
        async with httpx.AsyncClient(proxy=get_proxy_url(), timeout=60) as client:
            r = await client.get(f"{self.BASE_URL}/download.php?id={torrent_id}",
                                 headers=self._headers(), cookies=effective_cookies, follow_redirects=True)
            
            # Проверяем, что получили торрент, а не HTML
            content_type = r.headers.get("content-type", "")
            if "text/html" in content_type or r.text.strip().startswith("<!DOCTYPE") or r.text.strip().startswith("<html"):
                # Получили HTML вместо торрента — сессия протухла
                # Пытаемся перелогиниться один раз
                if self.username and self.password:
                    try:
                        new_cookies = await self.login()
                        r = await client.get(f"{self.BASE_URL}/download.php?id={torrent_id}",
                                             headers=self._headers(), cookies=new_cookies, follow_redirects=True)
                        content_type = r.headers.get("content-type", "")
                        if "text/html" in content_type or r.text.strip().startswith("<!DOCTYPE") or r.text.strip().startswith("<html"):
                            raise KinozalForbiddenError(
                                "kinozal.me вернул HTML вместо торрента. Сессия не восстановилась. "
                                "Обновите cookies вручную или войдите через браузер."
                            )
                    except KinozalAuthError as e:
                        raise KinozalForbiddenError(f"Не удалось перелогиниться: {e}")
                else:
                    raise KinozalForbiddenError(
                        "kinozal.me вернул HTML вместо торрента. Сессия истекла. "
                        "Настройте логин/пароль или обновите cookies."
                    )
            
            if r.status_code == 403:
                raise KinozalForbiddenError("403 Forbidden при скачивании торрента")
            if r.status_code != 200:
                raise KinozalError(f"HTTP {r.status_code} при скачивании торрента")
            
            return r.content

    def _parse_size(self, size_str: str) -> int:
        m = re.match(r"([\d.]+)\s*([КMG])Б", size_str)
        if not m:
            return 0
        num, unit = float(m.group(1)), m.group(2)
        if unit == "К":
            return int(num * 1024)
        if unit == "M":
            return int(num * 1024 * 1024)
        if unit == "G":
            return int(num * 1024 * 1024 * 1024)
        return 0