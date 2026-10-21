"""应用操作边界：串行改变选择与配置，远端登录按会话代次提交。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading

from .catalog import Catalog
from .codex import CodexConfig, find_codex, model_entries, utc_now
from .discovery import Discovery
from .errors import BridgeError, HTTPStatusError
from .net import api_base_url, normalize_site
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
        self.account_error = None
        self.account_updated_at = None
        self.account_refreshing = False
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

    @property
    def api_url(self) -> str:
        # 默认使用网站地址是用户指定的产品规则；手动值单独保存在设置中。
        return self.settings.get("api_url", self.settings.get("site", ""))

    def _set_site(self, site: str, api_url: str | None = None):
        address = normalize_site(api_url) if api_url is not None else (
            self.api_url if self.settings.get("site") == site else site)
        self.settings.update({"site": site, "api_url": address})
        self._save_settings()

    def _account_ready(self) -> bool:
        return bool(self.client and self.client.access_token and self.user and not self.account_error
                    and not self.account_refreshing and not self.login_pending)

    def _clear_station(self):
        """切换账号或密钥即丢弃原目录，读取失败也不会继续显示旧密钥的数据。"""
        self.catalog.clear_remote()
        self.station_checked, self.station_error = False, None
        self.codex.invalidate()

    def _read_station(self) -> dict:
        """选择密钥和手动重试共用入口；读取失败与密钥选择成功分别表达。"""
        self._clear_station()
        try:
            self.client.api_url = self.api_url
            models = self.client.model_manifest(self.key_id, self.codex.version())
            self.codex.validate_catalog(models)
        except BridgeError as exc:
            self.station_error = str(exc)
            return {"count": 0, "error": self.station_error}
        self.catalog.remote = models
        self.station_checked = True
        return {"count": len(models), "error": None}

    def begin_login(self, site: str, api_url: str | None = None) -> dict:
        with self._lock:
            if self.login_pending:
                raise BridgeError("站点登录窗口已打开，请先完成或关闭它。")
            if not self.login_opener:
                raise BridgeError("站点登录需要桌面窗口，请使用 ModelBridge 可执行程序。")
            # 先确认公开入口；识别失败时保留已有账号、密钥及设置。
            site = site_from_input(site)
            self._set_site(site, api_url)
            self.epoch += 1
            epoch = self.epoch
            self.client, self.user, self.key_id = None, None, ""
            self._clear_station()
            self.login_pending, self.login_error = True, None
            self.account_error, self.account_updated_at, self.account_refreshing = None, None, False
        try:
            self.login_opener(site, epoch)
        except Exception:
            self.finish_login_error(epoch, "无法打开站点登录窗口，请检查 WebView2 运行环境。")
            raise BridgeError("无法打开站点登录窗口。") from None
        return {"started": True, "site": site}

    def complete_login(self, site: str, token: str, epoch: int) -> None:
        # 先接管本轮令牌，再读取资料；网络中断不会丢掉刚完成的网页登录。
        with self._lock:
            if self.epoch != epoch:
                return
            self._set_site(site)
            self.client = Sub2Api(site, token)
            self.login_pending, self.login_error = True, None
        self.refresh_account()

    def refresh_account(self) -> dict:
        with self._lock:
            client, epoch = self.client, self.epoch
            if not client or not client.access_token:
                raise BridgeError("请先登录站点；登录已过期时需要重新登录。")
            if self.account_refreshing:
                raise BridgeError("账户信息正在刷新，请稍候。")
            self.account_refreshing = True
            self.codex.invalidate()
            candidate = Sub2Api(client.site, client.access_token)
        # 账号与全部分页作为一次快照提交；失败不发布部分列表。
        stage, error, expired = "读取账号", None, False
        try:
            user = candidate.current_user()
            stage = "读取密钥"
            candidate.list_keys()
        except BridgeError as exc:
            expired = isinstance(exc, HTTPStatusError) and exc.status == 401
            error = stage + "失败：" + ("登录已过期或失效，请重新登录。" if expired else str(exc))
        with self._lock:
            if self.epoch != epoch or self.client is not client:
                return {"ignored": True, "error": None}
            self.account_refreshing = False
            self.login_pending = False
            self.account_error = self.login_error = error
            self._clear_station()
            if error:
                if expired:
                    client.access_token = ""
                return {"error": error}
            self.user, client.keys = user, candidate.keys
            # 已删除或停用的 Key 不得继续用于模型读取或配置切换。
            selected = client.keys.get(self.key_id)
            if not selected or selected.get("status") != "active":
                self.key_id = ""
            self.account_updated_at = utc_now()
            return {"error": None, "count": len(client.keys)}

    def test_connection(self, site: str, api_url: str) -> dict:
        """逐项报告公开入口、账号及 Key；没有前置条件的项目显示未检测。"""
        checks = []
        resolved, endpoint = None, None
        try:
            resolved = site_from_input(site)
            checks.append({"label": "网站连接", "ok": True, "detail": resolved})
        except BridgeError as exc:
            checks.append({"label": "网站连接", "ok": False, "detail": str(exc)})
        client = self.client
        same_site = bool(client and resolved == client.site)
        logged_in = bool(same_site and client.access_token)
        if not logged_in:
            checks.append({"label": "登录状态", "ok": None,
                           "detail": "网站连接未通过，本次未检测登录状态。" if not resolved else "尚未登录该网站，请先登录。"})
        else:
            try:
                client.current_user()
                checks.append({"label": "登录状态", "ok": True, "detail": "当前登录有效。"})
            except BridgeError as exc:
                expired = isinstance(exc, HTTPStatusError) and exc.status == 401
                detail = "登录已过期或失效，请重新登录。" if expired else str(exc)
                checks.append({"label": "登录状态", "ok": False, "detail": detail})
                if expired:
                    client.access_token = ""
                    self.account_error = self.login_error = detail
                    self._clear_station()
        if not same_site or not self.key_id or not self._account_ready():
            checks.append({"label": "Key 访问", "ok": None, "detail": "请先刷新账户并选择有效 Key。"})
        else:
            try:
                endpoint = api_base_url(api_url)
                count = client.check_api(self.key_id, endpoint)
                checks.append({"label": "Key 访问", "ok": True, "detail": f"{endpoint} · 已读取 {count} 个模型。"})
            except BridgeError as exc:
                detail = "Key 无效或已失效，请刷新账户并核对 Key。" if isinstance(exc, HTTPStatusError) and exc.status == 401 else str(exc)
                checks.append({"label": "Key 访问", "ok": False, "detail": detail})
        return {"checks": checks, "api_url": endpoint, "checked_at": utc_now()}

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
                    "api_url": self.api_url,
                    "api_base_url": api_base_url(self.api_url) if self.api_url else "",
                    "has_session": bool(self.client and self.client.access_token),
                    "account_ready": self._account_ready(), "account_error": self.account_error,
                    "account_refreshing": self.account_refreshing, "account_updated_at": self.account_updated_at,
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
            return self.begin_login(str(body.get("site", "")), body.get("api_url"))
        if action in ("refresh-account", "refresh-keys"):
            return self.refresh_account()
        if action == "state":
            return self.state()
        # 所有会改变密钥/模型/方案的操作串行，避免旧响应与新选择交叉。
        with self._lock:
            if action == "logout":
                self.epoch += 1
                self.client, self.user, self.key_id = None, None, ""
                self.login_pending, self.login_error = False, None
                self.account_error, self.account_updated_at, self.account_refreshing = None, None, False
                self._clear_station()
                return {"ok": True}
            if action == "set-api-address":
                if not self.settings.get("site"):
                    raise BridgeError("请先连接网站，再修改 API 地址。")
                address = normalize_site(str(body.get("api_url", "")))
                if address != self.api_url:
                    self.settings["api_url"] = address
                    self._save_settings()
                    self._clear_station()
                return {"api_url": address, "api_base_url": api_base_url(address)}
            if action == "test-connection":
                return self.test_connection(str(body.get("site", "")), str(body.get("api_url", "")))
            if action == "select-key":
                if not self._account_ready():
                    raise BridgeError("请先刷新账户信息；登录失效时需要重新登录。")
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
                if not self._account_ready() or not self.key_id:
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
                if activate and (not self._account_ready() or not self.key_id):
                    raise BridgeError("应用新连接前，请先登录并选择 Key。")
                rows = self.catalog.selected(names)
                key = self.client.selected_key(self.key_id) if activate else ""
                label = self.client.key_summary(self.client.keys[self.key_id]) if activate else {}
                return self.codex.prepare(rows, api_url=self.api_url if activate else "", key=key,
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
