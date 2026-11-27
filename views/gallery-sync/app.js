const bridge = window.AstrBotPluginView;
const kinds = ["panel", "background"];
const labels = {panel: "面板图", background: "背景图"};
const collator = new Intl.Collator("zh-CN", {numeric: true});
const $ = id => document.getElementById(id);
let state = {albums: [], roles: {panel: [], background: []}, mappings: {panel: {}, background: {}}, specifications: {panel: "竖图", background: "横图"}};
let mappingRows = {panel: [], background: []};
const selected = {panel: new Set(), background: new Set()};
let active = "panel", syncKind = "panel", busy = false, syncing = false, dirty = false;
let openPicker = null, toastTimer;

function notice(text, kind = "") {
  $("notice").textContent = text;
  $("notice").className = `notice ${kind}`;
}

function toast(text) {
  $("toast").textContent = text;
  $("toast").classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("toast").classList.remove("visible"), 3000);
}

function sortedRoles(kind) {
  return [...(state.roles[kind] || [])].sort((a, b) => collator.compare(a.name, b.name) || collator.compare(a.id, b.id));
}

function roleLabel(role) { return `${role.name} · ${role.id}`; }
function searchMatches(role, query) { return roleLabel(role).toLocaleLowerCase().includes(query); }
function queryValue(id) { return $(id).value.trim().toLocaleLowerCase(); }
function readSpecifications() { return Object.fromEntries(kinds.map(kind => [kind, $(`${kind}-specification`).value.trim()])); }
function specificationsChanged() { const next = readSpecifications(); return kinds.some(kind => next[kind] !== state.specifications[kind]); }
function requireSavedSpecifications() {
  if (specificationsChanged()) throw new Error("规格名称尚未保存，请先保存设置与映射。保存规格改动后，两张映射表会清空并重新匹配。");
}
function requireSavedMappings() {
  requireSavedSpecifications();
  if (dirty) throw new Error("映射尚未保存，请先保存设置与映射后再同步。");
}

function eligibleAlbums(kind) {
  const suffix = `-${state.specifications[kind]}`;
  return state.albums.filter(album => album.name.endsWith(suffix)).sort((a, b) => collator.compare(a.name, b.name));
}

function collectMappings(kind) {
  const ids = new Set(state.roles[kind].map(role => String(role.id)));
  const albums = new Set(state.albums.map(album => album.name));
  const table = {};
  for (const row of mappingRows[kind]) {
    if (!ids.has(row.roleId) || Object.hasOwn(table, row.roleId)) throw new Error("角色 ID 重复或已失效，请刷新列表。");
    if (row.album && !albums.has(row.album)) throw new Error("相册已失效，请刷新列表。");
    table[row.roleId] = row.album;
  }
  return table;
}

function closePicker(restoreFocus = false) {
  if (!openPicker) return;
  const {menu, trigger} = openPicker;
  menu.remove();
  trigger.setAttribute("aria-expanded", "false");
  openPicker = null;
  if (restoreFocus) trigger.focus();
}

function createPicker({value, placeholder, choices, label, displayValue, onChange}) {
  const holder = document.createElement("div"); holder.className = "picker";
  const trigger = document.createElement("button"); trigger.type = "button"; trigger.className = "picker-trigger";
  trigger.disabled = busy;
  trigger.setAttribute("aria-label", label); trigger.setAttribute("aria-haspopup", "listbox"); trigger.setAttribute("aria-expanded", "false");
  const text = document.createElement("span"); text.className = `label${value ? "" : " placeholder"}`;
  text.textContent = displayValue || (value ? choices.find(choice => choice.value === value)?.label || value : placeholder);
  trigger.title = text.textContent;
  const chevron = document.createElement("span"); chevron.className = "chevron"; chevron.textContent = "⌄"; chevron.setAttribute("aria-hidden", "true");
  trigger.append(text, chevron); holder.append(trigger);
  trigger.onclick = () => {
    if (openPicker?.trigger === trigger) { closePicker(); return; }
    closePicker();
    const menu = document.createElement("div"); menu.className = "picker-menu"; menu.style.position = "fixed";
    const searchWrap = document.createElement("div"); searchWrap.className = "picker-search";
    const search = document.createElement("input"); search.type = "search"; search.placeholder = "输入名称或 ID 筛选…"; search.setAttribute("aria-label", `搜索${label}`);
    searchWrap.append(search);
    const options = document.createElement("div"); options.className = "picker-options"; options.setAttribute("role", "listbox"); options.setAttribute("aria-label", label);
    menu.append(searchWrap, options);
    const fill = () => {
      options.replaceChildren();
      const matches = choices.filter(choice => choice.label.toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase()));
      for (const choice of matches) {
        const button = document.createElement("button"); button.type = "button"; button.className = "picker-option"; button.textContent = choice.label;
        button.setAttribute("role", "option"); button.setAttribute("aria-selected", String(choice.value === value));
        button.onclick = () => { closePicker(); onChange(choice.value); };
        options.append(button);
      }
      if (!matches.length) { const empty = document.createElement("div"); empty.className = "picker-empty"; empty.textContent = "没有符合条件的选项"; options.append(empty); }
    };
    search.addEventListener("input", fill);
    search.addEventListener("keydown", event => {
      if (event.key === "ArrowDown") { event.preventDefault(); options.querySelector("button")?.focus(); }
    });
    menu.addEventListener("keydown", event => {
      if (!["ArrowDown", "ArrowUp"].includes(event.key) || event.target === search) return;
      const buttons = [...options.querySelectorAll("button")];
      const index = buttons.indexOf(document.activeElement);
      if (index < 0) return;
      event.preventDefault(); buttons[(index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length]?.focus();
    });
    fill(); document.body.append(menu);
    const rect = trigger.getBoundingClientRect(), width = Math.min(330, window.innerWidth - 24);
    menu.style.width = `${width}px`; menu.style.left = `${Math.max(12, Math.min(rect.left, window.innerWidth - width - 12))}px`;
    const height = Math.min(menu.getBoundingClientRect().height, 280);
    menu.style.top = `${Math.max(8, Math.min(rect.bottom + 5, window.innerHeight - height - 8))}px`;
    openPicker = {menu, trigger, top: rect.top, left: rect.left}; trigger.setAttribute("aria-expanded", "true"); search.focus({preventScroll: true});
  };
  return holder;
}

