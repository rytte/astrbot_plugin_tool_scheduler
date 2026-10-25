"use strict";

const ui = Object.fromEntries([...document.querySelectorAll("[id]")].map(element => [element.id, element]));
const labels = {full: "Full", skills_like: "Skills-like", dormant: "休眠", off: "已关闭", unavailable: "宿主不可用", mixed: "按配置决定"};
const strategies = {inherit: "继承默认", auto: "智能 / 默认", full: "固定 Full", skills_like: "固定 Skills-like"};
let bridge, state, busy = false, selectedTool = null, requestSerial = 0;

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}

function message(text, error = false) {
  ui.message.textContent = text;
  ui.message.className = error ? "error" : "";
  ui.message.hidden = !text;
}

function date(value) { return value ? new Date(value * 1000).toLocaleString("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"}) : "暂无"; }

function lock(value) {
  busy = value;
  document.querySelectorAll("button,input,select").forEach(element => { element.disabled = value; });
}

async function load() {
  const serial = ++requestSerial;
  const result = await bridge.apiGet("state", {profile: ui.profile.value, days: ui.days.value});
  if (serial !== requestSerial) return;
  state = result;
  render();
}

async function update(endpoint, payload) {
  if (busy || !state) return;
  lock(true);
  try {
    await bridge.apiPost(endpoint, {csrf: state.csrf, revision: state.revision, ...payload});
    await load();
    message("设置已保存，管理员修改立即生效。已发出的模型请求不变；后续模型请求使用新定义，未开始的工具调用会重新检查开关。");
  } catch (error) {
    message(error.message || "保存失败，请刷新后重试。", true);
    try { await load(); } catch (_) { /* Keep the original mutation error visible. */ }
  } finally { lock(false); }
}

function strategySelect(scope, subject, value) {
  const select = node("select", "strategy-select");
  select.setAttribute("aria-label", subject + " 提供策略");
  for (const [key, label] of Object.entries(strategies)) {
    const option = node("option", "", label); option.value = key; option.selected = key === (value || "inherit"); select.append(option);
  }
  select.addEventListener("change", () => update("policy", {scope, subject, policy: {strategy: select.value}}));
  return select;
}

function enabledSwitch(scope, subject, enabled, title) {
  const label = node("label", "mini-switch");
  const input = node("input"); input.type = "checkbox"; input.checked = enabled !== false; input.setAttribute("aria-label", title + " 允许模型使用");
  input.addEventListener("change", () => update("policy", {scope, subject, policy: {enabled: input.checked}}));
  label.append(input, node("span", "", "允许使用")); return label;
}

function render() {
  ui.smart.checked = state.settings.smart;
  ui["block-all"].checked = state.settings.block_all;
  ui["smart-caption"].textContent = state.settings.smart ? "自动调整在新会话或 reset 后应用，休眠工具仍可找回" : "正在收集统计，并遵循管理员即时策略";
  ui.connection.textContent = state.coverage.last_connection ? "已接入默认 Agent" : "等待首次 Agent 请求";
  ui.coverage.textContent = "接管范围：" + state.coverage.scope + "。当前运行 " + state.coverage.active_runs + " 个任务；未接入 " + state.coverage.unsupported_runs + " 次。统计不含聊天正文及工具参数。";
  const selectedProfile = ui.profile.value;
  ui.profile.replaceChildren();
  for (const profile of state.profiles) {const option = node("option", "", profile.name === "default" ? "默认配置" : profile.name); option.value = profile.id; ui.profile.append(option);}
  const all = node("option", "", "所有配置（统计汇总）"); all.value = ""; ui.profile.append(all); ui.profile.value = selectedProfile;
  const counts = Object.fromEntries(Object.keys(labels).map(key => [key, 0]));
  for (const tool of state.tools) counts[tool.effective.mode] = (counts[tool.effective.mode] || 0) + 1;
  ui.cards.replaceChildren();
  for (const [key, label, value] of [["total", "工具总数", state.tools.length], ["full", "完整参数", counts.full], ["skills_like", counts.mixed ? "按配置决定 / 按需展开" : "按需展开", counts.skills_like + counts.mixed], ["dormant", "休眠待发现", counts.dormant], ["off", "已关闭 / 不可用", counts.off + counts.unavailable]]) {
    const card = node("div", "card"); const caption = node("div", "card-label"); caption.append(node("span", "dot " + key), node("span", "", label)); card.append(caption, node("div", "card-value", value)); ui.cards.append(card);
  }
  for (const [key, value] of Object.entries(state.settings)) {const input = ui["policy-form"].elements.namedItem(key); if (input) {if (input.type === "checkbox") input.checked = value; else input.value = value;}}
  renderTools(); renderMetrics();
}

