"""本机 Sub2API 契约测试站点：所有账号、密钥和模型均为测试数据。"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import parse_qs, urlsplit


def make_station(models, *, key_count=205):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            # 可复现部署子路径和前端页面回退；记录路径，不记录认证头。
            self.server.request_paths.append(parsed.path)
            # 仅记录是否携带凭据，供入口识别测试核对；不保存认证内容。
            self.server.request_auth.append(bool(self.headers.get("Authorization")))
            prefix = self.server.base_path
            if prefix and not parsed.path.startswith(prefix + "/"):
                self.send_response(404); self.end_headers(); return
            path = parsed.path[len(prefix):]
            if path == "/api/v1/settings/public":
                self.send_response(self.server.public_status)
                self.send_header("Content-Type", "application/json")
                if self.server.public_redirect:
                    self.send_header("Location", self.server.public_redirect)
                self.end_headers()
                self.wfile.write(json.dumps(self.server.public_settings).encode("utf-8"))
                return
            if path == "/login":
                page = b'<!doctype html><html><body><h1>ModelBridge test station</h1><button onclick="localStorage.setItem(\'auth_token\',\'fixture-session\');document.getElementById(\'status\').textContent=\'Signed in\'">Test sign in</button><p id="status"></p></body></html>'
                self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(page)
                return
            authorization = self.headers.get("Authorization", "")
            if path.startswith("/api/") and authorization != "Bearer fixture-session":
                self.send_response(401); self.end_headers(); return
            if path == "/api/v1/auth/me":
                value = {"code": 0, "data": {"id": 90071992547409931234, "email": "demo@example.invalid", "username": "测试账号"}}
            elif path == "/api/v1/keys":
                query = parse_qs(parsed.query); page = int(query["page"][0]); size = int(query["page_size"][0])
                start, end = (page - 1) * size, min(page * size, self.server.key_count)
                rows = [{"id": 90071992547409931234 + i, "name": "混合分组测试密钥 " + str(i + 1), "key": "sk-fixture-not-real-key-" + str(i),
                         "status": "active", "group_id": 90071992547409939999,
                         "group": {"name": "测试混合分组", "platform": "composite"}} for i in range(start, end)]
                if self.server.broken_page and page == 2:
                    self.send_response(503); self.end_headers(); return
                value = {"code": 0, "data": {"items": rows, "total": self.server.key_count, "page": page, "page_size": size}}
            elif path == "/v1/models":
                if not authorization.startswith("Bearer sk-fixture-not-real-key-"):
                    self.send_response(401); self.end_headers(); return
                # 记录真实接口调用，并允许测试失败后重试；所有值均为假密钥。
                self.server.model_requests.append(authorization)
                if self.server.model_error or authorization in self.server.fail_model_keys:
                    self.send_response(503); self.end_headers(); return
                value = {"models": self.server.models}
            else:
                if self.server.spa_pages:
                    self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
                    self.wfile.write(b'<!doctype html><html><body>Sub2API page</body></html>'); return
                self.send_response(404); self.end_headers(); return
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.key_count, server.broken_page = key_count, False
    server.models, server.model_requests = models, []
    server.model_error, server.fail_model_keys = False, set()
    server.base_path, server.spa_pages, server.request_paths = "", False, []
    server.request_auth = []
    server.public_status, server.public_redirect = 200, ""
    server.public_settings = {"code": 0, "data": {"site_name": "测试站点",
                              "registration_enabled": False, "email_verify_enabled": True}}
    return server
