// The access token lives only in memory (not localStorage): a reload signs you out, and XSS cannot persist it.
let token = null, me = null, watch = new Set();
const $ = (s) => document.querySelector(s);
const el = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; };
const won = (v) => (v === null || v === undefined) ? "–" : Number(v).toLocaleString("ko-KR");

async function api(path, opts = {}) {
  const { raw, ...init } = opts;                                        // raw: a file body, sent as-is instead of JSON
  const r = await fetch(path, { ...init, headers: { "Content-Type": raw ? "text/csv" : "application/json", ...(token ? { Authorization: "Bearer " + token } : {}) } });
  if (r.status === 401 && token) { signOut(); throw new Error(t("session_expired")); }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
}

// ---- small line charts (14-day trends) -------------------------------------------------------------------------
const SVGNS = "http://www.w3.org/2000/svg", DAY_MS = 86400000, W = 96, H = 32, PAD_X = 5, PAD_Y = 6;
const svgEl = (tag, attrs = {}) => { const e = document.createElementNS(SVGNS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); return e; };
const parsePoints = (series) => (series || []).map(([d, v]) => ({ d, t: Date.parse(d + "T00:00:00Z"), v: Number(v) })).filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v));
const tip = () => document.querySelector("#tip");

// Direction of change over the window, as words and an arrow (never colour alone, and never red/green: up is not "bad").
function change(points, points_unit = false) {
  if (points.length < 2) return "";
  const a = points[0].v, b = points[points.length - 1].v, days = Math.round((points[points.length - 1].t - points[0].t) / DAY_MS);
  const d = points_unit ? b - a : (a ? (b - a) / a * 100 : 0);                       // margins change in percentage points, prices in %
  const arrow = Math.abs(d) < 0.05 ? "▬" : d > 0 ? "▲" : "▼";
  return t("chart.over", { arrow, delta: Math.abs(d).toFixed(1) + (points_unit ? " pt" : "%"), d: days });
}

function sparkline(series, cls, name, fmt) {
  const pts = parsePoints(series);
  if (pts.length < 2) return el("span", pts.length ? t("chart.one_day") : t("chart.none"), "nohist");
  const t0 = pts[0].t, span = pts[pts.length - 1].t - t0 || 1;
  const lo = Math.min(...pts.map((p) => p.v)), hi = Math.max(...pts.map((p) => p.v)), range = hi - lo;
  const x = (p) => PAD_X + (p.t - t0) / span * (W - 2 * PAD_X);                       // calendar-scaled: gaps stay visible
  const y = (p) => range === 0 ? H / 2 : PAD_Y + (hi - p.v) / range * (H - 2 * PAD_Y);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "spark " + cls, role: "img",
    "aria-label": t("chart.aria", { name, n: pts.length, points: pts.map((p) => p.d + " " + fmt(p.v)).join(", ") }) });
  svg.append(svgEl("path", { class: "line", d: pts.map((p, i) => (i ? "L" : "M") + x(p).toFixed(1) + " " + y(p).toFixed(1)).join(" ") }));
  const last = pts[pts.length - 1];
  svg.append(svgEl("circle", { class: "dot", cx: x(last).toFixed(1), cy: y(last).toFixed(1), r: 4 }));
  const cross = svgEl("line", { class: "cross", y1: 0, y2: H, hidden: "" }), mark = svgEl("circle", { class: "dot", r: 4, hidden: "" });
  svg.append(cross, mark);
  const show = (ev) => {
    const box = svg.getBoundingClientRect(), frac = box.width ? (ev.clientX - box.left) / box.width : 1;
    let best = pts[0]; for (const p of pts) if (Math.abs(x(p) - frac * W) < Math.abs(x(best) - frac * W)) best = p;
    cross.setAttribute("x1", x(best)); cross.setAttribute("x2", x(best)); mark.setAttribute("cx", x(best)); mark.setAttribute("cy", y(best));
    cross.removeAttribute("hidden"); mark.removeAttribute("hidden");
    const tipEl = tip(); tipEl.replaceChildren(el("b", fmt(best.v)), document.createTextNode(name + " · " + best.d)); tipEl.hidden = false;
    tipEl.style.left = Math.min(ev.clientX + 14, window.innerWidth - 190) + "px"; tipEl.style.top = Math.max(ev.clientY - 52, 4) + "px";
  };
  const hide = () => { cross.setAttribute("hidden", ""); mark.setAttribute("hidden", ""); tip().hidden = true; };
  svg.addEventListener("pointermove", show); svg.addEventListener("pointerdown", show); svg.addEventListener("pointerleave", hide);
  return svg;
}

// value (+ context line) on the left, its own chart on the right; each measure has its own scale
function trendCell(value, series, cls, name, fmt, extra, tdClass = "num", pointsUnit = false) {
  const td = el("td", "", tdClass), box = el("div", "", "trend"), val = el("div", "", "val");
  val.append(el("b", value));
  const pts = parsePoints(series); const delta = change(pts, pointsUnit);
  if (delta) val.append(el("small", delta));
  if (extra) val.append(el("small", extra));
  box.append(val, sparkline(series, cls, name, fmt)); td.append(box);
  return td;
}

// ---- summary strip + tab badges ----------------------------------------------------------------------------------
let lastRows = [], pendingCount = null;
function renderSummary() {
  const ds = discoverSummary, figs = [];
  if (ds) figs.push([ds.new_products, t("sum.new"), false], [ds.rising, t("sum.rising"), false], [ds.top_not_in_catalog, t("sum.missing"), ds.top_not_in_catalog > 0], [ds.gaps, t("sum.gaps"), ds.gaps > 0]);
  if (pendingCount !== null) figs.push([pendingCount, t("sum.pending"), pendingCount > 0]);
  $("#summary").replaceChildren(...figs.map(([n, label, alert]) => { const f = el("div", "", alert ? "fig alert" : "fig"); f.append(el("b", won(n)), el("span", label)); return f; }));
}
function setBadge(tab, n) {
  const b = document.querySelector(`nav button[data-tab="${tab}"]`); let badge = b.querySelector(".badge");
  if (!n) { badge?.remove(); return; }
  if (!badge) { badge = el("span", "", "badge"); b.append(badge); }
  badge.textContent = n;
}


function toast(msg) { $("#toast").textContent = msg || ""; }

