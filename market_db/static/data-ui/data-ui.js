"use strict";

const page = document.querySelector("#page");
const title = document.querySelector("#page-title");
const asOf = document.querySelector("#as-of");
const timezoneSelect = document.querySelector("#timezone-select");
const healthDot = document.querySelector("#health-dot");
const healthLabel = document.querySelector("#health-label");
const state = {
  timezone: "Asia/Tokyo",
  chart: null,
  mainSeries: null,
  volumeSeries: null,
  markerPlugin: null,
  chartRows: [],
  chartMarks: [],
  detail: null,
  period: null,
  eligibility: "eligible",
  loadingOlder: false,
  chartReady: false,
};

const VIEW_TITLES = {
  overview: "データ概要",
  inventory: "データ在庫",
  catalog: "商品・データ辞書",
  series: "系列チャート",
  quality: "品質・鮮度",
  "daily-close": "C2日次終値",
  runs: "取込・由来",
  operations: "バックアップ・ストレージ",
};
const ROLE_LABELS = {
  CANONICAL_1H: "正式 1H",
  DERIVED_4H: "派生 4H",
  DERIVED_1D_RISK: "派生 1D",
  TOTAL_RETURN_DAILY: "Total Return 日次",
  RAW_ARCHIVE: "Raw / Archive",
  REFERENCE_METADATA: "Reference / Metadata",
  UNKNOWN_ROLE: "未分類",
};
const CATEGORY_LABELS = {
  equity_reit: "株式・REIT",
  bond_credit: "債券・クレジット",
  commodity: "コモディティ",
  gold: "金",
  fx: "外国為替",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat("ja-JP").format(number) : escapeHtml(value);
}