function markDirty(message) {
  dirty = true; notice(message); updateStats();
}

function renderMappings() {
  closePicker();
  const roles = sortedRoles(active), byId = new Map(roles.map(role => [String(role.id), role]));
  const query = queryValue("mapping-search"), list = $("map-list"); list.replaceChildren();
  const rows = [...mappingRows[active]].sort((a, b) => collator.compare(byId.get(a.roleId)?.name || "", byId.get(b.roleId)?.name || "") || collator.compare(a.roleId, b.roleId));
  for (const row of rows) {
    const role = byId.get(row.roleId);
    if (!role || !searchMatches(role, query)) continue;
    const node = document.createElement("div"); node.className = "map-row"; node.dataset.roleId = row.roleId;
    const rolePicker = createPicker({value: row.roleId, displayValue: role.name, label: "XWUID 角色", choices: roles.map(item => ({value: String(item.id), label: roleLabel(item)})), onChange: next => {
      const previous = row.roleId, other = mappingRows[active].find(item => item !== row && item.roleId === next);
      if (other) other.roleId = previous;
      row.roleId = next; markDirty("已交换角色分配并保留两个相册选择，请保存映射。"); renderMappings();
    }});
    const name = document.createElement("div"); name.className = "role-name"; name.append(rolePicker);
    const id = document.createElement("div"); id.className = "id-cell"; id.textContent = row.roleId;
    const albumChoices = [{value: "", label: "清除当前映射"}, ...eligibleAlbums(active).map(album => ({value: album.name, label: album.name}))];
    const albumPicker = createPicker({value: row.album, placeholder: "选择 Immich 相册", label: `${roleLabel(role)} 的 Immich 相册`, choices: albumChoices, onChange: next => {
      row.album = next; markDirty("相册映射已修改，请保存后同步。"); renderMappings();
    }});
    const status = document.createElement("span"); status.className = `status-tag ${row.album ? "mapped" : "empty"}`; status.textContent = row.album ? "已映射" : "未映射";
    node.append(name, id, albumPicker, status); list.append(node);
  }
  if (!list.children.length) { const empty = document.createElement("p"); empty.className = "picker-empty"; empty.textContent = roles.length ? "没有匹配的角色" : "请先刷新列表，读取 XWUID 角色"; list.append(empty); }
  $("visible-count").textContent = `显示 ${rows.filter(row => byId.has(row.roleId) && searchMatches(byId.get(row.roleId), query)).length} 个角色`;
  $("mapping-count").textContent = `${labels[active]} · ${roles.length} 个角色`;
  $("album-filter-label").textContent = `相册后缀「${state.specifications[active]}」`;
  kinds.forEach(kind => $("tab-" + kind).setAttribute("aria-selected", String(active === kind)));
}