function signOut() { token = me = null; discoverSummary = null; lastRows = []; miReport = null; crawlerId = null; pendingCount = null; tip().hidden = true; $("#app").hidden = true; $("#login").hidden = false; $("#who").textContent = ""; }
function renderWho() {
  if (!me) return;
  const out = el("button", t("signout")); out.onclick = signOut;
  $("#who").replaceChildren(document.createTextNode(me.name + " · " + me.role + "  "), out);
}
const TABS = ["discover", "matrix", "health", "radar", "gap", "crawlers", "recs", "matches", "promos", "flyers", "insights", "review", "selectors", "settings"];
const reloadAll = () => Promise.all([loadDiscover(), loadMatrix(), loadMi(), loadRecs(), ...(me.role === "BUYER" ? [] : [loadQuality(), loadPromos(), loadInsights(), loadFlyers(), loadMatches(), loadReview(), loadSelectors(), loadCrawlers(), loadSettings()])]);
function setLang(l) {
  lang = l;
  try { localStorage.setItem("mf-lang", l); } catch (e) { /* storage blocked: the choice lasts until reload */ }
  applyStatic(); $("#lang").textContent = t("lang.switch"); renderWho();
  if (token) reloadAll().catch((e) => toast(e.message));
}
$("#lang").addEventListener("click", () => setLang(lang === "ko" ? "en" : "ko"));
applyStatic(); $("#lang").textContent = t("lang.switch");

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  try {
    const res = await api("/auth/login", { method: "POST", body: JSON.stringify({ email: f.get("email"), password: f.get("password") }) });
    token = res.access_token; me = res.user; ev.target.reset(); $("#login-error").textContent = "";
    $("#login").hidden = true; $("#app").hidden = false;
    renderWho();
    document.querySelectorAll(".cost, .decider").forEach((c) => c.hidden = me.role === "BUYER");
    document.querySelectorAll(".admin-only").forEach((c) => c.hidden = !isAdmin());
    toast("");
    watch = new Set((await api("/watchlist")).std_item_ids);
    await reloadAll();
    await routeCrawler();                                              // a deep link like #/crawlers/news opens that page after sign-in
  } catch (e) { $("#login-error").textContent = e.message; }
});

document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("active", x === b));
  for (const id of TABS) $("#" + id).hidden = b.dataset.tab !== id;
  if (b.dataset.tab === "crawlers" && crawlerId) history.replaceState(null, "", "#/crawlers/" + crawlerId);
}));
$("#only-watch").addEventListener("change", loadMatrix);
$("#radar-stable").addEventListener("change", loadMi);

async function loadMatrix() {
  const { rows } = await api("/matrix" + ($("#only-watch").checked ? "?watchlist=true" : ""));
  lastRows = rows; renderSummary();
  const body = $("#matrix-table tbody"); body.replaceChildren();
  if (!rows.length) {
    const td = el("td", $("#only-watch").checked ? t("empty.watch") : t("empty.items"), "empty");
    td.colSpan = 9; const tr = el("tr"); tr.append(td); body.append(tr); return;
  }
  const decimals = (v) => v.toLocaleString(numLocale(), { maximumFractionDigits: 2 });
  for (const r of rows) {
    const tr = el("tr"), tr_ = r.trend || {};
    const name = el("td"); name.append(el("span", r.item_name, "item"), el("small", r.category_l + " / " + r.category_m + " · " + r.internal_sku));
    let margin = el("td", "", "num cost margin"); margin.hidden = me.role === "BUYER";
    if (r.internal.margin_rate !== undefined) {
      const low = Number(r.internal.margin_rate) <= 10;
      margin = trendCell((low ? "⚠ " : "✓ ") + r.internal.margin_rate + "%", r.internal.margin_trend, "c-margin", t("chart.margin"), (v) => v.toFixed(2) + "%", "", "num cost margin " + (low ? "red" : "green"), true);
    }
    const bench = trendCell(r.public_benchmark ? won(r.public_benchmark.normalized_price) : "–", tr_.wholesale, "c-wholesale", t("th.wholesale"), decimals);
    const ours = trendCell(won(r.internal.current_selling_price), tr_.ours, "c-ours", t("th.ours"), won);
    const unconf = r.competitors.unconfirmed ? t("unconfirmed", { n: r.competitors.unconfirmed }) : "";
    const oos = [r.competitors.out_of_stock ? t("oos", { n: r.competitors.out_of_stock, total: r.competitors.listings }) : "", unconf].filter(Boolean).join(" · ");
    const comp = trendCell(r.competitors.lowest_pack_price ? won(r.competitors.lowest_pack_price) : "–", tr_.competitor, "c-comp", t("th.lowest"), decimals, oos);
    const star = el("button", watch.has(r.std_item_id) ? "★" : "☆", "star" + (watch.has(r.std_item_id) ? " on" : ""));
    star.setAttribute("aria-label", t(watch.has(r.std_item_id) ? "watch.remove" : "watch.add", { name: r.item_name }));
    star.onclick = async () => {
      const had = watch.has(r.std_item_id);
      had ? watch.delete(r.std_item_id) : watch.add(r.std_item_id);
      try { await api("/watchlist", { method: "PUT", body: JSON.stringify({ std_item_ids: [...watch] }) }); toast(""); }
      catch (e) { had ? watch.add(r.std_item_id) : watch.delete(r.std_item_id); toast(e.message); return; }   // roll back local state
      loadMatrix();
    };
    const starCell = el("td"); starCell.append(star);
    const draftBtn = el("button", t("draft.open"), "link decider"); draftBtn.hidden = me.role === "BUYER";
    draftBtn.onclick = () => toggleDraftRow(tr, r);
    ours.append(draftBtn);
    tr.append(name, el("td", r.channel_name), bench, ours, margin, comp, matchedCell(r), el("td", won(r.stock_qty) + (r.remaining_days != null ? " (D-" + r.remaining_days + ")" : ""), "num"), starCell);
    body.append(tr);
  }
}

// A person's own price proposal, written from the comparison row. Below the defense line it needs an administrator's approval.
function toggleDraftRow(tr, r) {
  const next = tr.nextElementSibling;
  if (next && next.classList.contains("draft-row")) { next.remove(); return; }
  const row = el("tr", "", "draft-row"), td = el("td"); td.colSpan = 9;
  const price = el("input"); price.type = "number"; price.min = "1"; price.step = "1"; price.required = true; price.setAttribute("aria-label", t("draft.price")); price.placeholder = t("draft.price");
  const note = el("input"); note.type = "text"; note.maxLength = 500; note.setAttribute("aria-label", t("draft.note")); note.placeholder = t("draft.note");
  const send = el("button", t("draft.send")), cancel = el("button", t("draft.cancel"));
  cancel.onclick = () => row.remove();
  send.onclick = async () => {
    try {
      const res = await api("/recommendations/draft", { method: "POST", body: JSON.stringify({ internal_sku: r.internal_sku, channel_name: r.channel_name, price: Number(price.value), note: note.value }) });
      toast(t(res.requires_admin ? "draft.done_admin" : "draft.done", { id: res.rec_id })); row.remove(); await loadRecs();
    } catch (e) { toast(e.message); }
  };
  td.append(price, note, send, cancel); row.append(td); tr.after(row);
}

