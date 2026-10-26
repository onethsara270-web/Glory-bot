/* BNY GLORY BOT — BNY SHOP front end (no framework, no external files). All data comes from /api/*. */
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const app = $("#app");
const esc = (x) => String(x ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const S = { me: null, offset: 0, timers: [], gen: 0 };   // gen changes on every navigation: old async work stops itself

/* ---------- api ---------- */
async function api(path, body, method) {
  const opt = { method: method || (body === undefined ? "GET" : "POST"), credentials: "same-origin", headers: { "X-Requested-With": "bny" } };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  let r, data = {};
  try { r = await fetch(path, opt); } catch { throw new Error("No connection. Check your internet and try again."); }
  try { data = await r.json(); } catch { /* empty */ }
  if (r.status === 401 && S.me) { S.me = null; go("#/login"); throw new Error("Please log in again."); }
  if (!r.ok) { const e = new Error(data.error || "Something went wrong."); e.data = data; throw e; }
  return data;
}
function toast(msg, kind) {
  const t = document.createElement("div"); t.className = "t " + (kind || ""); t.textContent = msg; $("#toast").appendChild(t);
  setTimeout(() => t.remove(), 4200);
}
const fail = (e) => toast(e.message || String(e), "bad");
const npr = (n) => "NPR " + Number(n).toLocaleString("en-US", { maximumFractionDigits: 2 });

/* ---------- time (Nepal, 12-hour) ---------- */
const dtf = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kathmandu", day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: true });
const fmtDt = (iso) => (iso ? dtf.format(new Date(iso)).replace(/\bam\b/, "AM").replace(/\bpm\b/, "PM") : "—");
const nowMs = () => Date.now() + S.offset;
function syncClock(serverIso) { if (serverIso) S.offset = new Date(serverIso).getTime() - Date.now(); }
const hm = (sec) => { sec = Math.max(0, Math.floor(sec)); return Math.floor(sec / 3600) + ":" + String(Math.floor((sec % 3600) / 60)).padStart(2, "0"); };
function clockHtml(startIso, hours) {
  if (!startIso) return "";
  const total = (hours || 8) * 3600, el = (nowMs() - new Date(startIso).getTime()) / 1000;
  if (el >= total) return `<div class="ok">✅ 8-hour run finished · worked ${hm(total)}</div>`;
  const pct = Math.min(100, (el / total) * 100);
  return `<div class="row" style="justify-content:space-between"><span>⏳ Remaining <b class="big" style="font-size:1.5rem">${hm(total - el)}</b></span>` +
    `<span>✅ Worked <b>${hm(el)}</b> <span class="mute small">(h:mm · bot runs ${hours || 8} hrs)</span></span></div>` +
    `<div class="bar" style="margin-top:10px"><i style="width:${pct}%"></i></div>`;
}
function every(ms, fn) { const g = S.gen; const id = setInterval(() => { if (g !== S.gen) clearInterval(id); else fn(); }, ms); S.timers.push(id); return id; }
function live(el, startIso, hours) {   // keep a clock element ticking every second
  const draw = () => { if (el.isConnected) el.innerHTML = clockHtml(startIso, hours); };
  draw(); every(1000, draw);
}

/* ---------- routing ---------- */
const NAV = [["#/shop", "🛒", "SHOP NOW"], ["#/stock", "🔍", "Stock"], ["#/guilds", "⚔️", "Guilds"], ["#/orders", "🧾", "Orders"], ["#/wallet", "💰", "Wallet"]];
const MORE = [["#/profile", "👤", "Profile"], ["#/rules", "📜", "Rules"], ["#/support", "🛟", "Support"]];
function go(h) { if (location.hash === h) route(); else location.hash = h; }
window.addEventListener("hashchange", route);

function chrome() {
  const cur = location.hash || "#/";
  const link = (h, i, t, cls) => `<a href="${h}" class="${cls || ""} ${cur.startsWith(h) ? "on" : ""}">${i ? `<span>${i}</span>` : ""}${t}</a>`;
  if (!S.me) { $("#nav").innerHTML = ""; $("#tabbar").innerHTML = ""; $("#hdr-right").innerHTML = ""; return; }
  const all = [...NAV, ...MORE, ...(S.me.user.admin ? [["#/admin", "🛠", "Admin"]] : [])];
  $("#nav").innerHTML = all.map(([h, i, t]) => `<a href="${h}" class="${cur.startsWith(h) ? "on" : ""}">${i} ${t}</a>`).join("");
  const moreOn = ["#/more", "#/profile", "#/rules", "#/support", "#/admin"].some((x) => cur.startsWith(x));
  const tab = [...NAV.slice(1, 3), NAV[0], ...NAV.slice(3)];
  $("#tabbar").innerHTML = tab.map(([h, i, t]) => link(h, i, t === "SHOP NOW" ? "SHOP" : t, h === "#/shop" ? "shop" : "")).join("") +
    `<a href="#/more" class="${moreOn ? "on" : ""}"><span>👤</span>More</a>`;
  $("#hdr-right").innerHTML = `<a class="pill g" href="#/wallet" title="Wallet">💰 ${esc(S.me.user.balance_fmt)}</a>`;
}

async function loadMe() {
  try { S.me = await api("/api/me"); } catch { S.me = null; }
  return S.me;
}
const PUBLIC = ["#/login", "#/stock", "#/rules", "#/support"];
async function route() {
  S.gen++; S.timers.forEach(clearInterval); S.timers = [];
  const g = S.gen;
  let h = location.hash || "#/";
  if (!S.me) await loadMe();
  if (g !== S.gen) return;
  if (!S.me && !PUBLIC.some((p) => h.startsWith(p))) h = "#/login";
  if (S.me && (h === "#/" || h === "#/login")) h = "#/shop";
  if (S.me && !S.me.user.country && h !== "#/profile") { toast("Please set your country first."); h = "#/profile"; }
  chrome(); window.scrollTo(0, 0);
  const [, path, a, b] = h.split("/");       // "#", "shop", a, b
  const V = { login: vLogin, shop: vShop, stock: vStock, guilds: vGuilds, guild: vGuild, orders: vOrders, order: vOrder, wallet: vWallet,
    topup: vTopup, more: vMore, profile: vProfile, rules: vRules, support: vSupport, admin: vAdmin }[path] || vShop;
  app.innerHTML = '<div class="spin"></div>';
  try { await V(a, b, g); } catch (e) { if (g === S.gen) app.innerHTML = `<div class="card"><div class="err">${esc(e.message)}</div><div class="actions"><button class="btn blue" data-act="reload">🔄 Try again</button></div></div>`; }
}

/* ---------- login ---------- */
function vLogin() {
  app.innerHTML = `<div class="hero"><div class="logo">BNY <b>GLORY</b> BOT</div><div class="tag" style="margin-top:12px">BNY SHOP · Free Fire Guild Glory Push</div>
  <p>Buy credit with Nepali QR, pick your region, enter your Guild ID and start the glory bot — all in a few taps.</p></div>
  <div class="card glow auth"><div class="seg" id="seg"><button data-t="login" class="on">Login</button><button data-t="reg">Create account</button><button data-t="tg">Telegram code</button></div><div id="form"></div></div>`;
  const form = $("#form");
  const draw = (t) => {
    $("#seg").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.t === t));
    if (t === "login") form.innerHTML = `<label>Username</label><input id="u" autocomplete="username" autocapitalize="none"><label>Password</label><input id="p" type="password" autocomplete="current-password">
      <div id="msg"></div><div class="actions"><button class="btn blue block" id="go">🔐 Login</button></div>`;
    if (t === "reg") form.innerHTML = `<label>Your name</label><input id="n" maxlength="40"><label>Username</label><input id="u" autocomplete="username" autocapitalize="none" placeholder="4–20 letters / numbers / _">
      <label>Password</label><input id="p" type="password" autocomplete="new-password" placeholder="8+ characters"><div id="msg"></div>
      <div class="actions"><button class="btn green block" id="go">✨ Create account</button></div>`;
    if (t === "tg") form.innerHTML = `<p class="mute small">Already use our Telegram bot? Send <b>/weblogin</b> to the bot, then type the 8-digit code here. You get the <b>same wallet and orders</b>.</p>
      <label>One-time code</label><input id="c" inputmode="numeric" maxlength="9" placeholder="1234-5678"><div id="msg"></div><div class="actions"><button class="btn blue block" id="go">🔑 Login with code</button></div>`;
    $("#go").onclick = async () => {
      const btn = $("#go"); btn.disabled = true; $("#msg").innerHTML = "";
      try {
        const body = t === "tg" ? { code: $("#c").value } : t === "login" ? { username: $("#u").value, password: $("#p").value }
          : { name: $("#n").value, username: $("#u").value, password: $("#p").value };
        const r = await api(t === "reg" ? "/api/register" : "/api/login", body);
        await loadMe(); S.me = S.me || null; toast("Welcome, " + r.user.name + "!", "good"); go(r.user.country ? "#/shop" : "#/profile");
      } catch (e) { $("#msg").innerHTML = `<div class="err">${esc(e.message)}</div>`; btn.disabled = false; }
    };
    form.querySelectorAll("input").forEach((i) => i.addEventListener("keydown", (e) => { if (e.key === "Enter") $("#go").click(); }));
  };
  $("#seg").onclick = (e) => { if (e.target.dataset.t) draw(e.target.dataset.t); };
  draw("login");
}

