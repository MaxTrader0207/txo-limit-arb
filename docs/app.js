// 純前端：只讀 data/index.json 與 data/daily/<日期>.json
const $ = (s) => document.querySelector(s);
const esc = (x) => String(x ?? "-").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmt = (x, d = 1) => (x === null || x === undefined ? "-" : Number(x).toLocaleString("zh-TW", { maximumFractionDigits: d }));
const STATUS = { LIMIT_UP: "漲停鎖住", LIMIT_DOWN: "跌停鎖住", TOUCHED_UP: "觸及漲停", TOUCHED_DOWN: "觸及跌停",
  INTRA_NEAR_UP: "盤中接近漲停", INTRA_NEAR_DOWN: "盤中接近跌停",
  NEAR_UP: "接近漲停", NEAR_DOWN: "接近跌停", NORMAL: "正常", UNKNOWN: "資料不足" };

let INDEX = null, REP = null;
const FEE = { opt: null, fut: null };

// 手續費調整：後端以 config 的手續費計算 edge，並記錄每個訊號「收幾次選擇權／期貨手續費」
function feeDelta(optSides, futSides) {
  const c = REP.config || {};
  if (FEE.opt === null || c.opt_fee === undefined) return 0;
  return ((optSides || 0) * (c.opt_fee - FEE.opt) + (futSides || 0) * (c.fut_fee - FEE.fut)) / (c.point_value || 50);
}
function adj(s) {
  const pv = REP.config.point_value || 50, d = feeDelta(s.opt_sides, s.fut_sides);
  const edge = s.edge_pts + d, cap = s.capital ?? s.capital_ntd;
  const per = cap >= 1000 ? edge * pv / cap * 10000 : null;
  return { edge, ntd: edge * pv, cost: s.cost_pts - d, per,
           annual: per === null ? null : per / 100 * 365 / Math.max(s.hold_days || 1, 1) };
}
function adjItem(x) {
  const d = feeDelta(x.opt_sides, 0), pv = REP.config.point_value || 50;
  const last = x.pnl_last === null ? null : x.pnl_last + d, high = x.pnl_high === null ? null : x.pnl_high + d;
  return { ...x, pnl_last: last, pnl_high: high, per_10k: last === null ? null : last * pv / x.margin * 10000 };
}
function zoneStats(lst) {
  const pnl = lst.map((x) => x.pnl_last).filter((p) => p !== null);
  const per = lst.map((x) => x.per_10k).filter((p) => p !== null);
  return { strikes: lst.length, volume: lst.reduce((a, x) => a + x.vol, 0),
    premium_ntd: lst.reduce((a, x) => a + (x.last || 0) * x.vol, 0) * (REP.config.point_value || 50),
    max_last: Math.max(0, ...lst.map((x) => x.last || 0)), profitable: pnl.filter((p) => p > 0).length,
    losing: pnl.filter((p) => p < 0).length, best_pnl: pnl.length ? Math.max(...pnl) : null,
    best_per_10k: per.length ? Math.max(...per) : null };
}
function initFees() {
  const c = REP.config;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem("txoFees") || "null"); } catch (e) { saved = null; }
  FEE.opt = saved?.opt ?? c.opt_fee; FEE.fut = saved?.fut ?? c.fut_fee;
  $("#feeOpt").min = $("#feeFut").min = c.fee_floor ?? 10;
  $("#feeOpt").value = FEE.opt; $("#feeFut").value = FEE.fut;
}
function onFee() {
  const lo = REP.config.fee_floor ?? 10;
  FEE.opt = Math.max(lo, Number($("#feeOpt").value) || REP.config.opt_fee);
  FEE.fut = Math.max(lo, Number($("#feeFut").value) || REP.config.fut_fee);
  try { localStorage.setItem("txoFees", JSON.stringify(FEE)); } catch (e) { /* 無法儲存就算了 */ }
  renderSignals(); renderExpiry();
}

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
  // 排程健康檢查：平日超過 26 小時沒更新就提示
  const ageH = (Date.now() - new Date(INDEX.updated).getTime()) / 3600e3;
  const wd = new Date(Date.now() + 8 * 3600e3).getUTCDay();
  if (ageH > 26 && wd >= 2 && wd <= 6) {
    $("#updated").insertAdjacentHTML("afterend",
      `<div class="banner alert">⚠️ 資料已 ${Math.floor(ageH)} 小時未更新，排程可能沒有觸發。可到 Actions 手動執行，或設定 Cloudflare 觸發器。</div>`);
  }
  $("#date").innerHTML = INDEX.days.map((d) =>
    `<option value="${d.date}">${d.date}${d.limit_event ? " ⚠" : ""}${d.n_feasible ? " ★" + d.n_feasible : ""}</option>`).join("");
  const want = new URLSearchParams(location.search).get("date");
  $("#date").value = INDEX.days.some((d) => d.date === want) ? want : INDEX.latest;
  $("#date").onchange = () => loadDay($("#date").value);
  renderHistory();
  renderExpHist();
  renderLive();
  await loadDay($("#date").value);
}