// What matching the lowest confirmed competitor would do: expected margin and the weekly volume that earns today's total margin.
function matchedCell(r) {
  const td = el("td", "", "num cost matched"); td.hidden = me.role === "BUYER";
  const s = r.internal.match_simulation;
  if (!s) { td.textContent = "–"; return td; }
  td.append(el("span", s.expected_margin_rate + "%", "val"));
  const note = s.bep_status === "OK" ? t("bep.ok", { n: Number(s.bep_units).toLocaleString(numLocale()) })
    : s.bep_status === "NOT_A_CUT" ? t("bep.no_cut") : s.bep_status === "UNACHIEVABLE" ? t("bep.unachievable") : t("bep.none");
  td.append(el("small", note + (s.below_floor ? " · " + t("below_floor") : "")));
  return td;
}

// Download a server-built file (the request carries the bearer token, so a plain link cannot be used).
async function downloadFile(path, filename) {
  const r = await fetch(path, { headers: token ? { Authorization: "Bearer " + token } : {} });
  if (!r.ok) { const body = await r.json().catch(() => ({})); throw new Error(body.detail || r.statusText); }
  const url = URL.createObjectURL(new Blob(["\ufeff" + await r.text()], { type: "text/csv" }));
  const a = el("a"); a.href = url; a.download = filename; document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
}
$("#export-matrix").addEventListener("click", () => downloadFile("/matrix/export" + ($("#only-watch").checked ? "?watchlist=true" : ""), "marketflow-comparison.csv").catch((e) => toast(e.message)));

// ---- data-quality strip: one chip per crawler, plus the counts that need a person ------------------------------------
async function loadQuality() {
  const q = await api("/quality"), strip = $("#qstrip"), chips = [];
  for (const c of q.crawlers.filter((x) => x.enabled && x.configured)) {
    const state = c.stale ? "stale" : c.never_run ? "never" : (c.success_rate !== null && c.success_rate < 0.95) ? "low" : "ok";
    const li = el("li", "", state === "ok" ? "" : state);
    li.append(el("span", crawlerTitle(c.id)), el("small", state === "never" ? t("q.never") : state === "ok" ? (c.last_success_hours === null ? t("q.ok") : t("q.fresh_age", { h: c.last_success_hours })) : t("q." + state)));
    chips.push(li);
  }
  for (const [n, key] of [[q.unconfirmed_matches, "q.unconfirmed"], [q.matches_to_recheck, "q.recheck"], [q.unparseable_titles, "q.unparsed"]]) {
    if (n) { const li = el("li", "", "low"); li.append(el("span", t(key, { n }))); chips.push(li); }
  }
  strip.replaceChildren(...chips); strip.hidden = !chips.length;
}

// ---- flyers: the image beside what the reader found; only approved items reach the analysis -----------------------------
let flyerId = null, flyerUrl = null;
async function loadFlyers() {
  const { flyers } = await api("/flyers");
  setBadge("flyers", flyers.reduce((n, f) => n + (f.pending ? 1 : 0), 0));
  const ul = $("#flyer-list");
  if (!flyers.length) { const li = el("li"); li.append(el("p", t("fl.none"), "empty")); ul.replaceChildren(li); return; }
  ul.replaceChildren(...flyers.map((f) => {
    const li = el("li"), b = el("button", "", f.flyer_id === flyerId ? "active" : "");
    b.append(el("b", f.source_name), el("small", f.week_start + " · " + (f.pending ? t("fl.pending", { n: f.pending }) : t("fl.done"))));
    b.onclick = () => openFlyer(f.flyer_id); li.append(b); return li;
  }));
  if (flyerId && flyers.some((f) => f.flyer_id === flyerId)) await openFlyer(flyerId, true);
}
async function openFlyer(id, keepImage) {
  flyerId = id;
  const f = await api("/flyers/" + id), page = $("#flyer-page");
  const img = el("img", "", "flyer-img"); img.alt = f.source_name;
  const side = el("div", "", "flyer-side"); side.append(img);
  if (keepImage && flyerUrl && page.dataset.flyer === String(id)) img.src = flyerUrl;
  else {
    if (flyerUrl) URL.revokeObjectURL(flyerUrl);
    flyerUrl = null; page.dataset.flyer = String(id);
    fetch("/flyers/" + id + "/image", { headers: { Authorization: "Bearer " + token } })
      .then((r) => { if (!r.ok) throw new Error(); return r.blob(); })
      .then((blob) => { flyerUrl = URL.createObjectURL(blob); img.src = flyerUrl; })
      .catch(() => side.append(el("p", t("fl.image_failed"), "hint")));
  }
  const table = el("table"), head = el("tr");
  for (const k of ["fl.name", "fl.price", "fl.conf", "th.status", ""]) head.append(el("th", k ? t(k) : ""));
  const thead = el("thead"); thead.append(head); table.append(thead);
  const body = el("tbody"); table.append(body);
  const decide = async (decisions) => { try { await api("/flyers/" + id + "/review", { method: "POST", body: JSON.stringify({ decisions }) }); toast(t("fl.saved")); await loadFlyers(); } catch (e) { toast(e.message); } };
  const rowDecision = (it, action, name, price) => ({ item_id: it.item_id, action, name: name.value, ...(price.value === "" ? {} : { price: Number(price.value) }) });
  const pendingItems = [];
  for (const it of f.items) {
    const tr = el("tr", "", it.low_confidence && it.status === "PENDING" ? "draft-row" : ""), name = el("input"), price = el("input");
    name.value = it.name; name.maxLength = 80; name.setAttribute("aria-label", t("fl.name"));
    price.type = "number"; price.min = 1; price.value = it.price ?? ""; price.setAttribute("aria-label", t("fl.price"));
    const conf = el("td", it.confidence === null ? "–" : Math.round(it.confidence * 100) + "%", "num");
    if (it.low_confidence && it.status === "PENDING") conf.append(el("small", t("fl.low")));
    const st = el("td"); st.append(el("span", t("fstate." + it.status), "status " + (it.status === "APPROVED" ? "green" : it.status === "REJECTED" ? "red" : "")));
    const ok = el("button", t("fl.approve"), "primary"), no = el("button", t("fl.reject")), act = el("td");
    ok.onclick = () => decide([rowDecision(it, "approve", name, price)]); no.onclick = () => decide([{ item_id: it.item_id, action: "reject" }]);
    act.append(ok, no);
    const nameTd = el("td"), priceTd = el("td"); nameTd.append(name); priceTd.append(price);
    tr.append(nameTd, priceTd, conf, st, act); body.append(tr);
    if (it.status === "PENDING") pendingItems.push([it, name, price]);
  }
  const all = el("button", t("fl.approve_all"), "primary");
  all.onclick = () => decide(pendingItems.map(([it, n, p]) => rowDecision(it, "approve", n, p))); all.hidden = !pendingItems.length;
  const main = el("div", "", "flyer-items"), wrap = el("div", "", "scroll"); wrap.append(table);
  main.append(el("h2", f.source_name), el("p", f.week_start, "hint"), all, wrap);
  const split = el("div", "", "flyer-split"); split.append(side, main);
  page.replaceChildren(split);
}

