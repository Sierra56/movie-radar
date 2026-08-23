import re
from curl_cffi import requests as cffi_requests


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
    # Имитируем TLS-отпечаток Chrome 131 — обычно этого достаточно для Cloudflare.
    # Если rutracker начнёт блокировать конкретную версию — поменять на "chrome120", "chrome124", "safari_ios" и т.п.
    IMPERSONATE = "chrome"

    def __init__(self, username: str = "", password: str = "",
                 proxy: str | None = None, cookies: dict | None = None,
                 user_agent: str = ""):
        self.username = username
        self.password = password
        self.proxy = proxy
        self.cookies = cookies or {}
        self.user_agent = user_agent or (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        )

    def _request_kwargs(self):
        """Общие параметры для всех запросов: имперсонейт + прокси + заголовки."""
        kw = {
            "impersonate": self.IMPERSONATE,
            "timeout": 20,
            "allow_redirects": True,
        }
        if self.proxy:
            kw["proxies"] = {"http": self.proxy, "https": self.proxy}
        # Кастомный UA поверх имперсонейта. Cloudflare обычно терпит подмену UA
        # при совпадении TLS-отпечатка.
        headers = {"User-Agent": self.user_agent}
        return kw, headers

    def _session(self):
        kw, headers = self._request_kwargs()
        return cffi_requests.AsyncSession(headers=headers, **kw)

    async def validate_cookies(self, cookies: dict) -> tuple[bool, str]:
        try:
            async with self._session() as client:
                r = await client.get(f"{self.BASE_URL}/index.php", cookies=cookies)
                if r.status_code == 200 and 'href="login.php"' not in r.text[:5000]:
                    return True, "cookies валидны"
                if r.status_code == 403:
                    return False, "403 Forbidden (TLS/Cloudflare)"
                return False, f"cookies невалидны (HTTP {r.status_code})"
        except Exception as e:
            return False, f"ошибка: {e}"

    async def login(self) -> dict:
        if not self.username or not self.password:
            raise RuTrackerAuthError("не указаны логин/пароль")
        async with self._session() as client:
            r = await client.post(f"{self.BASE_URL}/login.php",
                                  data={"login_username": self.username,
                                        "login_password": self.password,
                                        "login": "Вход"})
            if "captcha" in r.text.lower():
                raise RuTrackerCaptchaError("требуется капча")
            if 'href="login.php"' in r.text[:5000]:
                raise RuTrackerAuthError("неверный логин/пароль")
            # curl_cffi возвращает cookies как объект, совместимый с dict
            return dict(r.cookies)

    async def fetch_files(self, torrent_id: str, cookies: dict) -> list[dict]:
        url = f"{self.BASE_URL}/viewtopic.php?t={torrent_id}"
        async with self._session() as client:
            r = await client.get(url, cookies=cookies)
            if r.status_code == 403:
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
        async with self._session() as client:
            r = await client.get(url, cookies=cookies)
            if r.status_code != 200:
                raise RuTrackerError(f"download HTTP {r.status_code}")
            return r.content