function visibleSyncRoles() { const query = queryValue("role-search"); return sortedRoles(syncKind).filter(role => searchMatches(role, query)); }
function renderSelection() {
  const visible = visibleSyncRoles(), box = $("role-options"); box.replaceChildren();
  for (const role of visible) {
    const roleId = String(role.id), label = document.createElement("label"); label.className = `role-option${selected[syncKind].has(roleId) ? " selected" : ""}`;
    const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.checked = selected[syncKind].has(roleId); checkbox.disabled = busy;
    checkbox.onchange = () => { checkbox.checked ? selected[syncKind].add(roleId) : selected[syncKind].delete(roleId); renderSelection(); };
    const text = document.createElement("span"); text.className = "role-label"; text.textContent = roleLabel(role); text.title = roleLabel(role);
    label.append(checkbox, text); box.append(label);
  }
  if (!visible.length) { const empty = document.createElement("p"); empty.className = "selected-empty"; empty.textContent = "没有匹配的角色"; box.append(empty); }
  const chips = $("selected-chips"); chips.replaceChildren();
  for (const role of sortedRoles(syncKind).filter(role => selected[syncKind].has(String(role.id)))) {
    const chip = document.createElement("span"); chip.className = "chip"; chip.append(document.createTextNode(roleLabel(role)));
    const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "×"; remove.setAttribute("aria-label", `移除${roleLabel(role)}`); remove.disabled = busy;
    remove.onclick = () => { selected[syncKind].delete(String(role.id)); renderSelection(); }; chip.append(remove); chips.append(chip);
  }
  if (!selected[syncKind].size) { const empty = document.createElement("span"); empty.className = "selected-empty"; empty.textContent = "尚未选择角色"; chips.append(empty); }
  $("selected-count").textContent = `已选 ${selected[syncKind].size} 位`; $("selected-summary-count").textContent = `${selected[syncKind].size} 位`;
  $("sync-kind").textContent = labels[syncKind]; $("sync-button").textContent = syncing ? "正在同步…" : `同步所选${labels[syncKind]}`;
  $("sync-button").disabled = busy || syncing;
  $("estimate").textContent = selected[syncKind].size ? `已选 ${selected[syncKind].size} 个目标图库` : "先选择要同步的角色";
  $("toggle-visible").textContent = visible.length && visible.every(role => selected[syncKind].has(String(role.id))) ? "取消当前结果" : "全选当前结果";
  kinds.forEach(kind => $("sync-tab-" + kind).setAttribute("aria-selected", String(syncKind === kind)));
}

function updateStats() {
  $("album-count").textContent = state.albums.length;
  const mapped = kinds.reduce((sum, kind) => sum + mappingRows[kind].filter(row => row.album).length, 0);
  const total = kinds.reduce((sum, kind) => sum + state.roles[kind].length, 0);
  $("mapped-count").textContent = `${mapped} / ${total}${dirty ? " · 未保存" : ""}`;
}

function applyData(data) {
  state = data; dirty = false;
  for (const kind of kinds) {
    state.roles[kind] = state.roles[kind].map(role => ({...role, id: String(role.id)}));
    const ids = new Set(state.roles[kind].map(role => role.id));
    selected[kind] = new Set([...selected[kind]].filter(id => ids.has(id)));
    mappingRows[kind] = state.roles[kind].map(role => ({roleId: role.id, album: state.mappings[kind]?.[role.id] || ""}));
    $(`${kind}-specification`).value = state.specifications[kind];
  }
  renderMappings(); renderSelection(); updateStats();
}

function setBusy(value) {
  busy = value;
  for (const id of ["refresh", "refresh-inline", "save", "create", "panel-specification", "background-specification", "toggle-visible", "clear-selection"]) $(id).disabled = value;
  renderMappings(); renderSelection();
}

async function refresh() {
  if (busy) return false;
  setBusy(true); notice("正在读取 Immich 相册和 XWUID 角色列表…");
  try {
    const result = await bridge.apiGet("refresh"); if (result.error) throw new Error(result.error);
    applyData(result); notice(`已读取 ${state.albums.length} 个 Immich 相册及 XWUID 角色列表。`, "ok"); return true;
  } catch (error) { notice(`读取失败：${error.message}`, "error"); return false; }
  finally { setBusy(false); }
}

$("save").onclick = async () => {
  setBusy(true);
  try {
    const specifications = readSpecifications();
    if (!specifications.panel || !specifications.background) throw new Error("两种规格名称均不能为空。");
    if (specifications.panel === specifications.background) throw new Error("两种规格名称不能相同。");
    const mappings = specificationsChanged() ? {} : Object.fromEntries(kinds.map(kind => [kind, collectMappings(kind)]));
    const result = await bridge.apiPost("save", {specifications, mappings}); if (result.error) throw new Error(result.error);
    applyData(result); const message = result.reset ? "规格名称已保存。两张映射表已清空，并按新规格名称重新匹配。" : "设置与映射已保存。";
    notice(message, "ok"); toast(message);
  } catch (error) { notice(`保存失败：${error.message}`, "error"); toast(`保存失败：${error.message}`); }
  finally { setBusy(false); }
};

