"""应用操作边界：串行改变选择与配置，远端登录按会话代次提交。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading

from .catalog import Catalog
from .codex import CodexConfig, find_codex, model_entries
from .discovery import Discovery
from .errors import BridgeError
from .station import site_from_input
from .storage import digest, write_json
from .sub2api import Sub2Api


class BridgeService:
    def __init__(self, root: Path):
        self.root = root
        self.data = root / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.data / "settings.json"
        self.settings = json.loads(self.settings_path.read_text(encoding="utf-8")) if self.settings_path.exists() else {}
        self.registry = root / "sources.json"
        self.discovery = Discovery(self.registry, self.data / "official-models.json")
        self.catalog = Catalog()
        self.catalog.discovery = self.discovery.cached()
        self.client: Sub2Api | None = None
        self.key_id = ""
        self.station_checked = False
        self.station_error = None
        self.user = None
        self.login_pending = False
        self.login_error = None
        self.epoch = 0
        self._lock = threading.RLock()
        self.login_opener = None
        self.external_opener = None
        binaries = find_codex()
        binary = self.settings.get("codex_binary") or (binaries[0] if binaries else "")
        directory = self.settings.get("codex_home") or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        self.codex = CodexConfig(Path(directory), Path(binary) if binary else root / "missing-codex.exe",
                                 self.data, root / "runtime" / "ModelBridge.Key.exe")
        self.load_error = None
        try:
            self._reload_catalog()
        except BridgeError as exc:
            self.load_error = str(exc)

    def _reload_catalog(self):
        self.catalog.local = self.codex.snapshot()["models"]
        self.catalog.bundled = self.codex.bundled()
        self.load_error = None

    def _save_settings(self):
        # 设置文件只包含路径和地址；登录凭据及完整 Key 永不写入。
        write_json(self.settings_path, self.settings)

    def _clear_station(self):
        """切换账号或密钥即丢弃原目录，读取失败也不会继续显示旧密钥的数据。"""
        self.catalog.clear_remote()
        self.station_checked, self.station_error = False, None
        self.codex.invalidate()

    def _read_station(self) -> dict:
        """选择密钥和手动重试共用入口；读取失败与密钥选择成功分别表达。"""
        self._clear_station()
        try:
            models = self.client.model_manifest(self.key_id, self.codex.version())
            self.codex.validate_catalog(models)
        except BridgeError as exc:
            self.station_error = str(exc)
            return {"count": 0, "error": self.station_error}
        self.catalog.remote = models
        self.station_checked = True
        return {"count": len(models), "error": None}

    def begin_login(self, site: str) -> dict:
        with self._lock:
            if self.login_pending:
                raise BridgeError("站点登录窗口已打开，请先完成或关闭它。")
            if not self.login_opener:
                raise BridgeError("站点登录需要桌面窗口，请使用 ModelBridge 可执行程序。")
            # 先确认公开入口；识别失败时保留已有账号、密钥及设置。
            site = site_from_input(site)
            self.epoch += 1
            epoch = self.epoch
            self.client, self.user, self.key_id = None, None, ""
            self._clear_station()
            self.login_pending, self.login_error = True, None
            self.settings["site"] = site
            self._save_settings()
        try:
            self.login_opener(site, epoch)
        except Exception:
            self.finish_login_error(epoch, "无法打开站点登录窗口，请检查 WebView2 运行环境。")
            raise BridgeError("无法打开站点登录窗口。") from None
        return {"started": True, "site": site}

    def complete_login(self, site: str, token: str, epoch: int) -> None:
        # 网络验证在锁外执行；提交时必须仍属于同一轮登录。
        client = Sub2Api(site, token)
        try:
            user = client.current_user()
            client.list_keys()
        except BridgeError as exc:
            self.finish_login_error(epoch, str(exc))
            return
        with self._lock:
            if self.epoch != epoch:
                return
            self.client, self.user = client, user
            self.login_pending, self.login_error = False, None

    def finish_login_error(self, epoch: int, message: str | None):
        with self._lock:
            if self.epoch == epoch and not self.client and self.login_pending:
                self.login_pending, self.login_error = False, message

    def state(self) -> dict:
        with self._lock:
            try:
                status = self.codex.status()
                error = None
            except BridgeError as exc:
                status, error = None, str(exc)
            return {"site": self.settings.get("site", ""), "user": self.user,
                    "keys": [self.client.key_summary(row) for row in self.client.keys.values()] if self.client else [],
                    "key_id": self.key_id, "login_pending": self.login_pending, "login_error": self.login_error,
                    "station_checked": self.station_checked, "station_error": self.station_error,
                    "station_count": len(self.catalog.remote),
                    "codex": status, "codex_error": error, "codex_home": str(self.codex.directory),
                    "codex_binary": str(self.codex.binary), "binaries": find_codex(),
                    "sources": self.discovery.sources(), "source_results": self.catalog.discovery["sources"],
                    "models": self.catalog.rows(self.discovery.sources())}

    def dispatch(self, action: str, body: dict) -> dict:
        if action == "start-login":
            return self.begin_login(str(body.get("site", "")))
        if action == "state":
            return self.state()
        # 所有会改变密钥/模型/方案的操作串行，避免旧响应与新选择交叉。
        with self._lock:
            if action == "logout":
                self.epoch += 1
                self.client, self.user, self.key_id = None, None, ""
                self.login_pending, self.login_error = False, None
                self._clear_station()
                return {"ok": True}
            if action == "refresh-keys":
                if not self.client:
                    raise BridgeError("请先登录站点。")
                keys = self.client.list_keys()
                self.key_id = ""
                self._clear_station()
                return {"keys": keys}
            if action == "select-key":
                if not self.client:
                    raise BridgeError("请先登录站点。")
                key_id = str(body.get("id", ""))
                if key_id:
                    self.client.selected_key(key_id)
                self.key_id = key_id
                if not key_id:
                    self._clear_station()
                    return {"selected": "", "count": 0, "error": None}
                return {"selected": key_id, **self._read_station()}
            if action == "refresh-official":
                self.catalog.discovery = self.discovery.refresh()
                return {"sources": self.catalog.discovery["sources"]}
            if action == "refresh-station":
                if not self.client or not self.key_id:
                    raise BridgeError("请先选择 API Key。")
                return self._read_station()
            if action == "add-model":
                model = str(body.get("id", "")).strip()
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}", model):
                    raise BridgeError("请输入准确模型 ID，只能包含字母、数字及 . _ : / -。")
                if model not in self.catalog.manual:
                    self.catalog.manual.append(model)
                return {"id": model, "ready": model in self.catalog.metadata()}
            if action == "import-catalog":
                try:
                    document = json.loads(str(body.get("content", "")))
                except ValueError:
                    raise BridgeError("导入内容不是有效 JSON。") from None
                models = model_entries(document)
                self.codex.validate_catalog(models)
                self.catalog.imported = models
                self.codex.invalidate()
                return {"count": len(models)}
            if action == "save-settings":
                directory, binary = Path(str(body.get("codex_home", ""))), Path(str(body.get("codex_binary", "")))
                if not directory.is_absolute() or not binary.is_absolute() or not binary.is_file() or binary.name.lower() != "codex.exe":
                    raise BridgeError("请选择绝对路径的 Codex 配置目录和 codex.exe。")
                candidate = CodexConfig(directory, binary, self.data, self.root / "runtime" / "ModelBridge.Key.exe")
                candidate.status()
                self.codex.invalidate()
                self.codex = candidate
                self.settings.update({"codex_home": str(directory), "codex_binary": str(binary)})
                self._save_settings()
                self._reload_catalog()
                return {"ok": True}
            if action == "prepare":
                names = body.get("models", [])
                if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                    raise BridgeError("模型选择格式错误。")
                activate = body.get("activate") is True
                if activate and (not self.client or not self.key_id):
                    raise BridgeError("应用新连接前，请先登录并选择 Key。")
                rows = self.catalog.selected(names)
                key = self.client.selected_key(self.key_id) if activate else ""
                label = self.client.key_summary(self.client.keys[self.key_id]) if activate else {}
                return self.codex.prepare(rows, site=self.client.site if activate else "", key=key,
                    key_label=(label.get("name", "") + " · " + label.get("masked", "")),
                    activate_connection=activate)
            if action == "apply":
                result = self.codex.apply(str(body.get("plan_id", "")))
                self._reload_catalog()
                return result
            if action == "backups":
                path = self.codex.directory / "config.toml"
                return {"items": self.codex.backups(), "digest": digest(path.read_bytes() if path.exists() else b"")}
            if action == "restore":
                result = self.codex.restore(str(body.get("id", "")), str(body.get("digest", "")))
                self._reload_catalog()
                return result
            if action == "open-source":
                source = next((row for row in self.discovery.sources() if row["id"] == body.get("id")), None)
                if not source or not self.external_opener:
                    raise BridgeError("无法打开来源页面。")
                self.external_opener(source["url"])
                return {"ok": True}
            raise BridgeError("未知操作。")
