"""有明确来源限制的 HTTP 读取；认证不会随跳转泄漏到其他站点。"""

from __future__ import annotations

import json
from http.client import HTTPException
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import VERSION
from .errors import BridgeError, HTTPStatusError

MAX_RESPONSE = 16 * 1024 * 1024


def origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    return parts.scheme, (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)


def normalize_site(value: str) -> str:
    """用户提供站点根路径，不猜测协议或剥除可能有意义的路径。"""
    value = value.strip().rstrip("/")
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        raise BridgeError("地址格式或端口不正确，请填写完整的 HTTP 或 HTTPS 地址。") from None
    if not parts.hostname or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise BridgeError("请填写完整站点地址，例如 https://your-sub2api.example.com。")
    # 按用户明确填写的协议连接；HTTP 提示由页面展示，不自动升级或降级。
    if parts.scheme not in ("http", "https"):
        raise BridgeError("请填写以 http:// 或 https:// 开头的完整站点地址。")
    return value


def api_base_url(value: str) -> str:
    """网站根地址与已含 /v1 的地址统一转换为 Codex 的最终 API 地址。"""
    base = normalize_site(value)
    return base if urlsplit(base).path.endswith("/v1") else base + "/v1"


def connection_error(url: str, error: Exception) -> BridgeError:
    """只输出错误类别和主机，避免异常正文回显认证、代理或请求参数。"""
    reason = error.reason if isinstance(error, URLError) else error
    host = urlsplit(url).hostname
    if isinstance(reason, ssl.SSLCertVerificationError):
        message = "HTTPS 证书验证失败，请检查站点证书和本机时间。"
    elif isinstance(reason, ssl.SSLEOFError):
        message = "HTTPS 连接被中途断开（SSL EOF）。请重试；持续失败时检查代理或联系站点管理员。"
    elif isinstance(reason, ssl.SSLError):
        message = "HTTPS 安全连接建立失败，请检查代理或站点的 HTTPS 服务。"
    elif isinstance(reason, (TimeoutError, socket.timeout)):
        message = "连接超时，请检查网络后重试。"
    elif isinstance(reason, socket.gaierror):
        message = "域名解析失败，请检查域名和 DNS。"
    elif isinstance(reason, ConnectionRefusedError):
        message = "连接被拒绝，请检查站点端口或本机代理是否运行。"
    elif isinstance(reason, ConnectionError):
        message = "连接被中途断开，请重试。"
    else:
        message = "网络连接失败，请检查网络或代理后重试。"
    return BridgeError(f"{host}：{message}")


class RestrictedRedirect(HTTPRedirectHandler):
    def __init__(self, request_origin: tuple, authenticated: bool, allowed_hosts: set[str]):
        self.request_origin = request_origin
        self.authenticated = authenticated
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination = urlsplit(newurl)
        if self.authenticated and origin(newurl) != self.request_origin:
            raise BridgeError("站点请求重定向到了其他来源，已停止发送认证信息。")
        if self.allowed_hosts and (destination.scheme != "https" or destination.hostname not in self.allowed_hosts):
            raise BridgeError("资料来源跳转到了未配置的域名，请检查来源配置。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, *, token: str | None = None, payload: dict | None = None,
          allowed_hosts: set[str] | None = None, timeout: int = 25) -> bytes:
    headers = {"User-Agent": "ModelBridge/" + VERSION, "Accept": "application/json, text/html, text/plain"}
    if token:
        if "\r" in token or "\n" in token:
            raise BridgeError("认证信息格式错误。")
        headers["Authorization"] = "Bearer " + token
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, headers=headers)
    opener = build_opener(RestrictedRedirect(origin(url), bool(token), allowed_hosts or set()))
    try:
        with opener.open(request, timeout=timeout) as response:
            content = response.read(MAX_RESPONSE + 1)
            if len(content) > MAX_RESPONSE:
                raise BridgeError("远端响应超过大小限制，未读取不完整内容。")
            return content
    except HTTPError as exc:
        # 不回显远端正文：部分部署的错误正文会包含请求或凭据。
        status = exc.code
        exc.close()
        raise HTTPStatusError(status, f"请求失败：HTTP {status}（{urlsplit(url).hostname}）。") from None
    except (URLError, TimeoutError, OSError, HTTPException) as exc:
        raise connection_error(url, exc) from None


def fetch_json(url: str, **kwargs) -> dict:
    try:
        value = json.loads(fetch(url, **kwargs))
    except (ValueError, UnicodeError):
        raise BridgeError("站点返回的不是有效 JSON，请检查站点地址及版本。") from None
    if not isinstance(value, dict):
        raise BridgeError("站点返回的数据结构不符合接口要求。")
    return value
