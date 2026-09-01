import re
from curl_cffi import requests as cffi_requests

from .core import get_proxy_url
from .notify import notify_expired_cookies_async


class KinozalError(Exception):
    pass


class KinozalAuthError(KinozalError):
    pass


class KinozalForbiddenError(KinozalError):
    pass


class KinozalClient:
    BASE_URL = "https://kinozal.me"
    # Имитируем реальный браузер, чтобы пройти проверку Cloudflare
    IMPERSONATE = "chrome110"

    def __init__(self, username="", password="", proxy=None, cookies=None, user_agent=""):
        self.username = username or ""
        self.password = password or ""
        self.proxy = proxy
        self.cookies = cookies or {}
        self.user_agent = user_agent  # не используем — curl_cffi подставит свой

    def _session(self):
        """Создаёт сессию с имитацией браузера."""
        return cffi_requests.Session(impersonate=self.IMPERSONATE)

    def _get(self, session, url, cookies=None, headers=None, timeout=30):
        """GET-запрос с куками (включая cf_clearance)."""
        effective_cookies = cookies if cookies is not None else self.cookies
        return session.get(
            url,
            cookies=effective_cookies,
            headers=headers or {},
            proxy=get_proxy_url(),
            timeout=timeout,
            allow_redirects=True,
        )

    def _save_dump(self, torrent_id: str, suffix: str, content: str):
        try:
            path = f"/data/debug_kinozal_{torrent_id}_{suffix}.html"
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            print(f"[kinozal] не удалось сохранить дамп: {e}")

    def _check_cloudflare(self, response) -> bool:
        """Проверяет, не вернул ли Cloudflare челлендж."""
        if response.status_code == 403:
            return True
        text = response.text[:2000].lower()
        if "cf-challenge" in text or "just a moment" in text or "checking your browser" in text:
            return True
        return False

    async def login(self) -> dict:
        if not self.username or not self.password:
            raise KinozalAuthError("Не указаны логин/пароль для kinozal.me")
        with self._session() as session:
            r = session.post(
                f"{self.BASE_URL}/takelogin.php",
                data={"username": self.username, "password": self.password, "returnto": ""},
                proxy=get_proxy_url(),
                timeout=30,
                allow_redirects=True,
            )
            if self._check_cloudflare(r):
                raise KinozalForbiddenError(
                    "Cloudflare заблокировал вход. Обновите cookies из браузера."
                )
            if r.status_code != 200:
                raise KinozalAuthError(f"HTTP {r.status_code} при входе")
            cookies = dict(r.cookies)
            if "uid" not in cookies or "pass" not in cookies:
                raise KinozalAuthError("Не удалось получить cookies")
            return cookies

    async def validate_cookies(self, cookies: dict) -> tuple:
        with self._session() as session:
            r = self._get(session, f"{self.BASE_URL}/my.php", cookies=cookies)
            if self._check_cloudflare(r):
                return False, "Cloudflare заблокировал запрос — обновите cookies"
            if r.status_code == 200 and "my.php" in str(r.url):
                return True, None
            return False, f"HTTP {r.status_code}, redirect to {r.url}"

    @staticmethod
    def _is_media_file(name: str) -> bool:
        lower = name.lower()
        return any(lower.endswith(ext) for ext in (
            '.mkv', '.mp4', '.avi', '.ts', '.m2ts', '.srt', '.sub', '.idx', '.nfo', '.jpg', '.png'
        ))

    def _parse_size(self, size_str: str) -> int:
        s = size_str.replace(",", ".").replace(" ", "")
        m = re.match(r"([\d.]+)\s*([КMГTKMGT])Б?", s, re.IGNORECASE)
        if not m:
            return 0
        num, unit = float(m.group(1)), m.group(2).upper()
        if unit == "К":
            return int(num * 1024)
        if unit == "M":
            return int(num * 1024 * 1024)
        if unit in ("Г", "G"):
            return int(num * 1024 * 1024 * 1024)
        if unit in ("Т", "T"):
            return int(num * 1024 * 1024 * 1024 * 1024)
        return 0

    def _parse_files_from_html(self, html_text: str) -> list:
        files = []
        seen = set()
        # Вариант 1: таблица <tr><td>имя</td><td>размер</td></tr>
        for m in re.finditer(
            r'<tr[^>]*>\s*<td[^>]*>([^<]+?)</td>\s*<td[^>]*>([\d.,]+\s*[КMГTKMGT]Б?)</td>',
            html_text, re.IGNORECASE | re.DOTALL
        ):
            name = m.group(1).strip()
            size = self._parse_size(m.group(2).strip())
            if self._is_media_file(name) and name not in seen:
                seen.add(name)
                files.append({"name": name, "size": size, "url": ""})
        # Вариант 2: имя файла + размер в одной строке
        for m in re.finditer(
            r'([A-Za-zА-Яа-яЁё0-9_\-\.\s\(\)\[\]&]+?\.(?:mkv|mp4|avi|ts|m2ts|srt|sub|idx|nfo|jpg))\s*(?:[\(\[])?\s*([\d.,]+)\s*([КMГTKMGT])Б',
            html_text, re.IGNORECASE
        ):
            name = m.group(1).strip()
            size_str = m.group(2) + " " + m.group(3) + "Б"
            size = self._parse_size(size_str)
            if name not in seen:
                seen.add(name)
                files.append({"name": name, "size": size, "url": ""})
        return files

    async def fetch_files(self, torrent_id: str, cookies: dict = None) -> list:
        effective_cookies = cookies if cookies is not None else self.cookies

        with self._session() as session:
            # 1. Проверяем основную страницу (сессия + Cloudflare)
            r = self._get(session, f"{self.BASE_URL}/details.php?id={torrent_id}", cookies=effective_cookies)
            if self._check_cloudflare(r):
                await notify_expired_cookies_async("kinozal")
                raise KinozalForbiddenError(
                    "Cloudflare заблокировал запрос. Обновите cookies из браузера."
                )
            if r.status_code == 403:
                await notify_expired_cookies_async("kinozal")
                raise KinozalForbiddenError("403 Forbidden — обновите cookies kinozal.me")
            if r.status_code != 200:
                raise KinozalError(f"HTTP {r.status_code} при загрузке details.php")

            # 2. Список файлов через AJAX-эндпоинт
            details_url = f"{self.BASE_URL}/get_srv_details.php?id={torrent_id}&action=2"
            r = self._get(session, details_url, cookies=effective_cookies, headers={
                "Referer": f"{self.BASE_URL}/details.php?id={torrent_id}",
                "X-Requested-With": "XMLHttpRequest",
            })

            if self._check_cloudflare(r):
                await notify_expired_cookies_async("kinozal")
                raise KinozalForbiddenError("Cloudflare заблокировал запрос списка файлов")
            if r.status_code == 403:
                await notify_expired_cookies_async("kinozal")
                raise KinozalForbiddenError("403 Forbidden при получении списка файлов")
            if r.status_code != 200:
                raise KinozalError(f"HTTP {r.status_code} при получении списка файлов")

            # Явно указываем кодировку ДО чтения текста
            r.encoding = "cp1251"
            ajax_html = r.text
            self._save_dump(torrent_id, "ajax", ajax_html)

            files = self._parse_files_from_html(ajax_html)

            # 3. Fallback — основная страница
            if not files:
                r_main = self._get(session, f"{self.BASE_URL}/details.php?id={torrent_id}", cookies=effective_cookies)
                r_main.encoding = "cp1251"
                main_html = r_main.text
                self._save_dump(torrent_id, "main", main_html)
                files = self._parse_files_from_html(main_html)

            return files

    async def download_torrent(self, torrent_id: str, cookies: dict = None) -> bytes:
        effective_cookies = cookies if cookies is not None else self.cookies

        with self._session() as session:
            r = self._get(session, f"{self.BASE_URL}/download.php?id={torrent_id}",
                          cookies=effective_cookies, timeout=60)

            if self._check_cloudflare(r):
                await notify_expired_cookies_async("kinozal")
                raise KinozalForbiddenError("Cloudflare заблокировал скачивание — обновите cookies")

            content_type = r.headers.get("content-type", "")
            body_start = r.text[:15].lower() if r.text else ""
            if "text/html" in content_type or body_start.startswith(("<!doctype", "<html")):
                await notify_expired_cookies_async("kinozal")
                raise KinozalForbiddenError(
                    "kinozal.me вернул HTML вместо торрента. Сессия истекла. "
                    "Обновите cookies из браузера."
                )

            if r.status_code == 403:
                await notify_expired_cookies_async("kinozal")
                raise KinozalForbiddenError("403 Forbidden при скачивании торрента")
            if r.status_code != 200:
                raise KinozalError(f"HTTP {r.status_code} при скачивании торрента")

            return r.content