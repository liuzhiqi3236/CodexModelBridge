"""独立测试入口：使用临时 Codex 配置和本机模拟站点，不读写真实密钥。"""

from copy import deepcopy
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import threading

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from bridge.codex import CodexConfig, find_codex
from bridge.server import make_server
from bridge.service import BridgeService
from bridge.storage import write_json
from fixture_station import make_station

parser = argparse.ArgumentParser()
parser.add_argument("--experience", action="store_true", help="离线验证五项体验改进")
parser.add_argument("--address-login", action="store_true", help="通过模拟站点验证页面地址登录")
args = parser.parse_args()
root = SOURCE / "work" / ("ui-address" if args.address_login else "ui-experience" if args.experience else "ui-validation")
root.mkdir(parents=True, exist_ok=True)
shutil.copy2(SOURCE / "sources.json", root / "sources.json")
(root / "runtime").mkdir(exist_ok=True)
package = Path(os.environ.get("MODELBRIDGE_TEST_ROOT") or SOURCE.parent)
shutil.copy2(package / "runtime" / "ModelBridge.Key.exe", root / "runtime" / "ModelBridge.Key.exe")
binary = Path(find_codex()[0])
home = root / "codex-test"
home.mkdir(exist_ok=True)
probe = CodexConfig(home, binary, root / "data", root / "runtime" / "ModelBridge.Key.exe")
base = probe.bundled()[0]
write_json(home / "initial.json", {"models":[base]})
(home / "config.toml").write_text('# 仅供隔离验证\nmodel = ' + json.dumps(base["slug"]) + '\nmodel_catalog_json = ' + json.dumps(str(home / "initial.json")) + '\n', encoding="utf-8")
write_json(root / "data" / "settings.json", {"codex_home":str(home),"codex_binary":str(binary)})
model = deepcopy(base)
model.update({"slug":"gpt-modelbridge-validation", "display_name":"隔离验证模型"})
if args.experience:
    model.update({"slug":"gpt-modelbridge-new", "display_name":"自动读取演示模型"})
station = make_station([model], key_count=3)
threading.Thread(target=station.serve_forever, daemon=True).start()
site = f"http://127.0.0.1:{station.server_port}"
service = BridgeService(root)
if args.experience:
    # 演示资料、第二个 Key 的失败、参数导入均为本机假数据，不访问官方或真实站点。
    station.fail_model_keys.add("Bearer sk-fixture-not-real-key-1")
    sources = service.discovery.sources()
    def read_fixture(source):
        prefix = source["prefixes"][0]
        return {"id":source["id"], "name":source["name"], "url":source["url"], "checked_at":"2026-10-21T04:00:00Z",
                "models":[prefix+"modelbridge-known",prefix+"modelbridge-new"], "error":None}
    seed = {"sources":[{**read_fixture(source), "models":[source["prefixes"][0]+"modelbridge-known"]} for source in sources]}
    write_json(root / "data" / "official-models.json", seed)
    service.catalog.discovery = seed
    service.discovery._read = read_fixture
    imported = deepcopy(base)
    imported.update({"slug":"claude-modelbridge-new", "display_name":"导入参数演示模型"})
    write_json(root / "claude-test-parameters.json", {"models":[imported]})
    service.external_opener = lambda url: None
service.settings["site"] = site
if args.address_login:
    # 测试登录适配器使用假令牌，真实账号与密钥从不进入浏览器验证。
    station.spa_pages = True
    service.settings["site"] = site + "/keys"
    service.login_opener = lambda base, epoch: service.complete_login(base, "fixture-session", epoch)
else:
    service.complete_login(site, "fixture-session", service.epoch)
server = make_server(service, SOURCE / "web")
print(f"http://127.0.0.1:{server.server_port}/#token={server.session_token}", flush=True)
server.serve_forever()
