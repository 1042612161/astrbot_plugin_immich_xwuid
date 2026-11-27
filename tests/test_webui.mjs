// Run with Playwright installed locally or made available through NODE_PATH.
import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {readFile} from "node:fs/promises";
import {createServer} from "node:http";
import {after, before, test} from "node:test";
const require = createRequire(import.meta.url);
const {chromium} = require("playwright");
let browser, server, base;

before(async () => {
  server = createServer(async (req, res) => {
    const name = req.url === "/app.js" ? "app.js" : "index.html";
    res.setHeader("Content-Type", name.endsWith("js") ? "text/javascript" : "text/html; charset=utf-8");
    res.end(await readFile(new URL(`../views/gallery-sync/${name}`, import.meta.url)));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || "chromium"});
});
after(async () => { await browser?.close(); await new Promise(resolve => server?.close(resolve)); });

async function page(t, viewport = {width: 1440, height: 1000}) {
  const page = await browser.newPage({viewport});
  t.after(() => page.close());
  const errors = []; page.on("pageerror", error => errors.push(error.message));
  t.after(() => assert.deepEqual(errors, []));
  await page.addInitScript(() => {
    const clone = value => JSON.parse(JSON.stringify(value));
    const roles = [{id: "1502", name: "漂泊者·衍射"}, {id: "1105", name: "折枝"}, {id: "1103", name: "白芷"}, {id: "1501", name: "漂泊者·衍射"}];
    let data = {roles: {panel: roles, background: roles}, albums: ["折枝-竖图", "折枝-横图", "白芷-竖图", "鸣潮杂图-竖图", "鸣潮杂图-横图", "漂泊者·衍射-竖图", "漂泊者·衍射-横图", "折枝-立绘", "折枝-壁纸"].map((name, id) => ({id: String(id), name})), specifications: {panel: "竖图", background: "横图"}, mappings: {panel: {"1105": "折枝-竖图", "1103": "白芷-竖图", "1501": "漂泊者·衍射-竖图", "1502": "漂泊者·衍射-竖图"}, background: {"1105": "折枝-横图"}}};
    window.__posts = []; window.__polls = 0;
    window.AstrBotPluginView = {
      ready: async () => {},
      apiGet: async route => {
        if (route === "refresh") return clone(data);
        window.__polls++;
        if (window.__expired) return {error: "同步任务不存在或已过期。"};
        return window.__polls === 1 ? {state: "running", progress: "正在同步折枝", success: 0, duplicate: 0, failures: []} : {state: "completed", progress: "同步完成", success: 3, duplicate: 2, failures: ["折枝 · 1105/test.jpg：比例错误"]};
      },
      apiPost: async (route, body) => {
        window.__posts.push({route, body: clone(body)});
        if (route === "save") {
          const reset = Object.keys(data.specifications).some(kind => data.specifications[kind] !== body.specifications[kind]);
          data = {...data, specifications: clone(body.specifications), mappings: reset ? {panel: {"1105": "折枝-立绘"}, background: {"1105": "折枝-壁纸"}} : clone(body.mappings)};
          return {ok: true, reset, ...clone(data)};
        }
        if (route === "create-albums") return {count: 2};
        return {job_id: "job-1"};
      },
    };
    window.confirm = () => { throw new Error("Native confirm is forbidden"); };
  });
  await page.goto(base);
  await page.waitForFunction(() => document.querySelector("#notice").textContent.includes("已读取"));
  return page;
}

const row = (p, id) => p.locator(`.map-row[data-role-id="${id}"]`);
async function chooseAlbum(p, id, label) {
  await row(p, id).locator(".picker-trigger").nth(1).click();
  await p.locator(".picker-search input").fill(label);
  await p.getByRole("option", {name: label, exact: true}).click();
}

test("same-name roles save separately by ID, including a manual shared album", async t => {
  const p = await page(t);
  assert.equal(await p.locator(".map-row").count(), 4);
  await chooseAlbum(p, "1103", "鸣潮杂图-竖图");
  await p.locator("#save").click();
  await p.waitForFunction(() => document.querySelector("#notice").textContent === "设置与映射已保存。");
  const posts = await p.evaluate(() => window.__posts);
  assert.equal(posts[0].body.mappings.panel["1103"], "鸣潮杂图-竖图");
  assert.equal(posts[0].body.mappings.panel["1501"], "漂泊者·衍射-竖图");
  assert.equal(posts[0].body.mappings.panel["1502"], "漂泊者·衍射-竖图");
});

test("album dropdown searches and filters by the active saved specification", async t => {
  const p = await page(t);
  await row(p, "1105").locator(".picker-trigger").nth(1).click();
  assert.equal(await p.getByRole("option", {name: "折枝-横图", exact: true}).count(), 0);
  await p.locator(".picker-search input").fill("杂图");
  assert.deepEqual(await p.getByRole("option").allTextContents(), ["鸣潮杂图-竖图"]);
  await p.keyboard.press("Escape");
  await p.locator("#tab-background").click();
  await row(p, "1105").locator(".picker-trigger").nth(1).click();
  assert.equal(await p.getByRole("option", {name: "折枝-竖图", exact: true}).count(), 0);
  assert.equal(await p.getByRole("option", {name: "折枝-横图", exact: true}).count(), 1);
});

