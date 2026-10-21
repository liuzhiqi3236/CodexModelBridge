"""关键边界回归：真实 Codex 解析、保留配置、加密凭据、分页与本地接口隔离。"""

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.codex import CodexConfig, find_codex
from bridge.catalog import Catalog
from bridge.discovery import Discovery, extract_ids
from bridge.errors import BridgeError
from bridge.net import RestrictedRedirect, fetch, normalize_site, origin
from bridge.server import make_server
from bridge.service import BridgeService
from bridge.storage import digest, protect, write_json
from bridge.sub2api import Sub2Api
from fixture_station import make_station

SOURCE = Path(__file__).resolve().parents[1]
HELPER = Path(os.environ.get("MODELBRIDGE_TEST_ROOT") or SOURCE.parent) / "runtime" / "ModelBridge.Key.exe"


class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(find_codex()[0])
        with tempfile.TemporaryDirectory() as root:
            cfg = CodexConfig(Path(root) / "codex", cls.binary, Path(root) / "data", HELPER)
            cls.base = cfg.bundled()[0]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="模桥 test ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "配置 目录"
        self.home.mkdir()
        self.catalog = self.home / "original.json"
        write_json(self.catalog, {"models": [self.base]})
        self.raw = ('# 保留用户注释\r\nmodel = "' + self.base["slug"] + '"\r\nmodel_provider = "original"\r\n'
                    + 'model_catalog_json = ' + json.dumps(str(self.catalog)) + '\r\n'
                    + '\n[mcp_servers.keep]\ncommand = "keep-this"\n\n[model_providers.original]\nname = "Original"\nbase_url = "https://example.invalid/v1"\nexperimental_bearer_token = "old-secret"\n').encode("utf-8")
        (self.home / "config.toml").write_bytes(self.raw)
        self.config = CodexConfig(self.home, self.binary, self.root / "data", HELPER)
        self.new = deepcopy(self.base)
        self.new["slug"] = "gpt-modelbridge-test"
        self.new["display_name"] = "测试模型"

    def prepare(self, activate=False):
        return self.config.prepare([self.new], site="https://station.example.invalid", key="sk-test-secret-for-unit-test",
                                   key_label="测试密钥", activate_connection=activate)

    def test_prepare_apply_restore_with_real_parser_and_encrypted_key(self):
        plan = self.prepare(True)
        self.assertEqual((self.home / "config.toml").read_bytes(), self.raw)
        self.assertNotIn("sk-test-secret", json.dumps(plan))
        result = self.config.apply(plan["plan_id"])
        raw = (self.home / "config.toml").read_bytes()
        self.assertIn("保留用户注释", raw.decode())
        self.assertIn(b'command = "keep-this"', raw)
        self.assertIn(b'model_provider = "modelbridge"', raw)
        self.assertNotIn(b"sk-test-secret", raw)
        self.assertEqual(self.config.snapshot()["config"]["model_providers"]["modelbridge"]["auth"]["command"], str(HELPER.resolve()))
        self.assertEqual(self.catalog.read_text(encoding="utf-8"), json.dumps({"models": [self.base]}, ensure_ascii=False, indent=2))
        secret = next((self.root / "data" / "secrets").glob("*.bin"))
        self.assertNotIn(b"sk-test-secret", secret.read_bytes())
        read = subprocess.run([str(HELPER), str(secret)], capture_output=True, check=True)
        self.assertEqual(read.stdout.decode(), "sk-test-secret-for-unit-test")
        with self.assertRaises(BridgeError): self.config.apply(plan["plan_id"])
        backup = next((self.root / "data" / "backups").glob("*.bin"))
        self.assertNotIn(b"old-secret", backup.read_bytes())
        self.config.restore(result["backup_id"], digest(raw))
        self.assertEqual((self.home / "config.toml").read_bytes(), self.raw)

    def test_external_edit_after_preview_is_preserved(self):
        plan = self.prepare()
        changed = self.raw + b"\n# another editor\n"
        (self.home / "config.toml").write_bytes(changed)
        with self.assertRaisesRegex(BridgeError, "发生变化"): self.config.apply(plan["plan_id"])
        self.assertEqual((self.home / "config.toml").read_bytes(), changed)

    def test_catalog_edit_after_preview_is_preserved(self):
        plan = self.prepare()
        self.catalog.write_bytes(b'{"models": []}')
        with self.assertRaisesRegex(BridgeError, "目录"): self.config.apply(plan["plan_id"])
        self.assertEqual((self.home / "config.toml").read_bytes(), self.raw)

    def test_real_parser_rejects_invalid_model_before_write(self):
        self.new["context_window"] = "not-a-number"
        with self.assertRaises(BridgeError): self.prepare()
        self.assertEqual((self.home / "config.toml").read_bytes(), self.raw)

    def test_backup_failure_never_changes_config(self):
        plan = self.prepare(True)
        with patch("bridge.codex.protect", side_effect=OSError("disk error")):
            with self.assertRaises(OSError): self.config.apply(plan["plan_id"])
        self.assertEqual((self.home / "config.toml").read_bytes(), self.raw)

    def test_existing_model_does_not_duplicate_when_switching_connection(self):
        self.new = deepcopy(self.base)
        plan = self.prepare(True)
        self.assertEqual(plan["added"], [])
        self.assertEqual(plan["catalog_count"], 1)
        self.config.apply(plan["plan_id"])
        self.assertEqual(len(self.config.snapshot()["models"]), 1)

    def test_restore_rejects_path_traversal_and_stale_config(self):
        plan = self.prepare()
        result = self.config.apply(plan["plan_id"])
        with self.assertRaises(BridgeError): self.config.restore("../secret", "")
        with self.assertRaises(BridgeError): self.config.restore(result["backup_id"], "stale")

    def test_added_model_is_visible_without_changing_abilities(self):
        self.new["visibility"] = "hide"
        plan = self.prepare()
        self.config.apply(plan["plan_id"])
        actual = self.config.snapshot()["models"][-1]
        self.assertEqual(actual["visibility"], "list")
        self.assertEqual(actual["context_window"], self.new["context_window"])
        self.assertEqual(actual["supported_reasoning_levels"], self.new["supported_reasoning_levels"])

    def test_append_only_keeps_connection_and_does_not_write_new_key(self):
        plan = self.prepare(False)
        self.assertIsNone(plan["site"])
        self.assertIsNone(plan["key_label"])
        self.config.apply(plan["plan_id"])
        config = self.config.snapshot()["config"]
        self.assertEqual(config["model_provider"], "original")
        self.assertEqual(config["model_providers"]["original"]["experimental_bearer_token"], "old-secret")
        self.assertNotIn("modelbridge", config["model_providers"])
        self.assertFalse((self.root / "data" / "secrets").exists())

    def test_active_profile_override_is_reported_before_write(self):
        raw = b'profile = "work"\n' + self.raw + b'\n[profiles.work]\nmodel_provider = "original"\n'
        (self.home / "config.toml").write_bytes(raw)
        with self.assertRaisesRegex(BridgeError, "profile"): self.prepare(True)
        self.assertEqual((self.home / "config.toml").read_bytes(), raw)


