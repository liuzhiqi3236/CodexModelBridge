"""模型名称和完整参数目录分开保存，明确每一项的资料来源。"""

from __future__ import annotations

from copy import deepcopy

from .errors import BridgeError


class Catalog:
    def __init__(self):
        self.local: list[dict] = []
        self.bundled: list[dict] = []
        self.remote: list[dict] = []
        self.imported: list[dict] = []
        self.discovery = {"sources": []}
        self.manual: list[str] = []

    def clear_remote(self) -> None:
        self.remote = []

    def metadata(self) -> dict[str, tuple[dict, str]]:
        # 来源优先级是明示的数据合并策略，并非请求失败时替换模型。
        # 本地已配置项始终优先保留，绝不自动覆盖用户调过的能力。
        result = {}
        for rows, label in ((self.bundled, "Codex 内置目录"), (self.remote, "所选 Key 的站点目录"),
                            (self.imported, "用户导入目录"), (self.local, "当前 Codex 配置")):
            for row in rows:
                result[row["slug"]] = (row, label)
        return result

    def rows(self, sources: list[dict]) -> list[dict]:
        metadata = self.metadata()
        local_names = {row["slug"] for row in self.local}
        known = {}
        # 刷新页面中出现的顺序保留为官方目录顺序，不假造发布日期。
        for source in self.discovery["sources"]:
            for name in source.get("models", []):
                known[name] = {"provider": source["id"], "provider_name": source["name"],
                               "url": source["url"], "checked_at": source.get("checked_at"),
                               "is_new": name in source.get("new_models", [])}
        for name in [*metadata.keys(), *self.manual]:
            if name not in known:
                source = next((s for s in sources if any(name.startswith(prefix) for prefix in s["prefixes"])), None)
                known[name] = {"provider": source["id"] if source else "", "provider_name": source["name"] if source else "自定义",
                               "url": None, "checked_at": None, "is_new": False}
        result = []
        for name, info in known.items():
            entry = metadata.get(name)
            model, parameter_source = entry if entry else ({}, None)
            result.append({"id": name, "name": model.get("display_name", name), **info,
                "configured": name in local_names, "ready": entry is not None, "parameter_source": parameter_source,
                "context_window": model.get("context_window"), "input_modalities": model.get("input_modalities", []),
                "reasoning": [level["effort"] for level in model.get("supported_reasoning_levels", [])]})
        # 新发现项前置，其余保留来源顺序；不冒充厂商的正式发布日期。
        return sorted(result, key=lambda row: not row["is_new"])

    def selected(self, names: list[str]) -> list[dict]:
        if not names:
            raise BridgeError("请先选择模型。")
        if len(names) != len(set(names)):
            raise BridgeError("选择的模型存在重复项。")
        metadata = self.metadata()
        for name in names:
            if name not in metadata:
                raise BridgeError(f"{name} 已发现，但尚缺完整配置参数。请刷新站点目录或显式导入该模型的目录条目。")
        return [deepcopy(metadata[name][0]) for name in names]
