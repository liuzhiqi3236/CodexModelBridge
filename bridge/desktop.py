"""桌面窗口与站点登录窗口隔离；登录页不获得本地应用接口。"""

from __future__ import annotations

import json
import threading

from .net import origin


class DesktopLogin:
    def __init__(self, service):
        self.service = service
        self.windows = []

    def open(self, site: str, epoch: int):
        import webview
        window = webview.create_window("登录 Sub2API · 模桥", site + "/login", width=960, height=760,
                                       min_size=(640, 520), text_select=True)
        self.windows.append(window)
        closed = threading.Event()
        window.events.closed += lambda: closed.set()

        def observe():
            # 仅读取准确站点来源的登录存储；不追踪 OAuth 第三方页面中的数据。
            try:
                while not closed.wait(0.8):
                    with self.service._lock:
                        if self.service.epoch != epoch:
                            window.destroy()
                            return
                    current = window.get_current_url()
                    if not current or origin(current) != origin(site):
                        continue
                    try:
                        # 取走本轮令牌后清除站点窗口的登录存储，再关闭页面，避免
                        # 下一次“更换账号”直接复用旧账号，或页面刷新计时器重新写入。
                        script = "(() => { if (location.origin !== new URL(" + json.dumps(site) + ").origin) return null;" + """
                            const token = localStorage.getItem('auth_token');
                            if (token) {
                                for (const key of ['auth_token', 'auth_user', 'refresh_token',
                                                   'token_expires_at', 'pending_auth_session']) {
                                    localStorage.removeItem(key);
                                }
                            }
                            return JSON.stringify({origin:location.origin,token});
                        })()"""
                        value = window.evaluate_js(script)
                        state = json.loads(value) if isinstance(value, str) else value
                    except Exception:
                        continue  # 页面导航中的短暂不可读，不改变会话或认证方式。
                    if not isinstance(state, dict) or origin(state.get("origin", "")) != origin(site):
                        continue
                    token = state.get("token")
                    if isinstance(token, str) and token:
                        window.destroy()
                        self.service.complete_login(site, token, epoch)
                        return
            finally:
                self.service.finish_login_error(epoch, None)

        threading.Thread(target=observe, daemon=True).start()

    def close(self):
        for window in self.windows:
            try:
                window.destroy()
            except Exception:
                pass
