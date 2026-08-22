import base64

import httpx


class DelugeError(Exception):
    pass


LOGIN_METHODS = ["auth.login", "login"]
VERSION_METHODS = ["web.get_version", "get_version", "core.get_version", "daemon.get_version"]


class DelugeClient:
    """Клиент Deluge (Web JSON-RPC, http://host:8115/json)."""

    def __init__(self, url="http://localhost:8115", password=""):
        self.url = (url or "http://localhost:8115").rstrip("/")
        if not self.url.endswith("/json"):
            self.url += "/json"
        self.password = password or ""
        self._sid = None
        self._id = 0

    def _post(self, method, params):
        self._id += 1
        cookies = {"_session_id": self._sid} if self._sid else None
        r = httpx.post(self.url, json={"method": method, "params": list(params), "id": self._id},
                       cookies=cookies, timeout=20)
        r.raise_for_status()
        sid = r.cookies.get("_session_id")
        if sid:
            self._sid = sid
        data = r.json()
        if data.get("error"):
            err = data["error"]
            raise DelugeError(str(err.get("message") if isinstance(err, dict) else err))
        return data.get("result")

    def _auth(self):
        if self._sid:
            try:
                if self._post("auth.check_session", []):
                    return
            except Exception:
                self._sid = None
        last = None
        for m in LOGIN_METHODS:
            try:
                ok = self._post(m, [self.password])
                if ok:
                    return
                raise DelugeError("Неверный пароль Deluge")
            except DelugeError as e:
                if "пароль" in str(e).lower() or "password" in str(e).lower():
                    raise
                last = e
            except Exception as e:
                last = e
        raise DelugeError(f"Не удалось войти в Deluge ({self.url}): {last}. "
                          f"Убедитесь, что это веб-интерфейс Deluge (порт 8115), а не демон (58846).")

    def _call(self, method, *params):
        self._auth()
        return self._post(method, list(params))

    def _version(self):
        for m in VERSION_METHODS:
            try:
                v = self._post(m, [])
                if v:
                    return v
            except Exception:
                continue
        return None

    # ── общий интерфейс (как у TransmissionClient) ──
    def add_torrent(self, torrent, download_dir=None, paused=False):
        options = {}
        if download_dir:
            options["download_location"] = download_dir
        if paused:
            options["add_paused"] = True
        if isinstance(torrent, (bytes, bytearray)):
            tid = self._call("core.add_torrent_file", "torrent",
                             base64.b64encode(bytes(torrent)).decode(), options)
        else:
            tid = self._call("core.add_torrent_magnet", str(torrent), options)
        if not tid:
            raise DelugeError("Deluge не добавил торрент")
        try:
            st = self._call("core.get_torrent_status", tid, ["name", "total_size"])
            name = st.get("name") or tid
            size = st.get("total_size") or 0
        except Exception:
            name, size = tid, 0
        return {"hash": tid, "name": name, "size": size}

    def get_torrent_status(self, h):
        try:
            st = self._call("core.get_torrent_status", h, ["progress", "total_size", "is_finished"])
        except Exception:
            return None
        if not st:
            return None
        return {"progress": st.get("progress") or 0,
                "is_finished": bool(st.get("is_finished")),
                "size": st.get("total_size") or 0}

    def remove_torrent(self, h, delete_data=False):
        self._call("core.remove_torrent", h, bool(delete_data))

    def test_connection(self):
        try:
            self._auth()
        except Exception as e:
            return False, str(e)
        v = self._version()
        if v:
            return True, f"Deluge {v}"
        try:
            if self._post("auth.check_session", []):
                return True, "Deluge (подключено)"
        except Exception as e:
            return False, str(e)
        return True, "Deluge (подключено)"