"""可安全显示给用户的业务错误。"""


class BridgeError(Exception):
    """错误内容不含密钥、登录凭据或未脱敏的远端响应。"""