// ---- insights: promotion results, sales response, long discounts, new products -----------------------------------------
async function loadInsights() {
  const d = await api("/insights");
  const fill = (id, cols, rows, make) => { const body = $(id + " tbody"); body.replaceChildren(); if (!rows.length) emptyRow(body, cols, t("ins.empty")); rows.forEach((r) => body.append(make(r))); };
  const tr = (...cells) => { const row = el("tr"); cells.forEach((c) => row.append(c instanceof Node ? c : el("td", c))); return row; };
  const num = (v) => el("td", v, "num"), pctText = (v) => v === null || v === undefined ? "–" : (v > 0 ? "+" : "") + v + "%";
  fill("#ins-promos", 7, d.promotions, (p) => { const st = el("td"); st.append(el("span", t("istate." + p.status), "status " + (p.status === "OK" ? "green" : p.status === "FAILED" ? "red" : "")));
    return tr(p.name, p.category_l || t("promos.all"), el("td", p.start_date + " – " + p.end_date, "num"), num(p.status === "INSUFFICIENT_DATA" ? "–" : won(p.actual_units)), num(p.status === "INSUFFICIENT_DATA" ? "–" : won(p.baseline_units)), num(pctText(p.uplift_pct)), st); });
  const resp = Object.entries(d.response);
  fill("#ins-response", 6, resp, ([cat, r]) => r.status === "OK"
    ? tr(cat, num(r.promotions_used), el("td", r.span[0] + " – " + r.span[1], "num"), num(r.elasticity.pessimistic), num(r.elasticity.base), num(r.elasticity.optimistic))
    : tr(cat, num(r.promotions_used), el("td", t("ins.of", { n: r.promotions_used, need: r.needed }), "num"), num("–"), num("–"), num("–")));
  fill("#ins-prolonged", 4, d.prolonged_discounts, (x) => tr(x.platform, x.title, num(x.days_discounted + " / " + x.days_observed), num(x.share_pct + "%")));
  fill("#ins-new", 3, d.new_products, (x) => tr(x.platform, x.title, el("td", x.first_seen.slice(0, 10), "num")));
}

// ---- promotion calendar -------------------------------------------------------------------------------------------
const PROMO_BEFORE = 90, PROMO_AFTER = 30;
// ---- settings (ADMIN): users and data ----------------------------------------------------------------------------------
const parseCats = (s) => s.split(",").map((x) => x.trim()).filter(Boolean).map((x) => { const [l, m = ""] = x.split("/").map((y) => y.trim()); return { category_l: l, category_m: m }; });
let demoPoll = null, demoWatching = false;
async function loadSettings() {
  if (!isAdmin()) return;
  await Promise.all([loadUsers(), loadSystem()]);
}
async function loadUsers() {
  const { users } = await api("/admin/users");
  const body = $("#users-table tbody"); body.replaceChildren();
  for (const u of users) {
    const self = me && u.email === me.email, tr = el("tr");
    const role = el("select"); for (const r of ["ADMIN", "MD", "BUYER"]) { const o = el("option", r); o.selected = r === u.role; role.append(o); }
    const active = el("input"); active.type = "checkbox"; active.checked = !!u.active;
    role.disabled = active.disabled = self;                              // the server refuses self-demotion anyway; say so up front
    const patch = async (change) => { try { await api("/admin/users/" + u.user_id, { method: "PATCH", body: JSON.stringify(change) }); toast(t("set.saved")); } catch (e) { toast(e.message); await loadUsers(); } };
    role.onchange = () => patch({ role: role.value }); active.onchange = () => patch({ active: active.checked });
    const cats = el("input"); cats.type = "text"; cats.value = u.categories.join(", "); cats.maxLength = 200;
    const save = el("button", t("set.save"));
    save.onclick = async () => { try { await api("/admin/users/" + u.user_id + "/assignments", { method: "PUT", body: JSON.stringify(parseCats(cats.value)) }); toast(t("set.saved")); } catch (e) { toast(e.message); } };
    const who = el("td", u.email); if (self) who.append(" ", el("small", "(" + t("set.you") + ")"));
    const c1 = el("td"), c2 = el("td"), c3 = el("td"), c4 = el("td"); c1.append(role); c2.append(active); c3.append(cats); c4.append(save);
    tr.append(who, el("td", u.name), c1, c2, c3, c4); body.append(tr);
  }
}
$("#user-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = Object.fromEntries(new FormData(ev.target).entries());
  try {
    const { user_id } = await api("/admin/users", { method: "POST", body: JSON.stringify({ email: f.email, name: f.name, role: f.role, password: f.password }) });
    if (f.categories.trim()) await api("/admin/users/" + user_id + "/assignments", { method: "PUT", body: JSON.stringify(parseCats(f.categories)) });
    ev.target.reset(); toast(t("set.user_added")); await loadUsers();
  } catch (e) { toast(e.message); }
});
const COUNT_KEYS = ["ext_public_benchmarks", "ext_retail_prices", "int_operations_inventory", "price_recommendations", "promotions", "mi_observations", "product_channel_map", "users", "master_products"];
async function loadSystem() {
  const s = await api("/admin/system"), running = s.job.state === "RUNNING";
  $("#data-hint").textContent = t(s.local ? "set.hint.local" : "set.hint.remote", { store: s.store });
  const box = $("#data-status"); box.replaceChildren();
  if (s.counts) {
    const hasData = ["ext_public_benchmarks", "ext_retail_prices", "int_operations_inventory", "price_recommendations", "promotions", "mi_observations"].some((k) => s.counts[k] > 0);
    for (const k of COUNT_KEYS) { const c = el("span", t("cnt." + k), "chip"); c.append(el("b", String(s.counts[k]))); box.append(c); }
    if (!hasData) box.append(el("span", t("cnt.empty"), "chip"));
    $("#demo-load").disabled = !s.local || hasData; $("#demo-load").title = hasData ? t("set.demo.has_data") : "";
  } else { box.append(el("span", t("set.demo.running"), "chip")); $("#demo-load").disabled = true; }
  $("#data-clear").disabled = !s.local || running;
  clearTimeout(demoPoll);
  if (running) { demoWatching = true; demoPoll = setTimeout(() => loadSystem().catch(() => {}), 3000); return; }
  if (!demoWatching) return;                                           // report a finished job once, not on every reload
  demoWatching = false;
  if (s.job.state === "DONE") { toast(t("set.demo.done")); await reloadAll(); }
  else if (s.job.state === "FAILED") toast(t("set.demo.failed", { msg: s.job.message }));
}
$("#demo-load").addEventListener("click", async () => {
  try { await api("/admin/demo", { method: "POST" }); toast(""); await loadSystem(); } catch (e) { toast(e.message); }
});
$("#data-clear").addEventListener("click", () => { $("#clear-form").hidden = false; $("#clear-form").elements.confirm.focus(); });
$("#clear-cancel").addEventListener("click", () => { $("#clear-form").hidden = true; $("#clear-form").reset(); });
$("#clear-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("/admin/clear", { method: "POST", body: JSON.stringify({ confirm: new FormData(ev.target).get("confirm") }) });
    ev.target.reset(); ev.target.hidden = true; toast(t("set.cleared")); await reloadAll();
  } catch (e) { toast(e.message); }
});

