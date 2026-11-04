// The access token lives only in memory (not localStorage): a reload signs you out, and XSS cannot persist it.
let token = null, me = null, watch = new Set();
const $ = (s) => document.querySelector(s);
const el = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; };
const won = (v) => (v === null || v === undefined) ? "–" : Number(v).toLocaleString("ko-KR");

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...(token ? { Authorization: "Bearer " + token } : {}) } });
  if (r.status === 401 && token) { signOut(); throw new Error("Session expired"); }
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
  return arrow + " " + Math.abs(d).toFixed(1) + (points_unit ? " pt" : "%") + " over " + days + "d";
}

function sparkline(series, cls, name, fmt) {
  const pts = parsePoints(series);
  if (pts.length < 2) return el("span", pts.length ? "1 day of data" : "No history", "nohist");
  const t0 = pts[0].t, span = pts[pts.length - 1].t - t0 || 1;
  const lo = Math.min(...pts.map((p) => p.v)), hi = Math.max(...pts.map((p) => p.v)), range = hi - lo;
  const x = (p) => PAD_X + (p.t - t0) / span * (W - 2 * PAD_X);                       // calendar-scaled: gaps stay visible
  const y = (p) => range === 0 ? H / 2 : PAD_Y + (hi - p.v) / range * (H - 2 * PAD_Y);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "spark " + cls, role: "img",
    "aria-label": name + ", last " + pts.length + " days: " + pts.map((p) => p.d + " " + fmt(p.v)).join(", ") });
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
    const t = tip(); t.replaceChildren(el("b", fmt(best.v)), document.createTextNode(name + " · " + best.d)); t.hidden = false;
    t.style.left = Math.min(ev.clientX + 14, window.innerWidth - 190) + "px"; t.style.top = Math.max(ev.clientY - 52, 4) + "px";
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
  const priced = lastRows.filter((r) => r.competitors.lowest_pack_price);
  const above = priced.filter((r) => Number(r.internal.current_selling_price) > Number(r.competitors.lowest_pack_price)).length;
  const stockouts = lastRows.reduce((n, r) => n + (r.competitors.out_of_stock || 0), 0);
  const figs = [[lastRows.length, "items tracked", false], [above, "priced above the lowest competitor", above > 0],
                [stockouts, "competitor listings out of stock", false]];
  if (pendingCount !== null) figs.push([pendingCount, "recommendations awaiting a decision", pendingCount > 0]);
  $("#summary").replaceChildren(...figs.map(([n, label, alert]) => { const f = el("div", "", alert ? "fig alert" : "fig"); f.append(el("b", won(n)), el("span", label)); return f; }));
}
function setBadge(tab, n) {
  const b = document.querySelector(`nav button[data-tab="${tab}"]`); let badge = b.querySelector(".badge");
  if (!n) { badge?.remove(); return; }
  if (!badge) { badge = el("span", "", "badge"); b.append(badge); }
  badge.textContent = n;
}

const sentence = (code) => { const t = String(code).replace(/_/g, " ").toLowerCase(); return t.charAt(0).toUpperCase() + t.slice(1); };

function toast(msg) { $("#toast").textContent = msg || ""; }

function signOut() { token = me = null; lastRows = []; pendingCount = null; tip().hidden = true; $("#app").hidden = true; $("#login").hidden = false; $("#who").textContent = ""; }

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  try {
    const res = await api("/auth/login", { method: "POST", body: JSON.stringify({ email: f.get("email"), password: f.get("password") }) });
    token = res.access_token; me = res.user; ev.target.reset(); $("#login-error").textContent = "";
    $("#login").hidden = true; $("#app").hidden = false;
    const out = el("button", "Sign out"); out.onclick = signOut;
    $("#who").replaceChildren(document.createTextNode(me.name + " · " + me.role + "  "), out);
    document.querySelectorAll(".cost, .decider").forEach((c) => c.hidden = me.role === "BUYER");
    toast("");
    watch = new Set((await api("/watchlist")).std_item_ids);
    await Promise.all([loadMatrix(), loadRecs(), ...(me.role === "BUYER" ? [] : [loadReview(), loadSelectors()])]);
  } catch (e) { $("#login-error").textContent = e.message; }
});

document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("active", x === b));
  for (const id of ["matrix", "recs", "review", "selectors"]) $("#" + id).hidden = b.dataset.tab !== id;
}));
$("#only-watch").addEventListener("change", loadMatrix);

