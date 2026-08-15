import re
import os
import httpx
from bs4 import BeautifulSoup

LOGIN_URL = "https://rutracker.org/forum/login.php"
INDEX_URL = "https://rutracker.org/forum/index.php"
TOPIC_URL = "https://rutracker.org/forum/viewtopic.php"

DB_PATH = os.getenv("DB_PATH", "/data/catalog.db")
DEBUG_DUMP_PATH = os.path.join(os.path.dirname(DB_PATH), "debug_last_topic.html")

SIZE_UNITS = {
    "B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4,
    "Б": 1, "КБ": 1024, "МБ": 1024 ** 2, "ГБ": 1024 ** 3, "ТБ": 1024 ** 4,
}

_SIZE_RE = re.compile(r"([\d.,]+)\s*(Б|КБ|МБ|ГБ|ТБ|B|KB|MB|GB|TB)", re.I)

_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;"
               "q=0.9,image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}


class RuTrackerError(Exception):
    pass


class RuTrackerCaptchaError(RuTrackerError):
    pass


class RuTrackerAuthError(RuTrackerError):
    pass


def parse_size(text: str) -> int:
    m = _SIZE_RE.search(text or "")
    if not m:
        return 0
    num = float(m.group(1).replace(",", "."))
    unit = m.group(2).upper()
    return int(num * SIZE_UNITS.get(unit, 1))


def parse_files(html: str) -> list:
    """Extract list of {name, size} from a rutracker topic page.

    Heuristic: pick the table with the most rows whose last cell looks like
    a file size. Returns [] if nothing found.
    """
    soup = BeautifulSoup(html, "html.parser")
    best_table = None
    best_count = 0

    for table in soup.find_all("table"):
        count = 0
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) >= 2 and _SIZE_RE.search(cells[-1].get_text(strip=True)):
                count += 1
        if count > best_count:
            best_count = count
            best_table = table

    if not best_table:
        return []

    files = []
    for row in best_table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) >= 2:
            size_text = cells[-1].get_text(strip=True)
            if _SIZE_RE.search(size_text):
                name = cells[-2].get_text(" ", strip=True)
                if name:
                    files.append({"name": name, "size": parse_size(size_text)})
    return files


def save_debug_dump(html: str):
    try:
        os.makedirs(os.path.dirname(DEBUG_DUMP_PATH), exist_ok=True)
        with open(DEBUG_DUMP_PATH, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[rutracker] Debug dump saved to {DEBUG_DUMP_PATH}")
    except Exception as e:
        print(f"[rutracker] Failed to save debug dump: {e}")


class RuTrackerClient:
    def __init__(self, username: str, password: str, proxy: str | None = None):
        self.username = username
        self.password = password
        self.proxy = proxy

    def _client(self, cookies: dict | None = None) -> httpx.AsyncClient:
        kwargs = {"timeout": 25, "follow_redirects": True}
        if self.proxy:
            kwargs["proxy"] = proxy
        client = httpx.AsyncClient(**kwargs)
        if cookies:
            client.cookies = dict(cookies)
        return client

    async def login(self) -> dict:
        """Login and return cookies dict. Raises on captcha / auth / network failure."""
        try:
            async with self._client() as client:
                # Предварительный GET главной — получаем базовые cookies
                await client.get(INDEX_URL, headers=_BROWSER_HEADERS)

                r = await client.post(
                    LOGIN_URL,
                    data={
                        "login_username": self.username,
                        "login_password": self.password,
                        "login": "Вход",
                    },
                    headers={
                        **_BROWSER_HEADERS,
                        "Referer": INDEX_URL,
                        "Origin": "https://rutracker.org",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                print(f"[rutracker] Login status={r.status_code} final_url={r.url}")

                low = r.text.lower()
                if "капча" in low or "captcha" in low:
                    save_debug_dump(r.text)
                    raise RuTrackerCaptchaError("Rutracker требует капчу")

                session = client.cookies.get("bb_session")
                if not session:
                    save_debug_dump(r.text)
                    raise RuTrackerAuthError(
                        "Не удалось войти: нет cookie bb_session "
                        "(проверьте логин/пароль, дамп сохранён)")

                print(f"[rutracker] Login OK, cookies: {list(client.cookies.keys())}")
                return dict(client.cookies)
        except httpx.HTTPError as e:
            # Сетевые ошибки (таймаут, прокси недоступен, DNS) -> RuTrackerError
            raise RuTrackerError(f"Сетевая ошибка при входе: {e}")

    async def fetch_files(self, torrent_id: str, cookies: dict | None = None) -> list:
        """Fetch topic page and parse file list. Returns [] if parse fails
        (a debug dump is saved in that case)."""
        try:
            async with self._client(cookies) as client:
                r = await client.get(TOPIC_URL, params={"t": torrent_id},
                                     headers=_BROWSER_HEADERS)
                if r.status_code == 404:
                    raise RuTrackerError("Раздача не найдена (404)")
                r.raise_for_status()
                files = parse_files(r.text)
                if not files:
                    save_debug_dump(r.text)
                return files
        except httpx.HTTPError as e:
            raise RuTrackerError(f"Сетевая ошибка при получении раздачи: {e}")