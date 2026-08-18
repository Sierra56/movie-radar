"""Kinozal.me tracker client."""
import re
import html as _html
import httpx
from urllib.parse import urlparse, urljoin


class KinozalError(Exception):
    pass


class KinozalAuthError(KinozalError):
    pass


class KinozalForbiddenError(KinozalError):
    pass


class KinozalClient:
    BASE_URL = "https://kinozal.me"

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

    def _headers(self, extra=None):
        h = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.9",
        }
        if extra:
            h.update(extra)
        return h

    @staticmethod
    def _normalize_url(url: str, base_url: str) -> str:
        """Исправляет protocol-relative URL (//kinozal.me/...) в абсолютный."""
        if url.startswith("//"):
            return "https:" + url
        if not url.startswith(("http://", "https://")):
            return urljoin(base_url, url)
        return url

    @staticmethod
    def _is_login_redirect(url: str) -> bool:
        return "login.php" in url

    @staticmethod
    def _is_torrent(data: bytes) -> bool:
        return bool(data) and data[:1] == b"d" and \
            (b"announce" in data[:1000] or b"info" in data[:1000])

    async def validate_cookies(self, cookies: dict) -> tuple[bool, str]:
        try:
            kw = self._client_kwargs()
            async with httpx.AsyncClient(**kw) as client:
                r = await client.get(self.BASE_URL + "/my.php",
                                     cookies=cookies, headers=self._headers())
                if r.status_code == 200 and "loginform" not in r.text[:2000].lower():
                    return True, "cookies валидны"
                return False, "cookies невалидны (перенаправлено на логин)"
        except Exception as e:
            return False, f"ошибка: {e}"

    async def login(self) -> dict:
        if not self.username or not self.password:
            raise KinozalAuthError("не указаны логин/пароль")
        kw = self._client_kwargs()
        async with httpx.AsyncClient(**kw) as client:
            # Первым делом загружаем главную, чтобы получить начальные cookies
            await client.get(self.BASE_URL, headers=self._headers())

            r = await client.post(
                f"{self.BASE_URL}/takelogin.php",
                data={
                    "username": self.username,
                    "password": self.password,
                    "returnto": "/",
                },
                headers=self._headers({
                    "Referer": f"{self.BASE_URL}/login.php",
                    "Content-Type": "application/x-www-form-urlencoded",
                }))
            if "loginform" in r.text[:2000].lower() and r.status_code == 200:
                raise KinozalAuthError("неверный логин/пароль")
            # Возвращаем все cookies, которые накопил клиент (включая начальные)
            return dict(client.cookies)

    async def fetch_files(self, torrent_id: str, cookies: dict) -> list[dict]:
        url = f"{self.BASE_URL}/details.php?id={torrent_id}"
        kw = self._client_kwargs()
        async with httpx.AsyncClient(**kw) as client:
            r = await client.get(url, cookies=cookies, headers=self._headers())
            if r.status_code == 403:
                raise KinozalForbiddenError("403 Forbidden — обновите cookies")
            if r.status_code != 200:
                raise KinozalError(f"HTTP {r.status_code}")
            html = r.text
            try:
                with open("/data/debug_kinozal.html", "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception:
                pass

            files = []
            size_map = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3,
                        "КБ": 1024, "МБ": 1024**2, "ГБ": 1024**3}
            filelist_match = re.search(
                r'<(?:div|td)[^>]*class=["\'][^"\']*filelist[^"\']*["\'][^>]*>(.*?)</(?:div|td)>',
                html, re.DOTALL | re.IGNORECASE)
            table_html = filelist_match.group(1) if filelist_match else html
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL | re.IGNORECASE)
            for row in rows:
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
                if len(cells) < 2:
                    continue
                name = re.sub(r'<[^>]+>', '', cells[0]).strip()
                size_raw = re.sub(r'<[^>]+>', '', cells[1]).strip()
                if not name or name.startswith("№") or len(name) < 3:
                    continue
                size_bytes = 0
                m = re.search(r'([\d.,]+)\s*([A-Za-zА-Яа-я]{1,3})', size_raw)
                if m:
                    try:
                        size_bytes = int(float(m.group(1).replace(",", ".")) *
                                         size_map.get(m.group(2).upper(), 1))
                    except ValueError:
                        size_bytes = 0
                files.append({"name": name, "size": size_bytes})
            if not files:
                t = re.search(r'<title>([^<]+)</title>', html)
                if t:
                    files.append({"name": t.group(1).replace(" :: Кинозал", "").strip(), "size": 0})
            return files

    async def _try_download(self, client: httpx.AsyncClient,
                            torrent_id: str, cookies: dict,
                            method: str, use_post: bool) -> tuple[bytes | None, str]:
        """Пытается скачать .torrent. Возвращает (data, message)."""
        url = f"{self.BASE_URL}/download.php?id={torrent_id}"

        # Первый запрос: без follow_redirects, чтобы увидеть Location
        if use_post:
            r = await client.post(url, cookies=cookies, follow_redirects=False,
                                  headers=self._headers({
                                      "Referer": f"{self.BASE_URL}/details.php?id={torrent_id}"}))
        else:
            r = await client.get(url, cookies=cookies, follow_redirects=False,
                                 headers=self._headers({
                                     "Referer": f"{self.BASE_URL}/details.php?id={torrent_id}"}))

        # Обработка редиректа вручную
        if r.status_code in (301, 302, 303, 307):
            loc = r.headers.get("location", "")
            loc = self._normalize_url(loc, self.BASE_URL)
            print(f"[kinozal] download.php {method} -> {r.status_code} Location: {loc}")

            if self._is_login_redirect(loc):
                return None, "redirect_to_login"

            r2 = await client.get(loc, cookies=cookies, headers=self._headers())
            if self._is_torrent(r2.content):
                return r2.content, "torrent_via_redirect"
            return None, "redirect_not_torrent"

        if r.status_code == 200 and self._is_torrent(r.content):
            return r.content, "torrent_direct"

        return None, f"unexpected_status_{r.status_code}"

    async def download_torrent(self, torrent_id: str, cookies: dict):
        """Возвращает .torrent (bytes) или magnet-ссылку (str) как fallback."""
        kw = self._client_kwargs()
        async with httpx.AsyncClient(**kw) as client:
            # 1) GET
            data, msg = await self._try_download(client, torrent_id, cookies, "GET", use_post=False)
            if data:
                print(f"[kinozal] torrent downloaded: {msg}")
                return data

            # 2) POST (некоторые трекеры требуют)
            if msg == "redirect_to_login":
                # Cookies невалидны для скачивания — не пробуем POST, идём в magnet
                pass
            else:
                data, msg = await self._try_download(client, torrent_id, cookies, "POST", use_post=True)
                if data:
                    print(f"[kinozal] torrent downloaded: {msg}")
                    return data

            # 3) Fallback: magnet со страницы раздачи
            page = await client.get(f"{self.BASE_URL}/details.php?id={torrent_id}",
                                    cookies=cookies, headers=self._headers())
            text = _html.unescape(page.text)
            m = re.search(r'(magnet:\?[^"\'\s<>]+)', text)
            if m:
                magnet = m.group(1)
                print(f"[kinozal] using magnet fallback: {magnet[:80]}...")
                return magnet

            # Если и magnet не найден — пробуем получить его через API (info_hash)
            # kinozal не имеет публичного API, поэтому ищем на странице
            m2 = re.search(r'info_hash=([a-fA-F0-9]{40})', text)
            if m2:
                info_hash = m2.group(1).lower()
                display_name = ""
                t = re.search(r'<title>([^<]+)</title>', text)
                if t:
                    display_name = _html.unescape(
                        t.group(1).replace(" :: Кинозал", "").strip())
                magnet = (
                    f"magnet:?xt=urn:btih:{info_hash}"
                    f"&dn={quote(display_name, safe='')}"
                    f"&tr=udp%3A%2F%2Fbt.kinozal.me%3A2710"
                    f"&tr=http%3A%2F%2Fbt.kinozal.me%3A2710%2Fannounce"
                    f"&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce"
                    f"&tr=udp%3A%2F%2Fopen.tracker.clash.me%3A1337%2Fannounce"
                )
                print(f"[kinozal] constructed magnet from info_hash: {magnet[:80]}...")
                return magnet

            try:
                with open("/data/debug_kinozal_torrent.html", "wb") as f:
                    f.write(page.content)
            except Exception:
                pass

            if msg == "redirect_to_login":
                raise KinozalError(
                    "Cookies невалидны для скачивания — "
                    "скопируйте cookies из браузера в настройках кинозала")
            raise KinozalError(
                "kinozal не отдал .torrent и magnet не найден "
                "(дамп: /data/debug_kinozal_torrent.html)")


def quote(s, safe=""):
    from urllib.parse import quote as _q
    return _q(s, safe=safe)