/* ---------- SHOP NOW wizard ---------- */
const W = { step: 1, region: null, uid: "", squads: 0, quote: null };
const STEPS = ["Region", "Guild ID", "Check", "Squads", "Pay"];
function stepper(n) {
  let h = '<div class="steps">';
  STEPS.forEach((_, i) => { h += (i ? `<div class="ln ${i < n ? "done" : ""}"></div>` : "") + `<div class="s ${i + 1 < n ? "done" : i + 1 === n ? "now" : ""}">${i + 1 < n ? "✓" : i + 1}</div>`; });
  return h + `</div><div class="stepname">Step ${n} of 5 · ${STEPS[n - 1]}</div>`;
}
async function vShop(a, b, g) {
  if (W.step > 1 && !W.region) W.step = 1;
  const draw = { 1: shopRegion, 2: shopUid, 3: shopCheck, 4: shopSquads, 5: shopPay }[W.step];
  await draw(g);
}
function shopReset() { Object.assign(W, { step: 1, region: null, uid: "", squads: 0, quote: null }); }
function shopTo(n) { W.step = n; route(); }

async function shopRegion() {
  const st = await api("/api/stock");
  let notice = st.paused ? '<div class="warn">⏸ Orders are paused for a moment. Please try again soon.</div>' : "";
  if (st.closed) notice += '<div class="warn">🌙 Service is closed. It opens again Monday 12 AM (Nepal time).</div>';
  if (st.unavailable) notice += '<div class="warn">🟡 Stock can’t be loaded right now. Please refresh in a moment.</div>';
  const any = st.regions.some((r) => r.available);
  app.innerHTML = `<div class="card glow">${stepper(1)}<div class="title">🛒 SHOP NOW</div><p class="sub">1 squad = 1 glory push · ${npr(st.price)} per squad</p>${notice}
    <div class="grid g4">${st.regions.map((r) => `<button class="region ${r.available ? "" : "off"}" data-c="${esc(r.code)}" data-n="${esc(r.name)}" data-e="${esc(r.emoji)}" ${r.available ? "" : "disabled"}>
      <span class="f">${esc(r.emoji)}</span><span class="nm">${esc(r.name)} ${r.premium ? '<span class="chip prem">💎 PREMIUM</span>' : ""}</span>
      <span class="st">${r.available ? "✅ AVAILABLE" : "⛔ SOLD OUT"}</span></button>`).join("")}</div>
    ${any ? "" : '<div class="err">⛔ Everything is out of stock right now. Check back soon!</div>'}
    <div class="actions"><a class="btn blue" href="#/stock">🔍 Check SQD available</a></div></div>`;
  app.querySelectorAll(".region:not(.off)").forEach((b) => (b.onclick = () => { W.region = { code: b.dataset.c, name: b.dataset.n, emoji: b.dataset.e }; shopTo(2); }));
}
function regionLine() { return `${esc(W.region.emoji)} ${esc(W.region.name)}`; }
function shopUid() {
  app.innerHTML = `<div class="card glow">${stepper(2)}<div class="title">🛡 Guild ID</div><p class="sub">Region: ${regionLine()}</p>
    <label>Send your Guild ID (numbers only)</label><input id="uid" inputmode="numeric" maxlength="20" placeholder="e.g. 3012345678" value="${esc(W.uid)}"><div id="msg"></div>
    <div class="actions"><button class="btn green" id="next">Next ➜</button><button class="btn red" id="back">🌍 Change region</button></div></div>`;
  const next = () => { const v = $("#uid").value.replace(/\s/g, ""); if (!/^\d{4,20}$/.test(v)) { $("#msg").innerHTML = '<div class="err">Guild ID must be numbers only (4–20 digits).</div>'; return; } W.uid = v; shopTo(3); };
  $("#next").onclick = next; $("#back").onclick = () => shopTo(1); $("#uid").onkeydown = (e) => e.key === "Enter" && next(); $("#uid").focus();
}
function shopCheck() {
  app.innerHTML = `<div class="card glow">${stepper(3)}<div class="title">⚠️ Check before you order</div>
    <div class="sum"><div><span>🛡 Guild ID</span><b>${esc(W.uid)}</b></div><div><span>🌍 Region</span><b>${regionLine()}</b></div></div>
    <div class="warn"><b>Auto Approval must be OFF</b><br><b>All ranks must stay default</b><br><br>❌ Wrong ID or settings = no refund.</div>
    <div class="actions"><button class="btn green" id="ok">✅ I checked — continue</button><button class="btn red" id="cid">✏️ Change ID</button><button class="btn red" id="creg">🌍 Change region</button></div></div>`;
  $("#ok").onclick = () => shopTo(4); $("#cid").onclick = () => shopTo(2); $("#creg").onclick = () => shopTo(1);
}
async function shopSquads() {
  try { W.quote = await api("/api/quote", { region: W.region.code }); }
  catch (e) { app.innerHTML = `<div class="card"><div class="err">${esc(e.message)}</div><div class="actions"><button class="btn red" id="b">🌍 Change region</button></div></div>`; $("#b").onclick = () => shopTo(1); return; }
  const q = W.quote;
  app.innerHTML = `<div class="card glow">${stepper(4)}<div class="title">👥 How many squads?</div><p class="sub">${npr(q.price)} each · up to ${q.max} available now</p>
    <div class="grid g4">${q.options.map((o) => `<button class="sq ${W.squads === o.n ? "sel" : ""}" data-n="${o.n}"><b>${o.n}</b><span>${esc(o.total_fmt)}</span></button>`).join("")}</div>
    <div class="actions"><button class="btn red" id="back">⬅ Back</button></div></div>`;
  app.querySelectorAll(".sq").forEach((b) => (b.onclick = () => { W.squads = +b.dataset.n; shopTo(5); }));
  $("#back").onclick = () => shopTo(3);
}
async function shopPay(g) {
  if (!W.quote || !W.squads) return shopTo(4);
  const o = W.quote.options.find((x) => x.n === W.squads); if (!o) return shopTo(4);
  const bal = (await api("/api/wallet")).balance, can = bal >= o.total;
  app.innerHTML = `<div class="card glow">${stepper(5)}<div class="title">🏆 Your order</div>
    <div class="sum"><div><span>🌍 Region</span><b>${regionLine()}</b></div><div><span>🛡 Guild ID</span><b>${esc(W.uid)}</b></div><div><span>👥 Squads</span><b>${W.squads}</b></div>
    <div class="tot"><span>💳 Total</span><b>${esc(o.total_fmt)}</b></div></div>
    <p class="mute small">Pay and your bot request is sent automatically ✅ No waiting for approval.</p><div id="msg"></div>
    <div class="actions"><button class="btn green" id="qr">📲 Pay ${esc(o.total_fmt)} by QR</button>
    ${can ? `<button class="btn green" id="wal">💰 Pay from wallet (${npr(bal)})</button>` : `<a class="btn green" href="#/topup">➕ Add balance (wallet ${npr(bal)})</a>`}
    <button class="btn red" id="chg">✏️ Change squads</button><button class="btn red" id="cx">✖️ Cancel</button></div></div>`;
  $("#chg").onclick = () => shopTo(4); $("#cx").onclick = () => { shopReset(); go("#/shop"); route(); };
  $("#qr").onclick = async (e) => {
    e.target.disabled = true;
    try { const r = await api("/api/order/qr", { region: W.region.code, uid: W.uid, squads: W.squads }); payScreen(r.payment, { kind: "ORDER" }); }
    catch (err) { $("#msg").innerHTML = `<div class="err">${esc(err.message)}</div>`; e.target.disabled = false; }
  };
  if (can) $("#wal").onclick = async (e) => {
    e.target.disabled = true;
    try { const r = await api("/api/order/wallet", { region: W.region.code, uid: W.uid, squads: W.squads }); await loadMe(); chrome(); orderPlaced(r.order.id, r.order.number, "wallet"); }
    catch (err) { $("#msg").innerHTML = `<div class="err">${esc(err.message)}</div>`; e.target.disabled = false; }
  };
}

