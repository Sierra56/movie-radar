"""Kinozal.me tracker client."""
import re
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
                        num = float(m.group(1).replace(",", "."))
                        size_bytes = int(num * size_map.get(m.group(2).upper(), 1))
                    except ValueError:
                        size_bytes = 0
                files.append({"name": name, "size": size_bytes})

            if not files:
                title_match = re.search(r'<title>([^<]+)</title>', html)
                if title_match:
                    files.append({"name": title_match.group(1).replace(" :: Кинозал", "").strip(), "size": 0})
            return files

    async def download_torrent(self, torrent_id: str, cookies: dict) -> bytes:
        """Скачивает .torrent файл. Валидирует, что это bencoded-данные."""
        url = f"{self.BASE_URL}/download.php?id={torrent_id}"
        kw = self._client_kwargs()
        async with httpx.AsyncClient(**kw) as client:
            r = await client.get(
                url, cookies=cookies,
                headers=self._headers({
                    "Referer": f"{self.BASE_URL}/details.php?id={torrent_id}",
                }))
            if r.status_code != 200:
                raise KinozalError(f"download HTTP {r.status_code}")
            data = r.content
            if not data:
                raise KinozalError("пустой ответ .torrent")

            # Валидный .torrent — bencoded-словарь: начинается с 'd' и содержит announce/info
            is_torrent = (data[:1] == b"d" and
                          (b"announce" in data[:1000] or b"info" in data[:1000]))
            if not is_torrent:
                try:
                    with open("/data/debug_kinozal_torrent.html", "wb") as f:
                        f.write(data)
                except Exception:
                    pass
                raise KinozalError(
                    "kinozal вернул не .torrent файл (сохранено в "
                    "/data/debug_kinozal_torrent.html). Проверьте дамп.")
            return data