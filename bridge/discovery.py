"""联网发现官方模型名称，保留来源，不把目录出现误报为调用成功。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urlsplit, unquote

from .codex import utc_now
from .errors import BridgeError
from .net import fetch
from .storage import write_json


class DocumentText(HTMLParser):
    """只从可见文本和链接提取标识，避免扫描脚本中无关的构建数据。"""
    def __init__(self):
        super().__init__()
        self.parts = []
        self.links = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.ignored += 1
        if tag == "a" and not self.ignored:
            for key, value in attrs:
                if key == "href" and value:
                    self.parts.append(unquote(value))
                    self.links.append(unquote(value))

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.ignored:
            self.ignored -= 1

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def extract_ids(content: str, source: dict) -> list[str]:
    if "<html" in content[:2000].lower() or "<!doctype" in content[:2000].lower():
        parser = DocumentText()
        parser.feed(content)
        text = "\n".join(parser.links if source.get("links_only") else parser.parts)
    else:
        text = content
    if source.get("stop_before"):
        stop = re.search(source["stop_before"], text, re.IGNORECASE)
        if stop:
            text = text[:stop.start()]
    result, seen = [], set()
    pattern = re.compile(source["pattern"], re.IGNORECASE)
    exclude = re.compile(source["exclude"], re.IGNORECASE) if source.get("exclude") else None
    for match in pattern.finditer(text):
        model = match.group(source.get("capture_group", 0)).strip().lower()
        if model in seen or (exclude and exclude.search(model)):
            continue
        seen.add(model)
        result.append(model)
    return result


class Discovery:
    def __init__(self, registry: Path, cache: Path):
        self.registry = registry
        self.cache = cache

    def sources(self) -> list[dict]:
        if not self.registry.is_file():
            raise BridgeError("模型来源配置不存在，请保持程序文件夹完整。")
        try:
            value = json.loads(self.registry.read_text(encoding="utf-8-sig"))
            sources = value["sources"]
            if not isinstance(sources, list) or len({row["id"] for row in sources}) != len(sources):
                raise ValueError()
            for source in sources:
                parts = urlsplit(source["url"])
                if parts.scheme != "https" or parts.hostname not in source["allowed_hosts"]:
                    raise ValueError()
                re.compile(source["pattern"])
            return sources
        except (ValueError, KeyError, TypeError, re.error):
            raise BridgeError("模型来源配置格式不正确，请检查 sources.json。") from None

    def cached(self) -> dict:
        return json.loads(self.cache.read_text(encoding="utf-8")) if self.cache.is_file() else {"sources": []}

    @staticmethod
    def _read(source: dict) -> dict:
        content = fetch(source["url"], allowed_hosts=set(source["allowed_hosts"])).decode("utf-8")
        names = extract_ids(content, source)
        if not names:
            raise BridgeError("本次页面没有识别到模型名称，来源格式可能发生变化。")
        return {"id": source["id"], "name": source["name"], "url": source["url"], "checked_at": utc_now(),
                "models": names, "error": None}

    def refresh(self) -> dict:
        sources = self.sources()
        previous = self.cached()
        baselines = previous.get("baselines", {})
        # 旧版成功缓存显式升级为比较记录；失败结果不作为比较依据。
        if "baselines" not in previous:
            configured = {source["id"]: source for source in sources}
            for row in previous["sources"]:
                source = configured.get(row["id"])
                if source and not row.get("error") and row.get("checked_at") and row.get("url") == source["url"]:
                    baselines[row["id"]] = {"source": source, "models": row["models"]}
        # 来源被删除后不保留无用比较记录；配置变化会在成功读取时建立新基线。
        baselines = {source["id"]: baselines[source["id"]] for source in sources if source["id"] in baselines}
        result = {}
        with ThreadPoolExecutor(max_workers=4) as executor:
            tasks = {executor.submit(self._read, source): source for source in sources}
            for task in as_completed(tasks):
                source = tasks[task]
                try:
                    current = task.result()
                    baseline = baselines.get(source["id"])
                    comparable = baseline is not None and baseline["source"] == source
                    current["comparison_available"] = comparable
                    previous_names = set(baseline["models"]) if comparable else set()
                    current["new_models"] = [name for name in current["models"] if name not in previous_names] if comparable else []
                    result[source["id"]] = current
                    baselines[source["id"]] = {"source": source, "models": current["models"]}
                except (BridgeError, UnicodeError) as exc:
                    result[source["id"]] = {"id": source["id"], "name": source["name"], "url": source["url"],
                        "checked_at": None, "models": [],
                        "error": str(exc), "failed_at": utc_now(), "new_models": [], "comparison_available": False}
        value = {"sources": [result[source["id"]] for source in sources], "baselines": baselines}
        write_json(self.cache, value)
        return value
