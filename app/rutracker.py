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
               "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="8"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
}

_POST_HEADERS = {
    **_BROWSER_HEADERS,
    "Content-Type": "application/x-www-form-urlencoded",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
}


class RuTrackerError(Exception):
    pass


class RuTrackerCaptchaError(RuTrackerError):
    pass


class RuTrackerAuthError(RuTrackerError):
    pass


class RuTrackerForbiddenError(RuTrackerError):
    pass


def parse_size(text: str) -> int:
    m = _SIZE_RE.search(text or "")
    if not m:
        return 0
    num = float(m.group(1).replace(",", "."))
    unit = m.group(2).upper()
    return int(num * SIZE_UNITS.get(unit, 1))


def parse_files(html: str) -> list:
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


def _extract_form_fields(html: str, action_contains: str = "login") -> dict:
    soup = BeautifulSoup(html, "html.parser")
    result = {}

    for form in soup.find_all("form"):
        action = form.get("action", "")
        if action_contains not in action:
            continue
        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            itype = (inp.get("type") or "").lower()
            if itype in ("hidden", "checkbox"):
                value = inp.get("value", "")
                result[name] = value
            elif itype == "submit":
                result[name] = inp.get("value", "")
        for sel in form.find_all("select"):
            name = sel.get("name")
            if name:
                opt = sel.find("option", selected=True) or sel.find("option")
                if opt:
                    result[name] = opt.get("value", opt.get_text(strip=True))

    return result