// ---- discover: new competitor products and the most searched / fastest rising keywords (the first screen) ----------------
let discoverSummary = null;
function keywordRow(k, withGrowth) {
  const tr = el("tr"), name = el("td"), cat = k.in_catalog ? el("td", "✓ " + t("disc.in"), "green") : el("td"), searches = trendCell(compact(k.searches), k.series, "c-margin", k.keyword + " " + t("searches"), (v) => compact(v), t("vs_earlier", { pct: pctText(k.growth) }));
  name.append(el("span", k.keyword, "item"), el("small", k.category || t("no_category")));
  if (!k.in_catalog) cat.append(el("span", t("disc.missing"), "pill amber"));
  const sub = [k.wow != null ? t("wow", { pct: pctText(k.wow) }) : "", k.yoy != null ? t("yoy", { pct: pctText(k.yoy) }) : ""].filter(Boolean).join(" · ");
  if (sub) searches.append(el("small", sub, "pop"));
  tr.append(name, el("td", t("stage." + k.stage), "stage"), searches, cat, el("td", k.action || "–", "note"));
  return tr;
}
function renderDiscover(d) {
  const fill = (id, rows, cols, empty, make) => { const body = $(id + " tbody"); body.replaceChildren(); if (!rows.length) emptyRow(body, cols, empty); rows.forEach((r) => body.append(make(r))); };
  fill("#disc-new", d.new_products, 5, t("disc.empty_new", { days: d.window_days }), (p) => {
    const tr = el("tr"), name = el("td"); name.append(el("span", p.title, "item"), el("small", [p.item_name, p.category].filter(Boolean).join(" · ")));
    tr.append(el("td", p.platform), name, el("td", p.price === null ? "–" : won(p.price), "num"), el("td", p.first_seen.slice(0, 10), "num"),
              p.we_sell_item ? el("td", "✓ " + t("disc.yes"), "green") : el("td", t("disc.no"), "amber"));
    return tr;
  });
  fill("#disc-top", d.top_searches, 5, t("empty.radar"), (k) => keywordRow(k));
  fill("#disc-rising", d.rising, 5, t("empty.radar"), (k) => keywordRow(k));
  fill("#disc-gaps", d.gaps, 5, t("empty.gap"), (g) => {
    const tr = el("tr"), name = el("td"), cause = el("td");
    name.append(el("span", g.keyword, "item"), el("small", g.category || t("no_category")));
    cause.append(el("span", t("cause." + g.primary_cause), "pill " + (g.primary_cause === "OTHER" ? "" : "amber")));
    for (const c of g.contributing_causes) cause.append(el("small", "+ " + t("cause." + c)));
    tr.append(name, el("td", compact(g.searches), "num"), el("td", (g.conversion * 100).toFixed(1) + "%", "num"), cause, el("td", g.action || "–", "note"));
    return tr;
  });
}
async function loadDiscover() {
  const d = await api("/discover?lang=" + lang);
  discoverSummary = d.summary; renderSummary(); renderDiscover(d);
  setBadge("discover", d.summary.new_products);
}

async function loadPromos() {
  const { today, promotions } = await api("/promotions");
  const t0 = Date.parse(today + "T00:00:00Z"), from = t0 - PROMO_BEFORE * DAY_MS, span = (PROMO_BEFORE + PROMO_AFTER) * DAY_MS;
  const pct = (iso) => Math.min(100, Math.max(0, (Date.parse(iso + "T00:00:00Z") - from) / span * 100));
  const body = $("#promos-table tbody"); body.replaceChildren();
  if (!promotions.length) emptyRow(body, 7, t("empty.promos"));
  for (const p of promotions) {
    const tr = el("tr"), lane = el("div", "", "promo-lane"), bar = el("i", "", p.state === "ENDED" ? "past" : ""), now = el("b");
    const l = pct(p.start_date), r = pct(p.end_date) + 100 / (PROMO_BEFORE + PROMO_AFTER);
    bar.style.left = l + "%"; bar.style.width = Math.max(1, Math.min(100, r) - l) + "%"; now.style.left = pct(today) + "%"; lane.append(bar, now);
    const del = el("button", t("promos.remove"));
    del.onclick = async () => { try { await api("/promotions/" + p.promo_id, { method: "DELETE" }); toast(""); await loadPromos(); } catch (e) { toast(e.message); } };
    const name = el("td"); name.append(el("span", p.name, "item")); if (p.discount_pct) name.append(el("small", Number(p.discount_pct) + "%"));
    const st = el("td"); st.append(el("span", t("pstate." + p.state), "status " + (p.state === "ACTIVE" ? "green" : p.state === "UPCOMING" ? "amber" : "")));
    const tl = el("td"); tl.append(lane); const act = el("td"); act.append(del);
    tr.append(name, el("td", p.category_l || t("promos.all")), el("td", p.channel_name || t("promos.all")), el("td", p.start_date + " – " + p.end_date, "num"), tl, st, act);
    body.append(tr);
  }
}
$("#promo-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target), body = Object.fromEntries([...f.entries()].filter(([, v]) => v !== ""));
  if (body.discount_pct !== undefined) body.discount_pct = Number(body.discount_pct);
  try { await api("/promotions", { method: "POST", body: JSON.stringify(body) }); toast(""); ev.target.reset(); await loadPromos(); } catch (e) { toast(e.message); }
});

