import re

import httpx

from .core import get_proxy_url
from .notify import notify_expired_cookies


class KinozalError(Exception):
    pass


class KinozalAuthError(KinozalError):
    pass


class KinozalForbiddenError(KinozalError):
    pass


class KinozalCloudflareError(KinozalForbiddenError):
    """Анти-бот защита включена — нужно обновить cookies из браузера."""
    pass


def parse_cookie_string(cookie_str: str) -> dict:
    """Надёжный парсер строки cookies, скопированной из браузера."""
    cookies = {}
    if not cookie_str:
        return cookies
    for item in cookie_str.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, _, value = item.partition("=")
        key = key.strip()
        value = value.strip()
        if key:
            cookies[key] = value
    return cookies


class KinozalClient:
    BASE_URL = "https://kinozal.me"

    def __init__(self, username="", password="", proxy=None, cookies=None, user_agent=""):
        self.username = username or ""
        self.password = password or ""
        self.proxy = proxy
        self.user_agent = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

        self._raw_cookie_header = ""
        if isinstance(cookies, str):
            self._raw_cookie_header = cookies.strip()
            self.cookies = parse_cookie_string(cookies)
        else:
            self.cookies = cookies or {}

    def _headers(self):
        h = {
            "User-Agent": self.user_agent,
            "Referer": self.BASE_URL,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
        if self._raw_cookie_header:
            h["Cookie"] = self._raw_cookie_header
        elif self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return h

    def _save_dump(self, name: str, content: str):
        try:
            path = f"/data/debug_kinozal_{name}.html"
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            print(f"[kinozal] не удалось сохранить дамп: {e}")

    def _is_antibot_challenge(self, r) -> bool:
        """Определяет страницу анти-бот челленджа.

        ВАЖНО: челлендж диагностируется ТОЛЬКО при статусах 403/503 или по
        явному заголовку. Статус 200 никогда не считается челленджем — это
        защищает от ложных срабатываний на нормальном контенте.
        """
        # Явный заголовок
        if (r.headers.get("cf-mitigated") or "").lower() == "challenge":
            return True

        # Челлендж приходит только с 403/503. Любой другой статус — НЕ челлендж.
        if r.status_code not in (403, 503):
            return False

        server = (r.headers.get("server") or "").lower()
        if server == "cloudflare":
            return True

        # Собственная анти-бот защита кинозала: ищем специфичные маркеры
        # именно челлендж-страницы (не встречаются в обычном контенте).
        try:
            text = (r.text or "")[:3000].lower()
        except Exception:
            text = ""
        hard_markers = (
            "just a moment",
            "cf-chl-",
            "challenge-platform",
            "checking your browser",
            "cf-browser-verification",
        )
        return any(m in text for m in hard_markers)

    def _log_response(self, label: str, r):
        print(f"[kinozal] {label}: status={r.status_code} url={r.url} "
              f"content-type={r.headers.get('content-type')} len={len(r.text or '')}")

    async def login(self) -> dict:
        if not self.username or not self.password:
            raise KinozalAuthError("Не указаны логин/пароль для kinozal.me")
        async with httpx.AsyncClient(proxy=get_proxy_url(), timeout=30, follow_redirects=True) as client:
            r = await client.post(f"{self.BASE_URL}/takelogin.php", data={
                "username": self.username, "password": self.password, "returnto": ""
            }, headers={"User-Agent": self.user_agent})
            if self._is_antibot_challenge(r):
                raise KinozalCloudflareError(
                    "Анти-бот защита включена при входе. Обновите cookies из браузера."
                )
            if r.status_code != 200:
                raise KinozalAuthError(f"HTTP {r.status_code} при входе")
            cookies = dict(r.cookies)
            if "uid" not in cookies or "pass" not in cookies:
                raise KinozalAuthError("Не удалось получить cookies")
            return cookies

    async def validate_cookies(self, cookies: dict) -> tuple:
        async with httpx.AsyncClient(proxy=get_proxy_url(), timeout=30) as client:
            r = await client.get(f"{self.BASE_URL}/my.php", headers=self._headers(), cookies=cookies)
            self._log_response("validate my.php", r)
            if self._is_antibot_challenge(r):
                self._save_dump("validate_challenge", r.text)
                await notify_expired_cookies("kinozal")
                return False, "Анти-бот защита включена — обновите cookies из браузера"
            if r.status_code == 200 and "my.php" in str(r.url):
                return True, None
            return False, f"HTTP {r.status_code}, redirect to {r.url}"

    @staticmethod
    def _is_media_file(name: str) -> bool:
        lower = name.lower()
        return any(lower.endswith(ext) for ext in (
            '.mkv', '.mp4', '.avi', '.ts', '.m2ts', '.srt', '.sub', '.idx', '.nfo', '.jpg', '.png'
        ))

    def _parse_ajax_response(self, text: str) -> list:
        """Парсит AJAX-ответ kinozal (plain text).

        Формат:
          Инфо хеш: EBE5D2DB...
          Размер части торрента: 8 МБ
          The.Walking.Dead.Dead.City.2026.S03.WEB-DL.1080p.AMZN.TVShows.LostFilm
          The.Walking.Dead.Dead.City.2026.S03E01...mkv 2.82 ГБ (3027386396)
        """
        files = []
        seen = set()

        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            m = re.match(
                r'^([A-Za-z0-9_\-\.\(\)\[\] ]+?\.(?:mkv|mp4|avi|ts|m2ts|srt|sub|idx|nfo|jpg|png))'
                r'.*?\((\d+)\)\s*$',
                line,
                re.IGNORECASE
            )
            if not m:
                continue
            name = m.group(1).strip()
            try:
                size_bytes = int(m.group(2))
            except ValueError:
                continue
            if name in seen:
                continue
            seen.add(name)
            if not self._is_media_file(name):
                continue
            files.append({"name": name, "size": size_bytes, "url": ""})

        return files

    def _parse_html_files(self, html_text: str) -> list:
        files = []
        seen = set()
        for m in re.finditer(
            r'<tr[^>]*>\s*<td[^>]*>([^<]+?)</td>\s*<td[^>]*>([\d.,]+\s*[КMГTKMGT]Б?)</td>',
            html_text, re.IGNORECASE | re.DOTALL
        ):
            name = m.group(1).strip()
            size = self._parse_size(m.group(2).strip())
            if self._is_media_file(name) and name not in seen:
                seen.add(name)
                files.append({"name": name, "size": size, "url": ""})
        return files

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

    async def fetch_files(self, torrent_id: str, cookies: dict = None) -> list:
        effective_cookies = cookies if cookies is not None else self.cookies

        async with httpx.AsyncClient(proxy=get_proxy_url(), timeout=30) as client:
            # 1. Основная страница
            r = await client.get(f"{self.BASE_URL}/details.php?id={torrent_id}",
                                 headers=self._headers(), cookies=effective_cookies)
            self._log_response(f"details id={torrent_id}", r)

            if self._is_antibot_challenge(r):
                self._save_dump("details_challenge", r.text)
                await notify_expired_cookies("kinozal")
                raise KinozalCloudflareError(
                    "Анти-бот защита включена. Обновите cookies из браузера."
                )
            if r.status_code != 200:
                self._save_dump("details_error", r.text)
                raise KinozalError(f"HTTP {r.status_code} при загрузке details.php")

            # 2. Список файлов через AJAX
            details_url = f"{self.BASE_URL}/get_srv_details.php?id={torrent_id}&action=2"
            r = await client.get(details_url, headers={
                **self._headers(),
                "Referer": f"{self.BASE_URL}/details.php?id={torrent_id}",
                "X-Requested-With": "XMLHttpRequest",
            }, cookies=effective_cookies)
            self._log_response(f"ajax id={torrent_id}", r)

            if self._is_antibot_challenge(r):
                self._save_dump("ajax_challenge", r.text)
                await notify_expired_cookies("kinozal")
                raise KinozalCloudflareError(
                    "Анти-бот защита включена при получении списка файлов."
                )
            if r.status_code != 200:
                self._save_dump("ajax_error", r.text)
                raise KinozalError(f"HTTP {r.status_code} при получении списка файлов")

            try:
                ajax_text = r.content.decode("cp1251")
            except UnicodeDecodeError:
                ajax_text = r.text

            self._save_dump(f"{torrent_id}_ajax", ajax_text)
            files = self._parse_ajax_response(ajax_text)

            # Fallback: основная страница (HTML)
            if not files:
                try:
                    main_html = r.content.decode("cp1251")
                except UnicodeDecodeError:
                    main_html = r.text
                files = self._parse_html_files(main_html)

            print(f"[kinozal] id={torrent_id}: найдено файлов: {len(files)}")
            return files

    async def download_torrent(self, torrent_id: str, cookies: dict = None) -> bytes:
        effective_cookies = cookies if cookies is not None else self.cookies
        async with httpx.AsyncClient(proxy=get_proxy_url(), timeout=60) as client:
            r = await client.get(f"{self.BASE_URL}/download.php?id={torrent_id}",
                                 headers=self._headers(), cookies=effective_cookies, follow_redirects=True)
            self._log_response(f"download id={torrent_id}", r)

            if self._is_antibot_challenge(r):
                self._save_dump("download_challenge", r.text)
                await notify_expired_cookies("kinozal")
                raise KinozalCloudflareError(
                    "Анти-бот защита включена при скачивании. Обновите cookies из браузера."
                )

            content_type = r.headers.get("content-type", "")
            body_start = r.text[:15].lower() if r.text else ""
            if "text/html" in content_type or body_start.startswith(("<!doctype", "<html")):
                self._save_dump(f"{torrent_id}_download_got_html", r.text)
                if self.username and self.password:
                    try:
                        new_cookies = await self.login()
                        r = await client.get(f"{self.BASE_URL}/download.php?id={torrent_id}",
                                             headers=self._headers(), cookies=new_cookies, follow_redirects=True)
                        content_type = r.headers.get("content-type", "")
                        if "text/html" in content_type or r.text[:15].lower().startswith(("<!doctype", "<html")):
                            await notify_expired_cookies("kinozal")
                            raise KinozalForbiddenError(
                                "kinozal.me вернул HTML вместо торрента. Сессия не восстановилась. "
                                "Обновите cookies вручную."
                            )
                    except KinozalCloudflareError:
                        raise
                    except KinozalAuthError as e:
                        await notify_expired_cookies("kinozal")
                        raise KinozalForbiddenError(f"Не удалось перелогиниться: {e}")
                else:
                    await notify_expired_cookies("kinozal")
                    raise KinozalForbiddenError(
                        "kinozal.me вернул HTML вместо торрента. Сессия истекла. "
                        "Настройте логин/пароль или обновите cookies."
                    )

            if r.status_code != 200:
                await notify_expired_cookies("kinozal")
                raise KinozalError(f"HTTP {r.status_code} при скачивании торрента")

            return r.content