class RuTrackerClient:
    def __init__(self, username: str = "", password: str = "",
                 proxy: str | None = None, cookies: dict | None = None):
        self.username = username
        self.password = password
        self.proxy = proxy
        self.initial_cookies = cookies or {}

    def _client(self, cookies: dict | None = None) -> httpx.AsyncClient:
        kwargs = {"timeout": 30, "follow_redirects": True}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        client = httpx.AsyncClient(**kwargs)
        # Используем переданные cookies или initial
        effective_cookies = cookies or self.initial_cookies
        if effective_cookies:
            client.cookies = dict(effective_cookies)
        return client

    async def validate_cookies(self, cookies: dict) -> bool:
        """Проверяет валидность cookies без попытки логина.
        Делает GET на index.php и проверяет наличие признаков авторизации."""
        if not cookies:
            return False
        print(f"[rutracker] Validating cookies: keys={list(cookies.keys())}")
        try:
            async with self._client(cookies) as client:
                r = await client.get(INDEX_URL, headers=_BROWSER_HEADERS)
                print(f"[rutracker]   index status={r.status_code}")

                if r.status_code == 403:
                    print("[rutracker]   403 Forbidden (Cloudflare)")
                    return False

                low = r.text.lower()
                # Cloudflare challenge
                if "just a moment" in low or "cloudflare" in low and "challenge" in low:
                    print("[rutracker]   Cloudflare challenge detected")
                    return False

                # Проверяем признаки авторизации
                # У залогиненного пользователя есть ссылка на выход (logout) или профиль
                # У не залогиненного — ссылка на вход (login.php)
                soup = BeautifulSoup(r.text, "html.parser")

                # Ищем кнопку/ссылку "Выйти" или "logout"
                logout_indicators = ["logout", "выйти", "выход"]
                has_logout = any(ind in low for ind in logout_indicators)

                # Ищем username в HTML (он обычно показывается в шапке)
                username_in_page = self.username.lower() in low if self.username else False

                # Если есть logout или username на странице — cookies валидны
                if has_logout or username_in_page:
                    print(f"[rutracker]   Cookies valid (logout={has_logout}, username_in_page={username_in_page})")
                    return True

                print("[rutracker]   Cookies appear invalid (no logout indicator found)")
                return False
        except httpx.HTTPError as e:
            print(f"[rutracker]   Network error: {e}")
            return False

    async def login(self) -> dict:
        """Вход через логин/пароль. Используется как fallback."""
        if not self.username or not self.password:
            raise RuTrackerAuthError("Логин и пароль не указаны")

        print(f"[rutracker] Login attempt: user={self.username}, proxy={self.proxy or 'none'}")
        try:
            async with self._client() as client:
                print("[rutracker] Step 1: GET index.php to prime cookies")
                try:
                    r_idx = await client.get(INDEX_URL, headers=_BROWSER_HEADERS)
                    print(f"[rutracker]   index status={r_idx.status_code}")
                except Exception as e:
                    print(f"[rutracker]   index failed: {e}")

                print("[rutracker] Step 2: GET login.php (форма логина)")
                try:
                    r_form = await client.get(
                        LOGIN_URL,
                        headers={**_BROWSER_HEADERS, "Referer": INDEX_URL},
                    )
                    print(f"[rutracker]   login form status={r_form.status_code}")
                    form_fields = _extract_form_fields(r_form.text, "login")
                    print(f"[rutracker]   extracted form fields: {list(form_fields.keys())}")
                except Exception as e:
                    print(f"[rutracker]   login form failed: {e}")
                    form_fields = {}

                print("[rutracker] Step 3: POST login")
                post_data = {
                    **form_fields,
                    "login_username": self.username,
                    "login_password": self.password,
                    "login": "Вход",
                }
                if "autologin" not in post_data:
                    post_data["autologin"] = "1"

                r = await client.post(
                    LOGIN_URL,
                    data=post_data,
                    headers={
                        **_POST_HEADERS,
                        "Referer": LOGIN_URL,
                        "Origin": "https://rutracker.org",
                    },
                )
                print(f"[rutracker]   login POST status={r.status_code} url={r.url}")

                if r.status_code == 403:
                    save_debug_dump(r.text)
                    raise RuTrackerForbiddenError(
                        "Rutracker вернул 403. Используйте ручной ввод cookies.")

                low = r.text.lower()
                if "капча" in low or "captcha" in low:
                    save_debug_dump(r.text)
                    raise RuTrackerCaptchaError("Rutracker требует капчу")

                session = client.cookies.get("bb_session")
                bb_data = client.cookies.get("bb_data")
                print(f"[rutracker]   cookies after login: {list(client.cookies.keys())}")
                print(f"[rutracker]   bb_session={bool(session)}, bb_data={bool(bb_data)}")

                if not session and not bb_data:
                    soup = BeautifulSoup(r.text, "html.parser")
                    error_div = (soup.find(class_="warnColor1") or
                                 soup.find(class_="error") or
                                 soup.find("p", class_="error"))
                    err_text = error_div.get_text(strip=True) if error_div else ""
                    save_debug_dump(r.text)
                    msg = (f"Нет cookie сессии. "
                           f"{err_text or '(страница сохранена в debug_last_topic.html)'}")
                    raise RuTrackerAuthError(msg)

                print("[rutracker] Login OK")
                return dict(client.cookies)
        except httpx.HTTPError as e:
            print(f"[rutracker] Network error: {type(e).__name__}: {e}")
            raise RuTrackerError(f"Сетевая ошибка при входе: {e}")

    async def fetch_files(self, torrent_id: str, cookies: dict | None = None) -> list:
        try:
            effective_cookies = cookies or self.initial_cookies
            async with self._client(effective_cookies) as client:
                r = await client.get(
                    TOPIC_URL,
                    params={"t": torrent_id},
                    headers={**_BROWSER_HEADERS, "Referer": INDEX_URL},
                )
                if r.status_code == 404:
                    raise RuTrackerError("Раздача не найдена (404)")
                if r.status_code == 403:
                    save_debug_dump(r.text)
                    raise RuTrackerForbiddenError(
                        "Доступ к раздаче запрещён (403). Возможно, истекли cookies.")
                r.raise_for_status()
                files = parse_files(r.text)
                if not files:
                    save_debug_dump(r.text)
                return files
        except httpx.HTTPError as e:
            raise RuTrackerError(f"Сетевая ошибка при получении раздачи: {e}")