"""Kinozal.me tracker client."""
import re
import html as _html
import httpx


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
            r = await client.post(f"{self.BASE_URL}/takelogin.php",
                                  data={"username": self.username, "password": self.password,
                                        "returnto": "/"},
                                  headers=self._headers())
            if "loginform" in r.text[:2000].lower():
                raise KinozalAuthError("неверный логин/пароль")
            return dict(r.cookies)

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

    async def download_torrent(self, torrent_id: str, cookies: dict):
        """Возвращает .torrent (bytes) или magnet-ссылку (str) как fallback."""
        url = f"{self.BASE_URL}/download.php?id={torrent_id}"
        kw = self._client_kwargs()
        async with httpx.AsyncClient(**kw) as client:
            # 1) пробуем без следования редиректам, чтобы увидеть Location
            r = await client.get(url, cookies=cookies, follow_redirects=False,
                                 headers=self._headers({
                                     "Referer": f"{self.BASE_URL}/details.php?id={torrent_id}"}))
            if r.status_code in (301, 302):
                loc = r.headers.get("location", "")
                print(f"[kinozal] download.php -> {r.status_code} Location: {loc}")
                if loc:
                    r2 = await client.get(loc, cookies=cookies, headers=self._headers())
                    if self._is_torrent(r2.content):
                        return r2.content
                    r = r2
            elif r.status_code == 200 and self._is_torrent(r.content):
                return r.content

            # 2) прямой запрос со следованием редиректов
            r3 = await client.get(url, cookies=cookies, headers=self._headers({
                "Referer": f"{self.BASE_URL}/details.php?id={torrent_id}"}))
            if self._is_torrent(r3.content):
                return r3.content

            # 3) fallback: ищем magnet на странице раздачи
            page = await client.get(f"{self.BASE_URL}/details.php?id={torrent_id}",
                                    cookies=cookies, headers=self._headers())
            text = _html.unescape(page.text)
            m = re.search(r'(magnet:\?[^"\'\s<]+)', text)
            if m:
                magnet = m.group(1)
                print(f"[kinozal] using magnet fallback: {magnet[:60]}...")
                return magnet

            try:
                with open("/data/debug_kinozal_torrent.html", "wb") as f:
                    f.write(page.content)
            except Exception:
                pass
            raise KinozalError(
                "kinozal не отдал .torrent и magnet не найден "
                "(дамп: /data/debug_kinozal_torrent.html)")