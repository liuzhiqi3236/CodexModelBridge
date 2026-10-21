"""Sub2API 当前用户接口，管理凭据与网关 Key 严格分开。"""

from __future__ import annotations

from urllib.parse import urlencode

from .errors import BridgeError
from .net import fetch_json, normalize_site
from .storage import mask_key


def unwrap(document: dict):
    if document.get("code") not in (0, "0") or "data" not in document:
        raise BridgeError("Sub2API 接口返回失败，请重新登录或核对站点版本。")
    return document["data"]


class Sub2Api:
    def __init__(self, site: str, access_token: str):
        self.site = normalize_site(site)
        self.access_token = access_token
        self.keys: dict[str, dict] = {}

    def current_user(self) -> dict:
        data = unwrap(fetch_json(self.site + "/api/v1/auth/me", token=self.access_token))
        if not isinstance(data, dict) or "id" not in data:
            raise BridgeError("无法识别当前登录用户。")
        return {"id": str(data["id"]), "email": str(data.get("email", "")), "name": str(data.get("username", ""))}

    def list_keys(self) -> list[dict]:
        """读取全部分页；中途失败不替换当前完整列表。"""
        collected: dict[str, dict] = {}
        page = 1
        while True:
            query = urlencode({"page": page, "page_size": 100})
            data = unwrap(fetch_json(self.site + "/api/v1/keys?" + query, token=self.access_token))
            if not isinstance(data, dict) or not isinstance(data.get("items"), list) or "total" not in data:
                raise BridgeError("站点密钥分页格式不受支持。")
            total = int(data["total"])
            previous = len(collected)
            for row in data["items"]:
                if "id" not in row or not isinstance(row.get("key"), str):
                    raise BridgeError("站点未提供完整密钥数据，无法生成连接配置。")
                collected[str(row["id"])] = row
            if len(collected) == total:
                break
            if len(collected) > total or previous == len(collected) or page >= 1000:
                raise BridgeError("密钥分页在读取期间发生变化，请重新刷新。")
            page += 1
        self.keys = collected
        return [self.key_summary(row) for row in collected.values()]

    @staticmethod
    def key_summary(row: dict) -> dict:
        group = row.get("group") or {}
        return {"id": str(row["id"]), "name": row.get("name", ""), "masked": mask_key(row["key"]),
                "status": row.get("status", ""), "group_id": str(row.get("group_id") or ""),
                "group_name": group.get("name", ""), "platform": group.get("platform", ""),
                "expires_at": row.get("expires_at")}

    def selected_key(self, key_id: str) -> str:
        if key_id not in self.keys:
            raise BridgeError("请选择当前账号列表中的 API Key。")
        row = self.keys[key_id]
        if row.get("status") != "active":
            raise BridgeError("此密钥当前未启用，请到站点确认密钥状态。")
        if "*" in row["key"] or "•" in row["key"]:
            raise BridgeError("站点只返回了密钥掩码，不能使用掩码配置 Codex。")
        return row["key"]

    def model_manifest(self, key_id: str, client_version: str) -> list[dict]:
        token = self.selected_key(key_id)
        url = self.site + "/v1/models?" + urlencode({"client_version": client_version})
        data = fetch_json(url, token=token)
        if not isinstance(data.get("models"), list):
            raise BridgeError("此站点未返回 Codex 完整模型目录；可继续查看官方模型或显式导入模型目录。")
        models = data["models"]
        if any(not isinstance(row, dict) or not isinstance(row.get("slug"), str) for row in models):
            raise BridgeError("站点模型目录条目不完整。")
        return models
