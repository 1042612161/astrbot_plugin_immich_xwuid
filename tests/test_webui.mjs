import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../views/gallery-sync/app.js", import.meta.url), "utf8");
const clone = value => JSON.parse(JSON.stringify(value));

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.listeners = new Map();
    this.dataset = {};
    this.value = "";
    this.textContent = "";
    this.selectedOptions = [];
    this.disabled = false;
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  async change(value) {
    this.value = value;
    await this.listeners.get("change")?.();
  }
}

async function page() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  let data = {
    albums: ["折枝-竖图", "折枝-横图", "测试相册", "手工相册", "折枝-立绘", "折枝-壁纸"].map((name, index) => ({id: String(index), name})),
    roles: {panel: [{id: "1105", name: "折枝"}, {id: "test-role", name: "测试角色"}], background: [{id: "1105", name: "折枝"}]},
    mappings: {panel: {"折枝": "折枝-竖图", "测试角色": "测试相册"}, background: {"折枝": "折枝-横图"}},
    specifications: {panel: "竖图", background: "横图"},
  };
  const posts = [];
  let polls = 0, waits = 0;
  const bridge = {
    ready: async () => {},
    apiPost: async (route, body) => {
      posts.push({route, body: clone(body)});
      if (route === "save") {
        const reset = Object.keys(data.specifications).some(kind => body.specifications[kind] !== data.specifications[kind]);
        data = {...data, specifications: clone(body.specifications), mappings: reset
          ? {panel: {"折枝": "折枝-立绘"}, background: {"折枝": "折枝-壁纸"}}
          : clone(body.mappings)};
        return {ok: true, reset, ...clone(data)};
      }
      if (route === "create-albums") {
        assert.equal(element("create").disabled, true);
        return {count: 2};
      }
      return {job_id: "job-1"};
    },
    apiGet: async route => {
      if (route === "refresh") return clone(data);
      polls++;
      assert.ok(polls <= 2, "A final result must stop polling");
      return polls === 1
        ? {state: "running", progress: "正在同步折枝"}
        : {state: "completed", progress: "同步完成", failures: ["折枝/test.jpg：比例错误"]};
    },
  };
  const context = vm.createContext({
    window: {AstrBotPluginView: bridge},
    document: {getElementById: element, createElement: tag => new Element(tag)},
    setTimeout(callback) { waits++; callback(); },
    // No confirm function: creating albums must work without native dialogs.
  });
  await vm.runInContext(`(async () => {${source}\nglobalThis.runSync = syncSelected;})()`, context);
  element("panel-sync-roles").selectedOptions = [{value: "折枝"}];
  const cell = (row, column) => element("panel").children[row].children[column].children[0];
  return {element, cell, bridge, posts, context, counts: () => ({polls, waits})};
}

test("final sync result stays on the page and stops polling immediately", async () => {
  const p = await page();
  await p.context.runSync("panel");
  assert.deepEqual(p.counts(), {polls: 2, waits: 1});
  assert.equal(p.element("panel-progress").textContent, "同步完成\n折枝/test.jpg：比例错误");
  assert.equal(p.element("panel-sync").disabled, false);
});

test("an expired result displays the error and restores the button", async () => {
  const p = await page();
  p.bridge.apiGet = async () => ({error: "同步任务不存在或已过期。"});
  await p.context.runSync("panel");
  assert.match(p.element("panel-progress").textContent, /同步任务不存在或已过期/);
  assert.equal(p.element("panel-sync").disabled, false);
});

test("changing a role exchanges assignments and keeps both album selections", async () => {
  const p = await page();
  await p.cell(0, 0).change("测试角色");
  assert.equal(p.cell(0, 0).value, "测试角色");
  assert.equal(p.cell(1, 0).value, "折枝");
  assert.equal(p.cell(0, 1).value, "折枝-竖图");
  assert.equal(p.cell(1, 1).value, "测试相册");
  await p.element("save").onclick();
  assert.deepEqual(p.posts[0].body.mappings.panel, {"测试角色": "折枝-竖图", "折枝": "测试相册"});
});

test("repeated role changes after an album edit do not use the original role", async () => {
  const p = await page();
  await p.cell(0, 0).change("测试角色");
  await p.cell(0, 1).change("手工相册");
  await p.cell(0, 0).change("折枝");
  await p.cell(0, 0).change("测试角色");
  await p.element("save").onclick();
  assert.deepEqual(p.posts[0].body.mappings.panel, {"测试角色": "手工相册", "折枝": "测试相册"});
  assert.equal(p.element("panel").children.length, 2);
});

test("saving new specifications redraws both tables with server rematching", async () => {
  const p = await page();
  await p.cell(0, 1).change("手工相册");
  p.element("panel-specification").value = "立绘";
  p.element("background-specification").value = "壁纸";
  await p.element("save").onclick();
  assert.deepEqual(p.posts[0].body.specifications, {panel: "立绘", background: "壁纸"});
  assert.equal(p.cell(0, 1).value, "折枝-立绘");
  assert.equal(p.cell(1, 1).value, "");
  assert.equal(p.element("background").children[0].children[1].children[0].value, "折枝-壁纸");
  assert.match(p.element("notice").textContent, /两张映射表已清空/);
  assert.equal(p.element("panel-suffix").textContent, "相册后缀「立绘」");
});

test("unsaved specifications block album creation and synchronization", async () => {
  const p = await page();
  p.element("panel-specification").value = "立绘";
  await p.element("create").onclick();
  await p.context.runSync("panel");
  assert.equal(p.posts.length, 0);
  assert.match(p.element("notice").textContent, /尚未保存/);
  assert.match(p.element("panel-progress").textContent, /尚未保存/);
  assert.equal(p.element("create").disabled, false);
  assert.equal(p.element("panel-sync").disabled, false);
});

test("creating missing albums works without native confirm", async () => {
  const p = await page();
  await p.element("create").onclick();
  assert.equal(p.posts[0].route, "create-albums");
  assert.equal(p.element("create").disabled, false);
  assert.equal(p.element("notice").textContent, "已创建 2 个相册。");
});

test("empty and identical specifications are rejected before posting", async () => {
  const p = await page();
  for (const value of ["", "横图"]) {
    p.element("panel-specification").value = value;
    await p.element("save").onclick();
    assert.equal(p.posts.length, 0);
    assert.match(p.element("notice").textContent, /保存失败/);
    assert.equal(p.element("save").disabled, false);
  }
});
