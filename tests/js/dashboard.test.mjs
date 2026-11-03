// Runs the real index.html + app.js in jsdom against a fake API. Run: cd tests/js && npm install && npm test
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const STATIC = new URL("../../src/marketflow/web/static/", import.meta.url);
const html = readFileSync(new URL("index.html", STATIC), "utf8");
const js = readFileSync(new URL("app.js", STATIC), "utf8");
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 5)); };

const ROW = (name, internal) => ({ std_item_id: 1, item_name: name, category_l: "Fresh", category_m: "Fruit", internal_sku: "APPLE-1", channel_name: "SHOPA",
  stock_qty: 100, remaining_days: 2, public_benchmark: { normalized_price: "523.00" }, internal,
  competitors: { listings: 2, out_of_stock: 1, lowest_pack_price: "8400.00" } });
const FULL = { current_selling_price: 10000, margin_rate: "15.85" };

function boot({ role = "MD", routes = {} } = {}) {
  const calls = [];
  const table = {
    "POST /auth/login": () => [200, { access_token: "tok123", user: { name: "Kim", role, email: "k@x.com" } }],
    "GET /watchlist": () => [200, { std_item_ids: [] }],
    "GET /matrix": () => [200, { rows: [ROW("Apple", role === "BUYER" ? { current_selling_price: 10000 } : FULL)] }],
    "GET /recommendations": () => [200, { recommendations: [
      { rec_id: 1, item_name: "Apple", channel_name: "SHOPA", current_price: 10000, suggested_price: 9000, reason_code: "COMPETITIVE_MATCH", flags: { REVERSE_MARGIN_WARNING: "RED", metrics: {} }, status: "PENDING" },
      { rec_id: 2, item_name: "Apple", channel_name: "SHOPA", current_price: 9000, suggested_price: 9500, reason_code: "X", flags: {}, status: "APPLIED" }] }],
    "GET /review": () => [200, { items: [{ review_id: 5, kind: "UNMATCHED_TITLE", source: "SHOPA", item_key: "수입 두리안 1kg", reason: "no match" }] }],
    "GET /selectors": () => [200, { candidates: [
      { cand_id: 3, platform: "SHOPA", status: "PENDING", selectors: { item: "article.card", fields: { title: { css: "h3" }, sale_price: { css: "b" } } } },
      { cand_id: 2, platform: "SHOPA", status: "APPROVED", selectors: { item: "li.old", fields: { title: { css: "a" }, sale_price: { css: "i" } } } }] }],
    ...routes,
  };
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost/" });
  const w = dom.window;
  w.fetch = async (path, opts = {}) => {
    const method = opts.method || "GET", key = `${method} ${path.split("?")[0]}`;
    calls.push({ method, path, auth: (opts.headers || {}).Authorization, body: opts.body });
    const handler = table[key] || (method !== "GET" ? () => [200, { ok: true }] : () => [404, { detail: "no route " + key }]);
    const [status, body] = handler();
    return { ok: status < 400, status, statusText: String(status), json: async () => body };
  };
  w.alert = () => { throw new Error("alert() must not be used"); };
  w.eval(js);
  const login = async () => {
    const f = w.document.querySelector("#login-form");
    f.elements.email.value = "k@x.com"; f.elements.password.value = "pw";
    f.dispatchEvent(new w.Event("submit", { cancelable: true, bubbles: true }));
    await settle();
  };
  return { w, d: w.document, calls, login, click: async (el) => { el.click(); await settle(); } };
}
const text = (el) => el.textContent.trim();

test("MD login shows the matrix with cost column and colored margin", async () => {
  const t = boot(); await t.login();
  assert.equal(t.d.querySelector("#login").hidden, true);
  assert.equal(t.d.querySelector("#app").hidden, false);
  assert.match(text(t.d.querySelector("#who")), /Kim · MD/);
  const cells = [...t.d.querySelectorAll("#matrix-table tbody tr")[0].children].map(text);
  assert.match(cells[0], /^Apple/); assert.equal(cells[2], "523"); assert.equal(cells[3], "10,000");
  assert.equal(cells[4], "15.85%"); assert.match(cells[5], /8,400/); assert.match(cells[5], /1\/2 out of stock/); assert.match(cells[6], /D-2/);
  assert.ok(t.d.querySelector("#matrix-table tbody td.green"));
  assert.equal(t.d.querySelector("th.cost").hidden, false);
});