/* ---------- payment screen (QR) — used for orders and top-ups ---------- */
function payScreen(p, ctx) {
  const gen = ++S.gen; S.timers.forEach(clearInterval); S.timers = [];
  const total = Math.max(1, p.left);
  const isOrder = p.kind === "ORDER";
  app.innerHTML = `<div class="card glow center"><div class="title" style="justify-content:center">${isOrder ? "📲 Pay by QR" : "➕ Add balance"}</div>
    <div class="big" style="font-size:2rem">${esc(p.amount_fmt)}</div>
    ${p.test ? '<div class="warn">🧪 Test mode: confirms by itself in a few seconds.</div>' : ""}
    <div class="qr" id="qrbox"></div><div class="count" id="cd">--:--</div><div class="bar" style="max-width:300px;margin:8px auto"><i id="bar" style="width:100%"></i></div>
    <p class="mute small">1️⃣ Open your bank or wallet app · 2️⃣ Scan this QR and pay the exact amount<br>3️⃣ ${isOrder ? "Your order starts by itself ✅" : "Your wallet is topped up by itself ✅"}</p>
    <div id="st" class="mute">Waiting for your payment…</div><div id="extra"></div>
    <div class="actions" style="justify-content:center"><button class="btn green" id="chk">🔄 I’ve paid — check</button><button class="btn red" id="cx">✖️ Cancel</button></div></div>`;
  const box = $("#qrbox");
  if (p.qr_data || p.qr_url) { const im = new Image(); im.alt = "Payment QR code"; im.src = p.qr_data || p.qr_url; box.appendChild(im); }
  else { box.style.background = "transparent"; box.innerHTML = `<div class="mute" style="word-break:break-all;color:#111">${esc(p.qr_string || "")}</div>`; }
  if (p.checkout_url && /^https:\/\//.test(p.checkout_url)) $("#extra").innerHTML = `<a class="btn blue sm" target="_blank" rel="noopener" href="${esc(p.checkout_url)}">🔗 Open payment page</a>`;
  let left = p.left, done = false;
  const tick = () => { if (done) return; left = Math.max(0, left - 1); const m = Math.floor(left / 60), s = left % 60;
    $("#cd").textContent = `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`; $("#cd").classList.toggle("low", left < 120); $("#bar").style.width = (left / total) * 100 + "%";
    if (left === 0) { $("#st").innerHTML = "⌛ <b>This QR expired.</b> If you already paid, don’t worry — we’ll still find it."; } };
  tick(); every(1000, tick);
  const poll = async (manual) => {
    if (done || gen !== S.gen) return;
    try {
      const r = await api(`/api/payment/${p.id}?check=1`); const q = r.payment;
      if (q.state === "paid" || q.state === "credited") {
        done = true;
        if (!isOrder) { await loadMe(); chrome(); return topupDone(q); }
        if (q.state === "credited") { await loadMe(); chrome(); return app.innerHTML = `<div class="card"><div class="warn">🟡 We received ${esc(q.amount_fmt)} but couldn’t start the order. Your money is safe in 💰 Wallet. Please order again or contact support.</div><div class="actions"><a class="btn green" href="#/shop" data-act="reset">🛒 SHOP NOW</a><a class="btn blue" href="#/wallet">💰 Wallet</a></div></div>`; }
        if (q.order) return orderPlaced(q.order.id, q.order.number, "qr");
        return app.innerHTML = `<div class="card"><div class="ok">✅ Payment received — finishing your order…</div><a class="btn blue" href="#/orders">🧾 My Orders</a></div>`;
      }
      if (q.state === "failed") { done = true; $("#st").innerHTML = "❌ This payment expired or failed. Please start again."; }
      if (q.state === "cancelled") { done = true; }
    } catch (e) { if (manual) fail(e); }
  };
  every(3000, () => poll(false));
  $("#chk").onclick = async () => { $("#st").textContent = "Checking…"; await poll(true); if (!done) $("#st").textContent = "Not paid yet — we’ll keep checking automatically."; };
  $("#cx").onclick = async () => { done = true; try { await api(`/api/payment/${p.id}/cancel`, {}); } catch { /* ignore */ } toast("Cancelled. If you already paid, we’ll still finish it for you."); go(isOrder ? "#/shop" : "#/wallet"); route(); };
}
function topupDone(q) {
  app.innerHTML = `<div class="sent"><h2>✅ Balance added: ${esc(q.amount_fmt)}</h2><p>💰 New balance: <b>${esc(S.me.user.balance_fmt)}</b></p>
    <div class="actions" style="justify-content:center"><a class="btn green" href="#/shop">🛒 SHOP NOW</a><a class="btn blue" href="#/wallet">💰 Wallet</a></div></div>`;
}

/* ---------- order placed -> BOT REQUEST HAVE BEEN SEND ---------- */
function orderPlaced(oid, number, how) {
  const gen = ++S.gen; S.timers.forEach(clearInterval); S.timers = [];
  shopReset();
  app.innerHTML = `<div class="card glow center"><div class="ok">✅ ${how === "wallet" ? "Paid from wallet" : "Payment received"} · 🧾 Order <b>${esc(number)}</b></div>
    <div class="spin"></div><b>Sending your bot request now…</b><p class="mute small">This usually takes a few seconds.</p><div id="res"></div></div>`;
  let tries = 0;
  const look = async () => {
    if (gen !== S.gen) return;
    tries++;
    try {
      const r = await api("/api/orders/" + oid); const o = r.order;
      if (o.sent_notice || o.status === "COMPLETED") { return showSent(o, r.details); }
      if (["FAILED", "REJECTED", "CANCELLED", "REFUNDED"].includes(o.status)) { app.innerHTML = `<div class="card"><div class="err">🔴 ${esc(o.status_text)}</div><div class="actions"><a class="btn blue" href="#/orders">🧾 My Orders</a></div></div>`; return; }
      if (o.status === "REQUIRES_RECONCILIATION") { app.innerHTML = `<div class="card"><div class="warn">🟡 <b>Order ${esc(o.number)} is being checked.</b> We’ll sort it out — you don’t need to do anything. Contact support if it takes long.</div><div class="actions"><a class="btn blue" href="#/orders">🧾 My Orders</a></div></div>`; return; }
    } catch { /* keep trying */ }
    if (tries < 60) setTimeout(look, 2500);
    else app.innerHTML = `<div class="card"><div class="warn">Your order <b>${esc(number)}</b> is still starting. Check <a href="#/orders">My Orders</a> in a minute.</div></div>`;
  };
  setTimeout(look, 1500);
}
function showSent(o, details) {
  app.innerHTML = `<div class="sent"><h2>🤖 BOT REQUEST HAVE BEEN SEND</h2><div class="accept">👉 PLEASE ACCEPT BOT REQUEST IN GUILD</div>
    <div class="sum" style="margin-top:16px;text-align:left">${o.guild ? `<div><span>🏷 Guild</span><b>${esc(o.guild)}</b></div>` : ""}<div><span>🛡 Guild ID</span><b>${esc(o.uid)}</b></div>
    <div><span>🧾 Order</span><b>${esc(o.number)} · ${o.squads} squad${o.squads === 1 ? "" : "s"}</b></div></div>
    ${o.asked && o.asked !== o.squads ? `<div class="warn">🟡 Asked ${o.asked}, started ${o.squads} (the rest is back in your wallet)</div>` : ""}
    <div class="det">${esc(details)}</div>
    <div class="actions" style="justify-content:center"><a class="btn blue" href="#/guild/${encodeURIComponent(o.region)}/${encodeURIComponent(o.uid)}">📈 Live glory</a><a class="btn green" href="#/shop">🛒 SHOP NOW</a><a class="btn blue" href="#/orders">🧾 My Orders</a></div></div>`;
}

/* ---------- stock ---------- */
async function vStock(a, b, g) {
  const st = await api("/api/stock");
  const note = st.paused ? '<div class="warn">⏸ Orders are paused for a moment.</div>' : st.closed ? '<div class="warn">🌙 Service closed until Monday 12 AM (Nepal time).</div>' : "";
  app.innerHTML = `<div class="card glow"><div class="title">🔍 Squads available now</div>${note}
    ${st.unavailable ? '<div class="warn">🟡 Stock can’t be loaded right now. Please refresh in a moment.</div>' : `<div class="grid g2"><div class="stat g"><div class="n">${st.normal}</div><div class="l">🟢 Normal regions · squads</div></div><div class="stat p"><div class="n">${st.premium}</div><div class="l">💎 Premium regions · squads</div></div></div>`}
    <h3 style="margin-top:20px">Regions</h3><div class="grid g4">${st.regions.map((r) => `<div class="stat"><div style="font-size:1.6rem">${esc(r.emoji)}</div><b>${esc(r.name)}</b> ${r.premium ? "💎" : ""}<br><span class="chip ${r.available ? "yes" : "no"}">${r.available ? "✅ AVAILABLE" : "⛔ SOLD OUT"}</span></div>`).join("")}</div>
    <p class="mute small" style="margin-top:14px">💵 Price: <b>${npr(st.price)}</b> per squad · 🕐 Updated ${esc(fmtDt(st.time))}</p>
    <div class="actions"><button class="btn blue" data-act="reload">🔄 Refresh</button><a class="btn green" href="#/shop">🛒 SHOP NOW</a></div></div>`;
}

/* ---------- guilds ---------- */
async function vGuilds() {
  const r = await api("/api/guilds");
  app.innerHTML = `<div class="card"><div class="title">⚔️ My Guilds</div>` + (r.guilds.length ? r.guilds.map((x) =>
    `<a class="item" href="#/guild/${encodeURIComponent(x.region)}/${encodeURIComponent(x.uid)}"><span class="ic">⚔️</span><span class="mid"><b>${esc(x.name || "Guild")}</b><span class="mute small">${esc(x.uid)} · ${esc(x.emoji)} ${esc(x.region_name)}</span></span><span>➜</span></a>`).join("")
    : '<p class="mute">No guilds yet. Tap SHOP NOW to start!</p><a class="btn green" href="#/shop">🛒 SHOP NOW</a>') + "</div>";
}
async function vGuild(region, uid, g) {
  region = decodeURIComponent(region || ""); uid = decodeURIComponent(uid || "");
  let prev = null, timerEl = null;
  const draw = async (force) => {
    const r = await api(`/api/guild?region=${encodeURIComponent(region)}&uid=${encodeURIComponent(uid)}${force ? "&force=1" : ""}`);
    if (g !== S.gen) return; syncClock(r.now); const x = r.guild;
    const d = prev === null ? "" : x.glory - prev > 0 ? `<span class="delta">▲ +${(x.glory - prev).toLocaleString()}</span>` : '<span class="mute small"> (no change yet)</span>';
    prev = x.glory;
    app.innerHTML = `<div class="card glow"><div class="row"><div class="title" style="margin:0">⚔️ ${esc(x.name || "Guild " + x.uid)}</div><span class="sp"></span>${x.squads ? '<span class="live">LIVE</span>' : ""}</div>
      <p class="sub">🆔 ${esc(x.uid)} · ${esc(x.emoji)} ${esc(x.region_name)}</p>
      ${x.squads ? `<div class="mute small">🏆 GLORY</div><div><span class="big">${x.glory.toLocaleString()}</span>${d}</div>
      <div class="grid g3" style="margin:16px 0"><div class="stat"><div class="n">${x.squads}</div><div class="l">Squads (${x.running} running)</div></div>
      <div class="stat"><div class="n">${x.bots ? x.bots_running + "/" + x.bots : "—"}</div><div class="l">🤖 Bots online</div></div><div class="stat"><div class="n">${x.orders}</div><div class="l">🧾 Orders</div></div></div>
      <div class="card pink" id="clk" style="margin:0"></div>` : '<p class="mute">No running squads yet.</p>'}
      <div class="actions"><button class="btn blue" id="rf">🔄 Refresh</button><button class="btn green" id="more">➕ Order more</button><a class="btn blue" href="#/guilds">⬅ My Guilds</a></div></div>
      <div class="card"><div class="title">ℹ️ Glory info</div>${x.groups.length ? x.groups.map((s, i) => `<div class="tx"><span><b>Squad ${i + 1}</b> · 🏆 ${s.glory.toLocaleString()} · <span class="chip ${s.status === "running" ? "run" : "wait"}">${esc(s.status)}</span></span></div>`).join("") : '<p class="mute">No running squads yet.</p>'}
      <div class="det">${esc(r.details)}</div></div>`;
    if (x.squads && x.start) { const el = $("#clk"); live(el, x.start, x.run_hours); }
    $("#rf").onclick = () => draw(true).catch(fail);
    $("#more").onclick = () => { W.region = { code: x.region, name: x.region_name, emoji: x.emoji }; W.uid = x.uid; W.step = 3; go("#/shop"); route(); };
  };
  await draw(false);
  every(15000, () => draw(false).catch(() => {}));     // live: glory + ▲ change refresh by themselves
}

/* ---------- orders ---------- */
async function vOrders(a) {
  const page = +a || 0, r = await api("/api/orders?page=" + page);
  app.innerHTML = `<div class="card"><div class="title">🧾 My Orders <span class="mute small">(${r.total})</span></div>` + (r.orders.length ? r.orders.map((o) =>
    `<a class="item" href="#/order/${o.id}"><span class="ic">🧾</span><span class="mid"><b>${esc(o.number)} · ${esc(o.guild || "Guild " + o.uid)}</b><span class="mute small">${o.squads} sq · 🕐 ${esc(o.created_fmt)}</span></span>
    <span class="small" style="text-align:right">${esc(o.status_text)}</span></a>`).join("") : '<p class="mute">No orders yet.</p><a class="btn green" href="#/shop">🛒 SHOP NOW</a>') +
    (r.pages > 1 ? `<div class="actions">${page > 0 ? `<a class="btn ghost" href="#/orders/${page - 1}">⬅ Prev</a>` : ""}${page + 1 < r.pages ? `<a class="btn ghost" href="#/orders/${page + 1}">Next ➡</a>` : ""}</div>` : "") + "</div>";
}
async function vOrder(id, b, g) {
  const r = await api("/api/orders/" + encodeURIComponent(id)), o = r.order;
  app.innerHTML = `<div class="card glow"><div class="title">🧾 ${esc(o.number)} ${o.mock ? '<span class="chip wait">TEST</span>' : ""}</div>
    <div class="sum"><div><span>🏷 Guild</span><b>${esc(o.guild || "—")}</b></div><div><span>🛡 Guild ID</span><b>${esc(o.uid)}</b></div><div><span>🌍 Region</span><b>${esc(o.region_emoji)} ${esc(o.region_name)}</b></div>
    <div><span>👥 Squads</span><b>${o.squads}${o.asked && o.asked !== o.squads ? ` (asked ${o.asked}, rest returned to wallet)` : ""}</b></div>
    <div><span>💳 Paid</span><b>${esc(o.total_fmt)} · ${o.method === "wallet" ? "wallet" : "QR payment"}</b></div><div><span>🕐 Time</span><b>${esc(o.created_fmt)}</b></div>
    <div><span>Status</span><b>${esc(o.status_text)}</b></div></div>
    ${o.start ? '<div class="card pink" id="clk" style="margin:14px 0 0"></div>' : ""}
    ${o.sent_notice ? `<div class="sent" style="margin-top:14px"><h2>🤖 BOT REQUEST HAVE BEEN SEND</h2><div class="accept">👉 PLEASE ACCEPT BOT REQUEST IN GUILD</div><div class="det">${esc(r.details)}</div></div>` : ""}
    <div class="actions">${o.live ? `<a class="btn blue" href="#/guild/${encodeURIComponent(o.region)}/${encodeURIComponent(o.uid)}">📈 Live glory</a>` : ""}<a class="btn ghost" href="#/orders">⬅ My Orders</a></div></div>`;
  if (o.start) live($("#clk"), o.start, 8);
}

/* ---------- wallet / top-up ---------- */
async function vWallet() {
  const r = await api("/api/wallet");
  app.innerHTML = `<div class="card glow"><div class="title">💰 Wallet</div><div class="mute small">🟢 BALANCE</div><div class="big">${esc(r.balance_fmt)}</div>
    <div class="actions"><a class="btn green" href="#/topup">➕ Add balance</a><a class="btn blue" href="#/shop">🛒 SHOP NOW</a></div></div>
    <div class="card"><div class="title">Recent</div>${r.history.length ? r.history.map((t) => {
      return `<div class="tx"><span>${esc(t.label)}<br><span class="mute small">${esc(t.time)}</span></span><span class="${t.sign < 0 ? "neg" : "pos"}">${t.sign < 0 ? "−" : "+"}${esc(t.amount_fmt)}</span></div>`; }).join("") : '<p class="mute">Nothing yet</p>'}</div>`;
}
async function vTopup() {
  const m = S.me;
  app.innerHTML = `<div class="card glow"><div class="title">➕ Add balance</div><p class="sub">Pay by QR with any Nepali bank or wallet app (FoneBiz / Fonepay). It is added automatically.</p>
    <div class="grid g3">${m.topups.map((a) => `<button class="sq" data-a="${a}"><b>${npr(a)}</b></button>`).join("")}</div>
    <label>Other amount (min ${npr(m.topup_min)})</label><div class="row"><input id="amt" inputmode="decimal" placeholder="e.g. 750" style="flex:1"><button class="btn green" id="go">Pay ➜</button></div><div id="msg"></div>
    <div class="actions"><a class="btn red" href="#/wallet">⬅ Wallet</a></div></div>`;
  const start = async (amount, btn) => {
    btn.disabled = true;
    try { const r = await api("/api/topup", { amount }); payScreen(r.payment, { kind: "TOPUP" }); }
    catch (e) { $("#msg").innerHTML = `<div class="err">${esc(e.message)}</div>`; btn.disabled = false; }
  };
  app.querySelectorAll(".sq").forEach((b) => (b.onclick = () => start(b.dataset.a, b)));
  $("#go").onclick = (e) => start($("#amt").value, e.target);
}

/* ---------- profile ---------- */
const COUNTRIES = ["Nepal", "India", "Bangladesh", "Pakistan", "Indonesia", "Brazil", "Thailand", "Middle East", "Other"];
async function vProfile() {
  const u = S.me.user, first = !u.country;
  app.innerHTML = `<div class="card glow"><div class="title">👤 ${first ? "Set up your profile" : esc(u.name)}</div>${first ? '<p class="sub">Quick setup — 10 seconds.</p>' : ""}
    <label>Name</label><input id="n" maxlength="40" value="${esc(u.name)}"><label>Country</label><input id="c" list="cl" maxlength="32" value="${esc(u.country || "")}" placeholder="Type or pick">
    <datalist id="cl">${COUNTRIES.map((c) => `<option value="${c}">`).join("")}</datalist><div class="mute small" style="margin-top:10px">💱 Currency: <b>NPR</b> (Nepali Rupee) · 👥 Squads bought: ${u.squads}</div>
    <div id="msg"></div><div class="actions"><button class="btn green" id="save">💾 Save</button>${first ? "" : '<button class="btn red" id="out">🚪 Log out</button>'}</div></div>
    ${first ? "" : `<div class="card"><div class="title">🔐 ${u.username ? "Change password" : "Website password"}</div>
      <p class="sub">${u.username ? "" : "You came from Telegram. Choose a username and password to log in here without a code."}</p>
      ${u.username ? `<div class="mute small">Username: <b>${esc(u.username)}</b></div>` : '<label>Username</label><input id="un" autocapitalize="none" placeholder="4–20 letters / numbers / _">'}
      <label>New password</label><input id="pw" type="password" autocomplete="new-password" placeholder="8+ characters"><div id="msg2"></div>
      <div class="actions"><button class="btn blue" id="sp">🔐 Save password</button></div></div>`}`;
  $("#save").onclick = async () => {
    try { await api("/api/profile", { name: $("#n").value, country: $("#c").value }); await loadMe(); chrome(); toast("Saved ✅", "good"); if (first) go("#/shop"); else route(); }
    catch (e) { $("#msg").innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  };
  const out = $("#out"); if (out) out.onclick = async () => { await api("/api/logout", {}); S.me = null; chrome(); go("#/login"); };
  const sp = $("#sp"); if (sp) sp.onclick = async () => {
    try { await api("/api/password", { password: $("#pw").value, username: $("#un") ? $("#un").value : undefined }); toast("Password saved ✅", "good"); await loadMe(); route(); }
    catch (e) { $("#msg2").innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  };
}

/* ---------- more (phone menu) ---------- */
function vMore() {
  const items = [...MORE, ...(S.me.user.admin ? [["#/admin", "🛠", "Admin panel"]] : [])];
  app.innerHTML = `<div class="card glow"><div class="title">☰ More</div>${items.map(([h, i, n]) => `<a class="item" href="${h}"><span class="ic">${i}</span><span class="mid"><b>${n}</b></span><span>➜</span></a>`).join("")}
    <div class="actions"><button class="btn red block" id="out">🚪 Log out</button></div></div>`;
  $("#out").onclick = async () => { await api("/api/logout", {}); S.me = null; chrome(); go("#/login"); };
}

/* ---------- rules / support ---------- */
async function vRules() { const r = await api("/api/rules"); app.innerHTML = `<div class="card glow"><div class="rules">${r.html}</div></div>`; }
function vSupport() {
  const sup = (S.me && S.me.support) || "ceobny";
  app.innerHTML = `<div class="card glow"><div class="title">🛟 Support</div><p>Chat with our admin on Telegram: <b>@${esc(sup)}</b></p>
    <div class="actions"><a class="btn green" target="_blank" rel="noopener" href="https://t.me/${encodeURIComponent(sup)}">💬 Chat on Telegram</a>${S.me ? `<a class="btn blue" target="_blank" rel="noopener" href="${esc(S.me.guild_site)}">🌐 Guild site</a>` : ""}</div></div>`;
}

/* ---------- admin ---------- */
async function vAdmin(tab) {
  if (!S.me.user.admin) return go("#/shop");
  tab = tab || "overview";
  const tabs = [["overview", "📊 Overview"], ["stock", "📦 Stock"], ["live", "🔴 Live orders"], ["users", "👥 Users"], ["rules", "📜 Rules"], ["bc", "📢 Broadcast"]];
  const head = `<div class="seg" style="overflow:auto">${tabs.map(([k, t]) => `<button data-k="${k}" class="${k === tab ? "on" : ""}" style="white-space:nowrap">${t}</button>`).join("")}</div>`;
  app.innerHTML = head + '<div id="adm"><div class="spin"></div></div>';
  app.querySelector(".seg").onclick = (e) => { if (e.target.dataset.k) go("#/admin/" + e.target.dataset.k); };
  const A = $("#adm");
  if (tab === "overview" || tab === "stock") {
    const o = await api("/api/admin/overview"), c = o.credits;
    const regs = `<div class="grid g2">${o.regions.map((r) => `<button class="tog ${r.enabled ? "on" : "off"}" data-c="${esc(r.code)}"><span>${esc(r.emoji)} ${esc(r.name)} ${r.premium ? "💎" : ""}</span><span>${r.enabled ? "✅ ON · " + r.stock : "⛔ OFF"}</span></button>`).join("")}</div>`;
    A.innerHTML = (tab === "overview" ? `<div class="grid g4"><div class="stat"><div class="n">${o.users}</div><div class="l">Users</div></div><div class="stat g"><div class="n">${o.orders}</div><div class="l">Orders</div></div>
      <div class="stat p"><div class="n" style="font-size:1.25rem">${esc(o.sales_fmt)}</div><div class="l">Sales</div></div><div class="stat"><div class="n" style="font-size:1.25rem">${c ? c.basic + " / " + c.premium : "—"}</div><div class="l">Credits basic / premium</div></div></div>
      ${o.mock ? '<div class="warn">🧪 Provider is in MOCK mode.</div>' : ""}
      <div class="card" style="margin-top:16px"><div class="title">💰 Price per squad: ${esc(o.price_fmt)}</div><div class="row">
        ${[-100, -25, 25, 100].map((d) => `<button class="btn sm ${d < 0 ? "red" : "green"}" data-d="${d}">${d < 0 ? "➖ " + -d : "➕ " + d}</button>`).join("")}
        <input id="np" inputmode="decimal" placeholder="exact" style="max-width:130px"><button class="btn sm blue" id="setp">Set</button></div><div id="pm"></div></div>
      <div class="card"><button class="btn ${o.paused ? "green" : "red"}" id="pz">${o.paused ? "▶️ Resume orders" : "⏸ Pause all orders"}</button></div>`
      : `<div class="card"><div class="title">📦 Stock &amp; Credits</div><p class="sub">Credits: basic <b>${c ? c.basic : "—"}</b> · premium <b>${c ? c.premium : "—"}</b>. Tap a region to switch it on/off.</p>${regs}
      <label>🎟 Add credits — redeem a coupon code</label><div class="row"><input id="rc" placeholder="coupon code" style="flex:1"><button class="btn green" id="rd">Redeem</button></div><div id="rm"></div></div>`);
    A.querySelectorAll(".tog").forEach((b) => (b.onclick = async () => { try { await api("/api/admin/region", { code: b.dataset.c }); route(); } catch (e) { fail(e); } }));
    const pz = $("#pz"); if (pz) pz.onclick = async () => { try { await api("/api/admin/pause", {}); route(); } catch (e) { fail(e); } };
    A.querySelectorAll("[data-d]").forEach((b) => (b.onclick = async () => { try { await api("/api/admin/price", { price: o.price + +b.dataset.d }); route(); } catch (e) { fail(e); } }));
    const sp = $("#setp"); if (sp) sp.onclick = async () => { try { await api("/api/admin/price", { price: $("#np").value }); route(); } catch (e) { fail(e); } };
    const rd = $("#rd"); if (rd) rd.onclick = async () => {
      try { const r = await api("/api/admin/redeem", { code: $("#rc").value }); $("#rm").innerHTML = `<div class="${r.ok ? "ok" : "err"}">${r.ok ? "✅ Code redeemed!" : "🔴 Code not accepted."} ${esc(r.message)}${r.credits ? ` · credits now ${r.credits.basic} / ${r.credits.premium}` : ""}</div>`; $("#rc").value = ""; }
      catch (e) { $("#rm").innerHTML = `<div class="err">${esc(e.message)}</div>`; }
    };
  } else if (tab === "live") {
    const draw = async () => {
      const r = await api("/api/admin/live");
      A.innerHTML = `<div class="card"><div class="title">🔴 Live orders</div>${r.orders.length ? `<div style="overflow:auto"><table><tr><th>Order</th><th>User</th><th>Guild</th><th>Sq</th><th>Paid</th><th>Status</th></tr>` +
        r.orders.map((o) => `<tr><td><b>${esc(o.number)}</b></td><td>${esc(o.user)}</td><td>${esc(o.region)} · ${esc(o.uid)}</td><td>${o.squads}</td><td>${esc(o.total_fmt)}</td><td>${esc(o.status_text)}</td></tr>`).join("") + "</table></div>" : '<p class="mute">No live orders.</p>'}</div>`;
    };
    await draw(); every(8000, () => draw().catch(() => {}));
  } else if (tab === "users") {
    A.innerHTML = `<div class="card"><div class="title">👥 Users</div><div class="row"><input id="q" placeholder="name, @username or Telegram ID" style="flex:1"><button class="btn blue" id="sq">Search</button></div><div id="ul"></div></div>`;
    const load = async () => {
      const r = await api("/api/admin/users?q=" + encodeURIComponent($("#q").value));
      $("#ul").innerHTML = '<div style="overflow:auto"><table><tr><th>Name</th><th>From</th><th>Squads</th><th>Status</th><th></th></tr>' + r.users.map((u) =>
        `<tr><td><b>${esc(u.name)}</b><br><span class="mute small">${esc(u.username || "")} ${u.tg ? "· " + u.tg : ""}</span></td><td>${esc(u.source)}</td><td>${u.squads}</td><td>${esc(u.status)}</td>
        <td><button class="btn sm ${u.status === "ACTIVE" ? "red" : "green"}" data-s="${u.id}">${u.status === "ACTIVE" ? "Suspend" : "Activate"}</button> <button class="btn sm blue" data-a="${u.id}">± NPR</button></td></tr>`).join("") + "</table></div>";
      $("#ul").querySelectorAll("[data-s]").forEach((b) => (b.onclick = async () => { try { await api("/api/admin/suspend", { id: +b.dataset.s }); load(); } catch (e) { fail(e); } }));
      $("#ul").querySelectorAll("[data-a]").forEach((b) => (b.onclick = async () => { const v = prompt("Adjust wallet by NPR (minus to deduct):"); if (!v) return;
        try { await api("/api/admin/adjust", { id: +b.dataset.a, amount: v }); toast("Wallet adjusted ✅", "good"); } catch (e) { fail(e); } }));
    };
    $("#sq").onclick = load; $("#q").onkeydown = (e) => e.key === "Enter" && load(); await load();
  } else if (tab === "rules") {
    const r = await api("/api/admin/rules/get");
    A.innerHTML = `<div class="card"><div class="title">📜 Edit rules</div><p class="sub">Shown to everyone on the website and in the bot. One rule per line.</p><textarea id="rt">${esc(r.text)}</textarea><div id="rm"></div>
      <div class="actions"><button class="btn green" id="sv">💾 Save</button><button class="btn red" id="rs">↩️ Reset to default</button></div></div>`;
    $("#sv").onclick = async () => { try { await api("/api/admin/rules", { text: $("#rt").value }); $("#rm").innerHTML = '<div class="ok">✅ Rules updated.</div>'; } catch (e) { $("#rm").innerHTML = `<div class="err">${esc(e.message)}</div>`; } };
    $("#rs").onclick = async () => { if (!confirm("Reset the rules to the default text?")) return; try { await api("/api/admin/rules", { reset: true }); route(); } catch (e) { fail(e); } };
  } else if (tab === "bc") {
    A.innerHTML = `<div class="card"><div class="title">📢 Broadcast</div><p class="sub">Sent to every active user on Telegram.</p><textarea id="bt" style="min-height:140px"></textarea><div id="bm"></div>
      <div class="actions"><button class="btn green" id="bs">📢 Send</button></div></div>`;
    $("#bs").onclick = async () => { if (!confirm("Send this message to all Telegram users?")) return; try { const r = await api("/api/admin/broadcast", { text: $("#bt").value }); $("#bm").innerHTML = `<div class="ok">✅ Sending to ${r.queued} users…</div>`; } catch (e) { $("#bm").innerHTML = `<div class="err">${esc(e.message)}</div>`; } };
  }
}

/* ---------- boot ---------- */
document.addEventListener("click", (e) => {      // CSP forbids inline onclick, so buttons use data-act
  const el = e.target.closest && e.target.closest("[data-act]"); if (!el) return;
  if (el.dataset.act === "reload") route(); else if (el.dataset.act === "reset") shopReset();
});
route();
