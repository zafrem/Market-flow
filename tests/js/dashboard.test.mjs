// Runs the real index.html + app.js in jsdom against a fake API. Run: cd tests/js && npm install && npm test
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const STATIC = new URL("../../src/marketflow/web/static/", import.meta.url);
const html = readFileSync(new URL("index.html", STATIC), "utf8");
const js = readFileSync(new URL("i18n.js", STATIC), "utf8") + "\n" + readFileSync(new URL("app.js", STATIC), "utf8");   // one eval: top-level let/const must share a scope, as <script> tags do
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 5)); };

const ROW = (name, internal) => ({ std_item_id: 1, item_name: name, category_l: "Fresh", category_m: "Fruit", internal_sku: "APPLE-1", channel_name: "SHOPA",
  stock_qty: 100, remaining_days: 2, public_benchmark: { normalized_price: "523.00" }, internal,
  competitors: { listings: 2, out_of_stock: 1, lowest_pack_price: "8400.00" },
  trend: { wholesale: [["2026-10-21", "500.00"], ["2026-10-28", "510.00"], ["2026-11-03", "523.00"]],
           ours: [["2026-10-21", "10400"], ["2026-11-03", "10000"]],
           competitor: [["2026-11-03", "8400.00"]] } });
const FULL = { current_selling_price: 10000, margin_rate: "15.85", margin_trend: [["2026-10-21", "14.10"], ["2026-11-03", "15.85"]] };

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
    "GET /mi/report": () => [200, { health: [], radar: [], as_of: null, signals: [] }],
    "GET /crawlers": () => [200, { crawlers: [] }],
    "GET /matches": () => [200, { matches: [] }],
    "GET /quality": () => [200, { crawlers: [], unparseable_titles: 0, unmapped_items: 0, unconfirmed_matches: 0, matches_to_recheck: 0, issues: [] }],
    "GET /promotions": () => [200, { today: "2026-11-03", promotions: [] }],
    "GET /flyers": () => [200, { flyers: [] }],
    "GET /discover": () => [200, { as_of: "2026-11-02", window_days: 14, top_searches: [], rising: [], new_products: [], gaps: [], summary: { new_products: 2, rising: 1, top_not_in_catalog: 1, gaps: 3 } }],
    "GET /insights": () => [200, { as_of: "2026-11-03", promotions: [], prolonged_discounts: [], new_products: [], response: {} }],
    ...routes,
  };
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost/" });
  const w = dom.window;
  w.fetch = async (path, opts = {}) => {
    const method = opts.method || "GET", key = `${method} ${path.split("?")[0]}`;
    calls.push({ method, path, auth: (opts.headers || {}).Authorization, body: opts.body });
    const handler = table[key] || (method !== "GET" ? () => [200, { ok: true }] : () => [404, { detail: "no route " + key }]);
    const [status, body] = handler();
    return { ok: status < 400, status, statusText: String(status), json: async () => body, text: async () => (typeof body === "string" ? body : JSON.stringify(body)) };
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
  const fig = (i) => text(t.d.querySelectorAll("#matrix-table tbody tr")[0].children[i].querySelector(".val b"));
  assert.match(cells[0], /^Apple/); assert.equal(fig(2), "523"); assert.equal(fig(3), "10,000");
  assert.match(cells[4], /15\.85%/); assert.match(cells[5], /8,400/); assert.match(cells[5], /1\/2 out of stock/); assert.match(cells[7], /D-2/);
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
  assert.match(text(rows[0].children[6]), /Reverse margin warning/);
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


const firstRow = (t) => t.d.querySelectorAll("#matrix-table tbody tr")[0];

test("each measure gets its own sparkline with a text equivalent and a change summary", async () => {
  const t = boot(); await t.login();
  const sparks = firstRow(t).querySelectorAll("svg.spark");
  assert.equal(sparks.length, 3);                                                  // wholesale, our price, margin; competitor has one point -> a plain note
  assert.match(sparks[0].getAttribute("aria-label"), /^Wholesale, last 3 days: 2026-10-21 500, 2026-10-28 510, 2026-11-03 523$/);
  assert.match(sparks[1].getAttribute("aria-label"), /^Our price, last 2 days: 2026-10-21 10,400, 2026-11-03 10,000$/);
  const small = [...firstRow(t).querySelectorAll(".val small")].map(text);
  assert.ok(small.includes("▲ 4.6% over 13d") && small.includes("▼ 3.8% over 13d"), small.join(" | "));   // words + arrow, not colour alone
  assert.equal(text(firstRow(t).querySelector(".nohist")), "1 day of data");
  assert.ok(sparks[0].querySelector("path.line").getAttribute("d").startsWith("M5.0 "));        // starts at the left padding
  assert.equal(sparks[0].querySelectorAll("circle.dot").length, 2);                              // end marker + (hidden) hover marker
});

test("hovering a chart shows that day's value in the shared tooltip and leaving hides it", async () => {
  const t = boot(); await t.login();
  const svg = firstRow(t).querySelector("svg.spark"), tip = t.d.querySelector("#tip");
  svg.getBoundingClientRect = () => ({ left: 100, width: 96, top: 0, height: 32 });
  assert.equal(tip.hidden, true);
  svg.dispatchEvent(new t.w.MouseEvent("pointermove", { clientX: 100, clientY: 80, bubbles: true }));    // far left -> first day
  assert.equal(tip.hidden, false);
  assert.match(text(tip), /500/); assert.match(text(tip), /Wholesale · 2026-10-21/);
  assert.equal(svg.querySelector("line.cross").hasAttribute("hidden"), false);
  svg.dispatchEvent(new t.w.MouseEvent("pointermove", { clientX: 196, clientY: 80, bubbles: true }));    // far right -> last day
  assert.match(text(tip), /523/);
  svg.dispatchEvent(new t.w.MouseEvent("pointerleave", { bubbles: true }));
  assert.equal(tip.hidden, true); assert.equal(svg.querySelector("line.cross").hasAttribute("hidden"), true);
});

test("a flat or missing history degrades gracefully", async () => {
  const flat = { ...ROW("Apple", FULL), trend: { wholesale: [["2026-11-02", "5.00"], ["2026-11-03", "5.00"]], ours: [], competitor: [] } };
  const t = boot({ routes: { "GET /matrix": () => [200, { rows: [flat] }] } }); await t.login();
  const d = firstRow(t).querySelector("svg.spark path.line").getAttribute("d");
  assert.match(d, /M5\.0 16\.0 L91\.0 16\.0/);                                      // no range -> mid-height line, no NaN
  assert.equal([...firstRow(t).querySelectorAll(".nohist")].map(text).join("|"), "No history|No history");
  assert.ok(!firstRow(t).textContent.includes("NaN"));
  const old = boot({ routes: { "GET /matrix": () => [200, { rows: [{ ...ROW("Apple", { ...FULL, margin_trend: undefined }), trend: undefined }] }] } }); await old.login();
  assert.equal(firstRow(old).querySelectorAll("svg.spark").length, 0);                 // an older server without trends still renders
});

test("summary strip and tab badges reflect the data", async () => {
  const t = boot(); await t.login();
  const figs = [...t.d.querySelectorAll("#summary .fig")].map((f) => text(f.querySelector("b")) + " " + text(f.querySelector("span")));
  assert.deepEqual(figs, ["2 New competitor products", "1 Rising keywords", "1 Top searches missing from the catalog", "3 Searched but not bought", "1 recommendations awaiting a decision"]);
  assert.equal(text(t.d.querySelector('nav button[data-tab="recs"] .badge')), "1");
  assert.equal(text(t.d.querySelector('nav button[data-tab="review"] .badge')), "1");
});

test("empty states say what to do next", async () => {
  const t = boot({ routes: { "GET /matrix": () => [200, { rows: [] }], "GET /recommendations": () => [200, { recommendations: [] }], "GET /review": () => [200, { items: [] }] } });
  await t.login();
  assert.match(text(t.d.querySelector("#matrix-table tbody")), /No items yet\. Import inventory/);
  assert.match(text(t.d.querySelector("#recs-table tbody")), /No recommendations yet/);
  assert.match(text(t.d.querySelector("#review-table tbody")), /Nothing to review/);
  assert.equal(t.d.querySelector('nav button[data-tab="recs"] .badge'), null);
});


test("margin gets its own chart, in percentage points, for MD but not BUYER", async () => {
  const t = boot(); await t.login();
  const cell = firstRow(t).children[4];
  assert.match(cell.querySelector("svg.spark").getAttribute("aria-label"), /^Margin %, last 2 days: 2026-10-21 14\.10%, 2026-11-03 15\.85%$/);
  assert.ok([...cell.querySelectorAll(".val small")].map(text).includes("▲ 1.8 pt over 13d"));
  assert.equal(cell.classList.contains("green"), true);
  const b = boot({ role: "BUYER" }); await b.login();
  assert.equal(firstRow(b).children[4].hidden, true); assert.equal(firstRow(b).children[4].querySelectorAll("svg").length, 0);
});

const MI = {
  as_of: "2026-10-12", signals: ["SEARCH", "BUYER"],
  health: [{ category: "Fresh", demand_metric: "gmv", demand_state: "GROWING", demand_change: 0.12, interest_change: 0.4,
    demand_series: [["2026-09-28", "1000"], ["2026-10-05", "1100"], ["2026-10-12", "1250000"]],
    interest_series: [["2026-09-28", "10"], ["2026-10-05", "12"], ["2026-10-12", "20"]],
    share_pct: 62.5, share_shift_pp: 1.8, supply: { cover_days: 2.5, status: "TIGHT", stock: 25 },
    changed: "GMV rose 12%", why: "Stock covers only 2.5 days of sales.", action: "Demand is rising while stock is tight: secure supply first." }],
  radar: [
    { keyword: "딸기라떼", category: "Fresh", stage: "EMERGING", growth: 0.8, score: 61.5, opportunity: 55.1, volume_percentile: 0.2,
      search_series: [["2026-09-28", "5"], ["2026-10-05", "9"], ["2026-10-12", "18"]], signals: { product: 0, promotion: 2, media: 1 }, corroborating: ["media", "promotion"],
      changed: "Searches rose 80%", why: "This measures interest, not demand.", action: "Review assortment." },
    { keyword: "<b>x</b>", category: "", stage: "FADING", growth: -0.4, score: 0, opportunity: 0, volume_percentile: null,
      search_series: [["2026-10-05", "9"], ["2026-10-12", "5"]], signals: { product: 0, promotion: 0, media: 0 }, corroborating: [],
      changed: "Searches fell 40%", why: "Interest is falling.", action: "Avoid new purchase commitments." }],
};

test("category health shows demand and interest charts, share, supply and the three-part story", async () => {
  const t = boot({ routes: { "GET /mi/report": () => [200, MI] } }); await t.login();
  const row = t.d.querySelector("#health-table tbody tr"), cells = [...row.children].map(text);
  assert.match(cells[0], /Fresh/); assert.match(cells[0], /▲ Rising/);
  assert.match(cells[1], /1\.3M/); assert.match(cells[1], /\+12% vs earlier/);
  assert.equal(row.querySelectorAll("svg.spark").length, 2);                                       // one chart per measure
  assert.match(cells[3], /62\.5% \(\+1\.8 pt\)/); assert.match(cells[4], /2\.5 days · tight/);
  assert.match(cells[5], /What changed: GMV rose 12%/); assert.match(cells[5], /Why it matters: Stock covers/); assert.match(cells[5], /What to do: Demand is rising/);
  assert.match(row.querySelector("svg.spark").getAttribute("aria-label"), /Fresh GMV, last 3 days/);
});

test("trend radar lists stages in words, marks interest-only, and escapes keyword text", async () => {
  const t = boot({ routes: { "GET /mi/report": () => [200, MI] } }); await t.login();
  const rows = [...t.d.querySelectorAll("#radar-table tbody tr")];
  assert.equal(rows.length, 2);
  const c = [...rows[0].children].map(text);
  assert.equal(c[0].startsWith("딸기라떼"), true); assert.equal(c[1], "◑ Emerging"); assert.match(c[2], /\+80% vs earlier/);
  assert.match(c[3], /Promotion 2/); assert.match(c[3], /Media 1/); assert.equal(c[4], "61.5"); assert.match(c[5], /interest, not demand/);
  assert.equal([...rows[1].children].map(text)[4], "–");                                           // fading keywords carry no opportunity score
  assert.equal(rows[1].children[0].querySelectorAll("b").length, 0);                                // "<b>x</b>" stayed text
  assert.match(text(rows[1].children[0]), /<b>x<\/b>/); assert.match(text(rows[1].children[0]), /No category/);
  assert.equal(text(t.d.querySelector('nav button[data-tab="radar"] .badge')), "1");               // one seed/emerging keyword to look at
});

test("market-intelligence tabs switch panels, and the flat-keyword toggle re-queries", async () => {
  const t = boot(); await t.login();
  await t.click(t.d.querySelector('nav button[data-tab="radar"]'));
  assert.equal(t.d.querySelector("#radar").hidden, false); assert.equal(t.d.querySelector("#matrix").hidden, true);
  assert.match(text(t.d.querySelector("#radar-table .empty")), /import-mi FILE\.csv --type SEARCH/);
  await t.click(t.d.querySelector('nav button[data-tab="health"]'));
  assert.match(text(t.d.querySelector("#health-table .empty")), /import-mi FILE\.csv --type BUYER/);
  const box = t.d.querySelector("#radar-stable"); box.checked = true; box.dispatchEvent(new t.w.Event("change", { bubbles: true })); await settle();
  assert.ok(t.calls.some((c) => c.path === "/mi/report?lang=en&include_stable=true"));
});

test("language switch: static text, dynamic text, number format and the server request all follow", async () => {
  const seen = [];
  const t = boot({ routes: { "GET /mi/report": () => [200, MI] } });
  const orig = t.w.fetch; t.w.fetch = async (p, o) => { seen.push(p); return orig(p, o); };
  await t.login();
  assert.equal(t.d.documentElement.lang, "en"); assert.equal(text(t.d.querySelector("#lang")), "한국어");
  assert.equal(text(t.d.querySelector('nav button[data-tab="health"]')), "Category health");
  await t.click(t.d.querySelector("#lang"));
  assert.equal(t.d.documentElement.lang, "ko"); assert.equal(text(t.d.querySelector("#lang")), "English");
  assert.equal(text(t.d.querySelector('nav button[data-tab="health"]')), "카테고리 헬스");
  assert.equal(text(t.d.querySelector('nav button[data-tab="radar"] [data-i18n]')), "트렌드 레이더");
  assert.match(text(t.d.querySelector("#login-form label")), /이메일/);
  assert.match(text(t.d.querySelector("#who")), /로그아웃/);
  assert.ok(seen.includes("/mi/report?lang=ko"));                                                  // narratives are re-requested in Korean
  const health = [...t.d.querySelector("#health-table tbody tr").children].map(text);
  assert.match(health[0], /▲ 증가/); assert.match(health[1], /125만/); assert.match(health[1], /\+12% \(이전 대비\)/);
  assert.match(health[3], /\(\+1\.8%p\)/); assert.match(health[4], /2\.5일 · 빠듯/); assert.match(health[5], /무엇이 바뀌었나: GMV rose 12%/);   // the story text itself comes from the server
  const radar = [...t.d.querySelectorAll("#radar-table tbody tr")][0];
  assert.equal(text(radar.children[1]), "◑ 부상"); assert.match(text(radar.children[3]), /프로모션 2 · 미디어 1/);
  assert.match(text(t.d.querySelector("#summary")), /경쟁사 신상품/);
  assert.match(text(t.d.querySelector("#recs-table tbody tr td.status")), /대기/);                  // status codes are translated, unknown ones fall back
  assert.match(t.d.querySelector("#matrix-table .star").getAttribute("aria-label"), /관심 품목에 추가/);
  assert.match(t.d.querySelector("svg.spark").getAttribute("aria-label"), /최근 \d+일/);
  await t.click(t.d.querySelector("#lang"));                                                       // and back
  assert.equal(text(t.d.querySelector('nav button[data-tab="health"]')), "Category health"); assert.match(text(t.d.querySelector("#summary")), /New competitor products/);
});

test("the chosen language is remembered and unknown codes fall back to readable text", async () => {
  const rec = { rec_id: 9, item_name: "Apple", channel_name: "SHOPA", current_price: 1, suggested_price: 2, reason_code: "SOME_NEW_CODE", flags: {}, status: "PENDING" };
  const t = boot({ routes: { "GET /recommendations": () => [200, { recommendations: [rec] }] } }); await t.login();
  assert.match(text(t.d.querySelector("#recs-table tbody tr")), /Some new code/);
  await t.click(t.d.querySelector("#lang"));
  assert.equal(t.w.localStorage.getItem("mf-lang"), "ko");
  assert.match(text(t.d.querySelector("#recs-table tbody tr")), /Some new code/);                  // untranslated codes stay readable in Korean mode
  assert.match(text(t.d.querySelector("#recs-table tbody tr")), /승인/);                            // while the action buttons are translated
});

const CRAWLERS = [
  { id: "report", kind: "inbox", state: "HEALTHY", enabled: true, times: ["07:30"], not_configured: null, data_as_of: "2026-10-26", last_run: { status: "OK" } },
  { id: "news", kind: "news", state: "NOT_CONFIGURED", enabled: true, times: ["07:00", "12:00"], not_configured: "Add at least one RSS/Atom feed URL in the settings.", data_as_of: null, last_run: null },
  { id: "competitor", kind: "competitor", state: "FAILING", enabled: true, times: ["08:00"], not_configured: null, data_as_of: "2026-11-03", last_run: { status: "FAILED" } }];
const DETAIL = {
  report: { ...CRAWLERS[0], fields: [], config: {}, default_times: ["07:30"], last_success: "2026-11-02T22:30:00Z", inbox: { path: "/data/inbox/report", pending: ["w44.csv"] },
    runs: [{ run_id: 3, trigger_kind: "SCHEDULED", status: "OK", queued_at: "2026-11-02T22:30:00Z", started_at: "2026-11-02T22:30:00Z", finished_at: "2026-11-02T22:30:05Z", stored: 12, message: "1 file(s), 12 value(s) loaded <b>x</b>" },
           { run_id: 2, trigger_kind: "MANUAL", status: "FAILED", queued_at: "2026-11-01T22:30:00Z", started_at: null, finished_at: "2026-11-01T22:30:05Z", stored: 0, message: "bad.csv: no period column" }] },
  news: { ...CRAWLERS[1], fields: [{ key: "feeds", kind: "lines", max: 20 }, { key: "extra_keywords", kind: "lines", max: 100 }, { key: "lookback_days", kind: "int", max: 60 }],
    config: { feeds: ["https://n.example/rss"], extra_keywords: [], lookback_days: 14 }, default_times: ["07:00"], last_success: null, runs: [] },
};
const crawlerRoutes = (calls = []) => ({
  "GET /crawlers": () => [200, { crawlers: CRAWLERS }],
  "GET /crawlers/report": () => [200, DETAIL.report], "GET /crawlers/news": () => [200, DETAIL.news],
  "PUT /crawlers/news": () => [200, DETAIL.news], "POST /crawlers/report/run": () => [202, { run_id: 9, status: "QUEUED" }],
  "POST /crawlers/report/upload": () => [200, { status: "OK", stored: 3, message: "1 file(s), 3 value(s) loaded" }],
});

test("each crawler has its own page with status, settings, inbox and run history (message text escaped)", async () => {
  const t = boot({ role: "ADMIN", routes: crawlerRoutes() }); await t.login();
  const items = [...t.d.querySelectorAll("#crawler-list button")].map(text);
  assert.deepEqual(items, ["Search report● Healthy", "News○ Needs setup", "Competitor prices✖ Failing"]);
  assert.equal(text(t.d.querySelector('nav button[data-tab="crawlers"] .badge')), "1");                 // one failing crawler
  const page = t.d.querySelector("#crawler-page");
  assert.match(text(page.querySelector("h2")), /Search report/); assert.match(text(page), /\/data\/inbox\/report/); assert.match(text(page), /w44\.csv/);
  const rows = [...page.querySelectorAll("tbody tr")];
  assert.equal(rows.length, 2); assert.match(text(rows[0]), /Scheduled.*OK.*12.*1 file\(s\), 12 value\(s\) loaded <b>x<\/b>/); assert.equal(page.querySelectorAll("tbody b").length, 0);
  assert.match(text(rows[1]), /Manual.*Failed.*bad\.csv/);
  await t.click(t.d.querySelectorAll("#crawler-list button")[1]);
  assert.equal(t.w.location.hash, "#/crawlers/news");                                                   // a bookmarkable page per crawler
  assert.match(text(page.querySelector("h2")), /News/); assert.match(text(page), /Add at least one RSS\/Atom feed URL under Settings/);
  assert.equal(page.querySelector("textarea").value, "https://n.example/rss"); assert.equal(page.querySelector('input[type=number]').value, "14");
  assert.equal(page.querySelector('input[type=text]').value, "07:00, 12:00"); assert.match(text(page), /No runs yet/);
});

test("admin saves schedule and settings, queues a run and uploads a file; MD only reads", async () => {
  const t = boot({ role: "ADMIN", routes: crawlerRoutes() }); await t.login();
  await t.click(t.d.querySelectorAll("#crawler-list button")[1]);
  const page = t.d.querySelector("#crawler-page");
  page.querySelector('input[type=text]').value = "06:00, 18:30"; page.querySelector("textarea").value = "https://a.example/rss\n\n https://b.example/rss ";
  page.querySelector("form").dispatchEvent(new t.w.Event("submit", { cancelable: true, bubbles: true })); await settle();
  const put = t.calls.find((c) => c.method === "PUT" && c.path === "/crawlers/news");
  assert.deepEqual(JSON.parse(put.body), { times: ["06:00", "18:30"], config: { feeds: ["https://a.example/rss", "https://b.example/rss"], extra_keywords: [], lookback_days: 14 } });
  await t.click(t.d.querySelectorAll("#crawler-list button")[0]);
  await t.click([...t.d.querySelectorAll("#crawler-page button")].find((b) => text(b) === "Run now"));
  assert.ok(t.calls.some((c) => c.method === "POST" && c.path === "/crawlers/report/run")); assert.match(text(t.d.querySelector("#toast")), /Run queued/);
  const file = t.d.querySelector('#crawler-page input[type=file]');
  Object.defineProperty(file, "files", { value: [{ name: "w44 report.csv", arrayBuffer: async () => new ArrayBuffer(4) }] });
  await t.click([...t.d.querySelectorAll("#crawler-page button")].find((b) => text(b) === "Upload file"));
  const up = t.calls.find((c) => c.path.startsWith("/crawlers/report/upload"));
  assert.equal(up.path, "/crawlers/report/upload?filename=w44%20report.csv"); assert.ok(up.body instanceof t.w.ArrayBuffer || up.body.byteLength === 4);
  assert.match(text(t.d.querySelector("#toast")), /Imported: 1 file\(s\), 3 value\(s\) loaded/);
  const md = boot({ role: "MD", routes: crawlerRoutes() }); await md.login();
  const mdPage = md.d.querySelector("#crawler-page");
  assert.equal(mdPage.querySelectorAll("button").length, 0); assert.equal(mdPage.querySelector('input[type=checkbox]').disabled, true);
  assert.match(text(mdPage), /Only administrators can change crawler settings/);
});

test("crawler pages follow the language switch and deep links open the right page", async () => {
  const t = boot({ role: "ADMIN", routes: crawlerRoutes() });
  t.w.location.hash = "#/crawlers/news"; await t.login();
  assert.equal(t.d.querySelector("#crawlers").hidden, false); assert.match(text(t.d.querySelector("#crawler-page h2")), /News/);
  await t.click(t.d.querySelector("#lang"));
  assert.deepEqual([...t.d.querySelectorAll("#crawler-list button")].map(text), ["검색 리포트● 정상", "뉴스○ 설정 필요", "경쟁사 가격✖ 실패 중"]);
  assert.match(text(t.d.querySelector("#crawler-page")), /설정에서 RSS\/Atom 피드 주소를 하나 이상 추가하세요/);
  assert.match(text(t.d.querySelector('nav button[data-tab="crawlers"]')), /크롤러/);
});

// ---- price/margin P0: matched-price simulation, comparison export, product matches -------------------------------------
const SIM = { target_price: 8400, expected_margin_rate: "5.20", expected_margin_amount: "400.00", bep_units: "113", bep_status: "OK", below_floor: false };
const withSim = (sim, extra = {}) => ({ "GET /matrix": () => [200, { rows: [{ ...ROW("Apple", { ...FULL, match_simulation: sim }), competitors: { listings: 1, unconfirmed: 2, out_of_stock: 0, lowest_pack_price: "8400.00" }, ...extra }] }] });

test("matrix shows the margin and break-even volume if we match, and how many listings are not confirmed", async () => {
  const t = boot({ routes: withSim(SIM) }); await t.login();
  const cell = firstRow(t).querySelector("td.matched");
  assert.match(text(cell), /5\.20%/); assert.match(text(cell), /break-even 113\/wk/);
  assert.match(text(firstRow(t)), /2 not confirmed/);
  const none = boot({ routes: withSim(null) }); await none.login();
  assert.equal(text(firstRow(none).querySelector("td.matched")), "–");
  const flo = boot({ routes: withSim({ ...SIM, bep_status: "UNACHIEVABLE", bep_units: null, below_floor: true }) }); await flo.login();
  assert.match(text(firstRow(flo).querySelector("td.matched")), /no margin at that price · below floor/);
});

test("BUYER sees no margin simulation column or export", async () => {
  const t = boot({ role: "BUYER" }); await t.login();
  assert.equal(firstRow(t).querySelector("td.matched").hidden, true);
  assert.equal(t.d.querySelector("#export-matrix").closest(".decider").hidden, true);
  assert.equal(t.d.querySelector('nav button[data-tab="matches"]').hidden, true);
});

test("comparison export is fetched with the token and saved as a file", async () => {
  const t = boot({ routes: { "GET /matrix/export": () => [200, "item,category\nApple,Fresh\n"] } });
  let blob = null, name = null;
  t.w.URL.createObjectURL = (b) => { blob = b; return "blob:x"; }; t.w.URL.revokeObjectURL = () => {};
  t.w.HTMLAnchorElement.prototype.click = function () { name = this.download; };
  await t.login(); await t.click(t.d.querySelector("#export-matrix"));
  assert.equal(name, "marketflow-comparison.csv");
  const csv = await new Promise((res) => { const fr = new t.w.FileReader(); fr.onload = () => res(fr.result); fr.readAsText(blob); });
  assert.match(csv, /Apple,Fresh/);
  assert.equal(t.calls.find((c) => c.path === "/matrix/export").auth, "Bearer tok123");
  const denied = boot({ routes: { "GET /matrix/export": () => [403, { detail: "MD or admin only" }] } });
  denied.w.URL.createObjectURL = () => { throw new Error("no file expected"); };
  await denied.login(); await denied.click(denied.d.querySelector("#export-matrix"));
  assert.match(text(denied.d.querySelector("#toast")), /MD or admin only/);
});

test("product matches: list, confirm with a grade, remove", async () => {
  const m = (code, extra) => ({ platform: "SHOPA", channel_item_code: code, std_item_id: 1, item_name: "Apple", product_title: "사과 " + code, match_grade: "SAME", match_confidence: "0.900", reviewed_at: null, confirmed: false, ...extra });
  const t = boot({ routes: { "GET /matches": () => [200, { matches: [m("X1"), m("X2", { reviewed_at: "2026-11-03 01:00:00", confirmed: true })] }] } });
  await t.login();
  const rows = t.d.querySelectorAll("#matches-table tbody tr");
  assert.equal(rows.length, 2);
  assert.match(text(rows[0]), /needs review/); assert.match(text(rows[1]), /confirmed/);
  assert.match(text(t.d.querySelector('nav button[data-tab="matches"] .badge')), /1/);               // one listing waits for a person
  const sel = rows[0].querySelector("select"); sel.value = "SIMILAR";
  await t.click([...rows[0].querySelectorAll("button")].find((b) => text(b) === "confirm"));
  const put = t.calls.find((c) => c.method === "PUT" && c.path === "/matches/SHOPA/X1");
  assert.deepEqual(JSON.parse(put.body), { std_item_id: 1, grade: "SIMILAR" });
  await t.click([...rows[1].querySelectorAll("button")].find((b) => text(b) === "remove"));
  assert.ok(t.calls.some((c) => c.method === "DELETE" && c.path === "/matches/SHOPA/X2"));
  await t.click(t.d.querySelector("#lang"));
  assert.match(text(t.d.querySelector('nav button[data-tab="matches"]')), /상품 매칭/);
  assert.match(text(t.d.querySelector("#matches-table tbody")), /검토 필요/);
});

// ---- P1: search gap tab, period-over-period change ---------------------------------------------------------------------
const GAP = { keyword: "사과 세트", category: "Fresh", searches: 9000, buyers: 90, conversion: 0.01, primary_cause: "OUT_OF_STOCK", contributing_causes: ["PRICE_DISADVANTAGE"],
  changed: "9,000 searches but only 1.0% buy", why: "Out of stock on 100% of days. Also contributing: price disadvantage.", action: "Restore supply." };
const RADAR_ROW = { keyword: "grower", category: "Fresh", stage: "GROWING", growth: 0.3, volume_percentile: 0.9, score: 40, opportunity: 4, signals: { product: 0, promotion: 0, media: 0 },
  search_series: [["2026-10-12", 100], ["2026-10-19", 130], ["2026-10-26", 160]], corroborating: [], changed: "c", why: "w", action: "a", wow: 0.2308, yoy: 1.0 };

test("search gap tab lists the cause and the contributing causes; radar shows WoW and YoY", async () => {
  const t = boot({ routes: { "GET /mi/report": () => [200, { health: [], radar: [RADAR_ROW], gap: [GAP], as_of: "2026-10-26", signals: ["SEARCH"] }] } }); await t.login();
  const row = t.d.querySelector("#gap-table tbody tr");
  assert.match(text(row), /사과 세트/); assert.match(text(row), /Out of stock/); assert.match(text(row), /\+ Price disadvantage/); assert.match(text(row), /1\.0%/);
  assert.match(text(t.d.querySelector("#radar-table tbody tr")), /\+23% vs last period · \+100% vs last year/);
  await t.click(t.d.querySelector('nav button[data-tab="gap"]'));
  assert.equal(t.d.querySelector("#gap").hidden, false); assert.equal(t.d.querySelector("#radar").hidden, true);
  await t.click(t.d.querySelector("#lang"));
  assert.match(text(t.d.querySelector("#gap-table tbody tr")), /품절/); assert.match(text(t.d.querySelector('nav button[data-tab="gap"]')), /검색-구매 간극/);
});

test("search gap tab explains itself when empty", async () => {
  const t = boot(); await t.login();
  assert.match(text(t.d.querySelector("#gap-table tbody")), /No search-to-purchase gaps/);
});

test("an MD proposes a price from the matrix row; the proposal carries the SKU, price and reason", async () => {
  const t = boot({ routes: { "POST /recommendations/draft": () => [201, { rec_id: 9, status: "PENDING", requires_admin: true }] } }); await t.login();
  await t.click(firstRow(t).querySelector("button.link"));
  const row = t.d.querySelector("tr.draft-row");
  row.querySelector('input[type=number]').value = "9100"; row.querySelector('input[type=text]').value = "match the market";
  await t.click([...row.querySelectorAll("button")].find((b) => text(b) === "Submit proposal"));
  const post = t.calls.find((c) => c.method === "POST" && c.path === "/recommendations/draft");
  assert.deepEqual(JSON.parse(post.body), { internal_sku: "APPLE-1", channel_name: "SHOPA", price: 9100, note: "match the market" });
  assert.match(text(t.d.querySelector("#toast")), /Proposal #9 submitted; it is below the defense line/);
  assert.equal(t.d.querySelector("tr.draft-row"), null);
  await t.click(firstRow(t).querySelector("button.link")); await t.click(firstRow(t).querySelector("button.link"));      // toggles open and closed
  assert.equal(t.d.querySelector("tr.draft-row"), null);
});

test("BUYER cannot propose prices", async () => {
  const t = boot({ role: "BUYER" }); await t.login();
  assert.equal(firstRow(t).querySelector("button.link").hidden, true);
});


// ---- P1: data-quality strip and promotion calendar -------------------------------------------------------------------
test("quality strip shows a chip per crawler and what needs a person; stale ones are marked", async () => {
  const q = { crawlers: [{ id: "news", enabled: true, configured: true, success_rate: 1, last_success_hours: 3, stale: false },
                         { id: "buyer", enabled: true, configured: true, success_rate: 0.5, last_success_hours: 80, stale: true },
                         { id: "flyer", enabled: false, configured: true, success_rate: null, last_success_hours: null, stale: false },
                         { id: "report", enabled: true, configured: true, success_rate: null, last_success_hours: null, stale: false, never_run: true }],
              unparseable_titles: 0, unmapped_items: 0, unconfirmed_matches: 4, matches_to_recheck: 0, issues: [] };
  const t = boot({ routes: { "GET /quality": () => [200, q] } }); await t.login();
  const chips = [...t.d.querySelectorAll("#qstrip li")];
  assert.equal(t.d.querySelector("#qstrip").hidden, false);
  assert.equal(chips.length, 4);                                                   // news, buyer, report (never run), and the unconfirmed count (flyer is disabled)
  assert.ok(chips[1].classList.contains("stale")); assert.match(text(chips[2]), /Not run yet/); assert.ok(!chips[2].classList.contains("stale")); assert.match(text(chips[3]), /4/);
});

test("BUYER never loads quality or promotions", async () => {
  const t = boot({ role: "BUYER" }); await t.login();
  assert.ok(!t.calls.some((c) => c.path === "/quality" || c.path === "/promotions"));
});

test("promotion calendar: list with state and lane, add, remove", async () => {
  const p = { promo_id: 7, name: "Chuseok", category_l: "Fresh", channel_name: "", start_date: "2026-11-01", end_date: "2026-11-09", discount_pct: "20.00", note: "", created_by: "k", state: "ACTIVE" };
  const t = boot({ routes: { "GET /promotions": () => [200, { today: "2026-11-03", promotions: [p] }], "POST /promotions": () => [201, { promo_id: 8 }] } });
  await t.login();
  const row = t.d.querySelector("#promos-table tbody tr");
  assert.match(text(row), /Chuseok/); assert.match(text(row), /Running/); assert.ok(row.querySelector(".promo-lane i"));
  const f = t.d.querySelector("#promo-form");
  f.elements.name.value = "Week"; f.elements.start_date.value = "2026-11-10"; f.elements.end_date.value = "2026-11-12"; f.elements.discount_pct.value = "15";
  f.dispatchEvent(new t.w.Event("submit", { cancelable: true, bubbles: true })); await new Promise((r) => setTimeout(r, 20));
  const post = t.calls.find((c) => c.method === "POST" && c.path === "/promotions");
  assert.deepEqual(JSON.parse(post.body), { name: "Week", start_date: "2026-11-10", end_date: "2026-11-12", discount_pct: 15 });
  await t.click([...row.querySelectorAll("button")].find((b) => text(b) === "Remove"));
  assert.ok(t.calls.some((c) => c.method === "DELETE" && c.path === "/promotions/7"));
  await t.click(t.d.querySelector("#lang"));
  assert.match(text(t.d.querySelector("#promos-table tbody")), /진행 중/);
});


test("insights: promotion verdicts, sales response basis, long discounts and new products", async () => {
  const d = { as_of: "2026-11-03",
    promotions: [{ promo_id: 1, name: "Chuseok", category_l: "Fresh", start_date: "2026-10-20", end_date: "2026-10-24", status: "OK", uplift_pct: 100, actual_units: 100, baseline_units: 50 },
                 { promo_id: 2, name: "Dud", category_l: "Fresh", start_date: "2026-10-27", end_date: "2026-10-31", status: "FAILED", uplift_pct: -2.5, actual_units: 49, baseline_units: 50 },
                 { promo_id: 3, name: "Fresh start", category_l: "", start_date: "2026-11-02", end_date: "2026-11-09", status: "INSUFFICIENT_DATA", uplift_pct: null, actual_units: 0, baseline_units: 0 }],
    response: { Fresh: { status: "OK", promotions_used: 3, span: ["2026-09-01", "2026-10-05"], elasticity: { pessimistic: 1.5, base: 2, optimistic: 3 } }, Dairy: { status: "INSUFFICIENT_DATA", promotions_used: 1, needed: 3 } },
    prolonged_discounts: [{ platform: "SHOPA", channel_item_code: "L", title: "사과 3kg", days_observed: 30, days_discounted: 24, share_pct: 80 }],
    new_products: [{ platform: "SHOPA", channel_item_code: "N", title: "딸기 500g", first_seen: "2026-10-31 08:00:00" }] };
  const t = boot({ routes: { "GET /insights": () => [200, d] } }); await t.login();
  const rows = [...t.d.querySelectorAll("#ins-promos tbody tr")];
  assert.equal(rows.length, 3);
  assert.match(text(rows[0]), /Worked/); assert.match(text(rows[0]), /\+100%/);
  assert.match(text(rows[1]), /Failed/); assert.ok(rows[1].querySelector(".status.red"));
  assert.match(text(rows[2]), /Not enough data/);                                  // no number invented
  const resp = [...t.d.querySelectorAll("#ins-response tbody tr")];
  const fresh = resp.find((r) => /Fresh/.test(text(r))), dairy = resp.find((r) => /Dairy/.test(text(r)));
  assert.match(text(fresh), /1\.5/);
  assert.match(text(dairy), /1 of 3 promotions needed/);                             // Dairy: not enough promotions, no numbers
  assert.match(text(t.d.querySelector("#ins-prolonged tbody")), /24 \/ 30/);
  assert.match(text(t.d.querySelector("#ins-new tbody")), /2026-10-31/);
  await t.click(t.d.querySelector("#lang"));
  assert.match(text(t.d.querySelector('nav button[data-tab="insights"]')), /인사이트/);
  assert.match(text(t.d.querySelector("#ins-promos tbody")), /효과 없음/);
});

test("BUYER never loads insights", async () => {
  const t = boot({ role: "BUYER" }); await t.login();
  assert.ok(!t.calls.some((c) => c.path === "/insights"));
});


test("flyers: list, item review with confidence flags, approve with a fixed name, reject", async () => {
  const flyer = { flyer_id: 4, source_name: "week44.png", week_start: "2026-11-02", status: "PENDING",
    items: [{ item_id: 10, name: "사과", price: 1000, confidence: 0.95, low_confidence: false, category_l: "Fresh", status: "PENDING" },
            { item_id: 11, name: "딸기 5OOg", price: null, confidence: 0.4, low_confidence: true, category_l: "", status: "PENDING" }] };
  const t = boot({ routes: { "GET /flyers": () => [200, { flyers: [{ flyer_id: 4, source_name: "week44.png", week_start: "2026-11-02", status: "PENDING", items: 2, pending: 2 }] }],
                             "GET /flyers/4": () => [200, flyer], "POST /flyers/4/review": () => [200, flyer] } });
  await t.login();
  assert.match(text(t.d.querySelector('nav button[data-tab="flyers"] .badge')), /1/);
  await t.click(t.d.querySelector("#flyer-list button"));
  const rows = [...t.d.querySelectorAll("#flyer-page tbody tr")];
  assert.equal(rows.length, 2);
  assert.match(text(rows[1]), /Check this one/); assert.ok(!/Check this one/.test(text(rows[0])));            // only the unsure one is flagged
  rows[1].querySelector("input").value = "딸기 500g";
  await t.click([...rows[1].querySelectorAll("button")].find((b) => text(b) === "Approve"));
  const post = t.calls.find((c) => c.method === "POST" && c.path === "/flyers/4/review");
  assert.deepEqual(JSON.parse(post.body), { decisions: [{ item_id: 11, action: "approve", name: "딸기 500g" }] });
  await t.click([...rows[0].querySelectorAll("button")].find((b) => text(b) === "Reject"));
  assert.deepEqual(JSON.parse(t.calls.filter((c) => c.path === "/flyers/4/review")[1].body), { decisions: [{ item_id: 10, action: "reject" }] });
});

test("BUYER never loads flyers", async () => {
  const t = boot({ role: "BUYER" }); await t.login();
  assert.ok(!t.calls.some((c) => c.path.startsWith("/flyers")));
});

// ---- login description and the ADMIN settings tab ------------------------------------------------------------------------
const SYSTEM = (over = {}) => ({ store: "duckdb", local: true, job: { state: "idle", message: "", result: null }, solo_mode: true,
  counts: { ext_public_benchmarks: 0, ext_retail_prices: 0, int_operations_inventory: 0, price_recommendations: 0, promotions: 0, mi_observations: 0, product_channel_map: 0, users: 1, master_products: 8 },
  integrations: {}, ...over });
const USERS = { users: [{ user_id: 1, email: "k@x.com", name: "Kim", role: "ADMIN", active: 1, categories: [] }, { user_id: 2, email: "m@x.com", name: "Lee", role: "MD", active: 1, categories: ["Fresh/Fruit"] }] };

test("the sign-in page says what the product is, in both languages", async () => {
  const t = boot();
  assert.match(text(t.d.querySelector("#login .intro")), /for food retail, fresh and processed/);
  await t.click(t.d.querySelector("#lang"));
  assert.match(text(t.d.querySelector("#login .intro")), /판매 채널에 가격을 직접 쓰지 않습니다/);
  assert.ok(t.d.querySelector("#login-form"));
});

test("settings tab is ADMIN only; MD and BUYER never load it", async () => {
  for (const role of ["MD", "BUYER"]) {
    const t = boot({ role }); await t.login();
    assert.ok(t.d.querySelector('nav button[data-tab="settings"]').hidden);
    assert.ok(!t.calls.some((c) => c.path.startsWith("/admin")));
  }
});

test("settings: list users, add a user with categories, change a role", async () => {
  const t = boot({ role: "ADMIN", routes: { "GET /admin/users": () => [200, USERS], "GET /admin/system": () => [200, SYSTEM()], "POST /admin/users": () => [201, { user_id: 3 }] } });
  await t.login();
  assert.ok(!t.d.querySelector('nav button[data-tab="settings"]').hidden);
  const rows = t.d.querySelectorAll("#users-table tbody tr");
  assert.equal(rows.length, 2);
  assert.ok(rows[0].querySelector("select").disabled);                               // no demoting yourself
  assert.equal(rows[1].querySelector('input[type=text]').value, "Fresh/Fruit");
  const f = t.d.querySelector("#user-form");
  f.elements.email.value = "new@x.com"; f.elements.name.value = "New"; f.elements.role.value = "BUYER"; f.elements.categories.value = "Fresh/Fruit, Dairy"; f.elements.password.value = "long-enough-pw";
  f.dispatchEvent(new t.w.Event("submit", { cancelable: true, bubbles: true })); await settle();
  const post = t.calls.find((c) => c.method === "POST" && c.path === "/admin/users");
  assert.deepEqual(JSON.parse(post.body), { email: "new@x.com", name: "New", role: "BUYER", password: "long-enough-pw" });
  const put = t.calls.find((c) => c.method === "PUT" && c.path === "/admin/users/3/assignments");
  assert.deepEqual(JSON.parse(put.body), [{ category_l: "Fresh", category_m: "Fruit" }, { category_l: "Dairy", category_m: "" }]);
  const sel = rows[1].querySelector("select"); sel.value = "BUYER"; sel.dispatchEvent(new t.w.Event("change")); await settle();
  assert.deepEqual(JSON.parse(t.calls.find((c) => c.method === "PATCH" && c.path === "/admin/users/2").body), { role: "BUYER" });
});

test("settings: demo data only into an empty database; clearing needs DELETE typed", async () => {
  let counts = SYSTEM().counts;
  const t = boot({ role: "ADMIN", routes: { "GET /admin/users": () => [200, USERS], "GET /admin/system": () => [200, SYSTEM({ counts })], "POST /admin/clear": () => [200, { removed: {} }] } });
  await t.login();
  assert.ok(!t.d.querySelector("#demo-load").disabled);                              // empty: demo can load
  counts = { ...counts, ext_retail_prices: 40 };
  await t.click(t.d.querySelector('nav button[data-tab="settings"]')); await t.w.eval("loadSystem()"); await settle();
  assert.ok(t.d.querySelector("#demo-load").disabled);                               // has data: clear first
  assert.ok(t.d.querySelector("#clear-form").hidden);
  await t.click(t.d.querySelector("#data-clear"));
  assert.ok(!t.d.querySelector("#clear-form").hidden);
  const f = t.d.querySelector("#clear-form");
  f.elements.confirm.value = "DELETE";
  f.dispatchEvent(new t.w.Event("submit", { cancelable: true, bubbles: true })); await settle();
  assert.deepEqual(JSON.parse(t.calls.find((c) => c.path === "/admin/clear").body), { confirm: "DELETE" });
  assert.ok(f.hidden);
});

// ---- discover is the first screen ------------------------------------------------------------------------------------------
const KW = (over = {}) => ({ keyword: "그릭요거트", category: "Processed", searches: 1200, wow: 0.1, yoy: null, growth: 0.23, stage: "EMERGING", in_catalog: false, action: "Review the range", changed: "", series: [["2026-10-26", "900"], ["2026-11-02", "1200"]], ...over });
const DISCOVER = { as_of: "2026-11-02", window_days: 14, summary: { new_products: 1, rising: 1, top_not_in_catalog: 1, gaps: 1 },
  gaps: [{ keyword: "라면", category: "Processed", searches: 1700, conversion: 0.02, primary_cause: "OUT_OF_STOCK", contributing_causes: ["PRICE_DISADVANTAGE"], action: "Restock" }],
  top_searches: [KW(), KW({ keyword: "사과", category: "Fresh", searches: 900, in_catalog: true, growth: 0.02, stage: "MAINSTREAM" })], rising: [KW()],
  new_products: [{ platform: "KURLY", channel_item_code: "N1", title: "그릭 요거트 프리미엄 1kg", std_item_id: 2, first_seen: "2026-10-29 01:00:00", item_name: "Yogurt", category: "Processed", price: 7900, we_sell_item: false }] };

test("discover opens first: new competitor products and the most searched keywords come before any price", async () => {
  const t = boot({ routes: { "GET /discover": () => [200, DISCOVER] } }); await t.login();
  assert.ok(!t.d.querySelector("#discover").hidden && t.d.querySelector("#matrix").hidden);
  assert.equal(t.d.querySelector("nav button.active").dataset.tab, "discover");
  assert.equal(t.d.querySelector("nav button").dataset.tab, "discover");                       // first in the menu, prices come later
  const np = text(t.d.querySelector("#disc-new tbody"));
  assert.match(np, /KURLY/); assert.match(np, /그릭 요거트 프리미엄 1kg/); assert.match(np, /7,900/); assert.match(np, /no/);   // we do not sell this item
  const top = [...t.d.querySelectorAll("#disc-top tbody tr")];
  assert.equal(top.length, 2);
  assert.match(text(top[0]), /그릭요거트/); assert.match(text(top[0]), /not in catalog/);
  assert.match(text(top[1]), /사과/); assert.match(text(top[1]), /✓/);
  assert.match(text(t.d.querySelector("#disc-rising tbody")), /그릭요거트/);
  const gap = text(t.d.querySelector("#disc-gaps tbody"));
  assert.match(gap, /라면/); assert.match(gap, /2\.0%/); assert.match(gap, /Out of stock/); assert.match(gap, /Price disadvantage/);
  assert.equal(text(t.d.querySelector('nav button[data-tab="discover"] .badge')), "1");
});

test("discover is empty-safe and also loads for BUYER (no cost data in it)", async () => {
  const t = boot({ role: "BUYER" }); await t.login();
  assert.ok(t.calls.some((c) => c.path.startsWith("/discover")));
  assert.match(text(t.d.querySelector("#disc-new tbody")), /No new competitor products in the last 14 days/);
});
