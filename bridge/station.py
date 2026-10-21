"""登录前识别 Sub2API 入口；只验证同一来源的公开接口，不接触登录凭据。"""

from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import VERSION
from .errors import BridgeError
from .net import MAX_RESPONSE, normalize_site, origin


class NoProbeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 跳转后的成功响应不能证明原路径可用，也不借此跳到其他站点。
        return None


def _is_station(site: str, timeout: float) -> bool:
    """按 Sub2API 的公开设置契约确认入口，网页和普通 JSON 均不能通过。"""
    request = Request(site + "/api/v1/settings/public", headers={
        "User-Agent": "ModelBridge/" + VERSION, "Accept": "application/json"})
    try:
        # 没有 Cookie 容器，也不传 Authorization；每个候选只读一次。
        with build_opener(NoProbeRedirect()).open(request, timeout=timeout) as response:
            content = response.read(MAX_RESPONSE + 1)
            if len(content) > MAX_RESPONSE:
                raise BridgeError("站点公开信息过大，无法识别入口。")
    except HTTPError as exc:
        status = exc.code
        exc.close()
        if status in (301, 302, 303, 307, 308, 404, 405):
            return False
        # 限流、拒绝访问和服务异常需要用户处理，不能误报为路径错误。
        raise BridgeError(f"站点入口识别失败：HTTP {status}，请确认站点能正常访问后重试。") from None
    except (URLError, TimeoutError, OSError):
        raise BridgeError("无法连接站点，未能识别入口。请检查地址和网络后重试。") from None
    try:
        document = json.loads(content)
    except (ValueError, UnicodeError):
        return False
    if not isinstance(document, dict) or document.get("code") not in (0, "0"):
        return False
    data = document.get("data")
    return (isinstance(data, dict) and isinstance(data.get("site_name"), str)
            and isinstance(data.get("registration_enabled"), bool)
            and isinstance(data.get("email_verify_enabled"), bool))


def site_from_input(value: str) -> str:
    """从页面所在路径向上验证，找到最接近页面的真实部署入口后停止。"""
    try:
        parts = urlsplit(value.strip())
        if parts.username is not None or parts.password is not None:
            raise ValueError("embedded credentials")
        # 页面参数、锚点不属于部署入口；中文路径按 URL 编码，保留已有编码。
        path = quote(parts.path.rstrip("/"), safe="/%:@!$&'()*+,;=-._~")
        site = normalize_site(urlunsplit((parts.scheme, parts.netloc, path, "", "")))
        origin(site)  # 校验端口，避免错误地址进入网络请求。
    except ValueError:
        raise BridgeError("请填写完整的 HTTP 或 HTTPS 站点网页地址，不要在地址中包含账号密码。") from None

    deadline = time.monotonic() + 15
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BridgeError("站点入口识别超时，请确认站点能正常打开后重试。")
        if _is_station(site, timeout=min(5, remaining)):
            return site
        if not path:
            break
        # 只缩短用户提供的路径，保留协议、主机、端口；不猜测其他目录。
        path = path.rsplit("/", 1)[0]
        site = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    raise BridgeError("没有识别到 Sub2API 站点入口。请确认这是站点网页地址，且站点能正常打开；识别不会跟随地址跳转。")
