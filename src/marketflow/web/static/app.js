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

function toast(msg) { $("#toast").textContent = msg || ""; }

function signOut() { token = me = null; $("#app").hidden = true; $("#login").hidden = false; $("#who").textContent = ""; }

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
  const body = $("#matrix-table tbody"); body.replaceChildren();
  for (const r of rows) {
    const tr = el("tr");
    const name = el("td", r.item_name); name.append(el("br"), el("small", r.category_l + " / " + r.category_m + " · " + r.internal_sku));
    const bench = r.public_benchmark ? won(r.public_benchmark.normalized_price) : "–";
    const margin = el("td", "", "num cost"); margin.hidden = me.role === "BUYER";
    if (r.internal.margin_rate !== undefined) { margin.textContent = r.internal.margin_rate + "%"; margin.classList.add(Number(r.internal.margin_rate) <= 10 ? "red" : "green"); }
    const comp = r.competitors.lowest_pack_price ? won(r.competitors.lowest_pack_price) : "–";
    const compCell = el("td", comp, "num"); if (r.competitors.out_of_stock) compCell.append(el("br"), el("small", r.competitors.out_of_stock + "/" + r.competitors.listings + " out of stock"));
    const star = el("button", watch.has(r.std_item_id) ? "★" : "☆");
    star.onclick = async () => {
      const had = watch.has(r.std_item_id);
      had ? watch.delete(r.std_item_id) : watch.add(r.std_item_id);
      try { await api("/watchlist", { method: "PUT", body: JSON.stringify({ std_item_ids: [...watch] }) }); toast(""); }
      catch (e) { had ? watch.add(r.std_item_id) : watch.delete(r.std_item_id); toast(e.message); return; }   // roll back local state
      loadMatrix();
    };
    const starCell = el("td"); starCell.append(star);
    tr.append(name, el("td", r.channel_name), el("td", bench, "num"), el("td", won(r.internal.current_selling_price), "num"), margin, compCell, el("td", won(r.stock_qty) + (r.remaining_days != null ? " (D-" + r.remaining_days + ")" : ""), "num"), starCell);
    body.append(tr);
  }
}

async function loadRecs() {
  const { recommendations } = await api("/recommendations");
  const body = $("#recs-table tbody"); body.replaceChildren();
  const canDecide = me.role !== "BUYER";
  for (const r of recommendations) {
    const tr = el("tr"), flags = el("td");
    for (const [k, v] of Object.entries(r.flags)) if (k === k.toUpperCase() && v) flags.append(el("span", k.replace(/_/g, " "), "pill " + (k.startsWith("REVERSE") ? "red" : "amber")));
    const act = el("td");
    const next = { PENDING: ["approve", "reject"], APPROVED: ["apply"], APPLIED: ["revert"] }[r.status] || [];
    if (canDecide) for (const a of next) { const b = el("button", a); b.onclick = async () => { try { await api("/recommendations/" + r.rec_id + "/" + a, { method: "POST" }); await Promise.all([loadRecs(), loadMatrix()]); } catch (e) { toast(e.message); } }; act.append(b); }
    tr.append(el("td", r.rec_id), el("td", r.item_name), el("td", r.channel_name), el("td", won(r.current_price), "num"), el("td", won(r.suggested_price), "num"), el("td", r.reason_code), flags, el("td", r.status), act);
    body.append(tr);
  }
}

async function post(path, reload) {
  try { await api(path, { method: "POST" }); toast(""); await reload(); } catch (e) { toast(e.message); }
}

async function loadReview() {
  const { items } = await api("/review");
  const body = $("#review-table tbody"); body.replaceChildren();
  for (const r of items) {
    const done = el("button", "resolve"); done.onclick = () => post("/review/" + r.review_id + "/resolve", loadReview);
    const act = el("td"); act.append(done);
    const tr = el("tr"); tr.append(el("td", r.review_id), el("td", r.kind), el("td", r.source), el("td", r.item_key), el("td", r.reason), act);
    body.append(tr);
  }
}

async function loadSelectors() {
  const { candidates } = await api("/selectors");
  const body = $("#selectors-table tbody"); body.replaceChildren();
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
