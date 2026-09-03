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
    """Cloudflare включил защиту — нужен ручной обновление cookies из браузера."""
    pass


def parse_cookie_string(cookie_str: str) -> dict:
    """Надёжный парсер строки cookies, скопированной из браузера.

    Корректно обрабатывает:
      - значения, содержащие '=' внутри (например, cf_clearance);
      - произвольное число пар (uid, pass, cf_clearance, __cf_bm, ...);
      - лишние пробелы и пустые фрагменты.
    """
    cookies = {}
    if not cookie_str:
        return cookies
    for item in cookie_str.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, _, value = item.partition("=")  # разбиваем только по ПЕРВОМУ '='
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
        # cookies могут прийти как dict или как сырая строка из браузера
        if isinstance(cookies, str):
            self.cookies = parse_cookie_string(cookies)
        else:
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
            # передаём все cookies как есть, включая cf_clearance
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return h

    def _save_dump(self, torrent_id: str, suffix: str, content: str):
        try:
            path = f"/data/debug_kinozal_{torrent_id}_{suffix}.html"
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            print(f"[kinozal] не удалось сохранить дамп: {e}")

    def _is_cloudflare_challenge(self, r) -> bool:
        """Определяет, вернул ли kinozal страницу-челлендж Cloudflare."""
        # Явные заголовки Cloudflare
        if (r.headers.get("cf-mitigated") or "").lower() == "challenge":
            return True
        server = (r.headers.get("server") or "").lower()
        # Страница челленджа обычно приходит со статусом 403/503
        if r.status_code in (403, 503) and server == "cloudflare":
            return True
        # Проверка содержимого
        try:
            text = (r.text or "")[:3000].lower()
        except Exception:
            text = ""
        markers = (
            "just a moment",
            "cf-chl",
            "challenge-platform",
            "checking your browser",
            "attention required",
            "cf-browser-verification",
            "enable javascript and cookies",
            "ray id",
        )
        return any(m in text for m in markers)

    async def login(self) -> dict:
        if not self.username or not self.password:
            raise KinozalAuthError("Не указаны логин/пароль для kinozal.me")
        async with httpx.AsyncClient(proxy=get_proxy_url(), timeout=30, follow_redirects=True) as client:
            r = await client.post(f"{self.BASE_URL}/takelogin.php", data={
                "username": self.username, "password": self.password, "returnto": ""
            }, headers={"User-Agent": self.user_agent})
            if self._is_cloudflare_challenge(r):
                raise KinozalCloudflareError(
                    "Cloudflare заблокировал вход. Обновите cookies из браузера."
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
            if self._is_cloudflare_challenge(r):
                await notify_expired_cookies("kinozal")
                return False, "Cloudflare включил защиту — обновите cookies из браузера"
            if r.status_code == 200 and "my.php" in str(r.url):
                return True, None
            await notify_expired_cookies("kinozal")
            return False, f"HTTP {r.status_code}, redirect to {r.url}"

    @staticmethod
    def _is_media_file(name: str) -> bool:
        lower = name.lower()
        return any(lower.endswith(ext) for ext in (
            '.mkv', '.mp4', '.avi', '.ts', '.m2ts', '.srt', '.sub', '.idx', '.nfo', '.jpg', '.png'
        ))

    def _parse_ajax_response(self, text: str) -> list:
        """Парсит AJAX-ответ kinozal (plain text, НЕ HTML).

        Формат:
          Инфо хеш: EBE5D2DB...
          Размер части торрента: 8 МБ
          The.Walking.Dead.2026.S03.WEB-DL.1080p
          The.Walking.Dead.2026.S03E01.mkv 2.82 ГБ (3027386396)
          ...

        Размер берём из скобок (байты) — не зависит от кодировки.
        Имена файлов — на латинице, устойчивы к проблемам cp1251/UTF-8.
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
        """Запасной парсер на случай, если kinozal отдаёт список файлов как HTML."""
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
            # 1. Проверяем основную страницу (сессия + Cloudflare)
            r = await client.get(f"{self.BASE_URL}/details.php?id={torrent_id}",
                                 headers=self._headers(), cookies=effective_cookies)
            if self._is_cloudflare_challenge(r):
                await notify_expired_cookies("kinozal")
                raise KinozalCloudflareError(
                    "Cloudflare включил защиту. Обновите cookies из браузера."
                )
            if r.status_code == 403:
                await notify_expired_cookies("kinozal")
                raise KinozalForbiddenError("403 Forbidden — обновите cookies kinozal.me")
            if r.status_code != 200:
                raise KinozalError(f"HTTP {r.status_code} при загрузке details.php")

            # 2. Список файлов через AJAX-эндпоинт (отдаёт plain text)
            details_url = f"{self.BASE_URL}/get_srv_details.php?id={torrent_id}&action=2"
            r = await client.get(details_url, headers={
                **self._headers(),
                "Referer": f"{self.BASE_URL}/details.php?id={torrent_id}",
                "X-Requested-With": "XMLHttpRequest",
            }, cookies=effective_cookies)

            if self._is_cloudflare_challenge(r):
                await notify_expired_cookies("kinozal")
                raise KinozalCloudflareError(
                    "Cloudflare включил защиту при получении списка файлов."
                )
            if r.status_code == 403:
                await notify_expired_cookies("kinozal")
                raise KinozalForbiddenError("403 Forbidden при получении списка файлов")
            if r.status_code != 200:
                raise KinozalError(f"HTTP {r.status_code} при получении списка файлов")

            # Декодируем как cp1251; при неудаче — fallback на стандартный текст.
            try:
                ajax_text = r.content.decode("cp1251")
            except UnicodeDecodeError:
                ajax_text = r.text

            self._save_dump(torrent_id, "ajax", ajax_text)
            files = self._parse_ajax_response(ajax_text)

            # Fallback: если AJAX ничего не дал — пробуем основную страницу (HTML)
            if not files:
                r_main = await client.get(f"{self.BASE_URL}/details.php?id={torrent_id}",
                                          headers=self._headers(), cookies=effective_cookies)
                try:
                    main_html = r_main.content.decode("cp1251")
                except UnicodeDecodeError:
                    main_html = r_main.text
                self._save_dump(torrent_id, "main", main_html)
                files = self._parse_html_files(main_html)

            return files

    async def download_torrent(self, torrent_id: str, cookies: dict = None) -> bytes:
        effective_cookies = cookies if cookies is not None else self.cookies
        async with httpx.AsyncClient(proxy=get_proxy_url(), timeout=60) as client:
            r = await client.get(f"{self.BASE_URL}/download.php?id={torrent_id}",
                                 headers=self._headers(), cookies=effective_cookies, follow_redirects=True)

            # Cloudflare-челлендж: автологин не поможет, сразу уведомляем
            if self._is_cloudflare_challenge(r):
                await notify_expired_cookies("kinozal")
                raise KinozalCloudflareError(
                    "Cloudflare включил защиту при скачивании. Обновите cookies из браузера."
                )

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
                            await notify_expired_cookies("kinozal")
                            raise KinozalForbiddenError(
                                "kinozal.me вернул HTML вместо торрента. Сессия не восстановилась. "
                                "Обновите cookies вручную."
                            )
                    except KinozalCloudflareError:
                        await notify_expired_cookies("kinozal")
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

            if r.status_code == 403:
                await notify_expired_cookies("kinozal")
                raise KinozalForbiddenError("403 Forbidden при скачивании торрента")
            if r.status_code != 200:
                raise KinozalError(f"HTTP {r.status_code} при скачивании торрента")

            return r.content