async function loadMatrix() {
  const { rows } = await api("/matrix" + ($("#only-watch").checked ? "?watchlist=true" : ""));
  lastRows = rows; renderSummary();
  const body = $("#matrix-table tbody"); body.replaceChildren();
  if (!rows.length) {
    const td = el("td", $("#only-watch").checked ? "Nothing on your watchlist yet. Star an item to follow it here." : "No items yet. Import inventory with the import-erp command to see them here.", "empty");
    td.colSpan = 8; const tr = el("tr"); tr.append(td); body.append(tr); return;
  }
  const decimals = (v) => v.toLocaleString("ko-KR", { maximumFractionDigits: 2 });
  for (const r of rows) {
    const tr = el("tr"), t = r.trend || {};
    const name = el("td"); name.append(el("span", r.item_name, "item"), el("small", r.category_l + " / " + r.category_m + " · " + r.internal_sku));
    let margin = el("td", "", "num cost margin"); margin.hidden = me.role === "BUYER";
    if (r.internal.margin_rate !== undefined) {
      const low = Number(r.internal.margin_rate) <= 10;
      margin = trendCell((low ? "⚠ " : "✓ ") + r.internal.margin_rate + "%", r.internal.margin_trend, "c-margin", "Margin %", (v) => v.toFixed(2) + "%", "", "num cost margin " + (low ? "red" : "green"), true);
    }
    const bench = trendCell(r.public_benchmark ? won(r.public_benchmark.normalized_price) : "–", t.wholesale, "c-wholesale", "Wholesale", decimals);
    const ours = trendCell(won(r.internal.current_selling_price), t.ours, "c-ours", "Our price", won);
    const oos = r.competitors.out_of_stock ? r.competitors.out_of_stock + "/" + r.competitors.listings + " out of stock" : "";
    const comp = trendCell(r.competitors.lowest_pack_price ? won(r.competitors.lowest_pack_price) : "–", t.competitor, "c-comp", "Lowest competitor", decimals, oos);
    const star = el("button", watch.has(r.std_item_id) ? "★" : "☆", "star" + (watch.has(r.std_item_id) ? " on" : ""));
    star.setAttribute("aria-label", (watch.has(r.std_item_id) ? "Remove " : "Add ") + r.item_name + (watch.has(r.std_item_id) ? " from" : " to") + " watchlist");
    star.onclick = async () => {
      const had = watch.has(r.std_item_id);
      had ? watch.delete(r.std_item_id) : watch.add(r.std_item_id);
      try { await api("/watchlist", { method: "PUT", body: JSON.stringify({ std_item_ids: [...watch] }) }); toast(""); }
      catch (e) { had ? watch.add(r.std_item_id) : watch.delete(r.std_item_id); toast(e.message); return; }   // roll back local state
      loadMatrix();
    };
    const starCell = el("td"); starCell.append(star);
    tr.append(name, el("td", r.channel_name), bench, ours, margin, comp, el("td", won(r.stock_qty) + (r.remaining_days != null ? " (D-" + r.remaining_days + ")" : ""), "num"), starCell);
    body.append(tr);
  }
}

async function loadRecs() {
  const { recommendations } = await api("/recommendations");
  const body = $("#recs-table tbody"); body.replaceChildren();
  pendingCount = recommendations.filter((r) => r.status === "PENDING").length; setBadge("recs", pendingCount); renderSummary();
  if (!recommendations.length) { const td = el("td", "No recommendations yet. They appear after the next scheduled decision run.", "empty"); td.colSpan = 9; const tr = el("tr"); tr.append(td); body.append(tr); }
  const canDecide = me.role !== "BUYER";
  for (const r of recommendations) {
    const tr = el("tr"), flags = el("td");
    for (const [k, v] of Object.entries(r.flags)) if (k === k.toUpperCase() && v) flags.append(el("span", "⚠ " + sentence(k), "pill " + (k.startsWith("REVERSE") ? "red" : "amber")));
    const act = el("td");
    const next = { PENDING: ["approve", "reject"], APPROVED: ["apply"], APPLIED: ["revert"] }[r.status] || [];
    if (canDecide) for (const a of next) { const b = el("button", a); b.onclick = async () => { try { await api("/recommendations/" + r.rec_id + "/" + a, { method: "POST" }); await Promise.all([loadRecs(), loadMatrix()]); } catch (e) { toast(e.message); } }; act.append(b); }
    tr.append(el("td", r.rec_id), el("td", r.item_name), el("td", r.channel_name), el("td", won(r.current_price), "num"), el("td", won(r.suggested_price), "num"), el("td", sentence(r.reason_code)), flags, el("td", sentence(r.status), "status " + ({ PENDING: "amber", APPLIED: "green" }[r.status] || "")), act);
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
  if (!items.length) { const td = el("td", "Nothing to review. Items the system cannot match or parse will show up here.", "empty"); td.colSpan = 6; const tr = el("tr"); tr.append(td); body.append(tr); }
  for (const r of items) {
    const done = el("button", "resolve"); done.onclick = () => post("/review/" + r.review_id + "/resolve", loadReview);
    const act = el("td"); act.append(done);
    const tr = el("tr"); tr.append(el("td", r.review_id), el("td", sentence(r.kind)), el("td", r.source), el("td", r.item_key), el("td", r.reason), act);
    body.append(tr);
  }
}

async function loadSelectors() {
  const { candidates } = await api("/selectors");
  setBadge("selectors", candidates.filter((c) => c.status === "PENDING").length);
  const body = $("#selectors-table tbody"); body.replaceChildren();
  if (!candidates.length) { const td = el("td", "No selector proposals. When a retailer changes its page, the system proposes new selectors here.", "empty"); td.colSpan = 7; const tr = el("tr"); tr.append(td); body.append(tr); }
  for (const c of candidates) {
    const act = el("td");
    if (c.status === "PENDING") for (const a of ["approve", "reject"]) { const b = el("button", a); b.onclick = () => post("/selectors/" + c.cand_id + "/" + a, loadSelectors); act.append(b); }
    const tr = el("tr");
    tr.append(el("td", c.cand_id), el("td", c.platform), el("td", c.status), el("td", c.selectors.item), el("td", c.selectors.fields.title.css), el("td", c.selectors.fields.sale_price.css), act);
    body.append(tr);
  }
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