class StationTests(unittest.TestCase):
    def setUp(self):
        self.server = make_station([])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = Sub2Api(self.url, "fixture-session")

    def test_all_pages_large_ids_and_masked_summaries(self):
        user = self.client.current_user()
        rows = self.client.list_keys()
        self.assertEqual(len(rows), 205)
        self.assertEqual(user["id"], "90071992547409931234")
        self.assertEqual(rows[-1]["id"], "90071992547409931438")
        self.assertNotIn("sk-fixture-not-real-key", json.dumps(rows))
        self.assertTrue(self.client.selected_key(rows[0]["id"]).startswith("sk-fixture"))

    def test_failed_page_keeps_previous_complete_set(self):
        self.client.list_keys()
        self.server.key_count, self.server.broken_page = 305, True
        with self.assertRaises(BridgeError): self.client.list_keys()
        self.assertEqual(len(self.client.keys), 205)

    def test_management_credentials_not_used_for_models(self):
        rows = self.client.list_keys()
        self.assertEqual(self.client.model_manifest(rows[0]["id"], "test"), [])

    def test_refuse_non_web_scheme_and_embedded_credentials(self):
        with self.assertRaises(BridgeError): normalize_site("ftp://example.com")
        with self.assertRaises(BridgeError): normalize_site("https://user:pass@example.com")

    def test_authentication_never_follows_cross_origin_redirect(self):
        redirect = RestrictedRedirect(origin(self.url), True, set())
        with self.assertRaisesRegex(BridgeError, "其他来源"):
            redirect.redirect_request(Request(self.url), None, 302, "Found", {}, "https://other.example/path")


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        shutil.copy2(SOURCE / "sources.json", root / "sources.json")
        write_json(root / "data" / "settings.json", {"codex_home": str(root / "codex")})
        self.service = BridgeService(root)
        self.server = make_station([], key_count=2)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def test_remote_http_reaches_login_window_with_original_address(self):
        # 在真实登录入口验证非本机 HTTP 地址，不向测试域名发出请求。
        opened = []
        self.service.login_opener = lambda site, epoch: opened.append((site, epoch))
        site = "http://203.0.113.10:8080/sub2api"
        self.service.begin_login(site + "/")
        self.assertEqual(opened, [(site, self.service.epoch)])
        self.assertEqual(self.service.settings["site"], site)
        self.assertTrue(self.service.login_pending)

    def test_credential_helper_is_in_internal_directory(self):
        self.assertEqual(self.service.codex.helper, self.service.root / "runtime" / "ModelBridge.Key.exe")

    def test_page_addresses_login_and_read_keys_through_actual_station(self):
        # 模拟真实站点把错误接口路径返回为 HTML，覆盖登录入口到密钥和模型读取。
        self.server.spa_pages = True
        opened = []
        def login(site, epoch):
            with urlopen(site + "/login") as response:
                opened.append((site, response.read()))
            self.service.complete_login(site, "fixture-session", epoch)
        self.service.login_opener = login
        for suffix in ("/login", "/keys", "/keys/", "/login?redirect=/keys", "/keys/?page=2#top"):
            with self.subTest(suffix=suffix):
                self.service.dispatch("logout", {})
                result = self.service.begin_login(self.url + suffix)
                self.assertIsNone(self.service.login_error)
                self.assertEqual(result["site"], self.url)
                self.assertEqual(opened[-1][0], self.url)
                self.assertIn(b"Test sign in", opened[-1][1])
                self.assertEqual(self.service.state()["site"], self.url)
                self.assertEqual(len(self.service.client.keys), 2)
                key_id = next(iter(self.service.client.keys))
                self.assertEqual(self.service.client.model_manifest(key_id, "test"), [])
        self.assertNotIn("/keys/api/v1/auth/me", self.server.request_paths)
        self.assertNotIn("/login/api/v1/auth/me", self.server.request_paths)

    def test_page_address_keeps_deployment_prefix_for_all_station_requests(self):
        self.server.base_path = "/team/sub2api"
        self.server.spa_pages = True
        site = self.url + self.server.base_path
        self.service.login_opener = lambda base, epoch: self.service.complete_login(base, "fixture-session", epoch)
        self.service.begin_login(site + "/keys?page=2")
        self.assertIsNone(self.service.login_error)
        self.assertEqual(self.service.client.site, site)
        key_id = next(iter(self.service.client.keys))
        self.service.client.model_manifest(key_id, "test")
        self.assertEqual(self.server.request_paths, [
            "/team/sub2api/api/v1/auth/me", "/team/sub2api/api/v1/keys", "/team/sub2api/v1/models"])
        saved = json.loads(self.service.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["site"], site)

    def test_logout_rejects_late_login_result(self):
        self.service.login_pending = True
        epoch = self.service.epoch
        self.service.dispatch("logout", {})
        self.service.complete_login(self.url, "fixture-session", epoch)
        self.assertIsNone(self.service.client)
        self.assertIsNone(self.service.user)

    def test_deselect_key_clears_remote_models_and_secrets_stay_private(self):
        self.service.complete_login(self.url, "fixture-session", self.service.epoch)
        key_id = next(iter(self.service.client.keys))
        self.service.dispatch("select-key", {"id":key_id})
        self.service.catalog.remote = [{"slug":"test-only"}]
        self.service.dispatch("select-key", {"id":""})
        self.assertEqual(self.service.key_id, "")
        self.assertEqual(self.service.catalog.remote, [])
        state = json.dumps(self.service.state())
        self.assertNotIn("sk-fixture-not-real-key", state)
        self.assertNotIn("fixture-session", state)

    def test_select_key_reads_and_validates_models_without_applying_config(self):
        self.server.models = [deepcopy(self.service.codex.bundled()[0])]
        self.server.models[0]["slug"] = "gpt-auto-read-test"
        self.service.complete_login(self.url, "fixture-session", self.service.epoch)
        key_id = next(iter(self.service.client.keys))
        config_path = self.service.codex.directory / "config.toml"
        self.assertFalse(config_path.exists())
        result = self.service.dispatch("select-key", {"id": key_id})
        self.assertEqual(result["count"], 1)
        self.assertIsNone(result["error"])
        self.assertEqual(len(self.server.model_requests), 1)
        self.assertTrue(self.service.state()["station_checked"])
        self.assertIn("gpt-auto-read-test", self.service.catalog.metadata())
        self.assertFalse(config_path.exists())

    def test_failed_new_key_read_clears_previous_models_and_can_retry(self):
        self.server.models = [deepcopy(self.service.codex.bundled()[0])]
        self.server.models[0]["slug"] = "gpt-old-key-only"
        self.service.complete_login(self.url, "fixture-session", self.service.epoch)
        keys = list(self.service.client.keys)
        self.service.dispatch("select-key", {"id": keys[0]})
        self.server.model_error = True
        result = self.service.dispatch("select-key", {"id": keys[1]})
        self.assertIn("503", result["error"])
        self.assertEqual(self.service.key_id, keys[1])
        self.assertEqual(self.service.catalog.remote, [])
        self.assertFalse(self.service.station_checked)
        self.assertNotIn("gpt-old-key-only", self.service.catalog.metadata())
        self.server.model_error = False
        self.server.models[0]["slug"] = "gpt-new-key-only"
        retry = self.service.dispatch("refresh-station", {})
        self.assertIsNone(retry["error"])
        self.assertIn("gpt-new-key-only", self.service.catalog.metadata())
        self.assertEqual(self.server.model_requests[-1], "Bearer sk-fixture-not-real-key-1")

    def test_invalid_auto_read_does_not_publish_partial_parameters(self):
        self.service.complete_login(self.url, "fixture-session", self.service.epoch)
        key_id = next(iter(self.service.client.keys))
        self.server.models = [{"slug": "gpt-incomplete", "context_window": "wrong"}]
        result = self.service.dispatch("select-key", {"id": key_id})
        self.assertTrue(result["error"])
        self.assertEqual(self.service.catalog.remote, [])
        self.assertFalse(self.service.station_checked)


