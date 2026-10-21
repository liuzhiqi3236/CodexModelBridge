/* 页面只处理展示和用户操作；密钥、配置读写与校验全部留在本机服务端。 */
"use strict";
const $ = id => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
const token = fragment.get("token") || sessionStorage.getItem("modelbridge-session");
if (token) sessionStorage.setItem("modelbridge-session", token);
history.replaceState(null, "", "/");
let state = null, busy = false, provider = "", plan = null, backupDigest = "", restoreId = "";
let selected = new Set(), loginTimer = null, toastTimer = null;
let stationLoading = false, helpModelId = "";

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function toast(message, failure = false) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").className = "toast" + (failure ? " failure" : "");
  toastTimer = setTimeout(() => $("toast").classList.add("hidden"), failure ? 10000 : 6500);
}

async function api(action, body = {}) {
  const response = await fetch("/api/" + action, {method:"POST", headers:{"Content-Type":"application/json", "X-Bridge-Token":token || ""}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "操作失败，请重新打开工具。");
  return data;
}

// 每个提交入口共用忙碌门闩；操作完成前不能再次发出提交。
async function action(operation) {
  if (busy) return;
  busy = true;
  updateControls();
  try { await operation(); }
  catch (error) { toast(error.message || "操作未完成。", true); }
  finally { busy = false; updateControls(); }
}

function updateControls() {
  document.querySelectorAll("button").forEach(button => {button.disabled = busy;});
  $("busy-line").classList.toggle("hidden", !busy);
  $("login-btn").disabled = busy || !!state?.login_pending;
  $("key").disabled = busy || !state?.user;
  $("refresh-keys-btn").disabled = busy || !state?.user;
  $("station-btn").disabled = busy || !state?.key_id;
  $("append-only").disabled = busy;
  $("switch-connection").disabled = busy || !state?.key_id;
  $("preview-btn").disabled = busy || !selected.size || !state?.codex;
  $("apply-btn").disabled = busy || !plan;
  $("select-all").disabled = busy;
  document.querySelectorAll(".model-row input").forEach(input => {input.disabled = busy || input.dataset.locked === "true";});
  $("catalog-file").disabled = busy;
  $("parameter-file").disabled = busy;
}

function timeLabel(value) {
  return value ? new Date(value).toLocaleString("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}) : "尚未刷新";
}

function renderSources() {
  const area = $("source-results"); area.replaceChildren();
  for (const source of state.sources) {
    const result = state.source_results.find(item => item.id === source.id);
    const card = element("div", "source-card" + (provider === source.id ? " active" : "") + (result?.error ? " failed" : ""));
    // 筛选和官方资料是并列按钮，点击卡片主体不会误打开外部页面。
    const filter = element("button", "source-select"); filter.type = "button";
    filter.dataset.provider = source.id;
    filter.setAttribute("aria-label", "筛选 " + source.name + " 模型");
    filter.setAttribute("aria-pressed", String(provider === source.id));
    filter.addEventListener("click", () => selectProvider(source.id));
    const link = element("button", "source-link", "↗"); link.type = "button"; link.title = "打开官方资料";
    link.setAttribute("aria-label", "打开 " + source.name + " 官方资料");
    link.addEventListener("click", () => action(() => api("open-source", {id:source.id})));
    filter.append(element("strong", "", source.name));
    const detail = element("span", "source-detail", result?.error ? "本次刷新失败" : result ? `${result.models.length} 个模型 · ${timeLabel(result.checked_at)}` : "点击联网刷新获取目录");
    if (result?.error) detail.title = result.error;
    filter.append(detail);
    if (result?.new_models?.length) filter.append(element("span", "source-new", `本次新发现 ${result.new_models.length} 个`));
    card.append(filter, link); area.append(card);
  }
  const fresh = state.models.filter(row => row.is_new).length;
  const compared = state.source_results.some(row => row.comparison_available);
  const recorded = state.source_results.some(row => row.comparison_available === false && !row.error);
  $("discovery-summary").textContent = fresh ? `本次新发现 ${fresh} 个模型，已排在前面。按上次成功刷新比较，不代表发布日期。` : compared ? "本次未发现新增模型。" : recorded ? "已建立对比记录，下次刷新会标出新发现的模型。" : "刷新后会标出本次新发现的模型。";
  if (state.source_results.some(row => row.error)) $("discovery-summary").textContent += " 部分来源刷新失败，请查看卡片提示。";
}

function syncProvider() {
  document.querySelectorAll("[data-provider]").forEach(button => {
    const active = button.dataset.provider === provider;
    button.setAttribute("aria-pressed", String(active));
    const surface = button.classList.contains("source-select") ? button.parentElement : button;
    surface.classList.toggle("active", active);
  });
  const source = state.sources.find(item => item.id === provider);
  $("current-filter").textContent = source ? `正在查看 ${source.name}` : "全部厂商";
}

function selectProvider(id) {
  // 厂商只在顶部切换，“全部模型”负责复位；保留搜索、勾选和按钮焦点。
  provider = id;
  syncProvider();
  renderModels();
}

function visibleModels() {
  const query = $("search").value.trim().toLowerCase();
  return state.models.filter(row => (!provider || row.provider === provider) && (!query || (row.id + " " + row.name).toLowerCase().includes(query)));
}

function updateSiteNotice() {
  // 只解释用户当前输入的协议，不改地址，也不增加确认步骤。
  $("http-notice").classList.toggle("hidden", !/^http:\/\//i.test($("site").value.trim()));
}

function selectionChanged() {
  plan = null;
  $("selected-count").textContent = selected.size ? `已选择 ${selected.size} 个模型` : "还没有选择模型";
  $("selection-hint").textContent = selected.size ? "先预览变化，再确认写入" : "勾选想加入 Codex 的模型";
  const selectable = visibleModels().filter(row => row.ready && !row.configured);
  const count = selectable.filter(row => selected.has(row.id)).length;
  $("select-all").checked = !!selectable.length && count === selectable.length;
  $("select-all").indeterminate = count > 0 && count < selectable.length;
  updateControls();
}

function renderModels() {
  const area = $("models"); area.replaceChildren();
  const rows = visibleModels();
  $("empty").classList.toggle("hidden", rows.length > 0);
  for (const row of rows) {
    const line = element("div", "model-row" + (selected.has(row.id) ? " selected" : ""));
    const title = element("div", "model-title");
    const label = element("label");
    const input = document.createElement("input"); input.type = "checkbox";
    input.checked = selected.has(row.id); input.dataset.locked = String(!row.ready);
    input.setAttribute("aria-label", "选择 " + row.id);
    input.addEventListener("change", () => {input.checked ? selected.add(row.id) : selected.delete(row.id); renderModels();});
    const names = element("div");
    const nameLine = element("div", "model-name-line"); nameLine.append(element("div", "model-name", row.name));
    if (row.is_new) nameLine.append(element("span", "badge newly-found", "本次新发现"));
    names.append(nameLine, element("div", "model-id", row.id));
    label.append(input, names); title.append(label);
    const source = element("div", "model-source", row.provider_name);
    const facts = [];
    if (row.context_window) facts.push(new Intl.NumberFormat("zh-CN", {notation:"compact"}).format(row.context_window) + " 上下文");
    facts.push(row.parameter_source || "尚缺完整参数");
    source.append(element("small", "", facts.join(" · ")));
    source.title = [row.parameter_source || "官方名称已发现，尚未取得完整配置参数。", row.reasoning?.length ? "思考强度：" + row.reasoning.join(" / ") : ""].filter(Boolean).join("\n");
    const status = element("div");
    status.append(element("span", "badge" + (row.configured ? "" : row.ready ? " ready" : " waiting"), row.configured ? "已在 Codex" : row.ready ? "参数已就绪" : "等待参数"));
    if (!row.ready) {
      const help = element("button", "parameter-help", "处理");
      help.setAttribute("aria-label", `处理 ${row.id} 的参数`);
      help.addEventListener("click", () => openParameterHelp(row.id));
      status.append(help);
    }
    line.append(title, source, status); area.append(line);
  }
  selectionChanged();
}

function render() {
  if (!state) return;
  if (document.activeElement !== $("site")) $("site").value = state.site;
  updateSiteNotice();
  $("account").classList.toggle("hidden", !state.user);
  $("account").textContent = state.user ? "已登录 · " + (state.user.email || state.user.name) : "";
  $("logout-btn").classList.toggle("hidden", !state.user);
  $("login-btn").textContent = state.login_pending ? "请在登录窗口完成登录…" : state.user ? "更换站点或账号" : "打开站点登录";
  $("login-status").textContent = state.login_error || "";
  $("login-status").classList.toggle("error-text", !!state.login_error);
  $("key").replaceChildren(new Option(state.user ? "选择一个 API Key" : "登录后选择密钥", ""));
  for (const key of state.keys) $("key").append(new Option(`${key.name} · ${key.masked}`, key.id));
  $("key").value = state.key_id;
  const key = state.keys.find(item => item.id === state.key_id);
  $("key-detail").textContent = key ? `${key.group_name || "分组 " + key.group_id} · ${key.platform || "站点分组"}\n${key.masked}` : "可按名称和分组辨认密钥。";
  if (!key) $("append-only").checked = true;
  renderStation(); updateConnectionSummary();
  $("current-model").textContent = state.codex?.model || "尚未识别默认模型";
  $("codex-summary").textContent = state.codex ? `${state.codex.count} 个模型 · ${state.codex.version}` : "请在设置中检查 Codex 位置";
  $("codex-error").textContent = state.codex_error || "";
  $("codex-dot").classList.toggle("hidden", !state.codex);
  const available = new Set(state.models.filter(row => row.ready).map(row => row.id));
  selected = new Set([...selected].filter(name => available.has(name)));
  renderSources(); syncProvider(); renderModels();
  if ($("parameter-dialog").open) renderParameterHelp();
}

async function reload() { state = await api("state"); render(); }

function renderStation() {
  $("station-status").textContent = stationLoading ? "正在读取所选 Key 的模型…" : state.station_error ? `读取失败：${state.station_error}` : state.station_checked ? `已自动读取 ${state.station_count} 个模型。` : "选择 Key 后会自动读取模型。";
  $("station-status").classList.toggle("error-text", !!state.station_error && !stationLoading);
  $("station-btn").textContent = stationLoading ? "正在读取…" : state.station_error ? "重试读取模型" : "重新读取模型";
  $("station-btn").classList.toggle("hidden", !state.key_id && !stationLoading);
}

async function readStation(actionName, body = {}) {
  // 同一 busy 操作内完成选择、自动读取及页面同步，失败也展示已确认的新选择。
  stationLoading = true; renderStation();
  try {
    const result = await api(actionName, body);
    await reload();
    if (result.error) toast("Key 已选中，但模型读取失败。请点击“重试读取模型”。", true);
    else if (state.key_id) toast(`已读取 ${result.count} 个模型，可以直接选择。`);
  } finally {
    stationLoading = false; renderStation();
  }
}

function updateConnectionSummary() {
  const switching = $("switch-connection").checked;
  const key = state?.keys.find(item => item.id === state.key_id);
  $("connection-summary").textContent = switching && key ? `后续请求将改用 ${state.site} · ${key.name}（${key.masked}）` : "保留 Codex 当前使用的站点和 Key。";
  $("preview-btn").textContent = switching ? "预览添加并切换 →" : "预览添加 →";
}

function openParameterHelp(id) {
  helpModelId = id;
  renderParameterHelp();
  $("parameter-dialog").showModal();
}

function renderParameterHelp() {
  const row = state.models.find(item => item.id === helpModelId);
  $("parameter-title").textContent = helpModelId;
  let message, label;
  if (row?.ready) { message = "参数已准备好，现在可以选中这个模型，再预览添加。"; label = "选中这个模型"; }
  else if (!state.user) { message = "先登录你的 Sub2API 站点，再选择一个 Key。工具会自动读取站点提供的模型参数。"; label = "去登录站点"; }
  else if (!state.key_id) { message = "先选择一个 API Key，工具会自动读取它的模型参数。"; label = "去选择 Key"; }
  else if (state.station_error) { message = `读取站点模型失败：${state.station_error} 可以重试；如果持续失败，请检查站点是否支持 Codex 模型目录。`; label = "重试读取"; }
  else if (!state.station_checked) { message = "还没有读取当前 Key 的模型参数，点击下方按钮读取。"; label = "读取模型参数"; }
  else { message = "已读取当前 Key 的目录，但站点暂未提供这个模型的完整参数。可以换一个 Key 再读取，或向站点管理员索取该模型的完整 Codex JSON 参数文件后导入。站点更新后也可重新读取。"; label = "重新读取参数"; }
  $("parameter-message").textContent = message;
  $("parameter-action").textContent = label;
  $("parameter-import-btn").classList.toggle("hidden", !!row?.ready);
  $("parameter-import-hint").classList.toggle("hidden", !!row?.ready);
}

async function importCatalog(input) {
  const file = input.files[0]; if (!file) return;
  try {
    if (file.size > 16 * 1024 * 1024) throw new Error("模型目录不能超过 16 MB。");
    const result = await api("import-catalog", {content:await file.text()});
    $("import-result").textContent = `已校验并导入 ${result.count} 个模型的参数。`;
    await reload(); toast(`已导入 ${result.count} 个模型的参数。`);
  } finally { input.value = ""; }
}

function previewRow(name, value) {
  const row = element("div", "preview-row"); row.append(element("span", "", name), element("span", "", value)); return row;
}

async function loadBackups() {
  const response = await api("backups"); backupDigest = response.digest;
  const area = $("backups"); area.replaceChildren();
  if (!response.items.length) area.append(element("p", "hint", "还没有备份。首次应用配置时会自动创建。"));
  for (const item of response.items) {
    const row = element("div", "backup-item");
    row.append(element("span", "", `${timeLabel(item.created)} · ${item.reason}`));
    const button = element("button", "secondary", "恢复");
    button.addEventListener("click", () => {restoreId = item.id; $("restore-description").textContent = `${timeLabel(item.created)} · ${item.reason}`; $("restore-dialog").showModal();});
    row.append(button); area.append(row);
  }
}

$("login-btn").addEventListener("click", () => action(async () => {
  await api("start-login", {site:$("site").value}); await reload();
  clearInterval(loginTimer);
  loginTimer = setInterval(async () => {
    if (busy) return;
    try { await reload(); if (!state.login_pending) {clearInterval(loginTimer); loginTimer = null; if (state.user) toast("登录成功，已读取全部 API Key。");} }
    catch(error) {clearInterval(loginTimer); toast(error.message, true);}
  }, 1400);
}));
$("logout-btn").addEventListener("click", () => action(async () => {await api("logout"); await reload();}));
$("refresh-keys-btn").addEventListener("click", () => action(async () => {await api("refresh-keys"); await reload(); toast("密钥列表已刷新，请重新选择。");}));
$("key").addEventListener("change", () => action(async () => {
  $("append-only").checked = true; plan = null; updateConnectionSummary();
  await readStation("select-key", {id:$("key").value});
}));
$("station-btn").addEventListener("click", () => action(() => readStation("refresh-station")));
$("refresh-official-btn").addEventListener("click", () => action(async () => {
  const result = await api("refresh-official"); await reload();
  const failed = result.sources.filter(row => row.error);
  const fresh = state.models.filter(row => row.is_new).length;
  toast(failed.length ? `${result.sources.length - failed.length} 个来源刷新成功；${failed.map(row=>row.name).join("、")} 刷新失败。` : fresh ? `本次新发现 ${fresh} 个模型，已排在列表前面。` : $("discovery-summary").textContent, failed.length > 0);
}));
$("all-sources-btn").addEventListener("click", () => selectProvider(""));
$("search").addEventListener("input", renderModels);
$("site").addEventListener("input", updateSiteNotice);
$("select-all").addEventListener("change", () => {for (const row of visibleModels().filter(item => item.ready && !item.configured)) {$("select-all").checked ? selected.add(row.id) : selected.delete(row.id);} renderModels();});
$("manual-btn").addEventListener("click", () => action(async () => {const result = await api("add-model", {id:$("manual-id").value}); $("search").value = result.id; provider = ""; await reload(); if(result.ready) {selected.add(result.id); renderModels(); toast("已找到模型参数，可以预览添加。");} else openParameterHelp(result.id);}));
document.querySelectorAll('[name="connection-mode"]').forEach(input => input.addEventListener("change", () => {plan = null; updateConnectionSummary(); updateControls();}));
$("parameter-action").addEventListener("click", () => {
  if (busy) return;
  const row = state.models.find(item => item.id === helpModelId);
  if (row?.ready) {
    selected.add(helpModelId); $("search").value = helpModelId; selectProvider(""); $("parameter-dialog").close();
  } else if (!state.user || !state.key_id) {
    $("parameter-dialog").close();
    const target = state.user ? $("key") : $("site"); target.scrollIntoView({block:"center"}); target.focus();
  } else action(() => readStation("refresh-station"));
});
$("parameter-import-btn").addEventListener("click", () => $("parameter-file").click());
$("parameter-file").addEventListener("change", () => action(() => importCatalog($("parameter-file"))));
$("preview-btn").addEventListener("click", () => action(async () => {
  plan = await api("prepare", {models:[...selected], activate:$("switch-connection").checked});
  const area = $("preview-content"); area.replaceChildren();
  $("preview-title").textContent = plan.site ? "添加模型，并切换站点 / Key" : "只添加模型，保留当前连接";
  $("apply-btn").textContent = plan.site ? "确认添加并切换连接" : "确认只添加模型";
  area.append(previewRow("新增模型", plan.added.length + " 个"), previewRow("已有模型", "保留，共 " + plan.catalog_count + " 个模型"), previewRow("默认模型", plan.new_model || "保持现状"), previewRow("连接变化", plan.site ? `改用 ${plan.site}` : "站点和 Key 均不改变"));
  if(plan.key_label) area.append(previewRow("使用密钥", plan.key_label));
  if(plan.site) area.append(element("p", "hint", "应用后，Codex 的新请求将使用此连接。原有模型是否可调用取决于该 Key 的分组权限。"));
  const names = element("div", "preview-models"); for(const name of plan.added) names.append(element("span", "badge ready", name)); area.append(names);
  $("preview-dialog").showModal();
}));
$("apply-btn").addEventListener("click", () => action(async () => {if(!plan) return; const result = await api("apply", {plan_id:plan.plan_id}); plan = null; selected.clear(); $("append-only").checked = true; $("preview-dialog").close(); await reload(); toast(result.message);}));
$("settings-btn").addEventListener("click", () => action(async () => {$("codex-home").value = state.codex_home; $("codex-binary").value = state.codex_binary; $("binary-options").replaceChildren(...state.binaries.map(path=>new Option(path,path))); await loadBackups(); $("settings-dialog").showModal();}));
$("save-settings-btn").addEventListener("click", () => action(async () => {await api("save-settings", {codex_home:$("codex-home").value,codex_binary:$("codex-binary").value}); await reload(); await loadBackups(); toast("Codex 位置已检查并保存。");}));
$("catalog-file").addEventListener("change", () => action(() => importCatalog($("catalog-file"))));
$("refresh-backups-btn").addEventListener("click", () => action(loadBackups));
$("restore-confirm").addEventListener("click", () => action(async () => {const result = await api("restore", {id:restoreId,digest:backupDigest}); $("restore-dialog").close(); await reload(); await loadBackups(); toast(result.message);}));
document.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => $(button.dataset.close).close()));
action(reload);