async function loadMatches() {
  const { matches } = await api("/matches");
  setBadge("matches", matches.filter((m) => !m.confirmed && m.match_grade === "SAME" && !m.reviewed_at).length);
  const body = $("#matches-table tbody"); body.replaceChildren();
  if (!matches.length) emptyRow(body, 7, t("empty.matches"));
  for (const m of matches) {
    const grade = el("select"); grade.setAttribute("aria-label", t("th.grade"));
    for (const g of ["SAME", "SIMILAR", "SUBSTITUTE"]) { const o = el("option", t("grade." + g)); o.value = g; o.selected = g === m.match_grade; grade.append(o); }
    const act = el("td");
    const save = el("button", t(m.reviewed_at ? "act.regrade" : "act.confirm"));
    save.onclick = async () => { try { await api("/matches/" + encodeURIComponent(m.platform) + "/" + encodeURIComponent(m.channel_item_code), { method: "PUT", body: JSON.stringify({ std_item_id: m.std_item_id, grade: grade.value }) }); toast(""); await Promise.all([loadMatches(), loadMatrix()]); } catch (e) { toast(e.message); } };
    const del = el("button", t("act.remove"));
    del.onclick = async () => { try { await api("/matches/" + encodeURIComponent(m.platform) + "/" + encodeURIComponent(m.channel_item_code), { method: "DELETE" }); toast(""); await Promise.all([loadMatches(), loadMatrix()]); } catch (e) { toast(e.message); } };
    act.append(save, del);
    const gradeCell = el("td"); gradeCell.append(grade);
    const state = m.confirmed ? t("match.confirmed") : m.reviewed_at ? t("match.graded") : t("match.unconfirmed");
    const tr = el("tr");
    tr.append(el("td", m.platform), el("td", m.channel_item_code), el("td", m.product_title || ""), el("td", m.item_name), gradeCell, el("td", state, "status " + (m.confirmed ? "green" : m.reviewed_at ? "" : "amber")), act);
    body.append(tr);
  }
}

async function loadRecs() {
  const { recommendations } = await api("/recommendations");
  const body = $("#recs-table tbody"); body.replaceChildren();
  pendingCount = recommendations.filter((r) => r.status === "PENDING").length; setBadge("recs", pendingCount); renderSummary();
  if (!recommendations.length) { const td = el("td", t("empty.recs"), "empty"); td.colSpan = 9; const tr = el("tr"); tr.append(td); body.append(tr); }
  const canDecide = me.role !== "BUYER";
  for (const r of recommendations) {
    const tr = el("tr"), flags = el("td");
    for (const [k, v] of Object.entries(r.flags)) if (k === k.toUpperCase() && v) flags.append(el("span", "⚠ " + codeText(k), "pill " + (k.startsWith("REVERSE") ? "red" : "amber")));
    const act = el("td");
    const next = { PENDING: ["approve", "reject"], APPROVED: ["apply"], APPLIED: ["revert"] }[r.status] || [];
    if (canDecide) for (const a of next) { const b = el("button", t("act." + a)); b.onclick = async () => { try { await api("/recommendations/" + r.rec_id + "/" + a, { method: "POST" }); await Promise.all([loadRecs(), loadMatrix()]); } catch (e) { toast(e.message); } }; act.append(b); }
    tr.append(el("td", r.rec_id), el("td", r.item_name), el("td", r.channel_name), el("td", won(r.current_price), "num"), el("td", won(r.suggested_price), "num"), el("td", codeText(r.reason_code)), flags, el("td", codeText(r.status), "status " + ({ PENDING: "amber", APPLIED: "green" }[r.status] || "")), act);
    body.append(tr);
  }
}

async function post(path, reload) {
  try { await api(path, { method: "POST" }); toast(""); await reload(); } catch (e) { toast(e.message); }
}

async function loadReview() {
  const { items } = await api("/review");
  setBadge("review", items.length);
  const body = $("#review-table tbody"); body.replaceChildren();
  if (!items.length) { const td = el("td", t("empty.review"), "empty"); td.colSpan = 6; const tr = el("tr"); tr.append(td); body.append(tr); }
  for (const r of items) {
    const done = el("button", t("act.resolve")); done.onclick = () => post("/review/" + r.review_id + "/resolve", loadReview);
    const act = el("td"); act.append(done);
    const tr = el("tr"); tr.append(el("td", r.review_id), el("td", codeText(r.kind)), el("td", r.source), el("td", r.item_key), el("td", r.reason), act);
    body.append(tr);
  }
}

async function loadSelectors() {
  const { candidates } = await api("/selectors");
  setBadge("selectors", candidates.filter((c) => c.status === "PENDING").length);
  const body = $("#selectors-table tbody"); body.replaceChildren();
  if (!candidates.length) { const td = el("td", t("empty.selectors"), "empty"); td.colSpan = 7; const tr = el("tr"); tr.append(td); body.append(tr); }
  for (const c of candidates) {
    const act = el("td");
    if (c.status === "PENDING") for (const a of ["approve", "reject"]) { const b = el("button", t("act." + a)); b.onclick = () => post("/selectors/" + c.cand_id + "/" + a, loadSelectors); act.append(b); }
    const tr = el("tr");
    tr.append(el("td", c.cand_id), el("td", c.platform), el("td", c.status), el("td", c.selectors.item), el("td", c.selectors.fields.title.css), el("td", c.selectors.fields.sale_price.css), act);
    body.append(tr);
  }
}

// ---- market intelligence: category health + trend radar -------------------------------------------------------------
let miReport = null;
const pctText = (x) => x === null || x === undefined ? "–" : (x > 0 ? "+" : x < 0 ? "−" : "") + Math.abs(x * 100).toFixed(0) + "%";

