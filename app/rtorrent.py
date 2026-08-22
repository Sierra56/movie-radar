import hashlib
import re
import socket
import xmlrpc.client


class RTorrentError(Exception):
    pass


# ── минимальный bencode для вычисления info-hash ──────
def _bdecode(data, i=0):
    c = data[i:i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if c == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            v, i = _bdecode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            k, i = _bdecode(data, i)
            v, i = _bdecode(data, i)
            out[k] = v
        return out, i + 1
    j = data.index(b":", i)
    n = int(data[i:j])
    return data[j + 1:j + 1 + n], j + 1 + n


def _bencode(o):
    if isinstance(o, int):
        return b"i" + str(o).encode() + b"e"
    if isinstance(o, str):
        o = o.encode()
    if isinstance(o, bytes):
        return str(len(o)).encode() + b":" + o
    if isinstance(o, list):
        return b"l" + b"".join(_bencode(x) for x in o) + b"e"
    if isinstance(o, dict):
        out = b"d"
        for k in sorted(o.keys()):
            out += _bencode(k) + _bencode(o[k])
        return out + b"e"
    raise ValueError("bencode: unsupported type")


def torrent_meta(data):
    meta, _ = _bdecode(data, 0)
    info = meta[b"info"]
    h = hashlib.sha1(_bencode(info)).hexdigest()
    name = info.get(b"name", b"").decode("utf-8", "ignore")
    if b"length" in info:
        size = info[b"length"]
    elif b"files" in info:
        size = sum(f.get(b"length", 0) for f in info[b"files"])
    else:
        size = 0
    return h, name, size


def _magnet_hash(magnet):
    m = re.search(r"btih:([0-9a-fA-F]{40})", magnet)
    return m.group(1).lower() if m else None


def _magnet_name(magnet):
    m = re.search(r"[&?]dn=([^&]+)", magnet)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1).replace("+", " "))
    return magnet


class RTorrentClient:
    """Клиент rTorrent (XML-RPC поверх HTTP или SCGI)."""

    def __init__(self, url="http://localhost:8080/RPC2", username="", password=""):
        self.url = (url or "http://localhost:8080/RPC2").strip()
        self.username = username or ""
        self.password = password or ""
        self._scgi = None
        if self.url.startswith("scgi://"):
            hostport = self.url[len("scgi://"):].rstrip("/")
            host, _, port = hostport.partition(":")
            self._scgi = (host or "localhost", int(port or 5000))

    # ── транспорт ──
    def _scgi_request(self, body):
        headers = (b"CONTENT_LENGTH\0" + str(len(body)).encode() + b"\0"
                   b"SCGI\01\0REQUEST_METHOD\0POST\0REQUEST_URI\0/RPC2\0")
        payload = str(len(headers)).encode() + b":" + headers + b"," + body
        with socket.create_connection(self._scgi, timeout=20) as s:
            s.sendall(payload)
            s.shutdown(socket.SHUT_WR)
            resp = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                resp += chunk
        _, _, bodyresp = resp.partition(b"\r\n\r\n")
        return bodyresp

    def _http_request(self, body):
        import httpx
        auth = (self.username, self.password) if self.username else None
        r = httpx.post(self.url, content=body, headers={"Content-Type": "text/xml"},
                       timeout=20, auth=auth)
        if r.status_code >= 400:
            raise RTorrentError(f"HTTP {r.status_code}")
        return r.content

    def _request(self, body):
        return self._scgi_request(body) if self._scgi else self._http_request(body)

    def _parse(self, body):
        stripped = body.lstrip()
        is_xml = stripped.startswith(b"<?xml") or b"<methodResponse" in stripped[:64]
        if not is_xml or stripped[:5].lower() == b"<html":
            raise RTorrentError(
                "Эндпоинт вернул не XML-RPC (похоже на веб-интерфейс). "
                "Укажите RPC: http://host:port/RPC2, scgi://host:port "
                "или http://user:pass@host/rutorrent/plugins/httprpc/action.php")
        params, _ = xmlrpc.client.loads(body)
        return params

    def _call(self, method, *args):
        body = xmlrpc.client.dumps(args, methodname=method, allow_none=True)
        try:
            params = self._parse(self._request(body))
        except RTorrentError:
            raise
        except Exception as e:
            raise RTorrentError(f"{method}: {e}")
        return params[0] if len(params) == 1 else params

    # ── общий интерфейс (как у TransmissionClient) ──
    def add_torrent(self, torrent, download_dir=None, paused=False):
        cmds = []
        if download_dir:
            cmds.append("d.directory.set=" + download_dir)
        if isinstance(torrent, (bytes, bytearray)):
            data = bytes(torrent)
            h, name, size = torrent_meta(data)
            method = "load.raw" if paused else "load.raw_start"
            self._call(method, "", xmlrpc.client.Binary(data), *cmds)
        else:
            magnet = str(torrent)
            h = _magnet_hash(magnet)
            name = _magnet_name(magnet)
            size = 0
            method = "load.normal" if paused else "load.start"
            self._call(method, "", magnet, *cmds)
        return {"hash": h, "name": name, "size": size}

    def get_torrent_status(self, h):
        try:
            size = int(self._call("d.size_bytes", h))
            done = int(self._call("d.completed_bytes", h))
            complete = int(self._call("d.complete", h))
        except Exception:
            return None
        progress = (done / size * 100) if size else (100 if complete else 0)
        return {"progress": progress, "is_finished": complete == 1, "size": size}

    def remove_torrent(self, h, delete_data=False):
        try:
            self._call("d.close", h)
        except Exception:
            pass
        self._call("d.erase", h)

    def test_connection(self):
        try:
            v = self._call("system.client_version")
            return True, f"rTorrent {v}"
        except Exception as e:
            return False, str(e)