function renderTools() {
  if (!state) return;
  const query = ui.search.value.trim().toLocaleLowerCase(), mode = ui["mode-filter"].value;
  const tools = state.tools.filter(tool => (!mode || tool.effective.mode === mode) && (!query || [tool.name, tool.owner_label, tool.description, ...(tool.rule.aliases || [])].join(" ").toLocaleLowerCase().includes(query)));
  ui["result-count"].textContent = "显示 " + tools.length + " / " + state.tools.length + " 个工具";
  ui["tool-groups"].replaceChildren();
  const groups = new Map(); for (const tool of tools) {if (!groups.has(tool.owner)) groups.set(tool.owner, []); groups.get(tool.owner).push(tool);}
  for (const [ownerId, entries] of groups) {
    const owner = state.owners.find(item => item.id === ownerId);
    const group = node("article", "group"), heading = node("div", "group-heading"), title = node("div");
    title.append(node("div", "owner-name", owner.label), node("div", "owner-sub", ({plugin: "插件", builtin: "内置工具", mcp: "MCP", request: "请求工具"}[owner.source] || "工具") + " · " + owner.tools + " 个工具 · " + owner.calls + " 次调用 / " + (owner.requests || 0) + " 个请求"));
    const controls = node("div", "group-controls"); controls.append(enabledSwitch("owner", ownerId, owner.rule.enabled, owner.label), strategySelect("owner", ownerId, owner.rule.strategy));
    heading.append(node("span", "owner-icon", owner.label.slice(0, 1)), title, controls); group.append(heading);
    const wrap = node("div", "table-wrap"), table = node("table"), thead = node("thead"), tr = node("tr");
    for (const text of ["工具 / 能力", "新会话策略", "调用 / 请求", "最后使用", "提供策略", "允许使用"]) tr.append(node("th", "", text));
    thead.append(tr); table.append(thead); const body = node("tbody");
    for (const tool of entries) {
      const row = node("tr"), nameCell = node("td"), name = node("button", "tool-name", tool.name); name.type = "button"; name.addEventListener("click", () => showDetail(tool));
      nameCell.append(name, node("div", "tool-description", tool.description || "暂无描述"));
      const modeCell = node("td"), badge = node("span", "badge " + tool.effective.mode, labels[tool.effective.mode]); badge.title = tool.effective.reason; modeCell.append(badge);
      if (tool.pending_sessions) modeCell.append(node("div", "muted small", tool.pending_sessions + " 个会话待新建 / reset"));
      const selectCell = node("td"); selectCell.append(strategySelect("tool", tool.id, tool.rule.strategy));
      const toggleCell = node("td"); toggleCell.append(enabledSwitch("tool", tool.id, tool.rule.enabled, tool.name));
      row.append(nameCell, modeCell, node("td", "number", (tool.stats.calls || 0) + " / " + (tool.stats.requests || 0)), node("td", "muted number", date(tool.stats.last_used)), selectCell, toggleCell); body.append(row);
    }
    table.append(body); wrap.append(table); group.append(wrap); ui["tool-groups"].append(group);
  }
  if (!tools.length) ui["tool-groups"].append(node("div", "empty", state.tools.length ? "没有符合筛选条件的工具。" : "尚未发现模型工具。加载提供工具的插件后，刷新目录即可。"));
}