$("create").onclick = async () => {
  setBusy(true);
  try {
    requireSavedMappings(); notice("正在创建缺失角色相册并共享给当前所有账号（可编辑）…");
    const result = await bridge.apiPost("create-albums", {}); if (result.error) throw new Error(result.error);
    const data = await bridge.apiGet("refresh"); if (data.error) throw new Error(data.error);
    applyData(data); const message = `已创建 ${result.count} 个相册。`; notice(message, "ok"); toast(message);
  } catch (error) { notice(`创建失败：${error.message}`, "error"); toast(`创建失败：${error.message}`); }
  finally { setBusy(false); }
};

function showTask(result) {
  $("taskbar").dataset.state = result.state === "running" ? "running" : result.state === "failed" ? "failed" : "done";
  $("task-symbol").textContent = result.state === "running" ? "…" : result.state === "failed" ? "!" : "✓";
  $("task-title").textContent = result.state === "running" ? "同步进行中" : result.state === "failed" ? "同步失败" : "同步已完成";
  $("task-text").textContent = result.progress || "正在准备同步…";
  const results = $("task-results"); results.replaceChildren();
  for (const [title, count, fail] of [["成功", result.success || 0, false], ["重复跳过", result.duplicate || 0, false], ["失败", result.failures?.length || 0, true]]) {
    const item = document.createElement("span"); item.className = `result${fail ? " fail" : ""}`; item.textContent = `${title} ${count}`; results.append(item);
  }
  for (const failure of result.failures || []) { const item = document.createElement("div"); item.className = "result fail"; item.textContent = failure; item.style.width = "100%"; results.append(item); }
  $("task-detail").hidden = result.state === "running";
  results.classList.toggle("open", result.state !== "running");
}

async function syncSelected(kind) {
  if (syncing || busy) return;
  const roles = [...selected[kind]];
  try {
    requireSavedMappings();
    if (!roles.length) throw new Error("请至少选择一个角色。");
    syncing = true; renderSelection();
    showTask({state: "running", progress: `正在准备同步${labels[kind]}…`});
    $("taskbar").scrollIntoView({behavior: "smooth", block: "nearest"});
    const started = await bridge.apiPost("sync", {type: kind, roles}); if (started.error) throw new Error(started.error);
    while (true) {
      const result = await bridge.apiGet("sync-status", {job_id: started.job_id}); if (result.error) throw new Error(result.error);
      showTask(result);
      if (result.state !== "running") break;
      await new Promise(resolve => setTimeout(resolve, 1200));
    }
  } catch (error) { notice(error.message, "error"); toast(error.message); if (syncing) showTask({state: "failed", progress: `同步状态读取失败：${error.message}`}); }
  finally { syncing = false; renderSelection(); }
}

$("refresh").onclick = refresh; $("refresh-inline").onclick = refresh;
$("mapping-search").addEventListener("input", renderMappings); $("role-search").addEventListener("input", renderSelection);
kinds.forEach(kind => {
  $("tab-" + kind).onclick = () => { active = kind; $("mapping-search").value = ""; renderMappings(); };
  $("sync-tab-" + kind).onclick = () => { syncKind = kind; $("role-search").value = ""; renderSelection(); };
  $(`${kind}-specification`).addEventListener("input", () => notice(specificationsChanged() ? "规格名称尚未保存。保存后会清空两张映射表，按新名称重新匹配；已有相册不会重命名。" : "规格名称与已保存设置一致。"));
});
$("toggle-visible").onclick = () => { const roles = visibleSyncRoles(), all = roles.length && roles.every(role => selected[syncKind].has(String(role.id))); roles.forEach(role => all ? selected[syncKind].delete(String(role.id)) : selected[syncKind].add(String(role.id))); renderSelection(); };
$("clear-selection").onclick = () => { selected[syncKind].clear(); renderSelection(); };
$("sync-button").onclick = () => syncSelected(syncKind);
$("task-detail").onclick = () => $("task-results").classList.toggle("open");
document.addEventListener("pointerdown", event => { if (openPicker && !openPicker.menu.contains(event.target) && !openPicker.trigger.contains(event.target)) closePicker(); });
document.addEventListener("keydown", event => { if (event.key === "Escape") closePicker(true); });
window.addEventListener("resize", () => closePicker());
window.addEventListener("scroll", event => {
  if (!openPicker || openPicker.menu.contains(event.target)) return;
  const rect = openPicker.trigger.getBoundingClientRect();
  if (Math.abs(rect.top - openPicker.top) > 1 || Math.abs(rect.left - openPicker.left) > 1) closePicker();
}, true);
await bridge.ready();
await refresh();