function formatBytes(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let selected = number;
  let index = 0;
  while (selected >= 1024 && index < units.length - 1) { selected /= 1024; index += 1; }
  return `${selected.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatTime(value, includeSeconds = false) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return escapeHtml(value);
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: state.timezone,
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
    second: includeSeconds ? "2-digit" : undefined,
    hour12: false,
  }).format(parsed);
}

function statusClass(value) {
  return String(value || "NOT_EVALUATED").toLowerCase().replaceAll("_", "-");
}

function badge(value) {
  const selected = value || "NOT_EVALUATED";
  return `<span class="status-badge ${statusClass(selected)}">${escapeHtml(selected)}</span>`;
}

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", credentials: "same-origin", ...options });
  let body = null;
  try { body = await response.json(); } catch (_) { body = null; }
  if (!response.ok) {
    const code = body?.error_code || `HTTP_${response.status}`;
    throw new Error(code);
  }
  return body;
}

function setAsOf(value) {
  asOf.textContent = value ? `as of ${formatTime(value, true)}` : "as of —";
}

function showLoading(label = "データを読み込んでいます") {
  page.innerHTML = `<section class="loading-card"><span class="spinner"></span>${escapeHtml(label)}</section>`;
}

function showError(error) {
  page.innerHTML = `<section class="error-panel"><strong>データを表示できません</strong><span>${escapeHtml(error.message || error)}</span><p>DB4 Read APIとPostgreSQLの状態を確認してください。</p></section>`;
}

function currentRoute() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  return { view: parts[1] || "overview", id: parts[2] || null };
}

function setNavigation(view) {
  document.querySelectorAll(".sidebar nav a").forEach(link => {
    link.classList.toggle("active", link.dataset.view === view);
    if (link.dataset.view === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  title.textContent = VIEW_TITLES[view] || "データ管理";
}

async function readHealth() {
  try {
    const payload = await api("/health");
    const pass = payload.status === "PASS";
    healthDot.className = `status-dot ${pass ? "pass" : "fail"}`;
    healthLabel.textContent = pass ? "DB接続正常" : "DB接続異常";
  } catch (_) {
    healthDot.className = "status-dot fail";
    healthLabel.textContent = "DB接続不可";
  }
}

function kpi(label, value, note, attention = false) {
  return `<article class="kpi${attention ? " attention" : ""}"><span class="label">${escapeHtml(label)}</span><strong>${formatNumber(value)}</strong><small>${escapeHtml(note)}</small></article>`;
}

function barList(values, labels = {}) {
  const entries = Object.entries(values || {});
  const maximum = Math.max(...entries.map(([, item]) => Number(item?.row_count ?? item ?? 0)), 1);
  return `<div class="bar-list">${entries.map(([key, item]) => {
    const value = Number(item?.row_count ?? item ?? 0);
    const name = labels[key] || key;
    return `<div class="bar-row"><span>${escapeHtml(name)}</span><progress class="bar-track" max="${maximum}" value="${Math.max(0, value)}">${formatNumber(value)}</progress><strong>${formatNumber(value)}</strong></div>`;
  }).join("")}</div>`;
}

function qualityTable(rows, maximum = 20) {
  if (!rows?.length) return `<div class="empty">対象系列はありません。</div>`;
  return `<div class="table-panel"><div class="table-scroll"><table><thead><tr><th>状態</th><th>銘柄</th><th>カテゴリ</th><th>現在blocker</th><th>カバレッジ</th><th>鮮度</th><th>欠損</th><th>セッション外</th><th>最新 complete</th></tr></thead><tbody>${rows.slice(0, maximum).map(row => `<tr>
    <td>${badge(row.status)}</td><td><strong>${escapeHtml(row.symbol)}</strong></td><td>${escapeHtml(row.category || "—")}</td>
    <td class="numeric">${formatNumber(row.current_blocker_count || 0)}</td><td>${badge(row.coverage_status)}</td><td>${badge(row.freshness_status)}</td><td class="numeric">${formatNumber(row.missing_rows)}</td>
    <td class="numeric">${formatNumber(row.out_of_session_rows)}</td><td>${formatTime(row.latest_complete_time_utc)}</td>
  </tr>`).join("")}</tbody></table></div></div>`;
}

function overviewGuardrailTable(rows) {
  if (!rows?.length) return `<div class="empty">正式1H系列はありません。</div>`;
  return `<div class="table-panel"><div class="table-scroll"><table><thead><tr><th>状態</th><th>銘柄</th><th>カテゴリ</th><th>品質</th><th>鮮度</th><th>最新 complete</th><th></th></tr></thead><tbody>${rows.map(row => `<tr>
    <td>${badge(row.status)}</td><td><strong>${escapeHtml(row.symbol)}</strong></td><td>${escapeHtml(row.category || "—")}</td>
    <td>${badge(row.quality_status)}</td><td>${badge(row.freshness_status)}</td><td>${formatTime(row.latest_complete_time_utc)}</td>
    <td><a class="link-button" href="/ui/series/${encodeURIComponent(row.series_id)}">詳細</a></td>
  </tr>`).join("")}</tbody></table></div></div>`;
}

async function renderOverview() {
  showLoading();
  const overviewResponse = await api("/api/v1/ui/overview");
  const data = overviewResponse.data;
  setAsOf(data.generated_at_utc);
  const cards = data.cards;
  page.innerHTML = `
    <section class="kpi-grid">
      ${kpi("有効データセット", cards.active_dataset_count, "catalog active")}
      ${kpi("正式系列の銘柄", cards.canonical_instrument_count, "canonical 1H")}
      ${kpi("正式 1H", cards.canonical_1h_rows, "complete + accepted")}
      ${kpi("派生 4H", cards.derived_4h_rows, "canonical derivation")}
      ${kpi("派生 1D", cards.derived_1d_rows, "risk layer")}
      ${kpi("要確認系列", cards.attention_series_count, "FAIL / STALE / WARN", cards.attention_series_count > 0)}
    </section>
    <section class="dashboard-grid">
      <article class="panel"><div class="panel-head"><div><span class="section-tag">DATA LAYERS</span><h2>正式・派生データ量</h2></div><small>層をまたいで合算しません</small></div>${barList(data.role_totals, ROLE_LABELS)}</article>
      <article class="panel"><div class="panel-head"><div><span class="section-tag">CATEGORIES</span><h2>正式系列の構成</h2></div><small>系列数</small></div>${barList(data.category_totals)}</article>
    </section>
    <div class="section-head"><div><span class="section-tag">CURRENT GUARDRAILS</span><h2>現在の品質・鮮度</h2><p>過去の監査eventとは分けて表示しています。</p></div><a class="link-button" href="/ui/quality">すべて確認</a></div>
    ${overviewGuardrailTable(data.canonical_guardrails)}
    <div class="section-head"><div><span class="section-tag">LATEST INGESTION</span><h2>最新の取込run</h2></div></div>
    <article class="panel"><div class="metric-strip">
      <span class="metric-chip"><small>status</small><strong>${badge(data.latest_run?.status)}</strong></span>
      <span class="metric-chip"><small>run id</small><strong>${escapeHtml(data.latest_run?.ingestion_run_id ?? "—")}</strong></span>
      <span class="metric-chip"><small>finished</small><strong>${formatTime(data.latest_run?.finished_at_utc)}</strong></span>
      <span class="metric-chip"><small>revision</small><strong>${formatNumber(data.latest_run?.revision_rows)}</strong></span>
      <span class="metric-chip"><small>rejected</small><strong>${formatNumber(data.latest_run?.rejected_rows)}</strong></span>
    </div></article>`;
}

function inventoryTable(rows) {
  if (!rows.length) return `<div class="empty">条件に一致する系列はありません。</div>`;
  return `<div class="table-panel"><div class="table-scroll"><table><thead><tr><th>状態</th><th>役割</th><th>銘柄</th><th>カテゴリ</th><th>足</th><th>価格基準</th><th>行数</th><th>最古</th><th>最新 complete</th><th></th></tr></thead><tbody>${rows.map(row => `<tr>
    <td>${badge(row.status)}</td><td>${badge(row.role)}</td><td><strong>${escapeHtml(row.symbol)}</strong><br><span class="mono subtle">${escapeHtml(row.source_dataset_id)}</span></td>
    <td>${escapeHtml(row.category)}</td><td>${escapeHtml(row.layer_label)}</td><td class="mono">${escapeHtml(row.price_basis)}</td><td class="numeric">${formatNumber(row.row_count)}</td>
    <td>${formatTime(row.min_time_utc)}</td><td>${formatTime(row.latest_complete_time_utc || row.max_time_utc)}</td>
    <td>${row.chart_available ? `<a class="link-button" href="/ui/series/${encodeURIComponent(row.series_id)}">チャート</a>` : `<span class="subtle">監査のみ</span>`}</td>
  </tr>`).join("")}</tbody></table></div></div>`;
}

async function renderInventory() {
  showLoading();
  let offset = 0;
  const pageSize = 50;
  let requestController = null;
  let debounceTimer = null;
  page.innerHTML = `
    <div class="section-head"><div><span class="section-tag">INVENTORY</span><h2>管理データを検索</h2><p>正式、派生、total return、raw監査を別系列として表示します。</p></div></div>
    <section class="filters" aria-label="在庫フィルタ">
      <input class="filter-input" id="filter-symbol" placeholder="銘柄またはsymbol" aria-label="銘柄">
      <select class="control-select" id="filter-role" aria-label="役割"><option value="">すべての役割</option>${Object.entries(ROLE_LABELS).map(([value, label]) => `<option value="${value}">${escapeHtml(label)}</option>`).join("")}</select>
      <select class="control-select" id="filter-layer" aria-label="足"><option value="">すべての足</option><option>1H</option><option>4H</option><option>1D</option><option>1D-TR</option></select>
      <select class="control-select" id="filter-status" aria-label="状態"><option value="">すべての状態</option><option>FAIL</option><option>STALE</option><option>WARN</option><option>NOT_EVALUATED</option><option>PASS</option></select>
      <select class="control-select" id="filter-canonical" aria-label="正式系列"><option value="false">全データ</option><option value="true">正式・派生のみ</option></select>
      <button class="button" id="filter-apply" type="button">適用</button>
    </section>
    <div class="result-summary"><span id="inventory-summary">検索しています…</span><div class="pagination-controls"><button class="button ghost" id="inventory-prev" type="button">前へ</button><span id="inventory-page">—</span><button class="button ghost" id="inventory-next" type="button">次へ</button></div></div>
    <div id="inventory-results" class="loading-card"><span class="spinner"></span>在庫を取得しています</div>`;

  const load = async () => {
    requestController?.abort();
    requestController = new AbortController();
    const params = new URLSearchParams({ limit: String(pageSize), offset: String(offset) });
    const symbol = document.querySelector("#filter-symbol").value.trim();
    const role = document.querySelector("#filter-role").value;
    const layer = document.querySelector("#filter-layer").value;
    const status = document.querySelector("#filter-status").value;
    const canonical = document.querySelector("#filter-canonical").value;
    if (symbol) params.set("symbol", symbol);
    if (role) params.set("role", role);
    if (layer) params.set("layer", layer);
    if (status) params.set("status", status);
    params.set("canonical_only", canonical);
    const response = await api(`/api/v1/ui/series?${params}`, { signal: requestController.signal });
    setAsOf(response.generated_at_utc);
    document.querySelector("#inventory-summary").textContent = `${formatNumber(response.paging.total)}系列中 ${formatNumber(response.data.length)}件を表示`;
    document.querySelector("#inventory-page").textContent = `${formatNumber(response.paging.offset + 1)}–${formatNumber(response.paging.offset + response.data.length)} / ${formatNumber(response.paging.total)}`;
    document.querySelector("#inventory-prev").disabled = response.paging.offset === 0;
    document.querySelector("#inventory-next").disabled = !response.paging.has_more;
    document.querySelector("#inventory-results").outerHTML = `<div id="inventory-results">${inventoryTable(response.data)}</div>`;
  };
  const safelyLoad = () => load().catch(error => { if (error.name !== "AbortError") showError(error); });
  const resetAndLoad = () => { offset = 0; safelyLoad(); };
  const scheduleLoad = () => {
    window.clearTimeout(debounceTimer);
    debounceTimer = window.setTimeout(resetAndLoad, 250);
  };
  document.querySelector("#filter-apply").addEventListener("click", resetAndLoad);
  document.querySelector("#filter-symbol").addEventListener("keydown", event => {
    if (event.key === "Enter") resetAndLoad();
  });
  document.querySelector("#filter-symbol").addEventListener("input", scheduleLoad);
  ["#filter-role", "#filter-layer", "#filter-status", "#filter-canonical"].forEach(selector => {
    document.querySelector(selector).addEventListener("change", scheduleLoad);
  });
  document.querySelector("#inventory-prev").addEventListener("click", () => { offset = Math.max(0, offset - pageSize); safelyLoad(); });
  document.querySelector("#inventory-next").addEventListener("click", () => { offset += pageSize; safelyLoad(); });
  await load();
}

function officialSourceLinks(sources) {
  return (sources || []).map(source => `<a class="official-link" href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.label)} ↗</a>`).join("");
}

function aiExplanationPrompt(instrumentKey) {
  return `saxo_db MCPを使って instrument_key=${instrumentKey} について、(1)どのような商品か、(2)DBの価格系列は何を表すか、(3)管理中の足と期間、(4)最新時刻と品質状態、(5)データ利用上の注意を初心者向けの日本語で説明してください。公式情報リンクも示してください。投資助言、売買判断、将来予測はしないでください。`;
}

async function copyAiPrompt(instrumentKey, button) {
  try {
    await navigator.clipboard.writeText(aiExplanationPrompt(instrumentKey));
    button.textContent = "質問文をコピーしました";
    setTimeout(() => { button.textContent = "AIへの質問文をコピー"; }, 1800);
  } catch (_) {
    button.textContent = "コピーできませんでした";
  }
}

function productReferencePanel(product, compact = false) {
  if (!product) return `<div class="empty">この系列の商品説明は登録されていません。</div>`;
  const instrument = product.managed_instrument || {};
  const cautions = (product.data_cautions_ja || []).map(item => `<li>${escapeHtml(item)}</li>`).join("");
  return `<article class="product-reference${compact ? " compact" : ""}">
    <div class="product-reference-head">
      <div><span class="section-tag">${escapeHtml(product.instrument_type_ja)}</span><h3>${escapeHtml(product.display_name_ja)}</h3><p class="product-symbol">${escapeHtml(product.short_name)} · <span class="mono">${escapeHtml(product.instrument_key)}</span></p></div>
      <span class="category-pill">${escapeHtml(CATEGORY_LABELS[product.category] || product.category)}</span>
    </div>
    <p class="product-summary">${escapeHtml(product.summary_ja)}</p>
    <dl class="definition-list">
      <div><dt>主なエクスポージャー</dt><dd>${escapeHtml(product.exposure_ja)}</dd></div>
      <div><dt>参照指数・基準</dt><dd>${escapeHtml(product.benchmark_or_reference)}</dd></div>
      <div><dt>このDBの値の意味</dt><dd>${escapeHtml(product.quote_interpretation_ja)}</dd></div>
      ${instrument.asset_type ? `<div><dt>DB登録</dt><dd>${escapeHtml(instrument.asset_type)} / ${escapeHtml(instrument.provider)} ${escapeHtml(instrument.environment)} / ${escapeHtml(instrument.currency)}</dd></div>` : ""}
    </dl>
    <div class="caution-box"><strong>データ利用上の注意</strong><ul>${cautions}</ul></div>
    <div class="product-actions">${officialSourceLinks(product.official_sources)}<button class="button secondary copy-ai-prompt" data-instrument-key="${escapeHtml(product.instrument_key)}" type="button">AIへの質問文をコピー</button></div>
  </article>`;
}

async function renderCatalog() {
  showLoading("商品説明と管理系列を読み込んでいます");
  const response = await api("/api/v1/ui/instruments");
  const catalog = response.data;
  let rows = catalog.instruments || [];
  setAsOf(response.generated_at_utc);
  page.innerHTML = `
    <section class="catalog-intro">
      <div><span class="section-tag">HUMAN-READABLE DATA DICTIONARY</span><h2>商品と時系列データの意味を調べる</h2><p>${escapeHtml(catalog.scope_note_ja)}</p></div>
      <div class="mcp-note"><strong>AIで説明する場合</strong><p>質問文をコピーし、ChatGPT/Codexからローカルの <span class="mono">saxo_db</span> MCPを使って質問します。saxo_db側にOpenAI APIキーは保存しません。</p></div>
    </section>
    <section class="catalog-filters" aria-label="商品辞書フィルタ">
      <input class="filter-input" id="catalog-search" placeholder="SPY、商品名、説明を検索" aria-label="商品辞書を検索">
      <select class="control-select" id="catalog-category" aria-label="カテゴリ"><option value="">すべてのカテゴリ</option>${Object.entries(CATEGORY_LABELS).map(([value, label]) => `<option value="${value}">${escapeHtml(label)}</option>`).join("")}</select>
      <span id="catalog-count" class="subtle"></span>
    </section>
    <section id="catalog-results" class="catalog-grid"></section>`;
  const draw = () => {
    const query = document.querySelector("#catalog-search").value.trim().toLocaleLowerCase("ja");
    const category = document.querySelector("#catalog-category").value;
    const selected = rows.filter(item => {
      const haystack = [item.instrument_key, item.short_name, item.display_name_ja, item.summary_ja, item.exposure_ja, item.benchmark_or_reference].join(" ").toLocaleLowerCase("ja");
      return (!query || haystack.includes(query)) && (!category || item.category === category);
    });
    document.querySelector("#catalog-count").textContent = `${selected.length} / ${rows.length} 商品`;
    document.querySelector("#catalog-results").innerHTML = selected.map(item => {
      const managed = item.managed_series || {};
      const seriesLink = managed.default_series_id ? `<a class="link-button" href="/ui/series/${encodeURIComponent(managed.default_series_id)}">管理系列・チャート</a>` : `<span class="subtle">表示可能な系列なし</span>`;
      return `<div class="catalog-entry">${productReferencePanel(item, true)}<div class="managed-summary"><span><small>管理系列</small><strong>${formatNumber(managed.series_count)}</strong></span><span><small>足</small><strong>${escapeHtml((managed.layers || []).join(" / ") || "—")}</strong></span><span><small>最新 complete</small><strong>${formatTime(managed.latest_complete_time_utc)}</strong></span>${seriesLink}</div></div>`;
    }).join("") || `<div class="empty">条件に一致する商品はありません。</div>`;
    document.querySelectorAll(".copy-ai-prompt").forEach(button => button.addEventListener("click", () => copyAiPrompt(button.dataset.instrumentKey, button)));
  };
  document.querySelector("#catalog-search").addEventListener("input", draw);
  document.querySelector("#catalog-category").addEventListener("change", draw);
  draw();
}

function chartTimestamp(row) {
  const source = row.time_utc || (row.session_date ? `${row.session_date}T00:00:00Z` : null);
  const time = Date.parse(source) / 1000;
  if (!Number.isFinite(time)) throw new Error("INVALID_CHART_TIME");
  return time;
}

function numeric(value, field) {
  const selected = Number(value);
  if (!Number.isFinite(selected)) throw new Error(`INVALID_${field.toUpperCase()}`);
  return selected;
}

function toChartPoint(row, kind) {
  const time = chartTimestamp(row);
  if (kind === "line") return { time, value: numeric(row.value, "value"), source: row };
  const open = numeric(row.open, "open");
  const high = numeric(row.high, "high");
  const low = numeric(row.low, "low");
  const close = numeric(row.close, "close");
  if (high < Math.max(open, close) || low > Math.min(open, close) || high < low) throw new Error("OHLC_INVARIANT_FAILED");
  return { time, open, high, low, close, source: row };
}

function addSeries(chart, type, options) {
  if (chart.addSeries && window.LightweightCharts[type]) return chart.addSeries(window.LightweightCharts[type], options);
  const legacy = { CandlestickSeries: "addCandlestickSeries", LineSeries: "addLineSeries", HistogramSeries: "addHistogramSeries" }[type];
  return chart[legacy](options);
}

function disposeChart() {
  if (state.chart) state.chart.remove();
  state.chart = null;
  state.mainSeries = null;
  state.volumeSeries = null;
  state.markerPlugin = null;
  state.chartReady = false;
}

function installMarkers() {
  if (!state.mainSeries || !state.chartMarks.length) return;
  const markers = state.chartMarks.filter(mark => mark.time_utc).map(mark => ({
    time: Date.parse(mark.time_utc) / 1000,
    position: "aboveBar",
    color: mark.severity === "CRITICAL" || mark.severity === "ERROR" ? "#a33b35" : "#bd7410",
    shape: "circle",
    text: String(mark.rule_id || "quality").slice(0, 28),
  }));
  if (!markers.length) return;
  if (window.LightweightCharts.createSeriesMarkers) state.markerPlugin = window.LightweightCharts.createSeriesMarkers(state.mainSeries, markers);
  else if (state.mainSeries.setMarkers) state.mainSeries.setMarkers(markers);
}

function drawChart(kind) {
  disposeChart();
  const container = document.querySelector("#chart");
  if (!container || !window.LightweightCharts) throw new Error("TRADINGVIEW_LIBRARY_UNAVAILABLE");
  state.chart = window.LightweightCharts.createChart(container, {
    autoSize: true,
    attributionLogo: true,
    layout: { background: { type: "solid", color: "#fbfcf8" }, textColor: "#53645c", fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif", fontSize: 11 },
    grid: { vertLines: { color: "#edf0eb" }, horzLines: { color: "#edf0eb" } },
    rightPriceScale: { borderColor: "#dfe3dc" },
    timeScale: { borderColor: "#dfe3dc", timeVisible: kind !== "line", secondsVisible: false, rightOffset: 4 },
    crosshair: { mode: window.LightweightCharts.CrosshairMode?.Normal ?? 0 },
  });
  const points = state.chartRows.map(row => toChartPoint(row, kind));
  const publicPoints = points.map(({ source, ...point }) => point);
  state.mainSeries = addSeries(state.chart, kind === "line" ? "LineSeries" : "CandlestickSeries", kind === "line" ? {
    color: "#0c6b51", lineWidth: 2, priceLineVisible: false,
  } : {
    upColor: "#167958", downColor: "#b94a42", borderUpColor: "#167958", borderDownColor: "#b94a42", wickUpColor: "#167958", wickDownColor: "#b94a42",
  });
  state.mainSeries.setData(publicPoints);
  const volume = points.filter(point => point.source.volume !== null && point.source.volume !== undefined).map(point => ({
    time: point.time,
    value: Number(point.source.volume),
    color: point.close !== undefined && point.close < point.open ? "rgba(185,74,66,.35)" : "rgba(22,121,88,.32)",
  })).filter(point => Number.isFinite(point.value));
  if (volume.length) {
    state.volumeSeries = addSeries(state.chart, "HistogramSeries", { priceFormat: { type: "volume" }, priceScaleId: "", lastValueVisible: false, priceLineVisible: false });
    state.volumeSeries.priceScale().applyOptions({ scaleMargins: { top: .82, bottom: 0 } });
    state.volumeSeries.setData(volume);
  }
  installMarkers();
  const tooltip = document.querySelector("#chart-tooltip");
  state.chart.subscribeCrosshairMove(param => {
    const item = param.seriesData?.get(state.mainSeries);
    if (!item || !tooltip) return;
    const time = new Date(Number(item.time) * 1000).toISOString();
    tooltip.textContent = kind === "line" ? `${time}  VALUE ${item.value}` : `${time}  O ${item.open}  H ${item.high}  L ${item.low}  C ${item.close}`;
  });
  state.chart.timeScale().fitContent();
  state.chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
    if (state.chartReady && range && range.from < 8) loadOlder().catch(error => setChartMessage(error.message, true));
  });
  window.setTimeout(() => { state.chartReady = true; }, 650);
}

function rangeFor(period, end, layer) {
  const selected = new Date(end);
  const days = { "1M": 31, "3M": 93, "6M": 186, "1Y": 366, "3Y": 1096 }[period];
  if (period === "ALL") return new Date(state.detail.series.min_time_utc);
  selected.setUTCDate(selected.getUTCDate() - days);
  return selected;
}

function olderWindowDays(layer) {
  if (layer === "1H") return 45;
  if (layer === "4H") return 180;
  return 1826;
}

function setChartMessage(message, warning = false) {
  const element = document.querySelector("#chart-message");
  if (!element) return;
  element.className = `chart-message${warning ? " warning" : ""}`;
  element.textContent = message;
}

function mergeRows(existing, incoming) {
  const combined = [...existing, ...incoming].sort((a, b) => chartTimestamp(a) - chartTimestamp(b));
  return validateChartRows(combined);
}

function validateChartRows(rows) {
  let previous = -Infinity;
  const timestamps = new Set();
  rows.forEach(row => {
    const selected = chartTimestamp(row);
    if (timestamps.has(selected)) throw new Error("DUPLICATE_CHART_TIME");
    if (selected < previous) throw new Error("NON_ASCENDING_CHART_TIME");
    timestamps.add(selected);
    previous = selected;
  });
  return rows;
}

function updateDataTable() {
  const target = document.querySelector("#tab-data");
  if (!target) return;
  const rows = state.chartRows.slice(-100).reverse();
  target.innerHTML = rows.length ? `<div class="table-panel"><div class="table-scroll"><table><thead><tr><th>時刻</th><th>Open / Value</th><th>High</th><th>Low</th><th>Close</th><th>Volume</th><th>品質</th></tr></thead><tbody>${rows.map(row => `<tr><td>${formatTime(row.time_utc || `${row.session_date}T00:00:00Z`)}</td><td class="numeric">${escapeHtml(row.open ?? row.value ?? "—")}</td><td class="numeric">${escapeHtml(row.high ?? "—")}</td><td class="numeric">${escapeHtml(row.low ?? "—")}</td><td class="numeric">${escapeHtml(row.close ?? "—")}</td><td class="numeric">${escapeHtml(row.volume ?? "—")}</td><td>${badge(row.quality_status)}</td></tr>`).join("")}</tbody></table></div></div>` : `<div class="empty">表示可能なbarがありません。</div>`;
}

async function fetchChart(start, end, append = false) {
  const id = state.detail.series.series_id;
  const params = new URLSearchParams({
    series_id: id,
    start: start.toISOString(),
    end: end.toISOString(),
    limit: "1000",
    eligibility: state.eligibility,
  });
  const [bars, marks] = await Promise.all([
    api(`/api/v1/ui/chart-bars?${params}`),
    api(`/api/v1/ui/chart-marks?series_id=${encodeURIComponent(id)}&start=${encodeURIComponent(start.toISOString())}&end=${encodeURIComponent(end.toISOString())}`),
  ]);
  const incoming = validateChartRows(bars.data);
  state.chartRows = append ? mergeRows(state.chartRows, incoming) : incoming;
  state.chartMarks = append ? [...state.chartMarks, ...marks.data] : marks.data;
  if (!append) drawChart(bars.series_kind);
  else {
    const kind = state.detail.series.chart_kind;
    const publicPoints = state.chartRows.map(row => {
      const { source, ...point } = toChartPoint(row, kind);
      return point;
    });
    state.mainSeries.setData(publicPoints);
  }
  updateDataTable();
  const warning = bars.warnings?.length > 0;
  setChartMessage(
    warning ? `${bars.warnings.join(" / ")} — ${formatNumber(state.chartRows.length)}本を表示` : `${formatNumber(state.chartRows.length)}本を表示。左端へ移動すると過去を追加取得します。`,
    warning,
  );
  return bars;
}

async function loadOlder() {
  if (state.loadingOlder || !state.chartRows.length) return;
  const earliest = new Date((state.chartRows[0].time_utc || `${state.chartRows[0].session_date}T00:00:00Z`));
  const minimum = new Date(state.detail.series.min_time_utc);
  if (earliest <= minimum) return setChartMessage("全期間の先頭まで読み込みました。", false);
  state.loadingOlder = true;
  const start = new Date(earliest);
  start.setUTCDate(start.getUTCDate() - olderWindowDays(state.detail.series.layer_label));
  try { await fetchChart(start < minimum ? minimum : start, earliest, true); }
  finally { state.loadingOlder = false; }
}

function coveragePanel(detail) {
  const row = detail.coverage?.[0];
  if (!row) return `<div class="empty">この系列にはcoverage集計がありません。</div>`;
  return `<article class="panel"><div class="metric-strip">
    ${["expected_rows","actual_rows","complete_rows","missing_rows","duplicate_rows","incomplete_rows","out_of_session_rows"].map(key => `<span class="metric-chip"><small>${escapeHtml(key)}</small><strong>${formatNumber(row[key])}</strong></span>`).join("")}
    <span class="metric-chip"><small>calendar</small><strong>${escapeHtml(row.calendar_verification_status)}</strong></span>
  </div></article>`;
}

function lineagePanel(detail) {
  if (!detail.lineage?.length) return `<div class="empty">個別lineageはありません。</div>`;
  return `<div class="table-panel"><div class="table-scroll"><table><thead><tr><th>source file</th><th>run</th><th>raw</th><th>curated</th><th>derived</th></tr></thead><tbody>${detail.lineage.map(row => `<tr><td class="mono">${escapeHtml(row.relative_path)}</td><td>${formatNumber(row.ingestion_run_id)}</td><td class="numeric">${formatNumber(row.raw_rows)}</td><td class="numeric">${formatNumber(row.curated_rows)}</td><td class="numeric">${formatNumber(row.derived_rows)}</td></tr>`).join("")}</tbody></table></div></div>`;
}

function installTabs() {
  document.querySelectorAll(".tab-button").forEach(button => button.addEventListener("click", () => {
    document.querySelectorAll(".tab-button").forEach(item => {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
      item.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll(".tab-panel").forEach(panel => {
      const active = panel.id === `tab-${button.dataset.tab}`;
      panel.classList.toggle("active", active);
      panel.hidden = !active;
    });
  }));
}

async function renderSeries(selectedId) {
  showLoading("系列情報とチャートを準備しています");
  if (!selectedId) {
    const candidates = await api("/api/v1/ui/series?role=CANONICAL_1H&limit=1");
    if (!candidates.data.length) throw new Error("NO_CHARTABLE_SERIES");
    selectedId = candidates.data[0].series_id;
    window.history.replaceState({}, "", `/ui/series/${selectedId}`);
  }
  const response = await api(`/api/v1/ui/series/${encodeURIComponent(selectedId)}`);
  state.detail = response.data;
  state.eligibility = "eligible";
  const series = state.detail.series;
  state.period = series.layer_label === "1H" ? "1M" : series.layer_label === "4H" ? "3M" : "3Y";
  setAsOf(response.generated_at_utc);
  page.innerHTML = `
    <section class="series-hero">
      <div class="series-title"><div>${badge(series.role)}</div><div><h2>${escapeHtml(series.symbol)}</h2><p>${escapeHtml(series.category)} · ${escapeHtml(series.price_basis)} · ${escapeHtml(series.layer_label)}</p></div></div>
      <div class="series-facts"><span><small>行数</small><strong>${formatNumber(series.row_count)}</strong></span><span><small>最古</small><strong>${formatTime(series.min_time_utc)}</strong></span><span><small>最新 complete</small><strong>${formatTime(series.latest_complete_time_utc || series.max_time_utc)}</strong></span><span><small>状態</small><strong>${badge(series.status)}</strong></span></div>
    </section>
    <section class="chart-panel">
      <div class="chart-toolbar">
        ${["1M","3M","6M","1Y","3Y","ALL"].map(value => `<button class="period-button${value === state.period ? " active" : ""}" data-period="${value}" type="button">${value}</button>`).join("")}
        <button class="button ghost" id="load-older" type="button">過去を追加</button>
        <span class="spacer"></span>
        <label for="eligibility">表示モード</label>
        <select class="control-select" id="eligibility"><option value="eligible">eligibleのみ</option><option value="stored_complete">管理確認モード</option></select>
      </div>
      <div class="chart-wrap"><div id="chart"></div><div id="chart-tooltip" class="chart-tooltip">チャート上へカーソルを移動してください</div></div>
      <div id="chart-message" class="chart-message"><span class="spinner"></span>barを取得しています</div>
    </section>
    <div class="tabs" role="tablist">
      <button class="tab-button active" id="tab-button-data" data-tab="data" type="button" role="tab" aria-selected="true" aria-controls="tab-data">データ</button><button class="tab-button" id="tab-button-coverage" data-tab="coverage" type="button" role="tab" aria-selected="false" aria-controls="tab-coverage" tabindex="-1">カバレッジ</button><button class="tab-button" id="tab-button-quality" data-tab="quality" type="button" role="tab" aria-selected="false" aria-controls="tab-quality" tabindex="-1">品質</button><button class="tab-button" id="tab-button-lineage" data-tab="lineage" type="button" role="tab" aria-selected="false" aria-controls="tab-lineage" tabindex="-1">由来</button><button class="tab-button" id="tab-button-definition" data-tab="definition" type="button" role="tab" aria-selected="false" aria-controls="tab-definition" tabindex="-1">定義</button>
    </div>
    <section id="tab-data" class="tab-panel active" role="tabpanel" aria-labelledby="tab-button-data"></section>
    <section id="tab-coverage" class="tab-panel" role="tabpanel" aria-labelledby="tab-button-coverage" hidden>${coveragePanel(state.detail)}</section>
    <section id="tab-quality" class="tab-panel" role="tabpanel" aria-labelledby="tab-button-quality" hidden><article class="panel"><div class="metric-strip"><span class="metric-chip"><small>series status</small><strong>${badge(series.status)}</strong></span>${state.detail.freshness.map(row => `<span class="metric-chip"><small>freshness</small><strong>${badge(row.freshness_status)}</strong></span><span class="metric-chip"><small>latest complete</small><strong>${formatTime(row.latest_complete_time_utc)}</strong></span>`).join("")}</div></article></section>
    <section id="tab-lineage" class="tab-panel" role="tabpanel" aria-labelledby="tab-button-lineage" hidden>${lineagePanel(state.detail)}</section>
    <section id="tab-definition" class="tab-panel" role="tabpanel" aria-labelledby="tab-button-definition" hidden><article class="panel series-definition"><p><strong>${escapeHtml(ROLE_LABELS[series.role] || series.role)}</strong></p><p>price basis: <span class="mono">${escapeHtml(series.price_basis)}</span></p><p>UTCで保存し、画面表示だけJST/UTCを切り替えます。クライアント側で別の足へ再集計しません。</p>${!series.authoritative ? `<div class="warning-banner">この系列は正式な戦略入力ではありません。</div>` : ""}</article>${productReferencePanel(state.detail.product)}</section>`;
  installTabs();
  document.querySelectorAll(".copy-ai-prompt").forEach(button => button.addEventListener("click", () => copyAiPrompt(button.dataset.instrumentKey, button)));
  document.querySelectorAll(".period-button").forEach(button => button.addEventListener("click", async () => {
    state.period = button.dataset.period;
    document.querySelectorAll(".period-button").forEach(item => item.classList.toggle("active", item === button));
    const end = new Date(series.latest_complete_time_utc || series.max_time_utc);
    end.setUTCSeconds(end.getUTCSeconds() + (series.layer_label === "1D" || series.layer_label === "1D-TR" ? 86400 : 3600));
    await fetchChart(rangeFor(state.period, end, series.layer_label), end);
  }));
  document.querySelector("#load-older").addEventListener("click", () => loadOlder().catch(error => setChartMessage(error.message, true)));
  document.querySelector("#eligibility").addEventListener("change", async event => {
    state.eligibility = event.target.value;
    const end = new Date(series.latest_complete_time_utc || series.max_time_utc);
    end.setUTCSeconds(end.getUTCSeconds() + (series.layer_label.startsWith("1D") ? 86400 : 3600));
    await fetchChart(rangeFor(state.period, end, series.layer_label), end);
  });
  const end = new Date(series.latest_complete_time_utc || series.max_time_utc);
  end.setUTCSeconds(end.getUTCSeconds() + (series.layer_label.startsWith("1D") ? 86400 : 3600));
  await fetchChart(rangeFor(state.period, end, series.layer_label), end);
}

async function renderQuality() {
  showLoading();
  const response = await api("/api/v1/ui/quality-summary");
  const data = response.data;
  setAsOf(data.generated_at_utc);
  const totals = data.current_status_totals || {};
  const applicability = data.applicability_totals || {};
  const eventCards = (rows, emptyText) => rows.slice(0, 100).map(event => `<article class="event"><span>${badge(event.applicability)}</span><strong>${escapeHtml(event.instrument_key || event.symbol || event.scope_kind || "GLOBAL")}</strong><p><b>${escapeHtml(event.rule_id)}</b><br>${escapeHtml(event.action)}<br><span class="subtle">${escapeHtml(event.severity)} / ${escapeHtml(event.scope_kind)}</span></p><time>#${escapeHtml(event.quality_event_id)}<br>${formatTime(event.created_at_utc)}</time></article>`).join("") || `<div class="empty">${escapeHtml(emptyText)}</div>`;
  page.innerHTML = `
    <section class="kpi-grid">
      ${kpi("現在 PASS", totals.PASS || 0, "current guardrail")}
      ${kpi("現在 WARN", totals.WARN || 0, "coverage warning", (totals.WARN || 0) > 0)}
      ${kpi("現在 STALE", totals.STALE || 0, "freshness", (totals.STALE || 0) > 0)}
      ${kpi("現在 FAIL", totals.FAIL || 0, "current failure", (totals.FAIL || 0) > 0)}
      ${kpi("未評価", totals.NOT_EVALUATED || 0, "calendar / threshold")}
      ${kpi("canonical blocker", data.canonical_blocking_event_count || 0, "curated 1H scope", (data.canonical_blocking_event_count || 0) > 0)}
      ${kpi("全scope blocker", data.blocking_event_count || 0, "CURRENT + UNKNOWN", (data.blocking_event_count || 0) > 0)}
      ${kpi("未判定 event", applicability.UNKNOWN || 0, "operator review required", (applicability.UNKNOWN || 0) > 0)}
      ${kpi("履歴 event", applicability.HISTORICAL || 0, "reviewed historical")}
    </section>
    <div class="section-head"><div><span class="section-tag">CURRENT</span><h2>現在の利用可否</h2><p>coverage・freshness・判定済み品質eventを銘柄単位で統合しています。</p></div></div>
    ${qualityTable(data.current, 100)}
    <div class="section-head"><div><span class="section-tag">GLOBAL / RUN SCOPE</span><h2>全系列blocker</h2><p>instrumentを特定できないGLOBAL・RUN・UNKNOWN scopeは全系列へ適用します。</p></div></div>
    <section class="event-list">${eventCards(data.global_blockers || [], "全系列blockerはありません。")}</section>
    <div class="section-head"><div><span class="section-tag">ACTION REQUIRED</span><h2>CURRENT / UNKNOWN event</h2><p>UNKNOWN は安全側で blocker として扱います。分類は運用CLIで監査証跡を残して行います。</p></div></div>
    <section class="event-list">${eventCards(data.unresolved_events || [], "未解決eventはありません。")}</section>
    <div class="section-head"><div><span class="section-tag">REVIEWED HISTORY</span><h2>HISTORICAL event</h2><p>運用者が根拠を記録し、現在データへの非適用を確認したeventです。</p></div></div>
    <section class="event-list">${eventCards(data.historical_open_events || [], "HISTORICAL eventはありません。")}</section>`;
}

