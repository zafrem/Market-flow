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
    document.querySelectorAll(".cost").forEach((c) => c.hidden = me.role === "BUYER");
    watch = new Set((await api("/watchlist")).std_item_ids);
    await Promise.all([loadMatrix(), loadRecs()]);
  } catch (e) { $("#login-error").textContent = e.message; }
});

document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("active", x === b));
  $("#matrix").hidden = b.dataset.tab !== "matrix"; $("#recs").hidden = b.dataset.tab !== "recs";
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
    star.onclick = async () => { watch.has(r.std_item_id) ? watch.delete(r.std_item_id) : watch.add(r.std_item_id); await api("/watchlist", { method: "PUT", body: JSON.stringify({ std_item_ids: [...watch] }) }); loadMatrix(); };
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
    if (canDecide) for (const a of next) { const b = el("button", a); b.onclick = async () => { try { await api("/recommendations/" + r.rec_id + "/" + a, { method: "POST" }); await Promise.all([loadRecs(), loadMatrix()]); } catch (e) { alert(e.message); } }; act.append(b); }
    tr.append(el("td", r.rec_id), el("td", r.item_name), el("td", r.channel_name), el("td", won(r.current_price), "num"), el("td", won(r.suggested_price), "num"), el("td", r.reason_code), flags, el("td", r.status), act);
    body.append(tr);
  }
}