test("login sends credentials once and later calls carry the bearer token", async () => {
  const t = boot(); await t.login();
  const login = t.calls.find((c) => c.path === "/auth/login");
  assert.deepEqual(JSON.parse(login.body), { email: "k@x.com", password: "pw" });
  assert.equal(login.auth, undefined);
  assert.ok(t.calls.filter((c) => c.path !== "/auth/login").every((c) => c.auth === "Bearer tok123"));
  assert.equal(t.d.querySelector("#login-form").elements.password.value, "");     // password field cleared
});

test("token is kept in memory only", async () => {
  const t = boot(); await t.login();
  assert.equal(t.w.localStorage.length, 0); assert.equal(t.w.sessionStorage.length, 0);
  assert.ok(!t.d.cookie);
});

test("BUYER sees no cost column, no decider tabs, and never calls curation endpoints", async () => {
  const t = boot({ role: "BUYER" }); await t.login();
  assert.equal(t.d.querySelector("th.cost").hidden, true);
  assert.equal(t.d.querySelector("#matrix-table tbody td.cost").hidden, true);
  assert.ok([...t.d.querySelectorAll("nav button.decider")].every((b) => b.hidden));
  assert.ok(!t.calls.some((c) => c.path === "/review" || c.path === "/selectors"));
  await t.click(t.d.querySelector('nav button[data-tab="recs"]'));
  assert.equal(t.d.querySelectorAll("#recs-table tbody button").length, 0);   // no action buttons for BUYER
});

test("untrusted text is rendered as text, never as HTML", async () => {
  const evil = '<img src=x onerror="window.pwned=1"><script>window.pwned=1</script>';
  const t = boot({ routes: {
    "GET /matrix": () => [200, { rows: [ROW(evil, FULL)] }],
    "GET /review": () => [200, { items: [{ review_id: 1, kind: "K", source: "S", item_key: evil, reason: evil }] }] } });
  await t.login();
  assert.equal(t.d.querySelectorAll("#matrix-table img, #review-table img, script[src='x']").length, 0);
  assert.ok(text(t.d.querySelector("#matrix-table tbody tr td")).includes("<img"));
  assert.equal(t.w.pwned, undefined);
});

test("recommendation actions depend on status and post to the right URL", async () => {
  const t = boot(); await t.login();
  const rows = [...t.d.querySelectorAll("#recs-table tbody tr")];
  assert.deepEqual([...rows[0].querySelectorAll("button")].map(text), ["approve", "reject"]);
  assert.deepEqual([...rows[1].querySelectorAll("button")].map(text), ["revert"]);
  assert.match(text(rows[0].children[6]), /REVERSE MARGIN WARNING/);
  await t.click(rows[0].querySelector("button"));
  const post = t.calls.find((c) => c.method === "POST" && c.path === "/recommendations/1/approve");
  assert.ok(post && post.auth === "Bearer tok123");
});

test("failed action is shown in the status line, not an alert()", async () => {
  const t = boot({ routes: { "POST /recommendations/1/approve": () => [409, { detail: "recommendation 1 is APPROVED" }] } });
  await t.login();
  await t.click(t.d.querySelector("#recs-table tbody button"));
  assert.match(text(t.d.querySelector("#toast")), /is APPROVED/);
});

test("bad credentials show an error and stay on the login screen", async () => {
  const t = boot({ routes: { "POST /auth/login": () => [401, { detail: "invalid email or password" }] } });
  await t.login();
  assert.equal(text(t.d.querySelector("#login-error")), "invalid email or password");
  assert.equal(t.d.querySelector("#app").hidden, true);
});

test("an expired session (401) signs the user out", async () => {
  const t = boot({ routes: { "GET /matrix": () => [401, { detail: "expired" }] } });
  await t.login();
  assert.equal(t.d.querySelector("#app").hidden, true); assert.equal(t.d.querySelector("#login").hidden, false);
  assert.equal(text(t.d.querySelector("#who")), "");
});

test("sign out clears the view", async () => {
  const t = boot(); await t.login();
  await t.click([...t.d.querySelectorAll("#who button")][0]);
  assert.equal(t.d.querySelector("#login").hidden, false); assert.equal(t.d.querySelector("#app").hidden, true);
});