async function renderDailyClose() {
  showLoading();
  const data = await api("/api/v1/c2/daily-close-status");
  setAsOf(data.generated_at_utc);
  const summary = data.state || {};
  page.innerHTML = `
    <section class="kpi-grid">
      ${kpi("公開状態", summary.status || "NOT_EVALUATED", "ETF11 daily close")}
      ${kpi("最新", summary.current_count || 0, "freshness PASS")}
      ${kpi("要更新", summary.stale_count || 0, "STALE", (summary.stale_count || 0) > 0)}
      ${kpi("未収録", summary.missing_count || 0, "missing", (summary.missing_count || 0) > 0)}
      ${kpi("DataVersion警告", summary.revision_warning_count || 0, "review pending", (summary.revision_warning_count || 0) > 0)}
      ${kpi("限定補完警告", summary.imputation_warning_count || 0, "最大2本 / session", (summary.imputation_warning_count || 0) > 0)}
      ${kpi("Scheduler", summary.scheduler_status || "NOT_EVALUATED", "operational state", summary.scheduler_status !== "PASS")}
      ${kpi("次の期待bar", formatTime(summary.next_expected_bar_time_utc), "weekend / holiday wait is non-blocking")}
    </section>
    <div class="section-head"><div><span class="section-tag">LOW FREQUENCY PAPER</span><h2>ETF11 日次終値の鮮度</h2><p>regular sessionの完成1Hから生成したnative OHLC日足です。短い内部欠落は最大2本だけ前の実closeでC2専用overlayへ補完し、WARNと来歴を必ず表示します。日次close自体はprovider実値が必須です。リアルタイム、tick、Bid/Askは不要で、Total Return、公式取引所終値、約定価格とは別です。</p></div></div>
    ${genericRowsTable(data.series, [
      {key:"instrument_key",label:"銘柄",render:value=>`<strong>${escapeHtml(String(value).toUpperCase())}</strong>`},
      {key:"latest_session_date",label:"最新session / as-of"},
      {key:"latest_expected_complete_time_utc",label:"期待bar",render:value=>formatTime(value)},
      {key:"expected_session_date",label:"期待session"},
      {key:"freshness_status",label:"鮮度",render:value=>badge(value)},
      {key:"quality_status",label:"品質",render:value=>badge(value)},
      {key:"imputation_status",label:"補完状態",render:value=>badge(value)},
      {key:"imputed_bar_count",label:"補完本数",className:"numeric",render:formatNumber},
      {key:"warning_ids",label:"補完警告",render:value=>escapeHtml((value || []).join(", ") || "—")},
      {key:"update_status",label:"更新",render:value=>badge(value)},
      {key:"revision_review_status",label:"Revision",render:value=>badge(value)},
      {key:"source_last_ingestion_run_id",label:"Run",className:"numeric",render:formatNumber},
    ])}
    <article class="panel"><p><strong>補完された1Hを確認:</strong> <code>/api/v1/c2/hourly-overlay</code> は実barと補完barを区別し、元timestamp・理由・連続欠落数を返します。canonical raw/curatedは変更しません。</p><div class="metric-strip">
      <span class="metric-chip"><small>市場待ち</small><strong>${badge(summary.market_wait_status)}</strong></span>
      <span class="metric-chip"><small>Saxo write</small><strong>${formatNumber(summary.write_requests_to_saxo)}</strong></span>
      <span class="metric-chip"><small>order / precheck</small><strong>${formatNumber(summary.orders_or_prechecks_sent)}</strong></span>
    </div></article>`;
}

