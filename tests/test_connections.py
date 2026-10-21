"""账户刷新、可修改 API 地址和连接检查的实际 HTTP 回归。"""
from copy import deepcopy
import json
import socket
import ssl
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

import test_core
from fixture_station import make_station
from bridge.errors import BridgeError
from bridge.net import fetch


class ConnectionTests(unittest.TestCase):
    setUp = test_core.SessionTests.setUp

    def login(self):
        self.service.complete_login(self.url, "fixture-session", self.service.epoch)

    def test_login_read_failure_can_refresh_without_login_again(self):
        self.server.user_disconnects = 1
        self.login()
        self.assertTrue(self.service.state()["has_session"])
        self.assertIn("读取账号", self.service.state()["account_error"])
        result = self.service.dispatch("refresh-account", {})
        self.assertIsNone(result["error"])
        self.assertEqual(len(self.service.state()["keys"]), 2)
        self.assertTrue(self.service.state()["account_ready"])
        self.assertNotIn("fixture-session", json.dumps(self.service.state()))

    def test_refresh_failure_keeps_snapshot_and_success_replaces_all(self):
        self.login()
        before = self.service.state()
        self.server.key_count, self.server.broken_page = 205, True
        result = self.service.dispatch("refresh-account", {})
        self.assertIn("读取密钥", result["error"])
        self.assertEqual(self.service.state()["keys"], before["keys"])
        self.assertEqual(self.service.state()["user"], before["user"])
        self.assertFalse(self.service.state()["account_ready"])
        self.server.broken_page, self.server.username = False, "更新的账号名称"
        self.service.dispatch("refresh-account", {})
        self.assertEqual(self.service.user["name"], "更新的账号名称")
        self.assertEqual(len(self.service.client.keys), 205)

    def test_expired_login_requires_relogin_but_server_failure_does_not(self):
        self.login()
        self.server.auth_status = 503
        self.service.dispatch("refresh-account", {})
        self.assertTrue(self.service.state()["has_session"])
        self.server.auth_status = 401
        result = self.service.dispatch("refresh-account", {})
        self.assertIn("重新登录", result["error"])
        self.assertFalse(self.service.state()["has_session"])
        self.assertEqual(len(self.service.state()["keys"]), 2)

    def test_separate_api_receives_only_key_and_configuration_uses_it(self):
        models = [deepcopy(self.service.codex.bundled()[0])]
        gateway = make_station(models)
        gateway.base_path = "/gateway"
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        self.addCleanup(gateway.server_close); self.addCleanup(gateway.shutdown)
        api_url = f"http://127.0.0.1:{gateway.server_port}/gateway/v1"
        self.login()
        self.service.dispatch("set-api-address", {"api_url": api_url})
        self.service.dispatch("select-key", {"id": next(iter(self.service.client.keys))})
        self.assertEqual(gateway.request_paths, ["/gateway/v1/models"])
        self.assertEqual(gateway.model_requests, ["Bearer sk-fixture-not-real-key-0"])
        with patch.object(self.service.codex, "prepare", return_value={}) as prepare:
            self.service.dispatch("prepare", {"models": [models[0]["slug"]], "activate": True})
            self.assertEqual(prepare.call_args.kwargs["api_url"], api_url)
        self.service.dispatch("refresh-account", {})
        self.assertEqual(self.service.state()["api_url"], api_url)

    def test_api_defaults_to_site_and_manual_edit_is_preserved_on_same_site_login(self):
        self.server.public_settings["data"]["api_base_url"] = "https://ignored.example.invalid"
        self.login()
        self.assertEqual(self.service.state()["api_url"], self.url)
        changed = "https://gateway.example.invalid/custom/v1"
        self.service.dispatch("set-api-address", {"api_url": changed})
        self.login()
        self.assertEqual(self.service.state()["api_url"], changed)
        self.service.login_opener = lambda site, epoch: None
        with patch("bridge.service.site_from_input", return_value="https://different.example.invalid"):
            self.service.begin_login("https://different.example.invalid")
        self.assertEqual(self.service.state()["api_url"], "https://different.example.invalid")

    def test_connection_check_reports_missing_login_without_model_requests(self):
        result = self.service.dispatch("test-connection", {"site": self.url + "/home", "api_url": self.url})
        self.assertEqual([row["ok"] for row in result["checks"]], [True, None, None])
        self.assertEqual(self.server.model_requests, [])

    def test_connection_check_reads_only_and_shows_actual_target(self):
        self.login()
        self.service.key_id = next(iter(self.service.client.keys))
        result = self.service.dispatch("test-connection", {"site": self.url, "api_url": self.url + "/v1"})
        self.assertEqual([row["ok"] for row in result["checks"]], [True, True, True])
        self.assertEqual(result["api_url"], self.url + "/v1")
        self.assertFalse((self.service.codex.directory / "config.toml").exists())
        self.assertTrue(all("responses" not in p for p in self.server.request_paths))

    def test_other_site_never_receives_current_login_or_key(self):
        self.login()
        self.service.key_id = next(iter(self.service.client.keys))
        other = make_station([])
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close); self.addCleanup(other.shutdown)
        url = f"http://127.0.0.1:{other.server_port}"
        result = self.service.dispatch("test-connection", {"site": url, "api_url": url})
        self.assertEqual([row["ok"] for row in result["checks"]], [True, None, None])
        self.assertFalse(any(other.request_auth))

    def test_logout_during_refresh_cannot_restore_old_session(self):
        self.login()
        entered, resume = threading.Event(), threading.Event()
        def wait_user(client):
            entered.set(); resume.wait(3)
            return {"id": "1", "email": "old@example.invalid", "name": "old"}
        with patch("bridge.sub2api.Sub2Api.current_user", wait_user):
            worker = threading.Thread(target=lambda: self.service.dispatch("refresh-account", {}))
            worker.start(); self.assertTrue(entered.wait(2))
            self.service.dispatch("logout", {})
            resume.set(); worker.join(3)
        self.assertFalse(self.service.state()["has_session"])
        self.assertIsNone(self.service.user)

    def test_duplicate_refresh_is_rejected_and_old_preview_invalidated(self):
        self.login()
        self.service.codex._plans["old"] = {"id": "old"}
        entered, resume = threading.Event(), threading.Event()
        def wait_user(client):
            entered.set(); resume.wait(3)
            return {"id": "1", "email": "demo@example.invalid", "name": "demo"}
        with patch("bridge.sub2api.Sub2Api.current_user", wait_user):
            worker = threading.Thread(target=lambda: self.service.dispatch("refresh-account", {}))
            worker.start(); self.assertTrue(entered.wait(2))
            try:
                with self.assertRaisesRegex(BridgeError, "正在刷新"):
                    self.service.dispatch("refresh-account", {})
                with self.assertRaisesRegex(BridgeError, "预览已失效"):
                    self.service.dispatch("apply", {"plan_id": "old"})
            finally:
                resume.set(); worker.join(3)

    def test_removed_key_is_cleared_after_account_refresh(self):
        self.login()
        self.service.key_id = list(self.service.client.keys)[-1]
        self.server.key_count = 1
        self.service.dispatch("refresh-account", {})
        self.assertEqual(self.service.key_id, "")

    def test_invalid_pagination_can_be_retried_without_sticking_refresh(self):
        self.login()
        from bridge import sub2api
        original = sub2api.fetch_json
        def invalid_keys(url, **kwargs):
            document = original(url, **kwargs)
            if "/keys?" in url:
                document["data"]["total"] = None
            return document
        with patch("bridge.sub2api.fetch_json", invalid_keys):
            result = self.service.dispatch("refresh-account", {})
        self.assertIn("分页总数", result["error"])
        self.assertFalse(self.service.account_refreshing)
        self.assertTrue(self.service.state()["has_session"])
        self.assertIsNone(self.service.dispatch("refresh-account", {})["error"])

    def test_key_unauthorized_does_not_expire_website_login(self):
        self.login()
        self.service.key_id = next(iter(self.service.client.keys))
        self.server.model_status = 401
        result = self.service.dispatch("test-connection", {"site": self.url, "api_url": self.url})
        self.assertEqual([row["ok"] for row in result["checks"]], [True, True, False])
        self.assertIn("Key 无效", result["checks"][-1]["detail"])
        self.assertTrue(self.service.state()["has_session"])

    def test_network_error_categories_do_not_expose_original_error(self):
        # 在请求边界注入 TLS、DNS 等错误，核对 UI 收到的可操作信息与脱敏结果。
        for error, expected in [(ssl.SSLEOFError("secret-detail"), "SSL EOF"),
                                (ssl.SSLCertVerificationError("secret-detail"), "证书"),
                                (socket.gaierror("secret-detail"), "域名解析"),
                                (TimeoutError("secret-detail"), "超时"),
                                (ConnectionRefusedError("secret-detail"), "连接被拒绝")]:
            with self.subTest(expected=expected), patch("bridge.net.build_opener") as opener:
                opener.return_value.open.side_effect = URLError(error)
                with self.assertRaisesRegex(BridgeError, expected) as failure:
                    fetch("https://station.example.invalid/api", token="fake-secret")
                self.assertNotIn("secret", str(failure.exception))
                self.assertEqual(opener.return_value.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