test("tabs switch panels", async () => {
  const t = boot(); await t.login();
  for (const tab of ["recs", "review", "selectors", "matrix"]) {
    await t.click(t.d.querySelector(`nav button[data-tab="${tab}"]`));
    for (const id of ["matrix", "recs", "review", "selectors"]) assert.equal(t.d.querySelector("#" + id).hidden, id !== tab, `${tab}/${id}`);
  }
});

test("review items can be resolved", async () => {
  const t = boot(); await t.login();
  const row = t.d.querySelector("#review-table tbody tr");
  assert.equal(text(row.children[3]), "수입 두리안 1kg");
  await t.click(row.querySelector("button"));
  assert.ok(t.calls.some((c) => c.method === "POST" && c.path === "/review/5/resolve"));
});

test("selector candidates: only pending ones are actionable", async () => {
  const t = boot(); await t.login();
  const rows = [...t.d.querySelectorAll("#selectors-table tbody tr")];
  assert.deepEqual([...rows[0].querySelectorAll("button")].map(text), ["approve", "reject"]);
  assert.equal(rows[1].querySelectorAll("button").length, 0);
  assert.equal(text(rows[0].children[3]), "article.card");
  await t.click(rows[0].querySelectorAll("button")[1]);
  assert.ok(t.calls.some((c) => c.method === "POST" && c.path === "/selectors/3/reject"));
});

test("watchlist star toggles via PUT", async () => {
  const t = boot(); await t.login();
  await t.click(t.d.querySelector("#matrix-table tbody tr td:last-child button"));
  const put = t.calls.find((c) => c.method === "PUT" && c.path === "/watchlist");
  assert.deepEqual(JSON.parse(put.body), { std_item_ids: [1] });
});

test("a failed watchlist save is reported and the star does not lie", async () => {
  const t = boot({ routes: { "PUT /watchlist": () => [404, { detail: "item 1 not found" }] } });
  await t.login();
  await t.click(t.d.querySelector("#matrix-table tbody tr td:last-child button"));
  assert.match(text(t.d.querySelector("#toast")), /item 1 not found/);
  assert.equal(text(t.d.querySelector("#matrix-table tbody tr td:last-child button")), "☆");
  await t.click(t.d.querySelector("#matrix-table tbody tr td:last-child button"));      // retry sends only the one item, not a stale set
  const puts = t.calls.filter((c) => c.method === "PUT");
  assert.deepEqual(puts.map((p) => JSON.parse(p.body).std_item_ids), [[1], [1]]);
});

test("export button downloads applied changes as CSV, with formula defusing", async () => {
  const t = boot({ routes: { "GET /recommendations": () => [200, { recommendations: [
    { rec_id: 2, item_name: "Apple", channel_name: "SHOPA", current_price: 9000, suggested_price: 9500, flags: { metrics: { internal_sku: "=EVIL,1" } }, status: "APPLIED", decided_at: "2026-11-03T01:00:00", decided_by: "k@x.com" },
    { rec_id: 1, item_name: "Apple", channel_name: "SHOPA", current_price: 10000, suggested_price: 9000, flags: {}, status: "PENDING" }] }] } });
  let blob = null, name = null;
  t.w.URL.createObjectURL = (b) => { blob = b; return "blob:x"; };
  t.w.URL.revokeObjectURL = () => {};
  t.w.HTMLAnchorElement.prototype.click = function () { name = this.download; };
  await t.login();
  await t.click(t.d.querySelector("#export-applied"));
  assert.equal(name, "price-changes.csv");
  const csv = await new Promise((res) => { const fr = new t.w.FileReader(); fr.onload = () => res(fr.result); fr.readAsText(blob); });
  assert.match(csv, /^internal_sku,channel_name,new_price/);          // FileReader strips the BOM that Excel needs; checked in bytes below
  const head = await new Promise((res) => { const fr = new t.w.FileReader(); fr.onload = () => res(new Uint8Array(fr.result)); fr.readAsArrayBuffer(blob.slice(0, 3)); });
  assert.deepEqual([...head], [0xef, 0xbb, 0xbf]);
  assert.match(csv, /"'=EVIL,1",SHOPA,9500,9000,2,/);
  assert.equal(csv.trim().split("\n").length, 2);                        // PENDING row excluded
  assert.ok(t.calls.some((c) => c.path === "/recommendations?status=APPLIED"));
});

test("BUYER does not see the export button", async () => {
  const t = boot({ role: "BUYER" }); await t.login();
  assert.equal(t.d.querySelector("#export-applied").closest(".decider").hidden, true);
});