function genericRowsTable(rows, columns) {
  if (!rows?.length) return `<div class="empty">データはありません。</div>`;
  return `<div class="table-panel"><div class="table-scroll"><table><thead><tr>${columns.map(column => `<th>${escapeHtml(column.label)}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${columns.map(column => `<td class="${column.className || ""}">${column.render ? column.render(row[column.key], row) : escapeHtml(row[column.key] ?? "—")}</td>`).join("")}</tr>`).join("")}</tbody></table></div></div>`;
}

async function renderRuns() {
  showLoading();
  const [runs, lineage] = await Promise.all([
    api("/api/v1/operations/runs?limit=100"),
    api("/api/v1/operations/lineage?limit=200"),
  ]);
  setAsOf(new Date().toISOString());
  page.innerHTML = `
    <div class="section-head"><div><span class="section-tag">INGESTION RUNS</span><h2>取込実行履歴</h2><p>失敗・BLOCKEDも監査証跡として保持します。</p></div></div>
    ${genericRowsTable(runs.rows, [
      {key:"status",label:"状態",render:value=>badge(value)}, {key:"ingestion_run_id",label:"Run"},
      {key:"started_at_utc",label:"開始",render:value=>formatTime(value)}, {key:"finished_at_utc",label:"終了",render:value=>formatTime(value)},
      {key:"inserted_rows",label:"Insert",className:"numeric",render:formatNumber}, {key:"updated_rows",label:"Update",className:"numeric",render:formatNumber},
      {key:"revision_rows",label:"Revision",className:"numeric",render:formatNumber}, {key:"rejected_rows",label:"Reject",className:"numeric",render:formatNumber},
      {key:"error_code",label:"Error"}, {key:"run_manifest_relative_path",label:"Manifest",className:"mono"},
    ])}
    <div class="section-head"><div><span class="section-tag">LINEAGE</span><h2>source fileからcuratedまで</h2></div></div>
    ${genericRowsTable(lineage.rows, [
      {key:"source_dataset_id",label:"Dataset",className:"mono"}, {key:"relative_path",label:"Source file",className:"mono"}, {key:"ingestion_run_id",label:"Run"},
      {key:"raw_rows",label:"Raw",className:"numeric",render:formatNumber}, {key:"curated_rows",label:"Curated",className:"numeric",render:formatNumber}, {key:"derived_rows",label:"Derived",className:"numeric",render:formatNumber},
    ])}`;
}

