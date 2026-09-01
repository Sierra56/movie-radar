import re
import json
from urllib.parse import urljoin
import httpx

from .notify import notify_expired_cookies


class BaseTracker:
    """Абстрактный интерфейс трекера."""
    async def validate_cookies(self, cookies: dict) -> tuple[bool, str]: ...
    async def fetch_files(self, torrent_id: str, cookies: dict) -> list[dict]: ...
    async def download_torrent(self, torrent_id: str, cookies: dict) -> bytes: ...
    async def login(self) -> dict: ...


class RuTrackerError(Exception):
    pass


class RuTrackerCaptchaError(RuTrackerError):
    pass


class RuTrackerAuthError(RuTrackerError):
    pass


class RuTrackerForbiddenError(RuTrackerError):
    pass


class RuTrackerClient(BaseTracker):
    BASE_URL = "https://rutracker.org/forum"

    def __init__(self, username: str = "", password: str = "",
                 proxy: str | None = None, cookies: dict | None = None,
                 user_agent: str = ""):
        self.username = username
        self.password = password
        self.proxy = proxy
        self.cookies = cookies or {}
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
        try:
            kw = self._client_kwargs()
            async with httpx.AsyncClient(**kw) as client:
                r = await client.get(f"{self.BASE_URL}/index.php",
                                     cookies=cookies, headers=self._headers())
                if r.status_code == 200 and 'href="login.php"' not in r.text[:5000]:
                    return True, "cookies валидны"
                await notify_expired_cookies("rutracker")
                return False, "cookies невалидны"
        except Exception as e:
            return False, f"ошибка: {e}"

    async def login(self) -> dict:
        if not self.username or not self.password:
            raise RuTrackerAuthError("не указаны логин/пароль")
        kw = self._client_kwargs()
        async with httpx.AsyncClient(**kw) as client:
            r = await client.post(f"{self.BASE_URL}/login.php",
                                  data={"login_username": self.username,
                                        "login_password": self.password,
                                        "login": "Вход"},
                                  headers=self._headers())
            if "captcha" in r.text.lower():
                raise RuTrackerCaptchaError("требуется капча")
            if 'href="login.php"' in r.text[:5000]:
                raise RuTrackerAuthError("неверный логин/пароль")
            return dict(r.cookies)

    async def fetch_files(self, torrent_id: str, cookies: dict) -> list[dict]:
        url = f"{self.BASE_URL}/viewtopic.php?t={torrent_id}"
        kw = self._client_kwargs()
        async with httpx.AsyncClient(**kw) as client:
            r = await client.get(url, cookies=cookies, headers=self._headers())
            if r.status_code == 403:
                await notify_expired_cookies("rutracker")
                raise RuTrackerForbiddenError("403 Forbidden")
            if r.status_code != 200:
                raise RuTrackerError(f"HTTP {r.status_code}")
            html = r.text
            try:
                with open("/data/debug_last_topic.html", "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception:
                pass

            files = []
            rows = re.findall(r'<tr[^>]*class=["\']?tCenter[^"\']*["\']?[^>]*>(.*?)</tr>',
                              html, re.DOTALL)
            for row in rows:
                name_match = re.search(r'<a[^>]*class="[^"]*med[^"]*"[^>]*>([^<]+)</a>', row)
                size_match = re.search(r'<td[^>]*class="[^"]*small[^"]*"[^>]*>([^<]+)</td>', row)
                if name_match and size_match:
                    name = name_match.group(1).strip()
                    size_raw = size_match.group(1).strip()
                    size_bytes = 0
                    m = re.search(r'([\d.,]+)\s*([A-Za-z]{1,2})', size_raw)
                    if m:
                        try:
                            num = float(m.group(1).replace(",", "."))
                            unit = m.group(2).upper()
                            size_bytes = int(num * {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}.get(unit, 1))
                        except ValueError:
                            pass
                    files.append({"name": name, "size": size_bytes})

            if not files:
                title_match = re.search(r'<title>([^<]+)</title>', html)
                if title_match:
                    files.append({"name": title_match.group(1).strip(), "size": 0})
            return files

    async def download_torrent(self, torrent_id: str, cookies: dict) -> bytes:
        url = f"{self.BASE_URL}/dl.php?t={torrent_id}"
        kw = self._client_kwargs()
        async with httpx.AsyncClient(**kw) as client:
            r = await client.get(url, cookies=cookies, headers=self._headers())

            # Проверка: не вернули ли нам HTML вместо торрента (сессия протухла)
            content_type = (r.headers.get("content-type") or "").lower()
            text_preview = r.text[:200].lower() if r.text else ""
            if ("text/html" in content_type
                    or 'href="login.php"' in text_preview
                    or "<!doctype html" in text_preview):
                # Пробуем перелогиниться
                if self.username and self.password:
                    try:
                        new_cookies = await self.login()
                        r = await client.get(url, cookies=new_cookies, headers=self._headers())
                        content_type = (r.headers.get("content-type") or "").lower()
                        text_preview = r.text[:200].lower() if r.text else ""
                        if not ("text/html" in content_type
                                or 'href="login.php"' in text_preview
                                or "<!doctype html" in text_preview):
                            # Обновим сохранённые куки в БД через caller'а, пока просто вернём контент
                            if r.status_code != 200:
                                await notify_expired_cookies("rutracker")
                                raise RuTrackerError(f"download HTTP {r.status_code}")
                            return r.content
                    except (RuTrackerAuthError, RuTrackerCaptchaError):
                        pass
                await notify_expired_cookies("rutracker")
                raise RuTrackerForbiddenError(
                    "rutracker вернул HTML вместо торрента — обновите cookies"
                )

            if r.status_code != 200:
                await notify_expired_cookies("rutracker")
                raise RuTrackerError(f"download HTTP {r.status_code}")
            return r.content