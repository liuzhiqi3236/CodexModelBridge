"""有明确来源限制的 HTTP 读取；认证不会随跳转泄漏到其他站点。"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import VERSION
from .errors import BridgeError

MAX_RESPONSE = 16 * 1024 * 1024


def origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    return parts.scheme, (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)


def normalize_site(value: str) -> str:
    """用户提供站点根路径，不猜测协议或剥除可能有意义的路径。"""
    value = value.strip().rstrip("/")
    parts = urlsplit(value)
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise BridgeError("请填写完整站点地址，例如 https://your-sub2api.example.com。")
    # 按用户明确填写的协议连接；HTTP 提示由页面展示，不自动升级或降级。
    if parts.scheme not in ("http", "https"):
        raise BridgeError("请填写以 http:// 或 https:// 开头的完整站点地址。")
    return value


def site_from_input(value: str) -> str:
    """仅在用户输入边界识别 Sub2API 页面，后续请求统一复用处理后的站点地址。"""
    parts = urlsplit(value.strip())
    path = parts.path.rstrip("/")
    # 只移除已知页面末段及其分页、跳转参数；前面的部署路径原样保留。
    if path.endswith(("/login", "/keys")):
        path = path.rsplit("/", 1)[0]
        value = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return normalize_site(value)


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
        raise BridgeError(f"请求失败：HTTP {exc.code}（{urlsplit(url).hostname}）。") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise BridgeError(f"无法连接 {urlsplit(url).hostname}，请检查网络或站点地址。") from None


def fetch_json(url: str, **kwargs) -> dict:
    try:
        value = json.loads(fetch(url, **kwargs))
    except (ValueError, UnicodeError):
        raise BridgeError("站点返回的不是有效 JSON，请检查站点地址及版本。") from None
    if not isinstance(value, dict):
        raise BridgeError("站点返回的数据结构不符合接口要求。")
    return value