class DiscoveryTests(unittest.TestCase):
    def test_scoped_links_exclude_script_and_non_model_links(self):
        source = {"links_only": True, "pattern": r"/models/(gpt-[\w.-]+)", "capture_group": 1}
        text = '<html><script>gpt-fake</script><a href="/api/docs/models/gpt-6-astra">Astra</a><a href="/guides/gpt-other">Guide</a></html>'
        self.assertEqual(extract_ids(text, source), ["gpt-6-astra"])

    def test_previous_models_not_in_current_discovery(self):
        source = {"pattern": r"gemini-[\w.-]+", "stop_before": "Previous models"}
        self.assertEqual(extract_ids("gemini-new\nPrevious models\ngemini-retired", source), ["gemini-new"])

    def test_failure_is_explicit_without_old_data_substitution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_json(root / "sources.json", {"sources":[{"id":"one","name":"One","url":"https://example.com/models","allowed_hosts":["example.com"],"pattern":"gpt-[a-z]+"}]})
            write_json(root / "cache.json", {"sources":[{"id":"one","models":["gpt-old"],"checked_at":"yesterday"}]})
            with patch("bridge.discovery.fetch", side_effect=BridgeError("offline")):
                result = Discovery(root / "sources.json", root / "cache.json").refresh()
            self.assertEqual(result["sources"][0]["models"], [])
            self.assertEqual(result["sources"][0]["error"], "offline")


