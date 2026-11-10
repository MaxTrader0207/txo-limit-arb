// 純前端：只讀 data/index.json 與 data/daily/<日期>.json
const $ = (s) => document.querySelector(s);
const esc = (x) => String(x ?? "-").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmt = (x, d = 1) => (x === null || x === undefined ? "-" : Number(x).toLocaleString("zh-TW", { maximumFractionDigits: d }));
const STATUS = { LIMIT_UP: "漲停鎖住", LIMIT_DOWN: "跌停鎖住", TOUCHED_UP: "觸及漲停", TOUCHED_DOWN: "觸及跌停",
  NEAR_UP: "接近漲停", NEAR_DOWN: "接近跌停", NORMAL: "正常", UNKNOWN: "資料不足" };

let INDEX = null, REP = null;

async function getJSON(url) {
  const r = await fetch(url + "?t=" + Date.now());
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}

async function init() {
  try {
    INDEX = await getJSON("data/index.json");
  } catch (e) {
    $("#updated").textContent = "尚無資料，等待第一次排程執行。";
    return;
  }
  $("#date").innerHTML = INDEX.days.map((d) =>
    `<option value="${d.date}">${d.date}${d.limit_event ? " ⚠" : ""}${d.n_feasible ? " ★" + d.n_feasible : ""}</option>`).join("");
  const want = new URLSearchParams(location.search).get("date");
  $("#date").value = INDEX.days.some((d) => d.date === want) ? want : INDEX.latest;
  $("#date").onchange = () => loadDay($("#date").value);
  renderHistory();
  await loadDay($("#date").value);
}

async function loadDay(date) {
  REP = await getJSON(`data/daily/${date}.json`);
  history.replaceState(null, "", `?date=${date}`);
  $("#updated").textContent = `資料產生於 ${REP.generated.replace("T", " ")}`;
  renderSessions();
  renderTypeFilter();
  renderSignals();
  renderFwd();
  renderSeriesSelect();
  renderChain();
}

function renderSessions() {
  const b = $("#banner");
  b.className = "banner" + (REP.limit_event ? " alert" : "");
  b.textContent = REP.limit_event
    ? "⚠️ 台指期出現漲跌停事件：期貨平價訊號請看「可否成交」，鎖住那一側的期貨腳多半排不到。"
    : "今日台指期無漲跌停；以下為純選擇權邊界檢查與一般平價偏離。";
  $("#sessions").innerHTML = Object.entries(REP.sessions).map(([k, v]) => `
    <div class="card">
      <div class="mut">${esc(k)}盤 · TX ${esc(v.expiry)}</div>
      <div class="big ${esc(v.status)}">${esc(v.label)}</div>
      <div>收 ${fmt(v.close, 0)}　<span class="${v.chg >= 0 ? "up" : "dn"}">${v.chg >= 0 ? "+" : ""}${fmt(v.chg, 0)} (${esc(v.pct)}%)</span></div>
      <div class="mut">漲停 ${fmt(v.limit_up, 0)} ／ 跌停 ${fmt(v.limit_down, 0)}</div>
    </div>`).join("");
}

function renderTypeFilter() {
  const types = [...new Set(REP.signals.map((s) => s.type))];
  $("#fType").innerHTML = `<option value="">全部類型</option>` + types.map((t) => `<option>${esc(t)}</option>`).join("");
}

function renderSignals() {
  const feas = $("#fFeas").checked, deep = $("#fDeep").checked, basis = $("#fBasis").checked, type = $("#fType").value;
  const riskless = (s) => s.riskless !== false;
  const rows = REP.signals.filter((s) => (!feas || s.feasible) && (!deep || s.deep_otm) &&
    (basis || riskless(s)) && (!type || s.type === type));
  const hidden = REP.signals.filter((s) => !riskless(s)).length;
  $("#sigTable").innerHTML = `<tr><th>類型</th><th>性質</th><th>週別</th><th>時段</th><th>組合</th><th>淨利(點)</th><th>淨利(元)</th>
    <th>成本(點)</th><th>可否成交</th><th>深價外</th><th>備註</th></tr>` +
    (rows.length ? rows.map((s) => `<tr class="click" data-exp="${esc(s.expiry)}" data-sess="${esc(s.session)}">
      <td>${esc(s.type)}</td><td class="${riskless(s) ? "yes" : "no"}">${riskless(s) ? "套利" : "基差"}</td><td>${esc(s.expiry)}</td><td>${esc(s.session)}</td><td>${esc(s.legs)}</td>
      <td class="n">${fmt(s.edge_pts, 2)}</td><td class="n">${fmt(s.edge_ntd, 0)}</td><td class="n">${fmt(s.cost_pts, 2)}</td>
      <td class="${s.feasible ? "yes" : "no"}">${s.feasible ? "可" : "需排隊"}</td><td>${s.deep_otm ? "★" : ""}</td>
      <td class="mut">${esc(s.note)}</td></tr>`).join("")
      : `<tr><td colspan="11" class="empty">扣除成本後無符合條件的訊號${!basis && hidden ? `（另有 ${hidden} 筆基差交易，勾選「含基差交易」可看）` : ""}</td></tr>`);
  document.querySelectorAll("#sigTable tr.click").forEach((tr) => tr.onclick = () => {
    $("#series").value = tr.dataset.exp + "|" + tr.dataset.sess;
    switchTab("chain"); renderChain();
  });
}

