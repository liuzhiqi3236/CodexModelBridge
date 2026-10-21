"""可安全显示给用户的业务错误。"""


class BridgeError(Exception):
    """错误内容不含密钥、登录凭据或未脱敏的远端响应。"""


class HTTPStatusError(BridgeError):
    """保留 HTTP 状态供调用方区分登录失效与网络故障，不携带远端正文。"""
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
