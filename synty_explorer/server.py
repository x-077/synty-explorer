"""Local web server: UI + JSON API + asset files. Binds to 127.0.0.1 only (licensed content stays local)."""
from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .catalog import public_index
from .search import search

WEB_DIR = Path(__file__).parent / "web"
FILE_KEYS = {"model": "_model_path", "texture": "_texture_path", "thumb": "_thumb_path"}


def make_handler(index: dict):
    by_id = {a["id"]: a for a in index["assets"]}
    pub = public_index(index)
    pub_by_id = {a["id"]: a for a in pub["assets"]}
    pub_json = json.dumps(pub).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache" if ctype.startswith(("application/json", "text/html")) else "max-age=3600")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode())

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            q = parse_qs(url.query)
            route = url.path
            if route in ("/", "/index.html"):
                return self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            if route == "/api/index":
                return self._send(200, pub_json)
            if route == "/api/search":
                limit = int(q.get("limit", ["200"])[0])
                res = search(pub["assets"], q.get("q", [""])[0], limit)
                return self._json({"count": len(res), "assets": res})
            if route.startswith("/api/asset/"):
                a = pub_by_id.get(unquote(route[len("/api/asset/"):]))
                return self._json(a) if a else self._json({"error": "not found"}, 404)
            if route.startswith("/file/"):
                _, _, kind, asset_id = route.split("/", 3)
                a = by_id.get(unquote(asset_id))
                p = a.get(FILE_KEYS.get(kind, ""), None) if a else None
                if not p or not Path(p).is_file():
                    return self._json({"error": "file not available"}, 404)
                ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
                return self._send(200, Path(p).read_bytes(), ctype)
            self._json({"error": "not found"}, 404)

    return Handler


def serve(index: dict, host: str = "127.0.0.1", port: int = 7860) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(index))
    print(f"Synty Explorer: http://{host}:{port}  ({index['asset_count']} assets)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