class DiscoveryChangeTests(unittest.TestCase):
    """跨次刷新走真实缓存读写，验证失败恢复及旧版缓存升级后的新发现标记。"""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = {"id": "test", "name": "Test", "url": "https://example.com/models",
                       "allowed_hosts": ["example.com"], "pattern": r"gpt-[a-z]+", "prefixes": ["gpt-"]}
        write_json(self.root / "sources.json", {"sources": [self.source]})
        self.discovery = Discovery(self.root / "sources.json", self.root / "cache.json")

    def refresh(self, content):
        with patch("bridge.discovery.fetch", return_value=content.encode("utf8")):
            return self.discovery.refresh()["sources"][0]

    def test_first_refresh_is_baseline_then_only_added_names_are_new(self):
        first = self.refresh("gpt-old")
        self.assertEqual(first["new_models"], [])
        self.assertFalse(first["comparison_available"])
        second = self.refresh("gpt-old gpt-new")
        self.assertEqual(second["new_models"], ["gpt-new"])
        self.assertTrue(second["comparison_available"])
        # 重建对象模拟重启；标记属于当前刷新结果，第三次成功后重新计算。
        reopened = Discovery(self.root / "sources.json", self.root / "cache.json")
        catalog = Catalog(); catalog.discovery = reopened.cached()
        rows = catalog.rows([self.source])
        self.assertEqual(rows[0]["id"], "gpt-new")
        self.assertTrue(rows[0]["is_new"])
        self.assertEqual(self.refresh("gpt-old gpt-new")["new_models"], [])

    def test_failed_refresh_keeps_baseline_but_never_displays_old_results(self):
        self.refresh("gpt-old")
        with patch("bridge.discovery.fetch", side_effect=BridgeError("offline")):
            failed = self.discovery.refresh()["sources"][0]
        self.assertEqual(failed["models"], [])
        self.assertEqual(failed["new_models"], [])
        self.assertEqual(failed["error"], "offline")
        self.assertEqual(self.refresh("gpt-old gpt-new")["new_models"], ["gpt-new"])

    def test_old_success_cache_becomes_initial_comparison_record(self):
        write_json(self.root / "cache.json", {"sources": [{"id":"test", "name":"Test",
            "url":self.source["url"], "checked_at":"previous", "models":["gpt-old"], "error":None}]})
        self.assertEqual(self.refresh("gpt-old gpt-new")["new_models"], ["gpt-new"])

    def test_old_failed_cache_and_changed_source_do_not_mark_everything_new(self):
        write_json(self.root / "cache.json", {"sources": [{"id":"test", "models":[], "error":"offline"}]})
        self.assertFalse(self.refresh("gpt-old")["comparison_available"])
        self.source["pattern"] = r"gpt-[a-z-]+"
        write_json(self.root / "sources.json", {"sources":[self.source]})
        changed = self.refresh("gpt-old gpt-new")
        self.assertFalse(changed["comparison_available"])
        self.assertEqual(changed["new_models"], [])


class LocalApiTests(unittest.TestCase):
    def setUp(self):
        class Service:
            def dispatch(self, action, body): return {"action":action}
        self.server = make_server(Service(), SOURCE / "web")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def call(self, origin, token):
        return urlopen(Request(self.url + "/api/state", data=b'{}', headers={"Origin":origin,"X-Bridge-Token":token,"Content-Type":"application/json"}))

    def test_nonce_and_origin_both_required(self):
        for origin, token in [(self.url, "wrong"),("https://evil.example",self.server.session_token)]:
            with self.assertRaises(HTTPError) as error: self.call(origin, token)
            self.assertEqual(error.exception.code, 403)
        with self.call(self.url,self.server.session_token) as result:
            self.assertEqual(json.load(result), {"action":"state"})

    def test_only_allowlisted_assets_are_served(self):
        with self.assertRaises(HTTPError) as error: urlopen(self.url + "/../sources.json")
        self.assertEqual(error.exception.code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
