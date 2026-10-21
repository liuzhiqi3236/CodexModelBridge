"""真实本机 HTTP 边界：入口验证、无凭据探测和失败隔离。"""

from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.errors import BridgeError
from bridge.station import site_from_input
from fixture_station import make_station


class StationTests(unittest.TestCase):
    def setUp(self):
        self.server = make_station([])
        self.server.spa_pages = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def test_only_public_requests_without_credentials_or_page_parameters(self):
        self.assertEqual(site_from_input(self.url + "/unknown/page?token=private#secret"), self.url)
        self.assertEqual(self.server.request_paths, ["/unknown/page/api/v1/settings/public",
                                                   "/unknown/api/v1/settings/public", "/api/v1/settings/public"])
        self.assertEqual(self.server.request_auth, [False, False, False])

    def test_real_deployment_can_have_a_page_like_name(self):
        # 即使部署目录名叫 keys，也必须验证后保留；不可按旧的后缀表删除。
        self.server.base_path = "/team/keys"
        site = self.url + self.server.base_path
        self.assertEqual(site_from_input(site + "/home?tab=1"), site)
        self.assertNotIn("/api/v1/settings/public", self.server.request_paths)

    def test_generic_json_is_not_a_station(self):
        for document in ({}, [], {"code": 0, "data": {}}, {"code": 0, "data": {
                "site_name": "普通网站", "registration_enabled": "false", "email_verify_enabled": True}}):
            with self.subTest(document=document):
                self.server.public_settings = document
                with self.assertRaisesRegex(BridgeError, "没有识别到"):
                    site_from_input(self.url + "/home")

    def test_redirect_is_not_followed_or_accepted_as_original_base(self):
        destination = make_station([])
        threading.Thread(target=destination.serve_forever, daemon=True).start()
        self.addCleanup(destination.server_close)
        self.addCleanup(destination.shutdown)
        self.server.public_status = 302
        self.server.public_redirect = f"http://127.0.0.1:{destination.server_port}/api/v1/settings/public"
        with self.assertRaisesRegex(BridgeError, "没有识别到"):
            site_from_input(self.url)
        self.assertEqual(destination.request_paths, [])

    def test_server_errors_are_not_mistaken_for_wrong_address(self):
        for status in (401, 403, 429, 503):
            with self.subTest(status=status):
                self.server.public_status = status
                self.server.request_paths.clear()
                with self.assertRaisesRegex(BridgeError, f"HTTP {status}"):
                    site_from_input(self.url)
                self.assertEqual(self.server.request_paths, ["/api/v1/settings/public"])

    def test_invalid_address_does_not_start_network_requests(self):
        with patch("bridge.station._is_station") as probe:
            for address in ("ftp://example.com/home", "https://user:pass@example.com/home",
                            "https://example.com:invalid/home", "https://@example.com/home", "not-a-url"):
                with self.subTest(address=address), self.assertRaises(BridgeError):
                    site_from_input(address)
            probe.assert_not_called()

    def test_identification_has_a_total_time_budget(self):
        with patch("bridge.station.time.monotonic", side_effect=[0, 0, 16]), \
                patch("bridge.station._is_station", return_value=False) as probe:
            with self.assertRaisesRegex(BridgeError, "超时"):
                site_from_input(self.url + "/some/page")
            self.assertEqual(probe.call_count, 1)


if __name__ == "__main__":
    unittest.main()
