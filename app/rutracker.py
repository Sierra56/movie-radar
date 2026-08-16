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

_SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(Б|КБ|МБ|ГБ|ТБ|B|KB|MB|GB|TB)", re.I)


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
    try:
        num = float(m.group(1).replace(",", "."))
        unit = m.group(2).upper()
        return int(num * SIZE_UNITS.get(unit, 1))
    except (ValueError, OverflowError) as e:
        print(f"[rutracker] parse_size failed for {text!r}: {e}")
        return 0


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
                size = parse_size(size_text)
                if name and size > 0:
                    files.append({"name": name, "size": size})
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
                result[name] = inp.get("value", "")
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
                 proxy: str | None = None, cookies: dict | None = None,
                 user_agent: str | None = None):
        self.username = username
        self.password = password
        self.proxy = proxy
        self.initial_cookies = cookies or {}
        self.user_agent = (user_agent or "").strip() or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        )

    def _headers(self, extra: dict | None = None) -> dict:
        h = {
            "User-Agent": self.user_agent,
            "Accept": ("text/html,application/xhtml+xml,application/xml;"
                       "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"),
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="8"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Upgrade-Insecure-Requests": "1",
        }
        if extra:
            h.update(extra)
        return h

    def _post_headers(self) -> dict:
        return self._headers({
            "Content-Type": "application/x-www-form-urlencoded",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
            "sec-fetch-user": "?1",
        })

    def _client(self, cookies: dict | None = None) -> httpx.AsyncClient:
        kwargs = {"timeout": 30, "follow_redirects": True}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        client = httpx.AsyncClient(**kwargs)
        effective_cookies = cookies or self.initial_cookies
        if effective_cookies:
            client.cookies = dict(effective_cookies)
        return client

    async def validate_cookies(self, cookies: dict) -> tuple:
        """Проверяет валидность cookies. Возвращает (ok, reason)."""
        if not cookies:
            return False, "cookies не заданы"
        print(f"[rutracker] Validating cookies: keys={list(cookies.keys())}")
        print(f"[rutracker] User-Agent: {self.user_agent[:80]}")
        try:
            async with self._client(cookies) as client:
                r = await client.get(INDEX_URL, headers=self._headers())
                print(f"[rutracker]   index status={r.status_code}, len={len(r.text)}")

                if r.status_code == 403:
                    save_debug_dump(r.text)
                    return False, ("403 Forbidden: Cloudflare отклонил запрос. "
                                   "Проверьте: 1) User-Agent в точности как в браузере; "
                                   "2) браузер ходит через тот же прокси, что и приложение.")

                low = r.text.lower()
                if "just a moment" in low or "cf_chl" in low:
                    save_debug_dump(r.text)
                    return False, ("Cloudflare challenge (Just a moment). "
                                   "Обновите cf_clearance и User-Agent из браузера.")

                soup = BeautifulSoup(r.text, "html.parser")
                title_tag = soup.find("title")
                print(f"[rutracker]   page title: "
                      f"{title_tag.get_text(strip=True) if title_tag else '-'}")

                has_login_form = ("login_username" in low) or ("login_password" in low)
                logout_indicators = ["logout", "выйти", "выход"]
                has_logout = any(ind in low for ind in logout_indicators)
                username_in_page = self.username.lower() in low if self.username else False

                print(f"[rutracker]   has_login_form={has_login_form}, "
                      f"has_logout={has_logout}, username_in_page={username_in_page}")

                if has_logout or username_in_page:
                    print("[rutracker]   Cookies valid")
                    return True, "ok"

                if has_login_form:
                    save_debug_dump(r.text)
                    return False, ("Страница открылась как ГОСТЬ (видна форма входа). "
                                   "Проверьте: 1) cookies скопированы одной строкой БЕЗ опечаток; "
                                   "2) заполнено поле User-Agent.")

                save_debug_dump(r.text)
                return False, ("Не удалось определить статус сессии. "
                               "Debug-дамп: /data/debug_last_topic.html")
        except httpx.HTTPError as e:
            print(f"[rutracker]   Network error: {e}")
            return False, f"сетевая ошибка: {e}"

    async def login(self) -> dict:
        if not self.username or not self.password:
            raise RuTrackerAuthError("Логин и пароль не указаны")

        print(f"[rutracker] Login attempt: user={self.username}, proxy={self.proxy or 'none'}")
        try:
            async with self._client() as client:
                print("[rutracker] Step 1: GET index.php to prime cookies")
                try:
                    r_idx = await client.get(INDEX_URL, headers=self._headers())
                    print(f"[rutracker]   index status={r_idx.status_code}")
                except Exception as e:
                    print(f"[rutracker]   index failed: {e}")

                print("[rutracker] Step 2: GET login.php (форма логина)")
                try:
                    r_form = await client.get(
                        LOGIN_URL,
                        headers=self._headers({"Referer": INDEX_URL}),
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
                    headers={**self._post_headers(),
                             "Referer": LOGIN_URL,
                             "Origin": "https://rutracker.org"},
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
                    headers=self._headers({"Referer": INDEX_URL}),
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

    async def download_torrent(self, torrent_id: str, cookies: dict = None) -> bytes:
        """Скачивает .torrent файл с rutracker."""
        download_url = "https://rutracker.org/forum/dl.php"
        
        try:
            effective_cookies = cookies or self.initial_cookies
            async with self._client(effective_cookies) as client:
                print(f"[rutracker] Downloading .torrent for t={torrent_id}")
                
                r = await client.get(
                    download_url,
                    params={"t": torrent_id},
                    headers=self._headers({
                        "Referer": f"{TOPIC_URL}?t={torrent_id}",
                        "sec-fetch-dest": "document",
                        "sec-fetch-mode": "navigate",
                        "sec-fetch-site": "same-origin",
                    })
                )
                
                print(f"[rutracker]   dl.php status={r.status_code}, "
                      f"content-type={r.headers.get('content-type', 'N/A')}, "
                      f"len={len(r.content)}")
                
                if r.status_code == 404:
                    raise RuTrackerError("Торрент-файл не найден (404)")
                if r.status_code == 403:
                    save_debug_dump(r.text)
                    raise RuTrackerForbiddenError(
                        "Доступ к торрент-файлу запрещён (403). Обновите cookies.")
                
                r.raise_for_status()
                
                # Проверяем известные текстовые ошибки rutracker
                low = r.text.lower()
                if "attachment data not found" in low:
                    raise RuTrackerError(
                        "Торрент-файл недоступен на сервере rutracker "
                        "(attachment data not found). "
                        "Возможно, раздача удалена или у вас нет прав на скачивание. "
                        "Проверьте раздачу в браузере вручную.")
                
                if "login" in low and ("username" in low or "пароль" in low):
                    raise RuTrackerError(
                        "Rutracker требует повторной авторизации для скачивания. "
                        "Обновите cookies из браузера.")
                
                if "соглас" in low or "accept" in low:
                    raise RuTrackerError(
                        "Rutracker требует согласия с правилами. "
                        "Скачайте любой торрент вручную в браузере (нажмите 'Согласен'), "
                        "затем обновите cookies.")
                
                if "captcha" in low or "капча" in low:
                    raise RuTrackerCaptchaError(
                        "Rutracker требует капчу для скачивания.")
                
                # Проверяем что получили именно торрент-файл
                content_type = r.headers.get('content-type', '').lower()
                
                is_torrent_content_type = (
                    'bittorrent' in content_type or
                    'application/x-bittorrent' in content_type or
                    'application/octet-stream' in content_type
                )
                is_torrent_format = r.content[:3] == b'd8:'
                
                if not is_torrent_content_type and not is_torrent_format:
                    save_debug_dump(r.text)
                    raise RuTrackerError(
                        f"Получен не торрент-файл (content-type: {content_type}). "
                        f"Debug-дамп: /data/debug_last_topic.html")
                
                print(f"[rutracker]   .torrent downloaded: {len(r.content)} bytes")
                return r.content
                
        except httpx.HTTPError as e:
            raise RuTrackerError(f"Сетевая ошибка при скачивании торрента: {e}")