async function loadDay(date) {
  REP = await getJSON(`data/daily/${date}.json`);
  history.replaceState(null, "", `?date=${date}`);
  $("#updated").textContent = `資料產生於 ${REP.generated.replace("T", " ")}`;
  initFees();
  renderSessions();
  renderTypeFilter();
  renderSignals();
  renderFwd();
  renderSeriesSelect();
  renderChain();
  renderExpiry();
}

function zoneTable(raw) {
  const lst = raw.map(adjItem);
  if (!lst.length) return `<div class="empty">當日此區無成交</div>`;
  return `<div class="wrap"><table><tr><th>履約價</th><th>買賣權</th><th>最後成交</th><th>最高</th><th>成交量</th>
    <th>到期價值</th><th>賣最後價損益(點)</th><th>賣最高價損益(點)</th><th>保證金(元)</th><th>每萬元賺(元)</th></tr>` +
    lst.map((x) => `<tr><td class="n">${fmt(x.k, 0)}</td><td>${x.side === "C" ? "Call" : "Put"}</td>
      <td class="n">${fmt(x.last)}</td><td class="n">${fmt(x.high)}</td><td class="n">${fmt(x.vol, 0)}</td>
      <td class="n">${fmt(x.payoff)}</td>
      <td class="n ${x.pnl_last > 0 ? "yes" : "no"}">${fmt(x.pnl_last, 2)}</td>
      <td class="n ${x.pnl_high > 0 ? "yes" : "no"}">${fmt(x.pnl_high, 2)}</td>
      <td class="n">${fmt(x.margin, 0)}</td><td class="n ${x.per_10k > 0 ? "yes" : "no"}">${fmt(x.per_10k, 2)}</td></tr>`).join("") + `</table></div>`;
}