// The three questions the framework asks of every signal, as one cell
function story(r) {
  const td = el("td", "", "story");
  for (const [label, text] of [[t("story.changed"), r.changed], [t("story.why"), r.why], [t("story.action"), r.action]]) {
    const p = el("p"); p.append(el("b", label + ": "), document.createTextNode(text)); td.append(p);
  }
  return td;
}
const emptyRow = (body, cols, text) => { const td = el("td", text, "empty"); td.colSpan = cols; const tr = el("tr"); tr.append(td); body.append(tr); };
const compact = (v) => Number(v).toLocaleString(numLocale(), { notation: "compact", maximumFractionDigits: 1 });

function renderHealth(rows) {
  const body = $("#health-table tbody"); body.replaceChildren();
  if (!rows.length) return emptyRow(body, 6, t("empty.health"));
  for (const h of rows) {
    const tr = el("tr"), name = el("td"), last = (s) => s.length ? s[s.length - 1][1] : null;
    name.append(el("span", h.category, "item"), el("small", t("demand." + h.demand_state)));
    const unit = t(["gmv", "unitsold"].includes(h.demand_metric) ? "metric." + h.demand_metric : "metric.default");
    tr.append(name,
      h.demand_series.length ? trendCell(compact(last(h.demand_series)), h.demand_series, "c-margin", h.category + " " + unit, (v) => compact(v), t("vs_earlier", { pct: pctText(h.demand_change) })) : el("td", "–", "num"),
      h.interest_series.length ? trendCell(compact(last(h.interest_series)), h.interest_series, "c-margin", h.category + " " + t("searches"), (v) => compact(v), t("vs_earlier", { pct: pctText(h.interest_change) })) : el("td", "–", "num"));
    tr.append(el("td", h.share_pct === null ? "–" : h.share_pct + "%" + (h.share_shift_pp === null ? "" : t("share_shift", { pp: (h.share_shift_pp > 0 ? "+" : h.share_shift_pp < 0 ? "−" : "") + Math.abs(h.share_shift_pp) })), "num"));
    const sup = h.supply;
    tr.append(el("td", !sup ? "–" : sup.cover_days === null ? t("no_sales") : t("cover", { days: sup.cover_days, status: t("supply." + sup.status) }), "num"), story(h));
    body.append(tr);
  }
}

function renderRadar(rows) {
  const body = $("#radar-table tbody"); body.replaceChildren();
  if (!rows.length) return emptyRow(body, 6, t("empty.radar"));
  for (const r of rows) {
    const tr = el("tr"), name = el("td"), sig = Object.entries(r.signals).filter(([, v]) => v > 0).map(([k, v]) => t("signal." + k) + " " + v);
    name.append(el("span", r.keyword, "item"), el("small", r.category || t("no_category")));
    const last = r.search_series[r.search_series.length - 1];
    const searches = trendCell(compact(last[1]), r.search_series, "c-margin", r.keyword + " " + t("searches"), (v) => compact(v), t("vs_earlier", { pct: pctText(r.growth) }));
    if (r.wow != null || r.yoy != null) searches.append(el("small", [r.wow != null ? t("wow", { pct: pctText(r.wow) }) : "", r.yoy != null ? t("yoy", { pct: pctText(r.yoy) }) : ""].filter(Boolean).join(" · "), "pop"));
    tr.append(name, el("td", t("stage." + r.stage), "stage"), searches,
      el("td", sig.length ? sig.join(" · ") : t("none_yet")), el("td", r.stage === "FADING" || r.stage === "STABLE" ? "–" : r.score, "num"), story(r));
    body.append(tr);
  }
}

function renderGap(rows) {
  const body = $("#gap-table tbody"); body.replaceChildren();
  if (!rows.length) return emptyRow(body, 5, t("empty.gap"));
  for (const g of rows) {
    const name = el("td"); name.append(el("span", g.keyword, "item"), el("small", g.category || t("no_category")));
    const cause = el("td"); cause.append(el("span", t("cause." + g.primary_cause), "pill " + (g.primary_cause === "OTHER" ? "" : "amber")));
    for (const c of g.contributing_causes) cause.append(el("small", "+ " + t("cause." + c)));
    const tr_ = el("tr"); tr_.append(name, el("td", compact(g.searches), "num"), el("td", (g.conversion * 100).toFixed(1) + "%", "num"), cause, story(g));
    body.append(tr_);
  }
}

async function loadMi() {
  miReport = await api("/mi/report?lang=" + lang + ($("#radar-stable").checked ? "&include_stable=true" : ""));
  renderHealth(miReport.health); renderRadar(miReport.radar); renderGap(miReport.gap || []);
  setBadge("radar", miReport.radar.filter((r) => ["SEED", "EMERGING"].includes(r.stage)).length);
}

// Applied price changes as CSV for a manual channel bulk upload (the system never writes to a channel itself).
const csvCell = (v) => { let s = String(v ?? ""); if (/^[=+\-@\t\r]/.test(s)) s = "'" + s; return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };   // also defuses spreadsheet formulas
function appliedCsv(recs) {
  const rows = recs.filter((r) => r.status === "APPLIED").map((r) => [r.flags?.metrics?.internal_sku, r.channel_name, r.suggested_price, r.current_price, r.rec_id, r.decided_at, r.decided_by]);
  return ["internal_sku,channel_name,new_price,previous_price,rec_id,applied_at,applied_by", ...rows.map((c) => c.map(csvCell).join(","))].join("\n") + "\n";
}
$("#export-applied").addEventListener("click", async () => {
  try {
    const { recommendations } = await api("/recommendations?status=APPLIED");
    const url = URL.createObjectURL(new Blob(["\ufeff" + appliedCsv(recommendations)], { type: "text/csv" }));
    const a = el("a"); a.href = url; a.download = "price-changes.csv"; document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  } catch (e) { toast(e.message); }
});


// ---- crawlers: one management page per crawler (#/crawlers/<id>) ----------------------------------------------------------
let crawlerId = null, crawlerList = [];
const isAdmin = () => me && me.role === "ADMIN";
const when = (iso) => iso ? new Date(iso).toLocaleString(numLocale(), { timeZone: "Asia/Seoul", dateStyle: "short", timeStyle: "short" }) + " KST" : t("c.never");
const crawlerTitle = (id) => t("crawler." + id + ".title");

function showTab(tab) {
  document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("active", x.dataset.tab === tab));
  for (const id of TABS) $("#" + id).hidden = tab !== id;
}
function gotoCrawler(id) { location.hash = "#/crawlers/" + id; }
window.addEventListener("hashchange", () => { if (token && me.role !== "BUYER") routeCrawler(); });

async function routeCrawler() {
  const m = /^#\/crawlers\/([a-z_]+)$/.exec(location.hash);
  if (!m) return;
  showTab("crawlers");
  await openCrawler(m[1]);
}