test("mapping roles sort by Chinese name then ID and search by ID", async t => {
  const p = await page(t);
  const ids = await p.locator(".id-cell").allTextContents();
  assert.deepEqual(ids, ["1103", "1501", "1502", "1105"]);
  await p.locator("#mapping-search").fill("1502");
  assert.deepEqual(await p.locator(".id-cell").allTextContents(), ["1502"]);
});

test("role exchange retains both albums and does not duplicate role IDs", async t => {
  const p = await page(t);
  await row(p, "1103").locator(".picker-trigger").first().click();
  await p.getByRole("option", {name: "折枝 · 1105", exact: true}).click();
  assert.equal(await row(p, "1105").locator(".picker-trigger").nth(1).textContent(), "白芷-竖图⌄");
  assert.equal(await row(p, "1103").locator(".picker-trigger").nth(1).textContent(), "折枝-竖图⌄");
  await p.locator("#save").click();
  assert.equal((await p.evaluate(() => window.__posts))[0].body.mappings.panel["1105"], "白芷-竖图");
});

test("both sync types show IDs and keep independent search selections", async t => {
  const p = await page(t);
  await p.locator("#role-search").fill("1501");
  assert.deepEqual(await p.locator(".role-label").allTextContents(), ["漂泊者·衍射 · 1501"]);
  await p.locator(".role-option input").check();
  assert.match(await p.locator("#selected-chips").textContent(), /1501/);
  await p.locator("#sync-tab-background").click();
  assert.equal(await p.locator(".role-option input:checked").count(), 0);
  assert.match((await p.locator(".role-label").allTextContents()).join(), /1502/);
  await p.locator("#role-search").fill("1105");
  await p.locator(".role-option input").check();
  await p.locator("#sync-tab-panel").click();
  assert.match(await p.locator("#selected-chips").textContent(), /1501/);
  assert.doesNotMatch(await p.locator("#selected-chips").textContent(), /1105/);
});

test("select all applies to filtered results and preserves hidden selections", async t => {
  const p = await page(t);
  await p.locator("#role-search").fill("漂泊者");
  await p.locator("#toggle-visible").click();
  await p.locator("#role-search").fill("折枝");
  await p.locator("#toggle-visible").click();
  assert.match(await p.locator("#selected-count").textContent(), /3/);
  await p.locator("#toggle-visible").click();
  assert.match(await p.locator("#selected-count").textContent(), /2/);
});

test("sync submits IDs and displays final results at the top without continued polling", async t => {
  const p = await page(t);
  await p.locator("#role-search").fill("1105"); await p.locator(".role-option input").check();
  await p.locator("#sync-button").click();
  await p.waitForFunction(() => document.querySelector("#task-title").textContent === "同步已完成");
  assert.deepEqual((await p.evaluate(() => window.__posts))[0].body.roles, ["1105"]);
  assert.equal(await p.evaluate(() => window.__polls), 2);
  assert.match(await p.locator("#task-results").textContent(), /比例错误/);
  assert.equal(await p.locator("#sync-button").isDisabled(), false);
});

test("expired sync restores controls and displays its reason", async t => {
  const p = await page(t);
  await p.evaluate(() => window.__expired = true);
  await p.locator("#role-search").fill("1105"); await p.locator(".role-option input").check();
  await p.locator("#sync-button").click();
  await p.waitForFunction(() => document.querySelector("#task-title").textContent === "同步失败");
  assert.match(await p.locator("#task-text").textContent(), /同步任务不存在或已过期/);
  assert.equal(await p.locator("#sync-button").isDisabled(), false);
});

test("unsaved mappings and specifications block synchronization", async t => {
  const p = await page(t);
  await chooseAlbum(p, "1103", "鸣潮杂图-竖图");
  await p.locator("#role-search").fill("1105"); await p.locator(".role-option input").check();
  await p.locator("#sync-button").click();
  assert.match(await p.locator("#notice").textContent(), /映射尚未保存/);
  await p.locator("#panel-specification").fill("立绘");
  await p.locator("#create").click();
  assert.match(await p.locator("#notice").textContent(), /规格名称尚未保存/);
  assert.equal((await p.evaluate(() => window.__posts)).length, 0);
});

test("new specifications re-match both tables and filter new album options", async t => {
  const p = await page(t);
  await p.locator("#panel-specification").fill("立绘"); await p.locator("#background-specification").fill("壁纸");
  await p.locator("#save").click();
  await p.waitForFunction(() => document.querySelector("#notice").textContent.includes("两张映射表已清空"));
  assert.match(await row(p, "1105").textContent(), /折枝-立绘/);
  await row(p, "1105").locator(".picker-trigger").nth(1).click();
  assert.deepEqual(await p.getByRole("option").allTextContents(), ["清除当前映射", "折枝-立绘"]);
});

test("create missing albums works without confirm", async t => {
  const p = await page(t); await p.locator("#create").click();
  await p.waitForFunction(() => document.querySelector("#notice").textContent === "已创建 2 个相册。");
  assert.equal((await p.evaluate(() => window.__posts))[0].route, "create-albums");
});

test("layout fits desktop, embedded widths, and mobile without horizontal overflow", async t => {
  const p = await page(t);
  for (const width of [1440, 1100, 920, 820, 640, 390, 320]) {
    await p.setViewportSize({width, height: 1000});
    await p.waitForTimeout(100);
    assert.equal(await p.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `overflow at ${width}px`);
    await row(p, "1105").locator(".picker-trigger").nth(1).click();
    const box = await p.locator(".picker-menu").boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= width + 1);
    await p.keyboard.press("Escape");
  }
});
