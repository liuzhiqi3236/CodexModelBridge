"""仅监听回环地址的本地页面服务；每次启动使用独立随机访问令牌。"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from .errors import BridgeError


def make_server(service, assets: Path, port: int = 0):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            # 请求和认证信息不写终端或普通访问日志。
            pass

        def send_content(self, code, content, mime):
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(content)

        def result(self, status, value):
            self.send_content(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def valid_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.valid_host():
                self.result(403, {"error": "不允许的本地来源。"})
                return
            names = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                     "/style.css": ("style.css", "text/css; charset=utf-8"), "/icon.svg": ("icon.svg", "image/svg+xml")}
            entry = names.get(urlsplit(self.path).path)
            if not entry:
                self.result(404, {"error": "页面不存在。"})
                return
            self.send_content(200, (assets / entry[0]).read_bytes(), entry[1])

        def do_POST(self):
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            if not self.valid_host() or self.headers.get("Origin") != expected_origin or not secrets.compare_digest(self.headers.get("X-Bridge-Token", ""), token):
                self.result(403, {"error": "本地会话无效，请重新打开工具。"})
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.result(415, {"error": "请求必须使用 JSON。"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 16 * 1024 * 1024:
                    raise BridgeError("请求内容过大。")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise BridgeError("请求格式错误。")
                path = urlsplit(self.path).path
                if not path.startswith("/api/") or "/" in path[5:]:
                    raise BridgeError("未知操作。")
                value = service.dispatch(path[5:], body)
                self.result(200, value)
            except BridgeError as exc:
                self.result(400, {"error": str(exc)})
            except (ValueError, UnicodeError, TypeError):
                self.result(400, {"error": "数据格式错误，请核对输入。"})
            except OSError:
                self.result(500, {"error": "本地文件读写失败，请检查目录权限和磁盘空间。"})
            except Exception:
                self.result(500, {"error": "操作没有完成，请重启工具或查阅使用说明。"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.session_token = token
    return server