function renderExpiry() {
  const es = REP.expiry_sell;
  const box = $("#expiryBox");
  if (!es) { box.innerHTML = `<div class="banner">${esc(REP.date)} 沒有序列在這天結算。</div>`; return; }
  if (es.error) { box.innerHTML = `<div class="banner alert">${esc(es.expiring.join("、"))} 結算日：${esc(es.error)}</div>`; return; }
  const t = es.taiex, b = es.bounds;
  box.innerHTML = `
    <div class="cards">
      <div class="card"><div class="mut">加權指數 · ${esc(es.date)}</div>
        <div class="big ${esc(es.state)}">${esc(es.state_label)}</div>
        <div>收 ${fmt(t.close, 2)}　<span class="${t.pct >= 0 ? "up" : "dn"}">${t.pct >= 0 ? "+" : ""}${esc(t.pct)}%</span></div>
        <div class="mut">前收 ${fmt(t.prev_close, 2)}　高 ${esc(t.high_pct)}%　低 ${esc(t.low_pct)}%　來源 ${esc(t.source || "-")}</div></div>
      <div class="card"><div class="mut">結算價必在區間內</div>
        <div class="big">${fmt(b.floor, 0)} ～ ${fmt(b.ceiling, 0)}</div>
        <div class="mut">零風險：Call K &gt; ${fmt(b.ceiling, 0)}、Put K &lt; ${fmt(b.floor, 0)}</div></div>
    </div>` + es.series.map((s) => `
    <h3>${esc(s.expiry)}　最後結算價 ${fmt(s.final_settle, 2)}${s.settle_approx ? "（以收盤近似）" : ""}</h3>
    ${(s.warnings || []).map((w) => `<div class="banner alert">⚠️ ${esc(w)}</div>`).join("")}
    <div class="wrap"><table><tr><th>區域</th><th>有成交履約價</th><th>總成交量</th><th>成交權利金(元)</th>
      <th>最高價格</th><th>賣出獲利檔數</th><th>虧損檔數</th><th>最佳損益(點)</th><th>最佳每萬元賺(元)</th></tr>
      ${[["zero", "零風險"], ["low", "低風險"]].map(([k, n]) => { const x = zoneStats(s.zones[k].map(adjItem)); return `<tr><td>${n}</td>
        <td class="n">${x.strikes}</td><td class="n">${fmt(x.volume, 0)}</td><td class="n">${fmt(x.premium_ntd, 0)}</td>
        <td class="n">${fmt(x.max_last)}</td><td class="n yes">${x.profitable}</td><td class="n no">${x.losing}</td>
        <td class="n">${fmt(x.best_pnl, 2)}</td><td class="n">${fmt(x.best_per_10k, 2)}</td></tr>`; }).join("")}</table></div>
    <div class="mut" style="margin:8px 0 4px">零風險區明細</div>${zoneTable(s.zones.zero)}
    <div class="mut" style="margin:8px 0 4px">低風險區明細</div>${zoneTable(s.zones.low)}`).join("");
}

async function renderLive() {
  let z;
  try { z = await getJSON("data/live.json"); } catch (e) { return; }
  const today = new Date(Date.now() + 8 * 3600e3).toISOString().slice(0, 10);
  if (z.date !== today) return;
  const b = z.bounds;
  $("#liveBox").innerHTML = `<div class="banner ${z.state !== "NORMAL" ? "alert" : ""}">
    <b>今日結算 ${esc(z.expiring.join("、"))}</b>｜${esc(z.time)} 加權 ${fmt(z.last, 2)}（${esc(z.pct)}%）${esc(z.state_label)}<br>
    盤中高 ${esc(z.high_pct ?? "-")}%／低 ${esc(z.low_pct ?? "-")}%　來源 ${esc(z.source || "-")}<br>
    零風險賣方：Call K &gt; ${fmt(b.ceiling, 0)}、Put K &lt; ${fmt(b.floor, 0)}　低風險：${esc(z.low_zone || "無")}<br>
    <span class="mut">${esc(z.margin_note || "")}</span><br>
    <span class="mut">盤中快照 ${z.snapshots.length} 次；請以即時報價確認買價是否高於成本（約 0.4 點）</span></div>`;
}

