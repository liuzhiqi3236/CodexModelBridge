"""模桥桌面入口；--serve 仅用于开发和浏览器界面验证。"""

from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import sys
import threading
import webbrowser

from bridge.server import make_server
from bridge.service import BridgeService


def main():
    parser = argparse.ArgumentParser(description="模桥 ModelBridge")
    parser.add_argument("--serve", action="store_true", help="开发模式：只启动本机页面服务")
    parser.add_argument("--root", type=Path, help="明确指定开发数据目录")
    args = parser.parse_args()
    frozen = getattr(sys, "frozen", False)
    root = args.root or (Path(sys.executable).parent if frozen else Path(__file__).resolve().parent.parent)
    assets = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "web"
    # 单实例互斥避免两个应用窗口同时替换同一个 Codex 配置。
    mutex = None
    if os.name == "nt" and not args.serve:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        mutex = kernel.CreateMutexW(None, False, "Local\\CodexModelBridge.Desktop")
        if ctypes.get_last_error() == 183:
            ctypes.windll.user32.MessageBoxW(None, "模桥已经打开，请切换到现有窗口。", "模桥 ModelBridge", 0x40)
            return
    service = BridgeService(root)
    service.external_opener = webbrowser.open
    server = make_server(service, assets)
    url = f"http://127.0.0.1:{server.server_port}/#token={server.session_token}"
    if args.serve:
        print(url, flush=True)
        server.serve_forever()
        return
    import webview
    from bridge.desktop import DesktopLogin
    login = DesktopLogin(service)
    service.login_opener = login.open
    window = webview.create_window("模桥 ModelBridge", url, width=1280, height=860, min_size=(980, 700),
                                   background_color="#f5f6f8", text_select=True)
    window.events.closed += login.close
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        webview.start(gui="edgechromium", private_mode=True, storage_path=str(service.data / "webview"))
    finally:
        server.shutdown()
        server.server_close()
        if mutex:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        if os.name == "nt" and getattr(sys, "frozen", False):
            ctypes.windll.user32.MessageBoxW(None, "程序启动失败。请保持程序文件夹完整（包括 runtime 文件夹和 sources.json），并确认已安装 Microsoft Edge WebView2。", "模桥 ModelBridge", 0x10)
        else:
            raise
