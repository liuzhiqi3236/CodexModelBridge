"""读取和校验真实 Codex 配置，版本化追加目录并提供手动恢复。"""

from __future__ import annotations

from copy import deepcopy
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import uuid

import tomlkit

from .errors import BridgeError
from .storage import atomic_write, digest, protect, write_json


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def find_codex() -> list[str]:
    """枚举已安装运行文件供用户明确选择，不下载安装 Codex。"""
    folder = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    paths = sorted(folder.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.exists() else []
    command = shutil.which("codex.exe")
    if command:
        paths.append(Path(command))
    return list(dict.fromkeys(str(path.resolve()) for path in paths))


def model_entries(document: object) -> list[dict]:
    if not isinstance(document, dict) or not isinstance(document.get("models"), list):
        raise BridgeError("模型目录必须是包含 models 数组的 JSON 文件。")
    rows = document["models"]
    names = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("slug"), str) or not row["slug"].strip():
            raise BridgeError("模型目录包含缺少名称的条目。")
        if row["slug"] in names:
            raise BridgeError("模型目录存在重复名称：" + row["slug"])
        names.add(row["slug"])
    return rows


class CodexConfig:
    def __init__(self, directory: Path, binary: Path, data_dir: Path, helper: Path):
        self.directory = directory.resolve()
        self.binary = binary.resolve()
        self.data_dir = data_dir.resolve()
        self.helper = helper.resolve()
        self._lock = threading.Lock()
        self._plans: dict[str, dict] = {}

    def _run(self, args: list[str], home: Path) -> dict:
        if not self.binary.is_file():
            raise BridgeError("没有找到 Codex 可执行文件，请在设置中选择。")
        env = os.environ.copy()
        env["CODEX_HOME"] = str(home)
        try:
            result = subprocess.run([str(self.binary), *args], capture_output=True, timeout=40,
                                    env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            raise BridgeError("Codex 配置校验未完成，请检查可执行文件与本机运行环境。") from None
        if result.returncode:
            # 只提取解析器的字段错误，避免配置、令牌或其他诊断串进入界面。
            stderr = result.stderr.decode("utf-8", errors="replace")
            missing = re.search(r"missing field [`']([a-z_]+)[`']", stderr)
            suffix = "，缺少字段：" + missing.group(1) if missing else ""
            raise BridgeError("当前 Codex 未接受这份模型或连接配置" + suffix + "。")
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise BridgeError("Codex 模型校验没有返回有效 JSON。") from None

    def version(self) -> str:
        try:
            result = subprocess.run([str(self.binary), "--version"], capture_output=True, timeout=10,
                                    creationflags=subprocess.CREATE_NO_WINDOW, check=True)
            return result.stdout.decode("utf-8").strip().removeprefix("codex-cli ")
        except (OSError, subprocess.SubprocessError, UnicodeError):
            raise BridgeError("无法读取 Codex 版本，请检查可执行文件。") from None

    def bundled(self) -> list[dict]:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="verify-", dir=self.data_dir) as temporary:
            return model_entries(self._run(["debug", "models", "--bundled"], Path(temporary)))

    def snapshot(self) -> dict:
        path = self.directory / "config.toml"
        exists = path.exists()
        raw = path.read_bytes() if exists else b""
        try:
            config = tomlkit.parse(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            raise BridgeError("现有 Codex 配置无法解析，请先修正配置文件。") from None
        catalog_path = None
        catalog_raw = b""
        if config.get("model_catalog_json"):
            catalog_path = Path(str(config["model_catalog_json"]))
            if not catalog_path.is_absolute():
                catalog_path = self.directory / catalog_path
            catalog_path = catalog_path.resolve()
            if not catalog_path.is_file():
                raise BridgeError("当前配置引用的模型目录不存在，请先在设置中核对 Codex 目录。")
            catalog_raw = catalog_path.read_bytes()
            try:
                models = model_entries(json.loads(catalog_raw.decode("utf-8-sig")))
            except (ValueError, UnicodeError):
                raise BridgeError("当前模型目录 JSON 无法解析。") from None
        else:
            models = self.bundled()
        return {"raw": raw, "exists": exists, "config": config, "models": models,
                "catalog_path": str(catalog_path) if catalog_path else None,
                "catalog_digest": digest(catalog_raw), "digest": digest(raw)}

    def status(self) -> dict:
        state = self.snapshot()
        config = state["config"]
        return {"directory": str(self.directory), "binary": str(self.binary), "version": self.version(),
                "model": str(config.get("model", "")), "provider": str(config.get("model_provider", "openai")),
                "catalog": state["catalog_path"], "count": len(state["models"]),
                "models": [{"id": row["slug"], "name": row.get("display_name", row["slug"])} for row in state["models"]]}

    def validate_catalog(self, models: list[dict], config_text: str | None = None) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="verify-", dir=self.data_dir) as temporary:
            directory = Path(temporary)
            catalog = directory / "catalog.json"
            write_json(catalog, {"models": models})
            # 校验副本只驻留专用临时目录，不读取或改写真实配置。
            if config_text is not None:
                config = tomlkit.parse(config_text)
                config["model_catalog_json"] = str(catalog)
                atomic_write(directory / "config.toml", tomlkit.dumps(config).encode("utf-8"))
            result = self._run(["debug", "models", "-c", "model_catalog_json=" + json.dumps(str(catalog))], directory)
            actual = {row["slug"] for row in model_entries(result)}
            if not {row["slug"] for row in models}.issubset(actual):
                raise BridgeError("Codex 没有加载全部选择的模型，未应用配置。")

    def prepare(self, selected: list[dict], *, site: str, key: str, key_label: str,
                activate_connection: bool) -> dict:
        with self._lock:
            snapshot = self.snapshot()
            profile_name = snapshot["config"].get("profile")
            profile = snapshot["config"].get("profiles", {}).get(profile_name, {}) if profile_name else {}
            if "model_catalog_json" in profile or (activate_connection and "model_provider" in profile):
                raise BridgeError("当前启用的 profile 覆盖了模型目录或连接。请先在 Codex 配置中处理该覆盖，再使用用户级配置追加。")
            models = deepcopy(snapshot["models"])
            names = {row["slug"] for row in models}
            added, skipped = [], []
            for row in selected:
                if row["slug"] in names:
                    skipped.append(row["slug"])
                else:
                    added_model = deepcopy(row)
                    # 用户明确选择“加入模型列表”，只改变目录可见性，不猜测模型能力。
                    added_model["visibility"] = "list"
                    models.append(added_model)
                    names.add(row["slug"])
                    added.append(row["slug"])
            if not added and not activate_connection:
                raise BridgeError("所选模型已经存在，没有需要应用的变化。")
            plan_id = uuid.uuid4().hex
            catalog = self.directory / "modelbridge" / (plan_id + ".json")
            secret = self.data_dir / "secrets" / (plan_id + ".bin")
            config = deepcopy(snapshot["config"])
            config["model_catalog_json"] = str(catalog)
            if activate_connection:
                if not self.helper.is_file():
                    raise BridgeError("凭据读取程序缺失，请保持应用文件夹完整。")
                providers = config.setdefault("model_providers", tomlkit.table())
                provider = tomlkit.table()
                provider.update({"name": "ModelBridge", "base_url": site + "/v1",
                                 "wire_api": "responses", "supports_websockets": False})
                auth = tomlkit.table()
                auth.update({"command": str(self.helper), "args": [str(secret)], "refresh_interval_ms": 0})
                provider["auth"] = auth
                providers["modelbridge"] = provider
                config["model_provider"] = "modelbridge"
            output = tomlkit.dumps(config)
            self.validate_catalog(models, output)
            plan = {"id": plan_id, "snapshot": snapshot, "models": models, "output": output,
                    "catalog": catalog, "secret": secret, "key": key, "activate": activate_connection,
                    "created": utc_now()}
            self._plans = {plan_id: plan}
            return {"plan_id": plan_id, "added": added, "skipped": skipped,
                    "old_provider": str(snapshot["config"].get("model_provider", "openai")),
                    "new_provider": str(config.get("model_provider", "openai")),
                    "old_model": str(snapshot["config"].get("model", "")),
                    "new_model": str(config.get("model", "")), "site": site if activate_connection else None,
                    "key_label": key_label if activate_connection else None,
                    "config_path": str(self.directory / "config.toml"),
                    "catalog_count": len(models), "validated": True}

    def _assert_snapshot(self, snapshot: dict) -> None:
        config_path = self.directory / "config.toml"
        if config_path.exists() != snapshot["exists"] or digest(config_path.read_bytes() if config_path.exists() else b"") != snapshot["digest"]:
            raise BridgeError("Codex 配置在预览后发生变化，请重新预览。")
        catalog = snapshot["catalog_path"]
        if catalog and (not Path(catalog).is_file() or digest(Path(catalog).read_bytes()) != snapshot["catalog_digest"]):
            raise BridgeError("模型目录在预览后发生变化，请重新预览。")

    def _backup(self, raw: bytes, exists: bool, reason: str) -> str:
        backup_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        record = {"directory": str(self.directory), "created": utc_now(), "reason": reason,
                  "exists": exists, "content_base64": base64.b64encode(raw).decode("ascii")}
        path = self.data_dir / "backups" / (backup_id + ".bin")
        atomic_write(path, protect(json.dumps(record, ensure_ascii=False).encode("utf-8")))
        return backup_id

    def apply(self, plan_id: str) -> dict:
        with self._lock:
            plan = self._plans.get(plan_id)
            if not plan:
                raise BridgeError("预览已失效或已经应用，请重新预览。")
            self._assert_snapshot(plan["snapshot"])
            backup = self._backup(plan["snapshot"]["raw"], plan["snapshot"]["exists"], "应用前")
            write_json(plan["catalog"], {"models": plan["models"]})
            if plan["activate"]:
                atomic_write(plan["secret"], protect(plan["key"].encode("utf-8")))
            self._assert_snapshot(plan["snapshot"])
            # 只在所有准备工作成功后切换入口；之前的目录与凭据仍可用于恢复。
            atomic_write(self.directory / "config.toml", plan["output"].encode("utf-8"))
            self._plans.clear()
            return {"backup_id": backup, "message": "已保存配置。请完整退出并重新打开 Codex，再选择新增模型。"}

    def backups(self) -> list[dict]:
        result = []
        for path in sorted((self.data_dir / "backups").glob("*.bin"), reverse=True):
            record = json.loads(protect(path.read_bytes(), decrypt=True))
            if record["directory"] == str(self.directory):
                result.append({"id": path.stem, "created": record["created"], "reason": record["reason"]})
        return result

    def restore(self, backup_id: str, expected_digest: str) -> dict:
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-f0-9]{8}", backup_id):
            raise BridgeError("备份标识无效。")
        with self._lock:
            path = self.data_dir / "backups" / (backup_id + ".bin")
            if not path.is_file():
                raise BridgeError("备份不存在。")
            record = json.loads(protect(path.read_bytes(), decrypt=True))
            if record["directory"] != str(self.directory):
                raise BridgeError("此备份属于另一个 Codex 配置目录。")
            config_path = self.directory / "config.toml"
            exists = config_path.exists()
            current = config_path.read_bytes() if exists else b""
            if digest(current) != expected_digest:
                raise BridgeError("配置已发生变化，请刷新备份页面后再恢复。")
            saved = self._backup(current, exists, "恢复前")
            # 原来没有配置文件时，以空配置恢复等效初始状态，避免删除操作。
            atomic_write(config_path, base64.b64decode(record["content_base64"]) if record["exists"] else b"")
            self._plans.clear()
            return {"backup_id": saved, "message": "已恢复配置，请完整重启 Codex。"}

    def invalidate(self) -> None:
        with self._lock:
            self._plans.clear()