function renderExpHist() {
  const rows = INDEX.days.filter((d) => d.expiry);
  $("#expHist").innerHTML = `<tr><th>結算日</th><th>序列</th><th>指數漲跌%</th><th>狀態</th><th>零風險成交量</th>
    <th>零風險可獲利檔</th><th>低風險成交量</th><th>低風險虧損檔</th><th>最佳損益(點)</th></tr>` +
    (rows.length ? rows.map((d) => { const e = d.expiry; return `<tr class="click" data-date="${d.date}"><td>${d.date}</td>
      <td>${esc(e.expiring.join("、"))}</td><td class="n">${esc(e.pct)}</td><td class="${esc(e.state)}">${esc(STATUS[e.state] || e.state)}</td>
      <td class="n">${fmt(e.zero_volume, 0)}</td><td class="n">${e.zero_profitable}</td><td class="n">${fmt(e.low_volume, 0)}</td>
      <td class="n">${e.low_losing}</td><td class="n">${fmt(e.best_pnl, 2)}</td></tr>`; }).join("")
      : `<tr><td colspan="9" class="empty">尚無結算日資料</td></tr>`);
  document.querySelectorAll("#expHist tr.click").forEach((tr) => tr.onclick = () => {
    $("#date").value = tr.dataset.date; loadDay(tr.dataset.date);
  });
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
  const feas = $("#fFeas").checked, deep = $("#fDeep").checked, basis = $("#fBasis").checked;
  const qual = $("#fQual").checked, type = $("#fType").value;
  const riskless = (s) => s.riskless !== false;
  const nature = (s) => s.nature || (riskless(s) ? "套利" : "基差");
  const doubtful = (s) => s.quality === "存疑";
  const minEdge = REP.config.min_edge ?? 1;
  const above = (s) => adj(s).edge >= minEdge;
  const rows = REP.signals.filter((s) => above(s) && (!feas || s.feasible) && (!deep || s.deep_otm) &&
    (basis || riskless(s)) && (!qual || !doubtful(s)) && (!type || s.type === type));
  rows.sort((a, b) => (b.feasible - a.feasible) || (adj(b).edge - adj(a).edge));
  const hidden = [];
  const nb = REP.signals.filter((s) => above(s) && !riskless(s)).length;
  const nq = REP.signals.filter((s) => above(s) && doubtful(s)).length;
  if (!basis && nb) hidden.push(`${nb} 筆基差／跨週別`);
  if (qual && nq) hidden.push(`${nq} 筆報價存疑`);
  $("#sigTable").innerHTML = `<tr><th>類型</th><th>性質</th><th>週別</th><th>時段</th><th>組合</th><th>淨利(點)</th><th>淨利(元)</th>
    <th>成本(點)</th><th>報價</th><th>所需資金(元)</th><th>每萬元賺(元)</th><th>年化%</th><th>持有天數</th><th>可否成交</th><th>深價外</th><th>備註</th></tr>` +
    (rows.length ? rows.map((s) => `<tr class="click" data-exp="${esc(s.expiry)}" data-sess="${esc(s.session)}">
      <td>${esc(s.type)}</td><td class="${riskless(s) ? "yes" : "no"}">${esc(nature(s))}</td><td>${esc(s.expiry)}</td><td>${esc(s.session)}</td><td>${esc(s.legs)}</td>
      <td class="n">${fmt(adj(s).edge, 2)}</td><td class="n">${fmt(adj(s).ntd, 0)}</td><td class="n">${fmt(adj(s).cost, 2)}</td>
      <td class="${doubtful(s) ? "no" : "yes"}" title="${esc((s.quality_reasons || []).join("、"))}">${doubtful(s) ? "存疑" : (s.quality ? "可靠" : "-")}</td>
      <td class="n">${s.capital_ntd === undefined ? "-" : fmt(s.capital_ntd, 0)}</td>
      <td class="n">${adj(s).per === null && s.capital_ntd !== undefined ? "免資金" : fmt(adj(s).per, 1)}</td>
      <td class="n">${fmt(adj(s).annual, 1)}</td><td class="n">${fmt(s.hold_days, 0)}</td>
      <td class="${s.feasible ? "yes" : "no"}">${s.feasible ? "可" : "需排隊"}</td><td>${s.deep_otm ? "★" : ""}</td>
      <td class="mut">${doubtful(s) ? `<span class="no">${esc(s.quality_reasons.join("、"))}</span>；` : ""}${esc(s.note)}</td></tr>`).join("")
      : `<tr><td colspan="16" class="empty">以目前手續費（選擇權 ${FEE.opt}、小台 ${FEE.fut} 元）扣除成本後，無符合條件的訊號</td></tr>`) +
    (hidden.length ? `<tr><td colspan="16" class="mut">已隱藏 ${hidden.join("、")}，可用上方勾選切換</td></tr>` : "");
  document.querySelectorAll("#sigTable tr.click").forEach((tr) => tr.onclick = () => {
    $("#series").value = tr.dataset.exp + "|" + tr.dataset.sess;
    switchTab("chain"); renderChain();
  });
}