async function loadCrawlers() {
  ({ crawlers: crawlerList } = await api("/crawlers"));
  setBadge("crawlers", crawlerList.filter((c) => c.state === "FAILING").length);
  renderCrawlerList();
  const m = /^#\/crawlers\/([a-z_]+)$/.exec(location.hash);
  const id = (m && crawlerList.some((c) => c.id === m[1]) ? m[1] : null) || crawlerId || crawlerList[0]?.id;
  if (id) await openCrawler(id, true);
}

function renderCrawlerList() {
  $("#crawler-list").replaceChildren(...crawlerList.map((c) => {
    const li = el("li"), b = el("button", "", c.id === crawlerId ? "active" : "");
    b.append(el("b", crawlerTitle(c.id)), el("small", t("cstate." + c.state)));
    b.onclick = () => gotoCrawler(c.id); li.append(b); return li;
  }));
}

async function openCrawler(id, quiet) {
  crawlerId = id;
  renderCrawlerList();
  try { renderCrawlerPage(await api("/crawlers/" + id)); } catch (e) { toast(e.message); }
}

function fact(dl, label, value) { dl.append(el("dt", label), el("dd", value)); }

function renderCrawlerPage(d) {
  const page = $("#crawler-page"), admin = isAdmin();
  const h = el("h2", crawlerTitle(d.id)), desc = el("p", t("crawler." + d.id + ".desc"), "hint");
  const dl = el("dl", "", "facts");
  fact(dl, t("c.status"), t("cstate." + d.state));
  if (d.state === "NOT_CONFIGURED" && d.not_configured) fact(dl, "", t("crawler.setup." + d.id));
  fact(dl, t("c.last_run"), d.last_run ? when(d.last_run.finished_at || d.last_run.queued_at) + " · " + codeText(d.last_run.status) : t("c.never"));
  fact(dl, t("c.last_ok"), when(d.last_success));
  fact(dl, t("c.data_as_of"), d.data_as_of || t("c.never"));
  if (d.inbox) { fact(dl, t("c.inbox"), d.inbox.path); fact(dl, t("c.pending"), d.inbox.pending.length ? d.inbox.pending.join(", ") : t("c.none")); }
  if (d.platforms) fact(dl, t("c.platforms"), Object.entries(d.platforms).map(([p, v]) => p + " (" + t("c.urls", { n: v.urls }) + (v.legal_review ? ", " + t("c.legal") + " ✓" : "") + ")").join(" · ") || t("c.none"));
  page.replaceChildren(h, desc, dl);

  const actions = el("div", "", "row");
  const enabled = el("input"); enabled.type = "checkbox"; enabled.checked = d.enabled; enabled.disabled = !admin;
  const lab = el("label", "", "inline"); lab.append(enabled, document.createTextNode(" " + t("c.enabled")));
  enabled.addEventListener("change", () => saveCrawler(d.id, { enabled: enabled.checked }));
  actions.append(lab);
  if (admin) {
    const run = el("button", t("c.run_now"));
    run.onclick = async () => { try { await api("/crawlers/" + d.id + "/run", { method: "POST" }); toast(""); await openCrawler(d.id); await loadCrawlers(); toast(t("c.queued")); } catch (e) { toast(e.message); } };
    actions.append(run);
    if (d.inbox) {
      const file = el("input"); file.type = "file"; file.accept = ".csv,.xlsx"; file.setAttribute("aria-label", t("c.upload"));
      const up = el("button", t("c.upload"));
      up.onclick = async () => {
        const f = file.files[0]; if (!f) return;
        try {
          const res = await api("/crawlers/" + d.id + "/upload?filename=" + encodeURIComponent(f.name), { method: "POST", raw: true, body: await f.arrayBuffer() });
          toast(t("c.uploaded", { message: res.message })); await loadCrawlers();
        } catch (e) { toast(e.message); }
      };
      actions.append(file, up);
    }
  } else page.append(el("p", t("c.read_only"), "note"));
  page.append(actions);
  if (d.inbox && admin) page.append(el("p", t("c.upload_hint"), "note"));

  // settings
  page.append(el("h3", t("c.settings")));
  const form = el("form"), times = el("input"); times.type = "text"; times.value = d.times.join(", "); times.disabled = !admin; times.setAttribute("aria-label", t("c.schedule"));
  const tl = el("label", t("c.schedule")); tl.append(times); form.append(tl);
  const inputs = {};
  for (const f of d.fields) {
    const l = el("label", t("field." + f.key)); let inp;
    if (f.kind === "lines") { inp = el("textarea"); inp.value = (d.config[f.key] || []).join("\n"); }
    else { inp = el("input"); inp.type = "number"; inp.min = 1; inp.max = f.max; inp.value = d.config[f.key]; }
    inp.disabled = !admin; inputs[f.key] = [f, inp]; l.append(inp); form.append(l);
  }
  if (admin) { const save = el("button", t("c.save")); save.type = "submit"; form.append(save); }
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const config = {};
    for (const [k, [f, inp]] of Object.entries(inputs)) config[k] = f.kind === "lines" ? inp.value.split("\n").map((x) => x.trim()).filter(Boolean) : Number(inp.value);
    saveCrawler(d.id, { times: times.value.split(",").map((x) => x.trim()).filter(Boolean), ...(d.fields.length ? { config } : {}) });
  });
  page.append(form);

  // history
  page.append(el("h3", t("c.history")));
  if (!d.runs.length) { page.append(el("p", t("c.no_history"), "note")); return; }
  const table = el("table"), head = el("tr");
  for (const k of ["when", "trigger", "status", "rows", "message"]) head.append(el("th", t("c.h." + k), k === "rows" ? "r" : ""));
  const thead = el("thead"); thead.append(head); const tbody = el("tbody");
  for (const r of d.runs) {
    const tr = el("tr");
    tr.append(el("td", when(r.finished_at || r.started_at || r.queued_at)), el("td", t("trigger." + r.trigger_kind)),
      el("td", codeText(r.status), "status " + ({ OK: "green", FAILED: "red", SKIPPED: "amber" }[r.status] || "")), el("td", r.stored, "num"), el("td", r.message));
    tbody.append(tr);
  }
  table.append(thead, tbody); const wrap = el("div", "", "scroll"); wrap.append(table); page.append(wrap);
}

async function saveCrawler(id, patch) {
  try { await api("/crawlers/" + id, { method: "PUT", body: JSON.stringify(patch) }); toast(t("c.saved")); await loadCrawlers(); }
  catch (e) { toast(e.message); await openCrawler(id); }
}