async function renderOperations() {
  showLoading();
  const [backups, storage] = await Promise.all([
    api("/api/v1/operations/backups?limit=20"),
    api("/api/v1/operations/storage?limit=200"),
  ]);
  setAsOf(new Date().toISOString());
  page.innerHTML = `
    <div class="section-head"><div><span class="section-tag">BACKUP STATUS</span><h2>検証済みバックアップ</h2><p>この画面からbackup・restore・削除は実行できません。</p></div></div>
    ${genericRowsTable(backups.rows, [
      {key:"database_name",label:"Database"}, {key:"last_success_at_utc",label:"最終成功",render:value=>formatTime(value)},
      {key:"pg_restore_list_pass",label:"pg_restore list",render:value=>badge(value ? "PASS" : "FAIL")}, {key:"restore_smoke_test_status",label:"Restore smoke",render:value=>badge(value || "NOT_EVALUATED")},
      {key:"sha256",label:"SHA-256",className:"mono"}, {key:"age_seconds",label:"経過秒",className:"numeric",render:formatNumber},
    ])}
    <div class="section-head"><div><span class="section-tag">STORAGE</span><h2>relation別使用量</h2><p>partition review thresholdを同時に確認します。</p></div></div>
    ${genericRowsTable(storage.rows, [
      {key:"schema_name",label:"Schema"}, {key:"relation_name",label:"Relation"}, {key:"size_bytes",label:"Size",className:"numeric",render:formatBytes},
      {key:"partition_review_threshold_status",label:"Partition review",render:value=>badge(value)},
    ])}`;
}

async function render() {
  disposeChart();
  const route = currentRoute();
  const view = VIEW_TITLES[route.view] ? route.view : "overview";
  setNavigation(view);
  try {
    if (view === "overview") await renderOverview();
    else if (view === "inventory") await renderInventory();
    else if (view === "catalog") await renderCatalog();
    else if (view === "series") await renderSeries(route.id);
    else if (view === "quality") await renderQuality();
    else if (view === "daily-close") await renderDailyClose();
    else if (view === "runs") await renderRuns();
    else if (view === "operations") await renderOperations();
  } catch (error) { showError(error); }
}

timezoneSelect.addEventListener("change", () => {
  state.timezone = timezoneSelect.value;
  render();
});
window.addEventListener("popstate", render);
readHealth();
render();