function renderFwd() {
  const md = (d) => (d ? d.slice(5).replace("-", "/") : "-");
  $("#fwdTable").innerHTML = `<tr><th>序列</th><th>時段</th><th>結算日</th><th>對應期貨</th><th>期貨結算</th><th>對沖</th>
    <th>市場狀態</th><th>期貨價（買賣中價）</th><th>隱含價</th><th>差距</th><th>履約價數</th></tr>` +
    REP.series.map((r) => `<tr><td>${esc(r.expiry)}</td><td>${esc(r.session)}</td><td>${md(r.opt_settle)}</td>
      <td>${esc(r.hedge_product || "TX")} ${esc(r.hedge)}</td><td>${md(r.hedge_settle)}</td>
      <td class="${r.matched ? "yes" : "no"}">${r.matched ? "同日結算" : `差 ${r.basis_days} 天`}</td>
      <td class="${esc(r.status)}">${esc(r.status_label)}</td>
      <td class="n">${fmt(r.futures, 1)}<div class="mut">${r.fut_bid !== undefined ? `買 ${fmt(r.fut_bid, 0)}／賣 ${fmt(r.fut_ask, 0)}・量 ${fmt(r.fut_vol, 0)}` : ""}</div></td>
      <td class="n">${fmt(r.implied_fwd)}</td><td class="n">${fmt(r.gap)}</td><td class="n">${r.chain_pruned ? "-" : r.chain.length}</td></tr>`).join("") +
    (REP.expired_today || []).map((r) => `<tr class="mut"><td>${esc(r.expiry)}</td><td>${esc(r.session)}</td><td>${md(r.opt_settle)}</td>
      <td colspan="8">今日 13:30 已結算，不列入掃描</td></tr>`).join("");
  $("#settleRule").textContent = REP.settle_rule ? "結算規則：" + REP.settle_rule + "。結算標的是加權指數，不是期貨。" : "";
}

function renderSeriesSelect() {
  $("#series").innerHTML = REP.series.map((s) => `<option value="${esc(s.expiry)}|${esc(s.session)}">${esc(s.expiry)} · ${esc(s.session)} · ${esc((s.opt_settle || "").slice(5))} 結算 → TX ${esc(s.hedge)}</option>`).join("");
}

function renderChain() {
  const [exp, sess] = ($("#series").value || "").split("|");
  const s = REP.series.find((x) => x.expiry === exp && x.session === sess);
  if (s && s.chain_pruned) {
    $("#chainTable").innerHTML = `<tr><td class="empty">較舊交易日只保留摘要與訊號；完整選擇權鏈可用 workflow 的 rebuild 從封存重算。</td></tr>`;
    return;
  }
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
  $("#histTable").innerHTML = `<tr><th>交易日</th><th>狀態</th><th>漲跌%</th><th>可成交套利<span class="mut">（預設手續費）</span></th><th>報價存疑</th><th>跨週別</th><th>基差交易</th><th>最佳訊號</th></tr>` +
    INDEX.days.map((d) => {
      const st = Object.entries(d.status).map(([k, v]) => `<span class="${v}">${k}:${STATUS[v] || v}</span>`).join(" ");
      const pct = Object.values(d.pct || {}).map((x) => fmt(x, 2)).join(" / ");
      const best = d.best ? `${esc(d.best.type)} ${fmt(d.best.edge_pts, 2)} 點` : "-";
      return `<tr class="click" data-date="${d.date}"><td>${d.date}${d.limit_event ? " ⚠" : ""}</td><td>${st}</td>
        <td class="n">${pct}</td><td class="n">${d.n_feasible}</td><td class="n">${d.n_doubtful ?? "-"}</td>
        <td class="n">${d.n_calendar ?? "-"}</td><td class="n">${d.n_basis ?? "-"}</td><td>${best}</td></tr>`;
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
["#fFeas", "#fDeep", "#fBasis", "#fQual", "#fType"].forEach((s) => ($(s).onchange = renderSignals));
$("#series").onchange = renderChain;
["#feeOpt", "#feeFut"].forEach((s) => ($(s).onchange = onFee));
$("#fQuoted").onchange = renderChain;
init();
