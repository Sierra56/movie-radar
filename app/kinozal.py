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
        """Получает список файлов раздачи через AJAX-эндпоинт."""
        effective_cookies = cookies if cookies is not None else self.cookies
        
        # Сначала проверяем доступность основной страницы (сессия)
        async with httpx.AsyncClient(proxy=get_proxy_url(), timeout=30) as client:
            r = await client.get(f"{self.BASE_URL}/details.php?id={torrent_id}",
                                 headers=self._headers(), cookies=effective_cookies)
            if r.status_code == 403:
                raise KinozalForbiddenError("403 Forbidden")
            if r.status_code != 200:
                raise KinozalError(f"HTTP {r.status_code}")
            
            # Список файлов загружается отдельным AJAX-запросом
            # get_srv_details.php?id=X&action=2 возвращает HTML со списком файлов
            details_url = f"{self.BASE_URL}/get_srv_details.php?id={torrent_id}&action=2"
            r = await client.get(details_url, headers={
                **self._headers(),
                "Referer": f"{self.BASE_URL}/details.php?id={torrent_id}",
                "X-Requested-With": "XMLHttpRequest",
            }, cookies=effective_cookies)
            
            if r.status_code != 200:
                raise KinozalError(f"HTTP {r.status_code} при получении списка файлов")
            
            # Явно указываем кодировку windows-1251 (не UTF-8!)
            r.encoding = "cp1251"
            html = r.text
            
            files = []
            
            # Парсим строки таблицы вида: <tr class='float'><td class='s'>имя.mkv</td><td class='s'>размер</td>...
            # Или строки с файлами в таблице без класса float
            for m in re.finditer(
                r'<tr[^>]*>\s*<td[^>]*>([^<]+?)</td>\s*<td[^>]*>([\d.]+\s*[КMГT]Б)</td>',
                html, re.IGNORECASE | re.DOTALL
            ):
                name = m.group(1).strip()
                size = self._parse_size(m.group(2).strip())
                # Фильтруем только файлы с расширениями медиа
                if any(name.lower().endswith(ext) for ext in ('.mkv', '.mp4', '.avi', '.ts', '.m2ts', '.srt', '.sub', '.idx', '.nfo', '.jpg')):
                    files.append({"name": name, "size": size, "url": ""})
            
            # Fallback: ищем в основной странице, если AJAX не сработал
            if not files:
                r2 = await client.get(f"{self.BASE_URL}/details.php?id={torrent_id}",
                                      headers=self._headers(), cookies=effective_cookies)
                r2.encoding = "cp1251"
                html2 = r2.text
                
                # Сохраняем дамп для диагностики
                try:
                    with open(f"/data/debug_kinozal_{torrent_id}.html", "w", encoding="utf-8") as f:
                        f.write(html2)
                except Exception:
                    pass
                
                # Ищем файлы в "Содержании" — обычно идут списком с расширением .mkv
                for m in re.finditer(r'([A-Za-zА-Яа-я0-9_\-\.\s\(\)]+\.(?:mkv|mp4|avi|ts|m2ts))\s+([\d.]+\s*[КMГT]Б)', html2, re.IGNORECASE):
                    name = m.group(1).strip()
                    size = self._parse_size(m.group(2).strip())
                    files.append({"name": name, "size": size, "url": ""})
            
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
        # Поддержка и кириллических (КБ, МБ, ГБ), и латинских (КБ, МБ, ГБ) сокращений
        m = re.match(r"([\d.]+)\s*([КMГT])Б", size_str)
        if not m:
            return 0
        num, unit = float(m.group(1)), m.group(2)
        if unit == "К":
            return int(num * 1024)
        if unit == "M":
            return int(num * 1024 * 1024)
        if unit in ("Г", "G"):
            return int(num * 1024 * 1024 * 1024)
        if unit in ("Т", "T"):
            return int(num * 1024 * 1024 * 1024 * 1024)
        return 0