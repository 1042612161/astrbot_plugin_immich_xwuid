const bridge = window.AstrBotPluginView;
const kinds = ["panel", "background"];
let state = {albums: [], roles: {panel: [], background: []}, mappings: {panel: {}, background: {}}, specifications: {panel: "竖图", background: "横图"}};
let mappingRows = {panel: [], background: []};
const $ = id => document.getElementById(id);

function notice(text, kind = "") {
  const el = $("notice");
  el.textContent = text;
  el.className = `notice ${kind}`;
}

function readSpecifications() {
  return Object.fromEntries(kinds.map(kind => [kind, $(`${kind}-specification`).value.trim()]));
}

function specificationsChanged() {
  const current = readSpecifications();
  return kinds.some(kind => current[kind] !== state.specifications[kind]);
}

function requireSavedSpecifications() {
  if (specificationsChanged()) throw new Error("规格名称尚未保存，请先点击“保存设置与映射”。保存后会清空两张映射表并重新匹配。");
}

function collectMappings(kind) {
  const roleNames = new Set((state.roles[kind] || []).map(role => role.name));
  const albumNames = new Set(state.albums.map(album => album.name));
  const table = {};
  for (const row of mappingRows[kind]) {
    if (!roleNames.has(row.role) || Object.hasOwn(table, row.role)) throw new Error("角色映射重复或已失效，请刷新列表。");
    if (row.album && !albumNames.has(row.album)) throw new Error("相册已失效，请刷新列表。");
    table[row.role] = row.album;
  }
  state.mappings[kind] = table;
}

function renderKind(kind) {
  const body = $(kind);
  body.replaceChildren();
  const roles = state.roles[kind] || [];
  if (!roles.length) {
    body.innerHTML = '<tr><td class="empty" colspan="2">未能读取 XWUID 角色列表</td></tr>';
    return;
  }
  for (const mapping of mappingRows[kind]) {
    const row = document.createElement("tr");
    row.dataset.rowId = mapping.id;
    const roleCell = document.createElement("td"), albumCell = document.createElement("td");
    const roleSelect = document.createElement("select"), albumSelect = document.createElement("select");
    roleSelect.className = "role";
    roleSelect.setAttribute("aria-label", "XWUID 角色");
    albumSelect.setAttribute("aria-label", "Immich 相册");
    for (const role of roles) {
      const option = document.createElement("option");
      option.value = role.name;
      option.textContent = role.name;
      roleSelect.append(option);
    }
    roleSelect.value = mapping.role;
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "选择 Immich 相册…";
    albumSelect.append(blank);
    for (const album of state.albums) {
      const option = document.createElement("option");
      option.value = album.name;
      option.textContent = album.name;
      albumSelect.append(option);
    }
    albumSelect.value = mapping.album;
    roleSelect.addEventListener("change", () => {
      const nextRole = roleSelect.value, previousRole = mapping.role;
      if (nextRole === previousRole) return;
      const other = mappingRows[kind].find(item => item !== mapping && item.role === nextRole);
      if (other) other.role = previousRole;
      mapping.role = nextRole;
      collectMappings(kind);
      renderKind(kind);
      notice("已交换角色分配，两个相册选择均已保留。请保存映射。");
    });
    albumSelect.addEventListener("change", () => {
      mapping.album = albumSelect.value;
      collectMappings(kind);
      notice("相册映射已修改，请保存后同步。");
    });
    roleCell.append(roleSelect);
    albumCell.append(albumSelect);
    row.append(roleCell, albumCell);
    body.append(row);
  }
}

function renderSyncSelector(kind) {
  const select = $(`${kind}-sync-roles`);
  select.replaceChildren();
  for (const role of state.roles[kind] || []) {
    const option = document.createElement("option");
    option.value = role.name;
    option.textContent = role.name;
    select.append(option);
  }
}

function render() {
  for (const kind of kinds) {
    mappingRows[kind] = (state.roles[kind] || []).map(role => ({
      id: String(role.id), role: role.name, album: (state.mappings[kind] || {})[role.name] || "",
    }));
    $(`${kind}-specification`).value = state.specifications[kind];
    $(`${kind}-suffix`).textContent = `相册后缀「${state.specifications[kind]}」`;
    renderKind(kind);
    renderSyncSelector(kind);
  }
}

async function refresh() {
  notice("正在读取 Immich 相册和 XWUID 角色列表…");
  try {
    const result = await bridge.apiGet("refresh");
    if (result.error) throw new Error(result.error);
    state = result;
    render();
    notice(`已读取 ${state.albums.length} 个 Immich 相册和角色列表。`, "ok");
    return true;
  } catch (error) {
    notice(`读取失败：${error.message}`, "error");
    return false;
  }
}

$("refresh").onclick = refresh;
for (const kind of kinds) {
  $(`${kind}-specification`).addEventListener("input", () => {
    if (specificationsChanged()) notice("规格名称尚未保存。保存后将清空两张映射表，按新名称重新匹配；已有 Immich 相册不会重命名。");
    else notice("规格名称与已保存设置一致。");
  });
}

$("save").onclick = async () => {
  const button = $("save");
  button.disabled = true;
  try {
    const specifications = readSpecifications();
    if (!specifications.panel || !specifications.background) throw new Error("两种规格名称均不能为空。");
    if (specifications.panel === specifications.background) throw new Error("两种规格名称不能相同。");
    if (!specificationsChanged()) for (const kind of kinds) collectMappings(kind);
    const result = await bridge.apiPost("save", {mappings: state.mappings, specifications});
    if (result.error) throw new Error(result.error);
    state = result;
    render();
    notice(result.reset ? "规格名称已保存。两张映射表已清空，并按新规格名称重新匹配。" : "设置与映射已保存。", "ok");
  } catch (error) {
    notice(`保存失败：${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
};

$("create").onclick = async () => {
  const button = $("create");
  button.disabled = true;
  try {
    requireSavedSpecifications();
    notice("正在创建缺失角色相册并设置共享权限…");
    const result = await bridge.apiPost("create-albums", {});
    if (result.error) throw new Error(result.error);
    if (await refresh()) notice(`已创建 ${result.count} 个相册。`, "ok");
  } catch (error) {
    notice(`创建失败：${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
};

async function syncSelected(kind) {
  const roles = Array.from($(`${kind}-sync-roles`).selectedOptions).map(option => option.value);
  if (!roles.length) {
    $(`${kind}-progress`).textContent = "请至少选择一个角色。";
    return;
  }
  const button = $(`${kind}-sync`), progress = $(`${kind}-progress`);
  button.disabled = true;
  progress.textContent = "正在准备同步…";
  try {
    requireSavedSpecifications();
    const started = await bridge.apiPost("sync", {type: kind, roles});
    if (started.error) throw new Error(started.error);
    while (true) {
      const result = await bridge.apiGet("sync-status", {job_id: started.job_id});
      if (result.error) throw new Error(result.error);
      progress.textContent = result.progress || "同步进行中…";
      if (result.state !== "running") {
        // The final result lives in this page after the server releases it.
        progress.textContent += result.failures?.length ? `\n${result.failures.join("\n")}` : "";
        break;
      }
      await new Promise(resolve => setTimeout(resolve, 1200));
    }
  } catch (error) {
    progress.textContent = `同步状态读取失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
}

$("panel-sync").onclick = () => syncSelected("panel");
$("background-sync").onclick = () => syncSelected("background");
await bridge.ready();
await refresh();