function renderFwd() {
  const md = (d) => (d ? d.slice(5).replace("-", "/") : "-");
  $("#fwdTable").innerHTML = `<tr><th>序列</th><th>時段</th><th>結算日</th><th>對應期貨</th><th>期貨結算</th><th>對沖</th>
    <th>期貨狀態</th><th>期貨價</th><th>隱含價</th><th>差距</th><th>履約價數</th></tr>` +
    REP.series.map((r) => `<tr><td>${esc(r.expiry)}</td><td>${esc(r.session)}</td><td>${md(r.opt_settle)}</td>
      <td>TX ${esc(r.hedge)}</td><td>${md(r.hedge_settle)}</td>
      <td class="${r.matched ? "yes" : "no"}">${r.matched ? "同日結算" : `差 ${r.basis_days} 天`}</td>
      <td class="${esc(r.status)}">${esc(r.status_label)}</td><td class="n">${fmt(r.futures, 0)}</td>
      <td class="n">${fmt(r.implied_fwd)}</td><td class="n">${fmt(r.gap)}</td><td class="n">${r.chain.length}</td></tr>`).join("");
}

function renderSeriesSelect() {
  $("#series").innerHTML = REP.series.map((s) => `<option value="${esc(s.expiry)}|${esc(s.session)}">${esc(s.expiry)} · ${esc(s.session)} · ${esc((s.opt_settle || "").slice(5))} 結算 → TX ${esc(s.hedge)}</option>`).join("");
}

function renderChain() {
  const [exp, sess] = ($("#series").value || "").split("|");
  const s = REP.series.find((x) => x.expiry === exp && x.session === sess);
  if (!s) { $("#chainTable").innerHTML = `<tr><td class="empty">無資料</td></tr>`; return; }
  const F = s.implied_fwd || s.futures, p = REP.config.deep_otm_pct;
  const hits = new Set(REP.signals.filter((x) => x.expiry === exp && x.session === sess).flatMap((x) => x.strikes));
  const has = (q) => q && (q.bid || q.ask);
  let atm = s.chain.reduce((b, r) => (Math.abs(r.k - F) < Math.abs(b.k - F) ? r : b), s.chain[0]).k;
  const rows = s.chain.filter((r) => !$("#fQuoted").checked || has(r.C) || has(r.P));
  const q = (o, f) => `<td class="n">${fmt(o?.[f])}</td>`;
  $("#chainTable").innerHTML = `<tr><th>C 結算</th><th>C 買</th><th>C 賣</th><th>履約價</th><th>P 買</th><th>P 賣</th><th>P 結算</th></tr>` +
    rows.map((r) => {
      const cls = hits.has(r.k) ? "hit" : r.k === atm ? "atm" : (r.k >= F * (1 + p) || r.k <= F * (1 - p)) ? "deep" : "";
      return `<tr class="${cls}">${q(r.C, "settle")}${q(r.C, "bid")}${q(r.C, "ask")}<td class="k">${fmt(r.k, 0)}</td>
        ${q(r.P, "bid")}${q(r.P, "ask")}${q(r.P, "settle")}</tr>`;
    }).join("");
}

function renderHistory() {
  $("#histTable").innerHTML = `<tr><th>交易日</th><th>狀態</th><th>漲跌%</th><th>可成交套利</th><th>基差交易</th><th>最佳訊號</th></tr>` +
    INDEX.days.map((d) => {
      const st = Object.entries(d.status).map(([k, v]) => `<span class="${v}">${k}:${STATUS[v] || v}</span>`).join(" ");
      const pct = Object.values(d.pct || {}).map((x) => fmt(x, 2)).join(" / ");
      const best = d.best ? `${esc(d.best.type)} ${fmt(d.best.edge_pts, 2)} 點` : "-";
      return `<tr class="click" data-date="${d.date}"><td>${d.date}${d.limit_event ? " ⚠" : ""}</td><td>${st}</td>
        <td class="n">${pct}</td><td class="n">${d.n_feasible}</td><td class="n">${d.n_basis ?? "-"}</td><td>${best}</td></tr>`;
    }).join("");
  document.querySelectorAll("#histTable tr.click").forEach((tr) => tr.onclick = () => {
    $("#date").value = tr.dataset.date; loadDay(tr.dataset.date); switchTab("signals");
  });
}

function switchTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== "tab-" + name));
}

document.querySelectorAll(".tabs button").forEach((b) => (b.onclick = () => switchTab(b.dataset.tab)));
["#fFeas", "#fDeep", "#fBasis", "#fType"].forEach((s) => ($(s).onchange = renderSignals));
$("#series").onchange = renderChain;
$("#fQuoted").onchange = renderChain;
init();
