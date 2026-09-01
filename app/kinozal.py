import re

import httpx

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

    def __init__(self, username="", password="", proxy=None, cookies=None, user_agent=""):
        self.username = username or ""
        self.password = password or ""
        self.proxy = proxy
        self.cookies = cookies or {}
        self.user_agent = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    def _headers(self):
        h = {
            "User-Agent": self.user_agent,
            "Referer": self.BASE_URL,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return h

    def _save_dump(self, torrent_id: str, suffix: str, content: str):
        try:
            path = f"/data/debug_kinozal_{torrent_id}_{suffix}.html"
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            print(f"[kinozal] не удалось сохранить дамп: {e}")

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

        async with httpx.AsyncClient(proxy=get_proxy_url(), timeout=30) as client:
            # 1. Проверяем основную страницу (сессия)
            r = await client.get(f"{self.BASE_URL}/details.php?id={torrent_id}",
                                 headers=self._headers(), cookies=effective_cookies)
            if r.status_code == 403:
                await notify_expired_cookies_async("kinozal")
                raise KinozalForbiddenError("403 Forbidden — обновите cookies kinozal.me")
            if r.status_code != 200:
                raise KinozalError(f"HTTP {r.status_code} при загрузке details.php")

            # 2. Список файлов через AJAX-эндпоинт
            details_url = f"{self.BASE_URL}/get_srv_details.php?id={torrent_id}&action=2"
            r = await client.get(details_url, headers={
                **self._headers(),
                "Referer": f"{self.BASE_URL}/details.php?id={torrent_id}",
                "X-Requested-With": "XMLHttpRequest",
            }, cookies=effective_cookies)

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
                r_main = await client.get(f"{self.BASE_URL}/details.php?id={torrent_id}",
                                          headers=self._headers(), cookies=effective_cookies)
                r_main.encoding = "cp1251"
                main_html = r_main.text
                self._save_dump(torrent_id, "main", main_html)
                files = self._parse_files_from_html(main_html)

            return files

    async def download_torrent(self, torrent_id: str, cookies: dict = None) -> bytes:
        effective_cookies = cookies if cookies is not None else self.cookies
        async with httpx.AsyncClient(proxy=get_proxy_url(), timeout=60) as client:
            r = await client.get(f"{self.BASE_URL}/download.php?id={torrent_id}",
                                 headers=self._headers(), cookies=effective_cookies, follow_redirects=True)

            content_type = r.headers.get("content-type", "")
            body_start = r.text[:15].lower() if r.text else ""
            if "text/html" in content_type or body_start.startswith(("<!doctype", "<html")):
                if self.username and self.password:
                    try:
                        new_cookies = await self.login()
                        r = await client.get(f"{self.BASE_URL}/download.php?id={torrent_id}",
                                             headers=self._headers(), cookies=new_cookies, follow_redirects=True)
                        content_type = r.headers.get("content-type", "")
                        if "text/html" in content_type or r.text[:15].lower().startswith(("<!doctype", "<html")):
                            await notify_expired_cookies_async("kinozal")
                            raise KinozalForbiddenError(
                                "kinozal.me вернул HTML вместо торрента. Сессия не восстановилась. "
                                "Обновите cookies вручную."
                            )
                    except KinozalAuthError as e:
                        await notify_expired_cookies_async("kinozal")
                        raise KinozalForbiddenError(f"Не удалось перелогиниться: {e}")
                else:
                    await notify_expired_cookies_async("kinozal")
                    raise KinozalForbiddenError(
                        "kinozal.me вернул HTML вместо торрента. Сессия истекла. "
                        "Настройте логин/пароль или обновите cookies."
                    )

            if r.status_code == 403:
                await notify_expired_cookies_async("kinozal")
                raise KinozalForbiddenError("403 Forbidden при скачивании торрента")
            if r.status_code != 200:
                raise KinozalError(f"HTTP {r.status_code} при скачивании торрента")

            return r.content