function renderMetrics() {
  const metrics = state.overview.metrics;
  ui.metrics.replaceChildren();
  for (const [label, value] of [["独立 Agent 请求", state.overview.requests], ["参数补全模型请求", metrics.extra_llm_calls || 0], ["工具发现 / 命中", (metrics.discovery_searches || 0) + " / " + (metrics.discovery_hits || 0)], ["已展开工具定义", metrics.schemas_loaded || 0], ["少发送的 Schema Token（估算）", Math.round(metrics.schema_tokens_avoided_estimate || 0)]]) ui.metrics.append(node("dt", "", label), node("dd", "", value));
  ui.trend.replaceChildren();
  const points = state.overview.trend;
  if (!points.length) {ui.trend.append(node("p", "muted small", "还没有调用记录。使用模型工具后，这里会显示趋势。")); return;}
  const maximum = Math.max(...points.map(point => point.calls), 1);
  for (const point of points) {const column = node("div", "trend-col"), bar = node("div", "trend-bar"); bar.style.height = Math.max(3, point.calls / maximum * 75) + "px"; column.title = date(point.day) + " · " + point.calls + " 次调用"; column.append(bar, node("small", "", new Date(point.day * 1000).toLocaleDateString("zh-CN", {month: "numeric", day: "numeric"}))); ui.trend.append(column);}
}

function showDetail(tool) {
  selectedTool = tool;
  ui["detail-title"].textContent = tool.name; ui["detail-owner"].textContent = tool.owner_label;
  ui["detail-description"].textContent = tool.description;
  const sessionModes = Object.entries(tool.session_modes || {}).map(([mode, count]) => (labels[mode] || mode) + " " + count).join("、");
  ui["detail-reason"].textContent = "新会话策略：" + tool.effective.reason + (sessionModes ? "。已保存会话的提供状态：" + sessionModes : "") + "。自动策略在新会话或 reset 后应用；管理员修改立即生效。";
  ui["detail-schema"].textContent = JSON.stringify(tool.parameters, null, 2);
  ui.aliases.value = (tool.rule.aliases || []).join(", ");
  ui["detail-stats"].replaceChildren();
  for (const [label, value] of [["调用", tool.stats.calls || 0], ["使用请求", tool.stats.requests || 0], ["执行错误", tool.stats.errors || 0], ["权限拒绝", tool.stats.denied || 0], ["平均耗时", ((tool.stats.duration || 0) * 1000).toFixed(0) + " ms"]]) ui["detail-stats"].append(node("span", "", label + " " + value));
  ui["detail-dialog"].showModal();
}

async function loadHistory() {
  try {
    const result = await bridge.apiGet("audit"); ui["audit-list"].replaceChildren();
    for (const item of result.items) {const row = node("article", "audit-item"), content = node("div"); content.append(node("p", "", item.message), node("small", "", item.subject === "global" ? "全局策略" : item.subject)); row.append(node("time", "", date(item.created)), node("span", "", item.actor), content); ui["audit-list"].append(row);}
    if (!result.items.length) ui["audit-list"].append(node("div", "empty", "暂时没有策略变更。"));
  } catch (error) { message(error.message || "读取变更记录失败。", true); }
}

ui.refresh.addEventListener("click", async () => {try {await load(); message("目录与统计已更新。");} catch (error) {message(error.message, true);}});
ui.search.addEventListener("input", renderTools); ui["mode-filter"].addEventListener("change", renderTools);
for (const key of ["profile", "days"]) ui[key].addEventListener("change", () => load().catch(error => message(error.message, true)));
ui.smart.addEventListener("change", () => update("settings", {settings: {smart: ui.smart.checked}}));
ui["block-all"].addEventListener("change", () => update("settings", {settings: {block_all: ui["block-all"].checked}}));
ui["policy-form"].addEventListener("submit", event => {
  event.preventDefault(); const settings = {};
  for (const input of ui["policy-form"].elements) if (input.name) settings[input.name] = input.type === "checkbox" ? input.checked : Number(input.value);
  update("settings", {settings});
});
document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => {document.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item === tab)); document.querySelectorAll(".panel").forEach(panel => {panel.hidden = panel.id !== tab.dataset.panel;}); if (tab.dataset.panel === "history-panel") loadHistory();}));
ui["close-detail"].addEventListener("click", () => ui["detail-dialog"].close());
ui["save-aliases"].addEventListener("click", async () => {if (!selectedTool) return; await update("policy", {scope: "tool", subject: selectedTool.id, policy: {aliases: ui.aliases.value.split(/[,，]/).map(item => item.trim()).filter(Boolean)}});});

(async () => {
  try {bridge = window.AstrBotPluginPage; if (!bridge) throw new Error("请从 AstrBot WebUI 的插件页面打开工具调度器。"); await bridge.ready(); await load(); lock(false);}
  catch (error) {lock(true); ui.refresh.disabled = false; message(error.message || "加载工具调度器失败。", true);}
})();
