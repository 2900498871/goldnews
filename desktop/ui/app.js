/* GoldDesk v1.0 前端：Windows 桌面端（单端，无手机端） */
(function () {
  'use strict';
  var S = null, C = null, reportCache = null, reportMode = 'html';
  var historyPreset = '6m', historyTouched = false;
  var CN = { bullish: '偏多', bearish: '偏空', neutral: '中性', unclear: '信号不明确' };
  var HIDIR = { bullish: '偏多倾向', bearish: '偏空倾向', neutral: '中性', unclear: '信号不明确' };
  var HORIZON_CN = { '1-4w': '未来 2–4 周', '1-3m': '未来 1–3 个月', '3-6m': '未来 3–6 个月' };
  var STANCE_CN = { supportive: '偏利多', pressuring: '偏利空', mixed: '混合', unclear: '不明确' };
  var CNF = { high: '高', medium: '中', low: '低' };
  var CNH = { intraday: '日内', '1-3d': '1–3 日', '1-2w': '1–2 周' };
  var FACTOR_CN = { usd_real_rates: '美元与实际利率', inflation_fed: '通胀与美联储', geopolitics: '地缘避险',
                    central_bank_etf: '央行购金与 ETF', fx_supply: '汇率与供给', macro: '宏观' };
  var QFLAG = { cached: '缓存值，可能滞后', stale: '陈旧数据', divergent: '源间分歧',
                derived: '折算参考价 · 非成交价', unknown_ts: '源未提供报价时间', mock: '演示数据', ok: '正常' };

  function $(id) { return document.getElementById(id); }
  function esc(v) { return String(v === undefined || v === null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
  // 后端时间统一是 ISO（2026-11-26T17:30:39+08:00），界面只展示到分钟
  function fmtBj(s) {
    var m = String(s || '').match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
    return m ? (m[1] + '-' + m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5]) : (s || '—');
  }
  function toast(msg) {
    var t = $('toast'); t.textContent = msg; t.className = 'toast show';
    clearTimeout(toast._t); toast._t = setTimeout(function () { t.className = 'toast'; }, 3600);
  }
  function api(path, method, body) {
    return fetch(path, { method: method || 'GET', headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined })
      .then(function (r) { return r.json().then(function (d) { return { status: r.status, data: d }; }); });
  }
  function ok(d) { return d && d.data && d.data.ok !== false; }

  function go(page) {
    var pages = document.querySelectorAll('.page');
    for (var i = 0; i < pages.length; i++) pages[i].className = 'page' + (pages[i].id === 'page-' + page ? ' active' : '');
    var navs = document.querySelectorAll('.nav,.tab');
    for (var j = 0; j < navs.length; j++) {
      var c = navs[j].className.replace(/\s*on\b/g, '').trim();
      navs[j].className = c + (navs[j].getAttribute('data-page') === page ? ' on' : '');
    }
    window.scrollTo(0, 0);
    if (page === 'settings') loadConfig();
    if (page === 'history') loadHistoryRuns();
    if (page === 'daily') loadDaily();
    if (page === 'market' && !$('mkSources').dataset.done) runSelfcheck('market');
  }
  window.go = go;

  function num(v, d) { var x = parseFloat(v); return isNaN(x) ? (d === undefined ? '—' : d) : x; }
  function fmt(v, n) { var x = num(v, null); return x === null ? '—' : x.toFixed(n === undefined ? 2 : n); }
  /* 快照间涨跌经常只有千分之几，固定两位小数会显示成 +0.00%，低于 0.1% 时多给一位 */
  function pct(v) {
    var x = num(v, null);
    if (x === null) return '—';
    var n = Math.abs(x) < 0.1 ? 3 : 2;
    return (x > 0 ? '+' : '') + x.toFixed(n) + '%';
  }
  function cls(v) { var x = num(v, 0); return x > 0 ? 'up' : (x < 0 ? 'down' : 'flat'); }
  /* 后端时间统一是带 +08:00 的 ISO 串，直接展示会出现 T 与偏移量，这里转成人读格式 */
  function fmtTs(v) {
    var s = String(v === undefined || v === null ? '' : v);
    if (!s) return '—';
    return s.replace('T', ' ').replace(/(\+08:00|Z)$/, '');
  }
  function fmtShort(v) {
    var s = fmtTs(v);
    return s.length >= 16 ? s.slice(5, 16) : s;
  }
  /* 只在用户没在编辑该输入框时才回填，避免 30 秒自动刷新清掉正在输入的内容 */
  function fillInput(id, v) {
    var e = $(id); if (!e) return;
    if (document.activeElement === e) return;
    var cur = String(e.value || '').trim();
    if (cur === '' || cur === e.getAttribute('data-last')) {
      e.value = v === undefined || v === null ? '' : v;
      e.setAttribute('data-last', String(e.value || ''));
    }
  }
  function tog(el) { el.className = el.className.indexOf('on') >= 0 ? 'sw' : 'sw on'; }
  window.tog = tog;
  function isOn(id) { var e = $(id); return !!e && e.className.indexOf('on') >= 0; }
  function setSw(id, v) { var e = $(id); if (e) e.className = v ? 'sw on' : 'sw'; }
  function toggleSecret(id, btn) {
    var e = $(id); e.type = e.type === 'password' ? 'text' : 'password';
    btn.textContent = e.type === 'password' ? '显示' : '隐藏';
  }
  window.toggleSecret = toggleSecret;

  /* ---------------- 状态渲染 ---------------- */
  function loadState() {
    return api('/api/state').then(function (r) {
      if (!r.data || r.data.error) { $('statusChip').textContent = '连接失败'; return; }
      S = r.data;
      $('statusChip').textContent = '本端运行中';
      $('statusChip').className = 'chip line tag down';
      $('kindLabel').textContent = 'Windows 桌面程序';
      $('sideKind').textContent = 'Windows 桌面程序';
      $('sideInst').textContent = '实例 ' + S.instance_id;
      $('sideNext').textContent = '下一次推送：' + (S.next_push || '—');
      $('setInst').textContent = S.instance_id;
      $('setPort').textContent = '端口 ' + S.port;
      $('setNext').textContent = '下一次推送：' + (S.next_push || '—');
      $('setVer').textContent = '本端配置 v' + S.config_version;
      $('ovVer').textContent = 'v' + S.config_version + ' · 桌面';
      $('ovSub').textContent = '北京时间 ' + fmtTs(S.server_time) + ' · 本端实例 ' + S.instance_id + ' · 准实时 2–5 分钟级';
      setSw('masterSw', S.push_master_switch);
      $('disclaimer').textContent = S.disclaimer || '';
      renderOverview(); renderMarket(); renderInsight(); renderHolding(); renderStrategy();
      $('inBudget').textContent = '本端今日 ' + S.usage.calls + ' 次调用 / ' +
        ((S.usage.prompt_tokens || 0) + (S.usage.completion_tokens || 0)) + ' tokens · 上限 ' +
        S.budget.max_calls_per_day + ' 次 / ' + S.budget.daily_token_budget + ' tokens';
      $('hiBudget').textContent = '本端今日 ' + S.usage.calls + ' 次 / ' + S.budget.max_calls_per_day + ' 次调用上限';
      $('dsUsage').textContent = '本端今日：' + S.usage.calls + ' 次调用，prompt ' +
        (S.usage.prompt_tokens || 0) + ' tokens，completion ' + (S.usage.completion_tokens || 0) +
        ' tokens；预算 ' + S.budget.max_calls_per_day + ' 次 / ' + S.budget.daily_token_budget + ' tokens。手动获取计入同一预算，超限自动降级为仅数据。';
      renderSettingsInfo();
    }).catch(function () { $('statusChip').textContent = '连接失败'; });
  }
  window.loadState = loadState;

  function latestPrices() {
    if (!S || !S.prices) return [];
    var seen = {}, out = [];
    for (var i = S.prices.length - 1; i >= 0; i--) {
      var p = S.prices[i];
      if (seen[p.symbol]) continue;
      seen[p.symbol] = 1; out.push(p);
    }
    return out.reverse();
  }
  /* ---------- 总览：国内实时金价卡 + 长期投资建议卡 ---------- */
  var LONH = { '6-12m': '6–12 个月', '1-2y': '1–2 年', '3y+': '3 年以上' };
  function goldCardHtml() {
    var p = latestPrices().filter(function (x) { return x.symbol === 'Au99.99'; })[0];
    if (!p) return '<div class="pcard"><div class="top"><span>国内实时金价</span></div><div class="empty">暂无上金所 Au99.99 报价，请点「一键获取」或等待定时采集</div></div>';
    var ex = p.extra || {}, pc = num(ex.prev_close, null), px = num(p.price, null);
    var chg = (pc && px) ? (px - pc) / pc * 100 : num(p.change_pct, null);
    var chgBase = (pc && px) ? '较昨收 ' + fmt(pc, 2) : '相对本端上一快照';
    var f = QFLAG[p.quality_flag] || p.quality_flag || '';
    return '<div class="pcard hero"><div class="top"><span>国内实时金价 <span style="color:var(--ink3)">· 上金所 Au99.99</span></span>' +
      '<span class="tag ' + (p.quality_flag === 'ok' ? 'line' : 'gold') + '">' + esc(f) + '</span></div>' +
      '<div class="val xl">' + esc(fmt(px, 2)) + '<span class="unit"> 元/克</span>' +
      '<span class="chg ' + cls(chg) + '" style="display:inline-block;margin-left:10px;font-size:15px;vertical-align:middle">' + esc(pct(chg)) + '</span></div>' +
      '<div class="ba"><span>买价<b>' + esc(fmt(ex.bid, 2)) + '</b></span><span>卖价<b>' + esc(fmt(ex.ask, 2)) + '</b></span>' +
      '<span>日内最高<b class="up">' + esc(fmt(ex.day_high, 2)) + '</b></span><span>日内最低<b class="down">' + esc(fmt(ex.day_low, 2)) + '</b></span></div>' +
      '<div class="meta">' + esc(chgBase) + '（非银行积存金成交价）<br>来源：' + esc(p.source || '—') +
      '<br>源报价时间：' + esc(p.source_ts || '源未提供') + ' ｜ 抓取：' + esc(fmtTs(p.fetched_at)) + '</div></div>';
  }
  function longAdviceCardHtml() {
    var s = S.signal, n = (S.news || []).length;
    var head = '<div class="top"><span>长期投资建议 <span style="color:var(--ink3)">· AI 依据国际新闻与区间走势</span></span>' +
      '<span class="tag line">' + n + ' 条要闻</span></div>';
    if (!s) return '<div class="pcard">' + head + '<div class="empty">尚无研判结果，点「一键获取并研判」生成</div></div>';
    var ol = (s.outlook || {}).long, adv = (s.advice || {}).long_term;
    if (!ol && !adv) return '<div class="pcard">' + head + '<div class="empty">当前是旧版研判（无走向预判与建议字段），点「重新研判」生成</div></div>';
    var d = (ol && ol.direction) || 'unclear';
    return '<div class="pcard">' + head +
      '<div class="advice"><div class="hd"><span class="tag ' + (d === 'bullish' ? 'up' : d === 'bearish' ? 'down' : 'line') + '">' + esc(CN[d]) + '</span>' +
      '<span style="font-size:11.5px;font-weight:500;color:var(--ink3)">长期走向 · ' + esc(LONH[(ol && ol.horizon)] || '1–2 年') + '</span></div>' +
      '<div>' + esc(adv || _SAFE_LONG) + '</div>' +
      (ol && ol.view ? '<div class="why"><b>判断依据：</b>' + esc(ol.view) + '</div>' : '') +
      '<div class="ft"><span>' + esc(fmtTs(s.as_of_bj)) + ' · ' + esc(s.model || '—') + (s._fallback ? ' · 服务不可用' : '') + '</span>' +
      '<button class="btn sm" onclick="quickFetch(\'fetch_and_analyze\')">重新研判</button></div></div></div>';
  }
  var _SAFE_LONG = '长期按既定积存计划分批、定期进行，不以短期波动改变节奏；先留足应急资金，不加杠杆。';

  /* ---------- 总览：金价详细走势 ---------- */
  var GT_PERIODS = [['1d', '今日'], ['1m', '近 1 月'], ['3m', '近 3 月'], ['6m', '近半年'], ['1y', '近 1 年'], ['5y', '近 5 年']];
  var GT = { symbol: 'domestic', period: '6m', data: null, at: 0, loading: false, tipIdx: -1 };
  window.setGoldChart = function (sym, per) {
    if (sym) GT.symbol = sym;
    if (per) GT.period = per;
    GT.data = null; GT.at = 0;
    renderGtTabs(); loadGoldChart();
  };
  function renderGtTabs() {
    var a = $('gtSym').querySelectorAll('.tab'), i;
    for (i = 0; i < a.length; i++) a[i].className = 'tab' + (a[i].getAttribute('data-sym') === GT.symbol ? ' on' : '');
    $('gtPeriods').innerHTML = GT_PERIODS.map(function (x) {
      return '<button class="tab' + (x[0] === GT.period ? ' on' : '') + '" onclick="setGoldChart(null,\'' + x[0] + '\')">' + x[1] + '</button>';
    }).join('');
  }
  function loadGoldChart(force) {
    renderGtTabs();
    // 今日分时 60 秒内不重复请求；日 K 周期已有数据就不重复请求（后端本身有缓存）
    if (!force && GT.data && (GT.period !== '1d' || Date.now() - GT.at < 60000)) { drawGoldChart(); return; }
    if (GT.loading) return;
    GT.loading = true;
    $('gtNote').textContent = '正在获取 ' + (GT.symbol === 'domestic' ? '国内' : '国际') + '金价走势…';
    api('/api/gold_history?symbol=' + GT.symbol + '&period=' + GT.period).then(function (r) {
      GT.loading = false; GT.at = Date.now();
      GT.data = r.data && r.data.ok ? r.data : null;
      GT.err = (!GT.data && r.data && (r.data.message || r.data.error)) || '';
      drawGoldChart();
    }).catch(function (e) { GT.loading = false; GT.at = Date.now(); GT.data = null; GT.err = String(e); drawGoldChart(); });
  }
  function drawGoldChart() {
    var d = GT.data, el = $('ovChart');
    if (!d || !d.series || d.series.length < 2) {
      // 接口失败：回退到本端已落库的 XAU/USD 快照，并如实标注
      var h = (S.price_history || []).filter(function (x) { return x.price; });
      $('gtStats').innerHTML = '';
      if (h.length >= 2) {
        $('gtNote').innerHTML = '<span style="color:var(--up)">走势接口失败：' + esc(GT.err || '无数据') + '</span><br>以下回退为本端已落库的 XAU/USD 快照（非所选周期）：' +
          esc(fmtTs(h[0].fetched_at)) + ' ~ ' + esc(fmtTs(h[h.length - 1].fetched_at));
        seriesChart(el, h.map(function (x) { return { t: fmtShort(x.fetched_at), c: parseFloat(x.price) }; }), 2, '#2456C4', false);
      } else {
        $('gtNote').innerHTML = '<span style="color:var(--up)">走势接口失败：' + esc(GT.err || '无数据') + '</span>；本端快照也不足，暂无法绘图';
        el.innerHTML = '<div class="chart-empty">暂无走势数据</div>';
      }
      return;
    }
    var st = d.stats || {}, dec = 2;
    $('gtStats').innerHTML =
      '<div class="st"><small>区间涨跌</small><b class="' + cls(st.chg_pct) + '">' + esc(pct(st.chg_pct)) + '</b></div>' +
      '<div class="st"><small>区间最高</small><b class="up">' + fmt(st.high, dec) + '</b></div>' +
      '<div class="st"><small>区间最低</small><b class="down">' + fmt(st.low, dec) + '</b></div>' +
      '<div class="st"><small>区间均价</small><b>' + fmt(st.avg, dec) + '</b></div>' +
      '<div class="st"><small>样本</small><b>' + esc(st.points || d.series.length) + ' <span style="font-size:10px;font-weight:500;color:var(--ink3)">点</span></b></div>';
    var rising = num(st.chg_pct, 0) >= 0;
    seriesChart(el, d.series, dec, rising ? '#D33A3A' : '#12946B', d.period !== '1d');
    $('gtNote').innerHTML = '<b>' + esc(d.title) + '</b> · 单位 ' + esc(d.unit) + ' · 来源：' + esc(d.source) +
      ' · 抓取 ' + esc(fmtTs(d.fetched_at)) + (d.note ? '<br>' + esc(d.note) : '') + '<br>鼠标悬停查看每个点的日期与价格；数据为准实时行情，非逐笔。';
  }
  function seriesChart(el, series, dec, color, band) {
    var n = series.length;
    if (n < 2) { el.innerHTML = '<div class="chart-empty">数据点不足</div>'; return; }
    var cs = series.map(function (s) { return num(s.c, 0); });
    var hs = series.map(function (s) { return num(s.h, null) || num(s.c, 0); });
    var ls = series.map(function (s) { return num(s.l, null) || num(s.c, 0); });
    var lo = Math.min.apply(null, ls), hi = Math.max.apply(null, hs), pad = (hi - lo) * .08 || 1;
    lo -= pad; hi += pad;
    var W = 760, H = 300, L = 58, R = 16, T = 14, B = 26;
    function X(i) { return L + i * (W - L - R) / (n - 1); }
    function Y(v) { return T + (hi - v) / (hi - lo) * (H - T - B); }
    var i, line = '', top = '', bot = [];
    for (i = 0; i < n; i++) {
      line += (i ? ' L' : 'M') + X(i).toFixed(1) + ',' + Y(cs[i]).toFixed(1);
      top += (i ? ' L' : 'M') + X(i).toFixed(1) + ',' + Y(hs[i]).toFixed(1);
      bot.push(X(i).toFixed(1) + ',' + Y(ls[i]).toFixed(1));
    }
    var area = 'M' + X(0).toFixed(1) + ',' + (H - B) + ' L' + line.slice(1).replace(/M/g, 'L') + ' L' + X(n - 1).toFixed(1) + ',' + (H - B) + ' Z';
    var bandPath = band && (hs.some(function (v, k) { return v !== cs[k]; })) ? top + ' L' + bot.reverse().join(' L') + ' Z' : '';
    var grid = '';
    for (i = 0; i <= 4; i++) {
      var v = lo + (hi - lo) * i / 4, y = Y(v).toFixed(1);
      grid += '<line x1="' + L + '" y1="' + y + '" x2="' + (W - R) + '" y2="' + y + '" stroke="#EDF1F7"/>' +
        '<text x="' + (L - 8) + '" y="' + (+y + 3.5) + '" text-anchor="end" fill="#8490A3" font-size="10">' + fmt(v, dec) + '</text>';
    }
    var mid = Math.floor(n / 2);
    function xt(i, anchor) { return '<text x="' + X(i).toFixed(1) + '" y="' + (H - 8) + '" text-anchor="' + anchor + '" fill="#8490A3" font-size="10">' + esc(series[i].t) + '</text>'; }
    var ly = Y(cs[n - 1]).toFixed(1), lastLabel = fmt(cs[n - 1], dec);
    el.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' +
      '<defs><linearGradient id="ag" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="' + color + '" stop-opacity=".18"/><stop offset="1" stop-color="' + color + '" stop-opacity="0"/></linearGradient></defs>' +
      grid +
      (bandPath ? '<path d="' + bandPath + '" fill="' + color + '" fill-opacity=".10"/>' : '') +
      '<path d="' + area + '" fill="url(#ag)"/>' +
      '<path d="' + line + '" fill="none" stroke="' + color + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>' +
      '<line x1="' + L + '" y1="' + ly + '" x2="' + (W - R) + '" y2="' + ly + '" stroke="' + color + '" stroke-dasharray="3 4" stroke-opacity=".55"/>' +
      '<circle cx="' + X(n - 1).toFixed(1) + '" cy="' + ly + '" r="3.5" fill="' + color + '"/>' +
      '<rect x="' + (W - R - 52) + '" y="' + Math.max(T, +ly - 18).toFixed(1) + '" width="52" height="16" rx="4" fill="' + color + '"/>' +
      '<text x="' + (W - R - 26) + '" y="' + (Math.max(T, +ly - 18) + 11.5).toFixed(1) + '" text-anchor="middle" fill="#fff" font-size="10" font-weight="700">' + esc(lastLabel) + '</text>' +
      xt(0, 'start') + xt(mid, 'middle') + xt(n - 1, 'end') +
      '<line id="gtCross" x1="0" y1="' + T + '" x2="0" y2="' + (H - B) + '" stroke="#8490A3" stroke-dasharray="2 3" style="display:none"/>' +
      '<circle id="gtDot" r="4" fill="#fff" stroke="' + color + '" stroke-width="2" style="display:none"/>' +
      '<rect id="gtHit" x="' + L + '" y="' + T + '" width="' + (W - L - R) + '" height="' + (H - T - B) + '" fill="transparent"/></svg>';
    var svg = el.querySelector('svg'), hit = el.querySelector('#gtHit'), cross = el.querySelector('#gtCross'),
      dot = el.querySelector('#gtDot'), tip = $('gtTip'), wrap = el.parentNode;
    function onMove(ev) {
      var rc = svg.getBoundingClientRect(), k = W / rc.width;
      var mx = (ev.clientX - rc.left) * k;
      var idx = Math.round((mx - L) / ((W - L - R) / (n - 1)));
      idx = Math.max(0, Math.min(n - 1, idx));
      var x = X(idx), y = Y(cs[idx]), s = series[idx];
      cross.setAttribute('x1', x); cross.setAttribute('x2', x); cross.style.display = '';
      dot.setAttribute('cx', x); dot.setAttribute('cy', y); dot.style.display = '';
      tip.innerHTML = '<b>' + esc(s.t) + '</b>　<span style="color:#FFD98A">' + fmt(s.c, dec) + '</span>' +
        (band ? '<br>开 ' + fmt(s.o, dec) + ' ｜ 高 ' + fmt(s.h, dec) + ' ｜ 低 ' + fmt(s.l, dec) : '') + (s.v ? ' ｜ 量 ' + esc(s.v) : '');
      var wr = wrap.getBoundingClientRect();
      tip.style.left = (x / k + (rc.left - wr.left)) + 'px';
      tip.style.top = (y * rc.height / H + (rc.top - wr.top)) + 'px';
      tip.className = 'gttip show';
    }
    function onLeave() { tip.className = 'gttip'; cross.style.display = 'none'; dot.style.display = 'none'; }
    hit.addEventListener('mousemove', onMove); hit.addEventListener('mouseleave', onLeave);
  }

  function renderOverview() {
    $('ovPriceCards').innerHTML = goldCardHtml() + longAdviceCardHtml();
    loadGoldChart();
    var sig = S.signal;
    $('ovSigTime').textContent = sig ? fmtTs(sig.as_of_bj) : '—';
    $('ovSignal').innerHTML = sig ? signalHtml(sig, true) :
      '<div class="empty">尚无研判结果，配置 DeepSeek API Key 后可获取</div>';
    $('ovNews').innerHTML = (S.news && S.news.length) ? S.news.slice(0, 4).map(newsHtml).join('') :
      '<div class="empty">暂无要闻（新闻源可能不可用，AI 输入会标注 news_available=false）</div>';
  }
  function newsHtml(n) {
    return '<div class="nitem"><a href="' + esc(n.url || '#') + '" target="_blank" rel="noopener">' + esc(n.title) + '</a>' +
      '<div class="m">' + esc(n.source || '—') + ' ｜ 本地 ' + esc(n.published_at_local || '—') +
      ' / 北京时间 ' + esc(n.published_at_bj || '—') + ' ｜ ' + esc(n.topic || '—') + ' ｜ 重要性 ' + esc(n.importance) + '</div>' +
      (n.summary ? '<div class="s">' + esc(n.summary) + '</div>' : '') + '</div>';
  }
  /* 走势预判块：短期 / 长期走向，放在研判最前面 */
  function outlookHtml(s) {
    var ol = s.outlook;
    if (!ol || !ol.short) return '<div class="warn" style="margin-bottom:9px">当前是旧版研判（Prompt ' + esc(s.prompt_version || '—') +
      '），没有走向预判与投资建议；点「重新研判」按新版生成。</div>';
    function one(k, lb, hmap, defH) {
      var o = ol[k] || {}, d = o.direction || 'unclear';
      return '<div class="ol"><div class="lb">' + lb + '</div><div class="dr ' + (d === 'bullish' ? 'up' : d === 'bearish' ? 'down' : '') + '">' +
        esc(CN[d]) + ' <span style="font-size:10.5px;font-weight:500;color:var(--ink3)">· ' + esc(hmap[o.horizon] || defH) + '</span></div>' +
        '<div class="v">' + esc(o.view || '—') + '</div></div>';
    }
    return '<div class="olgrid">' + one('short', '短期走向', CNH, '1–3 日') + one('long', '长期走向', LONH, '1–2 年') + '</div>';
  }
  function adviceHtml(s) {
    var a = s.advice;
    if (!a) return '';
    return '<div class="advgrid"><div class="adv short"><b>短期投资建议</b>' + esc(a.short_term || _SAFE_SHORT) + '</div>' +
      '<div class="adv long"><b>长期投资建议</b>' + esc(a.long_term || _SAFE_LONG) + '</div></div>';
  }
  var _SAFE_SHORT = '数据或置信度不足，暂不做单笔追入或集中操作；可维持原有积存节奏，等价格与消息面补齐后再研判。';
  function signalHtml(s, brief) {
    var d = (s.key_drivers || []).map(function (x) {
      return '<li>' + esc(FACTOR_CN[x.factor] || x.factor) + '（' + esc(STANCE_CN[x.stance] || x.stance) + '）：' + esc(x.note) + '　<span style="color:#8490A3">依据 ' + esc(x.evidence_ref || '—') + '</span></li>';
    }).join('');
    return '<div class="sigbox">' + outlookHtml(s) + adviceHtml(s) +
      '<div class="big ' + (s.direction === 'bullish' ? 'up' : s.direction === 'bearish' ? 'down' : '') + '">' +
      esc(CN[s.direction] || '信号不明确') + ' <span style="font-size:12px;font-weight:500;color:var(--ink2)">置信度 ' +
      esc(CNF[s.confidence] || '低') + '</span></div>' +
      '<div class="note">' + esc(s.summary_cn || '') + '</div>' +
      (s._fallback ? '<div class="warn" style="margin-top:8px">AI 研判暂不可用／数据不足，按规则不输出方向结论</div>' : '') +
      '<div class="kv"><b>置信度依据</b><span>' + esc(s.confidence_reason || '—') + '</span></div>' +
      (brief ? '<div class="kv"><b>反向情形</b><span style="color:#B45309">' + esc(s.counter_case || '—') + '</span></div>' :
        '<div class="kv"><b>主要驱动</b><span><ul class="dlist">' + (d || '<li>本次未列出可追溯驱动</li>') + '</ul></span></div>' +
        '<div class="kv"><b>反向情形</b><span style="color:#B45309">' + esc(s.counter_case || '—') + '</span></div>' +
        '<div class="kv"><b>关注</b><span>' + esc((s.watch_points || []).join('；') || '—') + '</span></div>' +
        '<div class="kv"><b>数据缺口</b><span>' + esc((s.data_gaps || []).join('；') || '无') + '</span></div>' +
        '<div class="kv"><b>模型</b><span>' + esc(s.model || '—') + ' ｜ Prompt ' + esc(s.prompt_version || '—') + '</span></div>') +
      '<div class="hint" style="margin-top:8px">走向预判与投资建议均为 AI 基于公开信息的条件化参考，不含买卖点位与仓位比例，不构成交易指令。</div>' +
      '</div>';
  }
  function renderMarket() {
    var ps = latestPrices();
    if (!ps.length) { $('mkTable').innerHTML = '<div class="empty">暂无价格数据</div>'; }
    else {
      $('mkTable').innerHTML = '<table><thead><tr><th>口径 / 角色</th><th style="text-align:right">价格 / 涨跌</th><th>来源与源报价时间</th><th>质量</th></tr></thead><tbody>' +
        ps.map(function (p) {
          return '<tr><td><div class="tname">' + esc(p.name || p.symbol) + '</div><div class="trole">' + esc(p.symbol) + ' ｜ ' + esc(p.role || '') + '</div></td>' +
            '<td class="num"><b style="font-size:15px">' + esc(fmt(p.price, p.symbol === 'USD/CNY' ? 4 : 2)) + '</b> <span style="font-size:10px;color:var(--ink3)">' + esc(p.unit) + '</span>' +
            '<div class="' + cls(p.change_pct) + '" style="font-size:11px">' + esc(pct(p.change_pct)) + '</div></td>' +
            '<td class="src">' + esc(p.source || '—') + '<br>源报价时间 ' + esc(p.source_ts || '源未提供') + '<br>抓取 ' + esc(fmtTs(p.fetched_at)) + '<br>涨跌基准 ' + esc(p.change_base || '—') + '</td>' +
            '<td><span class="tag ' + (p.quality_flag === 'ok' ? 'down' : 'gold') + '">' + esc(QFLAG[p.quality_flag] || p.quality_flag) + '</span></td></tr>';
        }).join('') + '</tbody></table>';
    }
    var notes = [];
    (S.quality_notes || []).forEach(function (x) { notes.push(x); });
    (S.events || []).slice(0, 6).forEach(function (e) {
      if (e.level !== 'info' && notes.length < 8 && notes.indexOf(e.message) < 0) notes.push(fmtTs(e.at) + ' ' + e.message);
    });
    if (!notes.length) notes.push('当前未发现降级记录。缓存值、源间分歧、陈旧数据、休市与新闻缺失会在每轮采集时如实标注。');
    $('mkQuality').innerHTML = '<ul class="dlist">' + notes.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>';
  }
  function renderInsight() {
    $('inNewsCount').textContent = (S.news || []).length + ' 条 · 最近 24 小时';
    $('inNews').innerHTML = (S.news || []).length ? S.news.map(newsHtml).join('') : '<div class="empty">暂无要闻</div>';
    $('inSignal').innerHTML = S.signal ? signalHtml(S.signal, false) : '<div class="empty">尚无研判结果</div>';
    $('inReports').innerHTML = (S.reports || []).length ? S.reports.map(function (r) {
      return '<div class="row"><div><div class="t">' + esc(r.subject) + '</div><div class="d">' +
        esc(r.generated_at) + ' ｜ ' + esc(r.report_type) + '</div></div><div class="r">' +
        '<button class="btn sm" onclick="openReport(' + r.id + ')">预览</button></div></div>';
    }).join('') : '<div class="empty">尚无报告，点「生成日报并发信」或等待定时推送</div>';
  }
  function renderHolding() {
    var h = S.holdings;
    var folded = latestPrices().filter(function (p) { return p.symbol === 'FOLDED_CNY_G'; })[0];
    if (!h) { $('hdSummary').innerHTML = '<div class="hint">尚未录入持仓。持仓只保存在本机数据库。</div>'; }
    else {
      var px = folded ? parseFloat(folded.price) : null;
      var mv = px ? h.grams * px : null, cost = h.grams * h.cost_per_gram;
      fillInput('hdGrams', h.grams); fillInput('hdCost', h.cost_per_gram); fillInput('hdNote', h.note || '');
      $('hdSummary').innerHTML = '<div class="sigbox"><div class="kv"><b>持有</b><span>' + fmt(h.grams, 4) + ' 克 ｜ 成本 ' + fmt(h.cost_per_gram) + ' 元/克</span></div>' +
        '<div class="kv"><b>成本合计</b><span>¥ ' + fmt(cost) + '</span></div>' +
        '<div class="kv"><b>参考市值</b><span>' + (mv === null ? '折算价不可用' : '¥ ' + fmt(mv)) + '</span></div>' +
        '<div class="kv"><b>浮动盈亏</b><span class="' + (mv !== null && mv - cost > 0 ? 'up' : (mv !== null && mv - cost < 0 ? 'down' : '')) + '">' +
        (mv === null ? '—' : '¥ ' + fmt(mv - cost) + '（' + pct((mv - cost) / cost * 100) + '）') + '</span></div>' +
        '<div class="hint mt">参考市值按<b>折算参考价</b>计算，<b>非银行成交价</b>；未扣点差与手续费，仅作信息展示，不构成加仓/减仓建议。</div></div>';
    }
    $('hdAudit').innerHTML = (S.audit || []).length ? S.audit.map(function (a) {
      return '<div class="row"><div><div class="t">' + esc(a.action) + '</div><div class="d">' + esc(fmtTs(a.at)) + ' ｜ ' + esc(a.instance_id) + '</div></div><div class="r" style="font-size:11px;color:var(--ink3)">' + esc(a.detail) + '</div></div>';
    }).join('') : '<div class="empty">暂无审计记录</div>';
  }
  function renderStrategy() {
    var sig = S.signal || {};
    $('stScenarios').innerHTML = '<ul class="dlist">' +
      '<li>若美元/实际利率上行且通胀预期回落，需关注无息资产承压的情形；反之亦然。</li>' +
      '<li>若地缘避险升温，关注阶段性利多；但流动性冲击下黄金也可能被抛售换现金。</li>' +
      '<li>若 USD/CNY 明显变化，国内克价可能与国际金价短期分化，须先拆汇率再归因。</li>' +
      '<li>央行购金 / ETF 资金流需多期确认，单周流量不构成趋势。</li>' +
      '<li>本次模型反向情形：' + esc(sig.counter_case || '数据不足，无法给出') + '</li>' +
      '<li>本次关注点：' + esc((sig.watch_points || []).join('；') || '等待下一轮数据') + '</li></ul>' +
      '<div class="hint">以上为条件化观察框架，不含买卖点位、仓位比例或收益承诺。</div>';
  }
  function renderSettingsInfo() {
    $('setInfo').innerHTML = '<div class="kv"><b>实例标识</b><span>' + esc(S.instance_id) + '</span></div>' +
      '<div class="kv"><b>程序形态</b><span>Windows 桌面程序</span></div>' +
      '<div class="kv"><b>配置版本</b><span>v' + esc(S.config_version) + ' ｜ 最后修改 ' + esc(fmtTs(S.last_modified)) + '</span></div>' +
      '<div class="kv"><b>调度器</b><span>' + (S.scheduler_running ? '运行中' : '未运行') + ' ｜ 下一次推送 ' + esc(S.next_push || '—') + '</span></div>' +
      '<div class="kv"><b>本端今日用量</b><span>' + esc(S.usage.calls) + ' 次调用 / ' +
      ((S.usage.prompt_tokens || 0) + (S.usage.completion_tokens || 0)) + ' tokens</span></div>' +
      '<div class="kv"><b>今日邮件</b><span>' + (S.deliveries || []).filter(function (d) { return d.status === 'ok'; }).length + ' 封成功（仅本端）</span></div>' +
      '<div class="hint mt">配置、数据库、密钥、调度与用量都只保存在本机，升级或重装程序不会动这些数据。</div>';
  }

  /* ---------------- 手动获取 / 报告 ---------------- */
  function quickFetch(action, sendMail) {
    toast('正在执行：' + action + ' …');
    api('/api/fetch', 'POST', { action: action, send_mail: !!sendMail }).then(function (r) {
      var d = r.data || {};
      if (d.throttled) { toast(d.message); return; }
      if (!d.ok) { toast('失败：' + (d.message || '未知错误')); loadState(); return; }
      var extra = d.delivery ? ('；邮件：' + (d.delivery.ok ? '已投递' : (d.delivery.message || '未发送'))) : '';
      toast('完成 · ' + fmtBj(d.finished_at) + ' · 价格 ' + ((d.snapshot && d.snapshot.prices) || []).length +
        ' 项 / 新闻 ' + ((d.news && d.news.news) || []).length + ' 条' + extra);
      var st = action + ' · ' + fmtBj(d.finished_at) + ' · ' +
        ((d.ai && d.ai.available) ? 'AI 研判完成' : 'AI 研判未启用/不可用');
      $('ovFetchSt').textContent = st; $('inFetchSt').textContent = st;
      loadState();
    }).catch(function (e) { toast('请求失败：' + e); });
  }
  window.quickFetch = quickFetch;

  function openReport(id) {
    api('/api/report?id=' + id).then(function (r) {
      reportCache = r.data; reportMode = 'html'; switchReport('html');
      $('reportModal').className = 'modal-mask show';
    });
  }
  window.openReport = openReport;
  function switchReport(mode) {
    reportMode = mode;
    if (!reportCache) return;
    $('reportBody').innerHTML = mode === 'html' ?
      '<iframe sandbox="" srcdoc="' + esc(reportCache.html || '') + '"></iframe>' :
      '<pre>' + esc(reportCache.text_body || '') + '</pre>';
  }
  window.switchReport = switchReport;
  function closeReport() { $('reportModal').className = 'modal-mask'; }
  window.closeReport = closeReport;

  /* ---------------- 时段新闻研判（FR-16） ---------------- */
  function fmtDate(d) {
    return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2);
  }
  function setHistoryPreset(p, init) {
    var btns = document.querySelectorAll('#hiPresets .preset');
    for (var i = 0; i < btns.length; i++)
      btns[i].className = 'preset' + (btns[i].getAttribute('data-preset') === p ? ' on' : '');
    var today = new Date(), s = new Date(today);
    if (p === '3m') s.setMonth(s.getMonth() - 3);
    else if (p === '1y') s.setFullYear(s.getFullYear() - 1);
    else if (p === '6m') s.setMonth(s.getMonth() - 6);
    if (p === 'custom') {
      if (!$('hiStart').value) $('hiStart').value = fmtDate(new Date(today.getFullYear(), 0, 1));
      if (!$('hiEnd').value) $('hiEnd').value = fmtDate(today);
    } else {
      $('hiStart').value = fmtDate(s);
      $('hiEnd').value = fmtDate(today);
    }
    $('hiStart').disabled = p !== 'custom';
    $('hiEnd').disabled = p !== 'custom';
    historyPreset = p;
    if (!init) historyTouched = true;
    $('hiStatus').textContent = '已选择：' + (p === 'custom' ? '自定义日期' : ($('hiStart').value + ' ~ ' + $('hiEnd').value)) + '；点「开始采集并 AI 研判」执行。';
  }
  window.setHistoryPreset = setHistoryPreset;

  function runHistory() {
    var btn = $('hiRunBtn');
    if (btn.disabled) return;
    if (!$('hiStart').value || !$('hiEnd').value) { toast('请先填写起止日期'); return; }
    var body = {
      preset: historyPreset, start: $('hiStart').value, end: $('hiEnd').value,
      horizon: $('hiHorizon').value,
      max_items: parseInt($('hiMaxItems').value, 10) || 60,
      send_mail: $('hiSendMail').checked
    };
    btn.disabled = true;
    $('hiStatus').textContent = '正在采集区间新闻并调用 AI（外部档案源不可达时最多约 15 秒）…';
    api('/api/history/analyze', 'POST', body).then(function (r) {
      btn.disabled = false;
      var d = r.data || {};
      if (!d.ok) {
        $('hiStatus').textContent = '未执行：' + (d.message || '未知错误');
        toast(d.message || '执行失败');
        return;
      }
      renderHistory(d);
      var mail = d.delivery ? ('；邮件：' + (d.delivery.ok ? '已投递' : (d.delivery.message || '未发送'))) : '';
      $('hiStatus').textContent = '完成 · ' + fmtBj(d.finished_at) + ' · 报告 #' +
        ((d.report || {}).id || '—') + ' · 样本 ' + ((d.collection || {}).total || 0) + ' 条' + mail;
      toast('时段研判完成 · 样本 ' + ((d.collection || {}).total || 0) + ' 条');
      loadState(); loadHistoryRuns();
    }).catch(function (e) {
      btn.disabled = false;
      $('hiStatus').textContent = '请求失败：' + e;
      toast('请求失败：' + e);
    });
  }
  window.runHistory = runHistory;

  function barHtml(label, count, max, gold) {
    var w = max > 0 ? Math.max(3, Math.round(count / max * 100)) : 0;
    return '<div class="barrow"><span class="lb" title="' + esc(label) + '">' + esc(label) + '</span>' +
      '<span class="bartrack"><span class="barfill' + (gold ? ' gold' : '') + '" style="width:' + w + '%"></span></span>' +
      '<span class="vl">' + esc(count) + '</span></div>';
  }
  function renderHistory(d) {
    var col = d.collection || {}, rng = d.range || {}, an = d.analysis || {}, res = an.result || {};
    $('hiCount').textContent = (col.total || 0) + ' 条 · ' + (rng.start || '—') + ' ~ ' + (rng.end || '—');
    var src = col.sources || {}, keys = Object.keys(src);
    $('hiSources').innerHTML = keys.length ? keys.map(function (k) {
      var good = String(src[k]).indexOf('ok') === 0;
      return '<div class="srcrow"><span class="sid">' + esc(k) + '</span><span class="sv">' +
        '<span class="tag ' + (good ? 'down' : 'gold') + '">' + (good ? '可用' : '不可用') + '</span> ' +
        esc(src[k]) + '</span></div>';
    }).join('') : '<div class="empty">无数据源</div>';
    var ts = col.topic_stats || [], tmax = ts.length ? ts[0].count : 0;
    $('hiTopics').innerHTML = '<div class="ch"><h3>主题分布</h3></div>' +
      (ts.length ? ts.map(function (t) { return barHtml(t.label || t.topic, t.count, tmax, true); }).join('') :
        '<div class="hint">无样本</div>');
    var ms = col.monthly || [], mmax = 0;
    ms.forEach(function (m) { if (m.count > mmax) mmax = m.count; });
    $('hiMonthly').innerHTML = '<div class="ch"><h3>月度分布</h3></div>' +
      (ms.length ? ms.map(function (m) { return barHtml(m.month, m.count, mmax, false); }).join('') :
        '<div class="hint">无样本</div>');
    $('hiCoverage').textContent = '覆盖说明：' + (col.coverage_note || '—');

    var dir = res.direction || 'unclear';
    $('hiModel').textContent = (res.model || '—') + ' ｜ Prompt ' + (res.prompt_version || '—');
    var themes = (res.key_themes || []).map(function (t) {
      return '<li><span class="tone ' + esc(t.stance) + '">' + esc(STANCE_CN[t.stance] || '混合') + '</span>' +
        '<b>' + esc(t.theme) + '</b>：' + esc(t.note) + ' <span style="color:#8490A3">依据 ' + esc(t.evidence_ref || '—') + '</span></li>';
    }).join('');
    var timeline = (res.timeline || []).map(function (t) {
      return '<li><span class="tone ' + esc(t.tone) + '">' + esc(STANCE_CN[t.tone] || '不明确') + '</span>' +
        esc(t.month) + '：' + esc(t.note) + '</li>';
    }).join('');
    $('hiResult').innerHTML = '<div class="sigbox">' +
      '<div class="hibig ' + (dir === 'bullish' ? 'up' : dir === 'bearish' ? 'down' : '') + '">' +
      esc(HIDIR[dir] || '信号不明确') + ' <span class="hidir">置信度 ' +
      esc(CNF[res.confidence] || '低') + ' ｜ ' + esc(HORIZON_CN[res.forward_horizon] || '—') + '</span></div>' +
      '<div class="note">' + esc(res.summary_cn || '') + '</div>' +
      (res._fallback ? '<div class="warn mt">AI 研判暂不可用／数据不足，按规则不输出方向结论：' + esc(an.reason || '') + '</div>' : '') +
      '<div class="kv"><b>置信度依据</b><span>' + esc(res.confidence_reason || '—') + '</span></div>' +
      '<div class="kv"><b>关键主题</b><span><ul class="dlist">' + (themes || '<li>本次未列出可追溯主题</li>') + '</ul></span></div>' +
      '<div class="kv"><b>月度节奏</b><span><ul class="dlist">' + (timeline || '<li>样本不足，未生成月度节奏</li>') + '</ul></span></div>' +
      '<div class="kv"><b>反向情形</b><span style="color:#B45309">' + esc(res.counter_case || '—') + '</span></div>' +
      '<div class="kv"><b>关注</b><span>' + esc((res.watch_points || []).join('；') || '—') + '</span></div>' +
      '<div class="kv"><b>数据缺口</b><span>' + esc((res.data_gaps || []).join('；') || '无') + '</span></div>' +
      '<div class="hint mt">' + esc(res.disclaimer || d.disclaimer || '') + '</div></div>';
    $('hiNewsCount').textContent = '共 ' + (col.total || 0) + ' 条 · 展示最新 12 条';
    var news = (col.news || []).slice(0, 12);
    $('hiNews').innerHTML = news.length ? news.map(function (n) {
      return '<div class="nitem"><a href="' + esc(n.url || '#') + '" target="_blank" rel="noopener">' + esc(n.title) + '</a>' +
        '<div class="m">' + esc(n.date || '—') + ' ｜ ' + esc(n.source || '—') + ' ｜ ' + esc(n.topic || '—') + ' ｜ ' + esc(n.origin || '') + '</div>' +
        (n.summary ? '<div class="s">' + esc(n.summary) + '</div>' : '') + '</div>';
    }).join('') : '<div class="empty">该区间未采集到新闻，请检查数据源状态或缩短/调整日期范围</div>';
  }

  function loadHistoryRuns() {
    return api('/api/history/runs').then(function (r) {
      var runs = (r.data || {}).runs || [];
      $('hiRuns').innerHTML = runs.length ? runs.map(function (x) {
        return '<div class="row"><div><div class="t">' + esc(x.start_date) + ' ~ ' + esc(x.end_date) +
          ' · ' + esc(HIDIR[x.direction] || '信号不明确') + '</div><div class="d">' + esc(fmtTs(x.created_at)) +
          ' ｜ 样本 ' + esc(x.news_count) + ' 条 ｜ ' + (x.ai_available ? 'AI 研判完成' : 'AI 不可用/数据不足') +
          ' ｜ 邮件 ' + (x.delivered ? '已发送' : '未发送') + '</div></div><div class="r">' +
          (x.report_id ? '<button class="btn sm" onclick="openReport(' + x.report_id + ')">预览报告</button>' : '') +
          '</div></div>';
      }).join('') : '<div class="empty">尚无记录</div>';
    }).catch(function () { });
  }
  window.loadHistoryRuns = loadHistoryRuns;

  /* ---------------- 退出本端程序 ---------------- */
  function quitApp() {
    if (!window.confirm('退出本端程序？后台定时任务也会一并停止。')) return;
    var done = function () { $('exitMask').className = 'exitmask show'; };
    api('/api/window', 'POST', { action: 'quit' }).then(function (r) {
      if (!r.data || !r.data.ok) {
        api('/api/shutdown', 'POST', {}).then(done, done);
      } else done();
    }).catch(function () { api('/api/shutdown', 'POST', {}).then(done, done); });
  }
  window.quitApp = quitApp;

  /* ---------------- 自检 ---------------- */
  function runSelfcheck(target) {
    toast('正在检测外部依赖…');
    api('/api/selfcheck', 'GET').then(function (r) {
      var d = r.data || {}, html = '<div class="hint mb">检测时间 ' + esc(fmtTs(d.checked_at)) +
        ' ｜ 通过 ' + esc(d.passed) + '/' + esc(d.total) + ' ｜ ' + esc(d.note || '') + '</div>';
      html += (d.items || []).map(function (x) {
        return '<div class="row"><div><div class="t">' + esc(x.category) + ' · ' + esc(x.name) + '</div>' +
          '<div class="d">' + esc(x.detail) + '</div></div><div class="r"><span class="tag ' +
          (x.ok ? 'down' : 'gold') + '">' + (x.ok ? '正常' : '未通过/未配置') + '</span></div></div>';
      }).join('');
      if (target === 'market') { $('mkSources').innerHTML = html; $('mkSources').dataset.done = '1'; }
      else $('setSelfcheck').innerHTML = html;
      toast('自检完成：' + d.passed + '/' + d.total + ' 项通过');
    });
  }
  window.runSelfcheck = runSelfcheck;

  /* ---------------- 配置 ---------------- */
  function loadConfig() {
    return api('/api/config').then(function (r) {
      C = r.data;
      var s = C.smtp || {}, d = C.deepseek || {}, sch = C.push_schedule || {}, mf = C.manual_fetch || {};
      setSw('f_smtp_enabled', s.enabled);
      $('f_smtp_host').value = s.host || ''; $('f_smtp_port').value = s.port || 465;
      $('f_smtp_security').value = s.security || 'ssl_tls'; $('f_smtp_user').value = s.user || '';
      $('f_smtp_credential').value = '';
      $('f_smtp_credential').placeholder = s.credential_set ? '已录入 ' + s.credential + '（留空保持原值）' : '尚未录入';
      $('f_smtp_from_name').value = s.from_name || '';
      $('f_smtp_to').value = (s.to || []).join(', '); $('f_smtp_cc').value = (s.cc || []).join(', ');
      $('f_smtp_max_mails_per_day').value = s.max_mails_per_day || 12;
      $('f_smtp_timeout_seconds').value = s.timeout_seconds || 20;
      $('f_smtp_retry_times').value = s.retry_times || 3;
      setSw('f_smtp_html_and_text', s.html_and_text !== false);
      $('smtpState').textContent = s.enabled ? '已启用' : '未启用';
      $('smtpTip').textContent = '授权码仅存本端安全存储（' + (C.secure_backend || '') + '），界面默认掩码；不入库、不入日志、不入邮件、不外传。';

      $('f_ds_api_key').value = '';
      $('f_ds_api_key').placeholder = d.api_key_set ? '已录入 ' + d.api_key + '（留空保持原值）' : '尚未录入';
      $('f_ds_base_url').value = d.base_url || ''; $('f_ds_model_flash').value = d.model_flash || '';
      $('f_ds_model_pro').value = d.model_pro || ''; $('f_ds_max_tokens').value = d.max_tokens || 1200;
      $('f_ds_timeout_seconds').value = d.timeout_seconds || 60; $('f_ds_retry_times').value = d.retry_times || 2;
      $('f_ds_max_calls_per_day').value = d.max_calls_per_day || 40;
      $('f_ds_daily_token_budget').value = d.daily_token_budget || 200000;
      $('f_ds_prompt_version').value = d.prompt_version || '';
      setSw('f_ds_json_output', d.json_output !== false);
      $('dsState').textContent = d.api_key_set ? '已配置 Key' : '未配置 Key';

      $('f_q_start').value = (sch.quiet_hours || {}).start || '23:00';
      $('f_q_end').value = (sch.quiet_hours || {}).end || '07:00';
      $('f_max_event').value = sch.max_event_mails_per_day || 4;
      setSw('f_skip_closed', sch.skip_on_market_closed !== false);
      $('f_mf_interval').value = mf.min_interval_seconds || 60;
      $('f_mf_cache').value = mf.snapshot_cache_seconds || 300;
      setSw('f_mf_budget', mf.count_toward_daily_budget !== false);
      renderSlots(sch.slots || []);
      var ha = C.history_analysis || {};
      $('f_ha_alpha_key').value = '';
      $('f_ha_alpha_key').placeholder = ha.alpha_vantage_api_key_set ?
        '已录入 ' + ha.alpha_vantage_api_key + '（留空保持原值）' : '尚未录入（可选）';
      $('f_ha_preset').value = ha.default_preset || '6m';
      $('f_ha_horizon').value = ha.forward_horizon || '1-3m';
      $('f_ha_max_items').value = ha.max_items || 60;
      $('f_ha_max_ai').value = ha.max_ai_items || 40;
      $('f_ha_min_news').value = ha.min_news_for_direction || 8;
      $('f_ha_timeout').value = ha.per_source_timeout_seconds || 10;
      $('f_ha_deadline').value = ha.total_deadline_seconds || 22;
      $('f_ha_interval').value = (ha.min_interval_seconds === 0 ? 0 : (ha.min_interval_seconds || 20));
      setSw('f_ha_send_mail', !!ha.send_mail_by_default);
      renderHaSources(ha.sources || []);
      $('hiHorizon').value = ha.forward_horizon || '1-3m';
      $('hiMaxItems').value = ha.max_items || 60;
      $('hiSendMail').checked = !!ha.send_mail_by_default;
      if (!historyTouched) setHistoryPreset(ha.default_preset || '6m', true);
      $('setVer').textContent = '本端配置 v' + C.config_version;
      var sb = $('setSecBackend'); if (sb) sb.textContent = (C.secure_backend || '—') + ' ｜ 密钥只落本端';
      // 开机自启动：真实状态来自注册表 Run 项
      var as = C.autostart_status || {};
      setSw('f_autostart', !!as.enabled);
      $('asState').textContent = !as.supported ? '不支持' : (as.enabled ? (as.stale ? '已开启 · 路径待更新' : '已开启') : '未开启');
      $('asPath').textContent = as.command || (as.frozen ? '当前为打包版，可直接开启' : '当前为源码运行，将注册 pythonw + run.py');
      $('asTip').textContent = as.message || '开关只写入 / 删除当前 Windows 用户的启动项（注册表 Run，不需要管理员权限），随时可以在这里关掉。';
      // 每日新闻 / 每日资讯 / 每日军事新闻
      loadDailyCfg(C.daily || {}, 'f_dn', 'news');
      loadDailyCfg(C.brief || {}, 'f_bf', 'brief');
      loadDailyCfg(C.mil || {}, 'f_mn', 'mil');
      $('f_doc_dir').value = C.doc_dir || '';
    });
  }
  window.loadConfig = loadConfig;

  function renderHaSources(list) {
    $('haSourceList').innerHTML = (list || []).map(function (s, i) {
      return '<div class="row"><div><div class="t">' + esc(s.name || s.id) + '</div>' +
        '<div class="d">' + esc(s.id) + ' ｜ ' + esc(s.kind || '') + '</div></div>' +
        '<div class="r"><button class="sw' + (s.enabled ? ' on' : '') + '" data-ha="' + i +
        '" onclick="tog(this)"><i></i></button></div></div>';
    }).join('') || '<div class="empty">未配置数据源</div>';
  }
  function collectHistoryConfig() {
    var ha = (C && C.history_analysis) ? JSON.parse(JSON.stringify(C.history_analysis)) : { sources: [] };
    ha.default_preset = $('f_ha_preset').value;
    ha.forward_horizon = $('f_ha_horizon').value;
    ha.max_items = parseInt($('f_ha_max_items').value, 10) || 60;
    ha.max_ai_items = parseInt($('f_ha_max_ai').value, 10) || 40;
    ha.min_news_for_direction = parseInt($('f_ha_min_news').value, 10) || 8;
    ha.per_source_timeout_seconds = parseInt($('f_ha_timeout').value, 10) || 6;
    ha.total_deadline_seconds = parseInt($('f_ha_deadline').value, 10) || 12;
    var iv = parseInt($('f_ha_interval').value, 10);
    ha.min_interval_seconds = isNaN(iv) ? 20 : iv;
    ha.send_mail_by_default = isOn('f_ha_send_mail');
    ha.alpha_vantage_api_key = $('f_ha_alpha_key').value || '__KEEP_EXISTING__';
    var sws = document.querySelectorAll('#haSourceList .sw');
    for (var i = 0; i < sws.length; i++) {
      var idx = parseInt(sws[i].getAttribute('data-ha'), 10);
      if (ha.sources[idx]) ha.sources[idx].enabled = sws[i].className.indexOf('on') >= 0;
    }
    return ha;
  }

  function renderSlots(slots) {
    $('slotList').innerHTML = slots.length ? slots.map(function (s) {
      return '<div class="slot"><input class="time" value="' + esc(s.time) + '" onchange="editSlot(\'' + esc(s.id) + '\',\'time\',this.value)">' +
        '<select onchange="editSlot(\'' + esc(s.id) + '\',\'type\',this.value)">' +
        '<option value="daily_summary"' + (s.type === 'daily_summary' ? ' selected' : '') + '>每日总结</option>' +
        '<option value="flash"' + (s.type === 'flash' ? ' selected' : '') + '>实时快报</option>' +
        '<option value="both"' + (s.type === 'both' ? ' selected' : '') + '>两者</option></select>' +
        '<button class="sw' + (s.enabled ? ' on' : '') + '" onclick="toggleSlot(\'' + esc(s.id) + '\',this)"><i></i></button>' +
        '<span class="nt">' + esc((s.channels || []).join('+')) + ' ' + esc(s.note || '') + '</span>' +
        '<button class="mini del" onclick="delSlot(\'' + esc(s.id) + '\')">删除</button></div>';
    }).join('') : '<div class="empty">时点列表为空，仅保留事件级告警</div>';
  }
  function addSlot() {
    api('/api/schedule', 'POST', { op: 'add', time: '07:15', type: 'flash', channels: ['mail'] })
      .then(afterSlot);
  }
  window.addSlot = addSlot;
  function editSlot(id, field, v) { api('/api/schedule', 'POST', { op: 'update', id: id, [field]: v }).then(afterSlot); }
  window.editSlot = editSlot;
  function toggleSlot(id, el) { api('/api/schedule', 'POST', { op: 'toggle', id: id }).then(afterSlot); }
  window.toggleSlot = toggleSlot;
  function delSlot(id) { api('/api/schedule', 'POST', { op: 'del', id: id }).then(afterSlot); }
  window.delSlot = delSlot;
  function clearSlots() { if (confirm('确认清空本端全部推送时点？')) api('/api/schedule', 'POST', { op: 'clear' }).then(afterSlot); }
  window.clearSlots = clearSlots;
  function afterSlot(r) {
    if (!ok(r)) {
      toast('失败：' + ((r.data || {}).message || '未知错误'));
      loadConfig();   // 后端拒绝时立刻用真实配置回填，避免输入框停在非法值上
      return;
    }
    toast((r.data || {}).message || '已更新'); loadConfig(); loadState();
  }

  function collectConfig() {
    var split = function (v) { return String(v || '').split(/[,，;；\s]+/).map(function (x) { return x.trim(); }).filter(Boolean); };
    return {
      smtp: {
        enabled: isOn('f_smtp_enabled'), host: $('f_smtp_host').value.trim(),
        port: parseInt($('f_smtp_port').value, 10), security: $('f_smtp_security').value,
        user: $('f_smtp_user').value.trim(), credential: $('f_smtp_credential').value || '__KEEP_EXISTING__',
        from_name: $('f_smtp_from_name').value.trim(), to: split($('f_smtp_to').value),
        cc: split($('f_smtp_cc').value), max_mails_per_day: parseInt($('f_smtp_max_mails_per_day').value, 10),
        timeout_seconds: parseInt($('f_smtp_timeout_seconds').value, 10),
        retry_times: parseInt($('f_smtp_retry_times').value, 10),
        html_and_text: isOn('f_smtp_html_and_text')
      },
      deepseek: {
        api_key: $('f_ds_api_key').value || '__KEEP_EXISTING__',
        base_url: $('f_ds_base_url').value.trim(), model_flash: $('f_ds_model_flash').value.trim(),
        model_pro: $('f_ds_model_pro').value.trim(), max_tokens: parseInt($('f_ds_max_tokens').value, 10),
        timeout_seconds: parseInt($('f_ds_timeout_seconds').value, 10),
        retry_times: parseInt($('f_ds_retry_times').value, 10),
        max_calls_per_day: parseInt($('f_ds_max_calls_per_day').value, 10),
        daily_token_budget: parseInt($('f_ds_daily_token_budget').value, 10),
        json_output: isOn('f_ds_json_output')
      },
      history_analysis: collectHistoryConfig()
    };
  }
  function saveConfig(silent) {
    return api('/api/config', 'POST', { config: collectConfig(), summary: '设置页保存' }).then(function (r) {
      if (!ok(r)) { toast('保存失败：' + ((r.data || {}).message || '未知错误')); return false; }
      if (!silent) toast('本端配置已保存（v' + r.data.config_version + '）');
      loadConfig(); loadState(); return true;
    });
  }
  window.saveConfig = saveConfig;
  function testSmtp() {
    saveConfig(true).then(function (good) {
      if (!good) return;
      toast('正在测试 SMTP 连接…');
      api('/api/test/smtp', 'POST', {}).then(function (r) {
        toast((r.data.ok ? '✓ ' : '✗ ') + r.data.message + (r.data.server_reply ? ' (' + r.data.server_reply + ')' : ''));
      });
    });
  }
  window.testSmtp = testSmtp;
  function testMail() {
    saveConfig(true).then(function (good) {
      if (!good) return;
      toast('正在发送测试邮件…');
      api('/api/test/mail', 'POST', {}).then(function (r) {
        toast((r.data.ok ? '✓ ' : '✗ ') + (r.data.message || ''));
        loadState();
      });
    });
  }
  window.testMail = testMail;
  function testDeepseek() {
    saveConfig(true).then(function (good) {
      if (!good) return;
      toast('正在测试 DeepSeek 连接（会产生极小额调用）…');
      api('/api/test/deepseek', 'POST', {}).then(function (r) {
        toast((r.data.ok ? '✓ ' : '✗ ') + (r.data.message || '') +
          (r.data.latency_ms ? ' · ' + r.data.latency_ms + 'ms' : ''));
        loadState();
      });
    });
  }
  window.testDeepseek = testDeepseek;
  function rollbackConfig() {
    if (!confirm('恢复本端上一版本配置？')) return;
    api('/api/config/rollback', 'POST', {}).then(function (r) {
      toast(ok(r) ? '已恢复上一版本（v' + r.data.config_version + '）' : '失败：' + r.data.message);
      loadConfig(); loadState();
    });
  }
  window.rollbackConfig = rollbackConfig;
  function saveScheduleSettings() {
    var sch = (C && C.push_schedule) ? JSON.parse(JSON.stringify(C.push_schedule)) : { slots: [] };
    sch.quiet_hours = { start: $('f_q_start').value.trim(), end: $('f_q_end').value.trim(),
                        allow_major_events: (sch.quiet_hours || {}).allow_major_events !== false };
    sch.max_event_mails_per_day = parseInt($('f_max_event').value, 10);
    sch.skip_on_market_closed = isOn('f_skip_closed');
    var mf = { min_interval_seconds: parseInt($('f_mf_interval').value, 10),
               snapshot_cache_seconds: parseInt($('f_mf_cache').value, 10),
               count_toward_daily_budget: isOn('f_mf_budget'),
               send_mail_by_default: false };
    api('/api/config', 'POST', { config: { push_schedule: sch, manual_fetch: mf }, summary: '时点与策略保存' })
      .then(function (r) {
        toast(ok(r) ? '本端时点与策略已保存，调度器即时重载' : '保存失败：' + (r.data.message || ''));
        loadConfig(); loadState();
      });
  }
  window.saveScheduleSettings = saveScheduleSettings;
  function toggleMaster(el) {
    var v = el.className.indexOf('on') < 0;
    api('/api/master', 'POST', { enabled: v }).then(function (r) {
      if (ok(r)) { toast(v ? '本端定时推送已开启' : '本端定时推送已关闭'); loadState(); }
      else { toast('操作失败'); loadState(); }
    });
  }
  window.toggleMaster = toggleMaster;

  /* ---------------- 持仓 / 交易 / 回测 ---------------- */
  function saveHoldings() {
    api('/api/holdings', 'POST', { grams: $('hdGrams').value, cost_per_gram: $('hdCost').value, note: $('hdNote').value })
      .then(function (r) { toast(ok(r) ? r.data.message : '失败：' + (r.data.message || '')); loadState(); });
  }
  window.saveHoldings = saveHoldings;
  function openTradeConfirm() {
    $('cmType').textContent = $('tdType').value;
    $('cmAmount').textContent = '¥ ' + $('tdAmount').value + '（示例）';
    $('cmInst').textContent = (S ? S.instance_id : '本端') + ' / 本端';
    $('tradeModal').className = 'modal-mask show';
  }
  window.openTradeConfirm = openTradeConfirm;
  function closeTradeConfirm() { $('tradeModal').className = 'modal-mask'; }
  window.closeTradeConfirm = closeTradeConfirm;
  function confirmTrade() {
    api('/api/trade', 'POST', { type: $('tdType').value, amount: $('tdAmount').value }).then(function (r) {
      closeTradeConfirm(); toast(r.data.message || '已完成'); loadState();
    });
  }
  window.confirmTrade = confirmTrade;
  function runBacktest() {
    api('/api/backtest', 'POST', {}).then(function (r) {
      var d = r.data || {};
      $('stBacktest').innerHTML = d.ok ?
        '<div class="sigbox"><div class="kv"><b>样本数</b><span>' + esc(d.samples) + ' 个本端快照</span></div>' +
        '<div class="kv"><b>区间低 / 高</b><span>' + fmt(d.range_low) + ' / ' + fmt(d.range_high) + ' 美元/盎司</span></div>' +
        '<div class="kv"><b>区间变动</b><span class="' + cls(d.period_change_pct) + '">' + pct(d.period_change_pct) + '</span></div>' +
        '<div class="warn mt">' + esc(d.note) + '</div></div>' :
        '<div class="empty">' + esc(d.message || '测算失败') + '</div>';
    });
  }
  window.runBacktest = runBacktest;

  /* ---------------- 无标题栏桌面窗口控制（WebView2 宿主 / Edge --app 回退） ---------------- */
  function initWindowControls() {
    if (document.body.className.indexOf('kind-desktop') < 0) return;
    var host = 'edge';   // webview：顶栏拖动由宿主按物理光标位置移动；edge：裁剪 + 逐帧移动（回退）
    // pywebview 注入的 js_api（拖动）可能晚于本脚本就绪，用到时再取，不缓存
    function wvApi() { return (window.pywebview && window.pywebview.api && window.pywebview.api.drag_start) ? window.pywebview.api : null; }
    var wvDragOn = false, wvDragging = false;
    // 页面测得标题栏高度（CSS px）后交给 Win32 裁剪；窗口未创建好时后端会暂存
    function pushCaption() {
      var cap = Math.round((window.outerHeight || 0) - (window.innerHeight || 0));
      var dpr = window.devicePixelRatio || 1;
      api('/api/window', 'POST', {
        action: 'caption', cap: cap > 0 ? cap : 30, dpr: dpr
      });
    }
    var tries = 0, bound = false;
    // compact-win = 普通窗口形态；最大化时顶栏不再是拖动区，避免把铺满的窗口拖散
    function setCompact(v) { document.body.classList.toggle('compact-win', !!v); }
    function setDrag(on) {
      wvDragOn = !!on && host === 'webview';
      var els = [document.querySelector('.brand'), $('dragFill')];
      for (var i = 0; i < els.length; i++) {
        if (els[i]) els[i].classList.toggle('gdk-drag', wvDragOn);
      }
      if (!wvDragOn && wvDragging) { wvDragging = false; var a0 = wvApi(); if (a0) a0.drag_end(); }
    }
    // 顶栏拖动：页面只上报「开始 / 移动 / 结束」，位置由宿主读取物理光标计算，终点不会偏
    function inDragZone(e) {
      var t = e.target;
      return !!(t && t.closest && t.closest('.brand, #dragFill'));
    }
    document.addEventListener('mousedown', function (e) {
      var a = wvApi();
      if (!wvDragOn || !a || e.button !== 0 || !inDragZone(e)) return;
      wvDragging = true;
      a.drag_start();
      e.preventDefault();
    });
    document.addEventListener('mousemove', function () { if (wvDragging) { var a = wvApi(); if (a) a.drag_move(); } });
    function endDrag() { if (wvDragging) { wvDragging = false; var a = wvApi(); if (a) a.drag_end(); } }
    document.addEventListener('mouseup', endDrag);
    document.addEventListener('mouseleave', endDrag);
    // 最小化后还原：通知宿主检查 WebView2 子窗口是否停在最小化位置（是则触发一次重排），
    // 否则页面会出现「画不出来 / 点哪都没反应」，直到下一次改窗口尺寸
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState !== 'visible' || host !== 'webview') return;
      var a = wvApi(); if (a && a.restored) a.restored();
      setTimeout(syncFromHost, 300);
    });
    function toggleMax() {
      api('/api/window', 'POST', { action: 'maximize' }).then(function (r) {
        if (r.data && r.data.ok) { setCompact(!r.data.maximized); setDrag(!r.data.maximized); }
      });
    }
    function syncFromHost() {
      api('/api/window', 'POST', { action: 'info' }).then(function (r) {
        if (!ok(r)) return;
        var max = !!(r.data || {}).maximized;
        setCompact(!max); setDrag(!max);
      });
    }
    // 窗口是异步创建的，短轮询直到后端能拿到窗口句柄
    function probe() {
      api('/api/window', 'POST', { action: 'info' }).then(function (r) {
        if (!ok(r)) { if (tries++ < 16) setTimeout(probe, 450); return; }
        host = (r.data || {}).host || 'edge';
        var max = !!(r.data || {}).maximized;
        setCompact(host === 'webview' ? !max : !!(r.data || {}).compact);
        if (bound) return;
        bound = true;
        setup();
      }).catch(function () { if (tries++ < 16) setTimeout(probe, 700); });
    }
    function setup() {
      var ctrls = $('winCtrls'); if (ctrls) ctrls.className = 'winctrls show';
      $('winMin').onclick = function () { api('/api/window', 'POST', { action: 'minimize' }); };
      $('winMax').onclick = toggleMax;
      var rt;
      window.addEventListener('resize', function () {
        clearTimeout(rt);
        if (host === 'webview') { rt = setTimeout(syncFromHost, 250); return; }
        rt = setTimeout(function () { api('/api/window', 'POST', { action: 'refresh' }); }, 350);
      });
      var bar = document.querySelector('.topbar');
      if (!bar) return;
      if (host === 'webview') {
        setDrag(document.body.classList.contains('compact-win'));
        // 任务栏还原/最大化等宿主侧变化后同步按钮状态
        setTimeout(syncFromHost, 300);
        bar.addEventListener('dblclick', function (e) {
          if (e.target.closest('button,input,select,a,.winctrls')) return;
          toggleMax();
        });
        return;
      }
      pushCaption();
      // Chromium 启动时会在页面加载后自己再摆一次窗口位置，把裁剪/上移覆盖掉；
      // 页面稳定后再补发几次，保证最终停在无边框形态
      setTimeout(pushCaption, 1000);
      setTimeout(pushCaption, 2600);
      window.addEventListener('load', pushCaption);
      bar.className = 'topbar dragzone';
      var drag = false, lx = 0, ly = 0, busy = false, moved = false;
      // 拖动请求按帧合并：一帧最多发一次，位移累加后一起发，避免每个 mousemove
      // 都打一次 HTTP 造成窗口一顿一顿/闪
      var accX = 0, accY = 0, raf = 0;
      function schedule() {
        if (raf || busy) return;
        raf = requestAnimationFrame(flush);
      }
      function flush() {
        raf = 0;
        if (busy || (!accX && !accY)) return;
        var dx = accX, dy = accY; accX = 0; accY = 0; busy = true;
        api('/api/window', 'POST', { action: 'move', dx: dx, dy: dy })
          .then(function (r) {
            busy = false;
            if (r.data && r.data.restored) { setCompact(true); drag = false; }
            else schedule();
          }, function () { busy = false; schedule(); });
      }
      bar.addEventListener('mousedown', function (e) {
        if (e.target.closest('button,input,select,a,.winctrls')) return;
        drag = true; moved = false;
        // 必须用屏幕坐标：窗口跟着鼠标移动后 clientX 几乎不变，用 client 差值会越拖越慢
        lx = e.screenX; ly = e.screenY;
        accX = 0; accY = 0;
        e.preventDefault();
      });
      document.addEventListener('mousemove', function (e) {
        if (!drag) return;
        var dx = e.screenX - lx, dy = e.screenY - ly;
        // 先累计 4px 再发第一次移动：避免手抖把最大化窗口误还原
        if (!moved) {
          if (Math.abs(dx) + Math.abs(dy) < 4) return;
          moved = true;
        }
        lx = e.screenX; ly = e.screenY;
        if (!dx && !dy) return;
        accX += dx; accY += dy;
        schedule();
      });
      document.addEventListener('mouseup', function () {
        if (accX || accY) flush();
        drag = false;
      });
    }
    probe();
  }

  /* ---------------- 每日新闻 / 每日资讯 ---------------- */
  var DK = 'news', DL = null, DLSRC = 'all', DLPATHS = [];
  var DLK = { news: '每日新闻', brief: '每日资讯', mil: '每日军事新闻' };
  var PERIOD_CN = { '1d': '每天', '1w': '每周', '1m': '每月', '6m': '每半年', '1y': '每年', 'custom': '自定义' };
  var PERIOD_ORDER = ['1d', '1w', '1m', '6m', '1y', 'custom'];
  /* 汇总周期是多选：设置页是一组同名复选框，配置里是列表 digest_periods（旧配置的单值 digest_period 兼容读取） */
  function periodBoxes(p) { return document.getElementsByName(p.slice(2) + '_per'); }
  function periodsOf(sec) {
    sec = sec || {};
    if (Array.isArray(sec.digest_periods)) return sec.digest_periods;
    return sec.digest_period ? [sec.digest_period] : ['1m'];
  }
  function setPeriods(p, list) {
    var bs = periodBoxes(p);
    for (var i = 0; i < bs.length; i++) bs[i].checked = list.indexOf(bs[i].value) >= 0;
  }
  function getPeriods(p) {
    var bs = periodBoxes(p), out = [];
    for (var i = 0; i < bs.length; i++) if (bs[i].checked) out.push(bs[i].value);
    return PERIOD_ORDER.filter(function (v) { return out.indexOf(v) >= 0; });
  }
  function periodText(sec) {
    var ps = periodsOf(sec);
    if (!ps.length) return '未勾选（不做周期汇总）';
    return ps.map(function (v) {
      return PERIOD_CN[v] || v; }).join(' + ').replace('自定义', function () {
      return '自定义（每 ' + ((sec && sec.digest_custom_days) || 15) + ' 天）'; });
  }
  var DL_ORDER = ['cctv_news', 'cctv_china', 'cctv_world', 'cctv_society', 'cctv_tech',
                  'mil_81', 'mil_people', 'mil_cctv', 'mil_cctv_intl',
                  'mil_defensenews', 'mil_defenseone', 'mil_militarytimes', 'mil_airforcetimes',
                  'mil_navalnews', 'mil_taskpurpose', 'mil_globaltimes', 'mil_france24', 'mil_un',
                  'mil_reuters', 'mil_ap',
                  'weibo', 'baidu', 'douyin', 'bilibili', 'zhihu'];

  function loadDailyCfg(sec, p, kind) {
    setSw(p + '_enabled', sec.enabled !== false);
    $(p + '_time').value = sec.time || '20:30';
    setSw(p + '_ai', sec.ai_enabled !== false);
    setSw(p + '_mail', !!sec.send_mail);
    setPeriods(p, periodsOf(sec));
    $(p + '_custom').value = sec.digest_custom_days || 15;
    $(p + '_dtime').value = sec.digest_time || '21:30';
    setSw(p + '_dmail', sec.digest_send_mail !== false);
    if (kind === 'news') {
      setSw('f_dn_cctv', sec.cctv_enabled !== false); $('f_dn_cctv_max').value = sec.cctv_max_items || 20;
      setSw('f_dn_hot', sec.hot_enabled !== false); $('f_dn_hot_max').value = sec.hot_max_items || 10;
      var hs = sec.hot_sources || {};
      ['weibo', 'baidu', 'douyin', 'bilibili', 'zhihu'].forEach(function (k) {
        $('f_dn_hs_' + k).checked = hs[k] !== false; });
    } else if (kind === 'mil') {
      $('f_mn_max').value = sec.max_items || 30;
    } else {
      $('f_bf_max').value = sec.max_items || 40;
    }
  }
  function saveDailySettings(kind) {
    var p = kind === 'news' ? 'f_dn' : (kind === 'mil' ? 'f_mn' : 'f_bf');
    var sec = { enabled: isOn(p + '_enabled'), time: $(p + '_time').value.trim(),
                ai_enabled: isOn(p + '_ai'), send_mail: isOn(p + '_mail'),
                digest_periods: getPeriods(p),
                digest_custom_days: parseInt($(p + '_custom').value, 10) || 15,
                digest_time: $(p + '_dtime').value.trim(), digest_send_mail: isOn(p + '_dmail') };
    if (kind === 'news') {
      sec.cctv_enabled = isOn('f_dn_cctv'); sec.cctv_max_items = parseInt($('f_dn_cctv_max').value, 10) || 20;
      sec.hot_enabled = isOn('f_dn_hot'); sec.hot_max_items = parseInt($('f_dn_hot_max').value, 10) || 10;
      sec.hot_sources = { weibo: $('f_dn_hs_weibo').checked, baidu: $('f_dn_hs_baidu').checked,
                          douyin: $('f_dn_hs_douyin').checked, bilibili: $('f_dn_hs_bilibili').checked,
                          zhihu: $('f_dn_hs_zhihu').checked };
    } else if (kind === 'mil') {
      sec.max_items = parseInt($('f_mn_max').value, 10) || 30;
    } else {
      sec.max_items = parseInt($('f_bf_max').value, 10) || 40;
    }
    /* 配置段名与页签 kind 并不同名：每日新闻写 daily，资讯写 brief，军事写 mil */
    var SECT = { news: 'daily', brief: 'brief', mil: 'mil' };
    var config = {}; config[SECT[kind] || kind] = sec;
    if (kind === 'news') config.doc_dir = $('f_doc_dir').value.trim();
    return api('/api/config', 'POST', { config: config, summary: DLK[kind] + '设置保存' }).then(function (r) {
      if (!ok(r)) { toast('保存失败：' + ((r.data || {}).message || '未知错误')); return false; }
      toast(DLK[kind] + '设置已保存（v' + r.data.config_version + '），调度器即时生效');
      loadConfig(); loadState(); return true;
    });
  }
  window.saveDailySettings = saveDailySettings;
  function toggleAutostart(el) {
    var v = el.className.indexOf('on') < 0;
    api('/api/autostart', 'POST', { enabled: v }).then(function (r) {
      var d = r.data || {};
      if (!d.ok) { toast('操作失败：' + (d.message || '未知错误')); }
      else { toast(v ? '已开启开机自启动' : '已关闭开机自启动'); if (d.stale) toast(d.message); }
      loadConfig();
    }).catch(function (e) { toast('请求失败：' + e); });
  }
  window.toggleAutostart = toggleAutostart;

  function mdInline(s) {
    return esc(s)
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
      .replace(/`([^`]+)`/g, '<code>$1</code>');
  }
  /* 与后端 md_to_html 同一套极简 Markdown：标题 / 列表 / 引用 / 粗体 / 链接 / 分隔线 */
  function mdToHtml(md) {
    var out = [], list = '';
    var close = function () { if (list) { out.push('</' + list + '>'); list = ''; } };
    String(md || '').split(/\r?\n/).forEach(function (raw) {
      var line = raw.trim(), m;
      if (!line) { close(); return; }
      m = /^(#{1,4})\s+(.*)$/.exec(line);
      if (m) { close(); var lv = Math.min(4, m[1].length + 1); out.push('<h' + lv + '>' + mdInline(m[2]) + '</h' + lv + '>'); return; }
      if (/^(-{3,}|\*{3,})$/.test(line)) { close(); out.push('<hr>'); return; }
      if (/^&gt;/.test(line)) { close(); out.push('<blockquote>' + mdInline(line.replace(/^(&gt;)+\s*/, '')) + '</blockquote>'); return; }
      m = /^[-*+]\s+(.*)$/.exec(line);
      if (m) { if (list !== 'ul') { close(); out.push('<ul>'); list = 'ul'; } out.push('<li>' + mdInline(m[1]) + '</li>'); return; }
      m = /^\d+[.、)]\s*(.*)$/.exec(line);
      if (m) { if (list !== 'ol') { close(); out.push('<ol>'); list = 'ol'; } out.push('<li>' + mdInline(m[1]) + '</li>'); return; }
      close(); out.push('<p>' + mdInline(line) + '</p>');
    });
    close();
    return out.join('\n');
  }
  function setDailyKind(k) {
    DK = (k === 'brief' || k === 'mil') ? k : 'news'; DLSRC = 'all';
    var t = document.querySelectorAll('#dlKind .ktab');
    for (var i = 0; i < t.length; i++) t[i].className = 'ktab' + (t[i].getAttribute('data-kind') === DK ? ' on' : '');
    var subs = {
      news: '央视新闻 + 互联网热搜 → AI 总结 → 每日本地文档 → 周期汇总发送到邮箱',
      brief: '「资讯与 AI 研判」页当日要闻 + 当日研判 → 每日本地文档 → 周期汇总发送到邮箱',
      mil: '中国军网 / 人民网军事（国内）+ 央视国际军事（国际）→ AI 总结（含与黄金积存买卖的关系）→ 每日本地文档 → 周期汇总发送到邮箱'
    };
    $('dlSub').textContent = subs[DK];
    $('dlRunBtn').textContent = DK === 'news' ? '立即采集并 AI 总结'
      : (DK === 'mil' ? '立即采集军事要闻并 AI 总结' : '立即获取资讯并 AI 总结');
    $('dlListTitle').textContent = DK === 'news' ? '今日新闻列表'
      : (DK === 'mil' ? '今日军事新闻列表' : '今日资讯列表（来自「资讯与 AI 研判」）');
    DL = null;
    $('dlAi').innerHTML = '<div class="empty">加载中…</div>';
    $('dlList').innerHTML = ''; $('dlSrcTabs').innerHTML = ''; $('dlDocs').innerHTML = '<div class="empty">加载中…</div>';
    loadDaily();
  }
  window.setDailyKind = setDailyKind;
  function setDailySrc(s) { DLSRC = s; renderDaily(); }
  window.setDailySrc = setDailySrc;
  function loadDaily() {
    var k = DK;   /* 切页签时旧请求可能后到：只接受当前页签的返回 */
    return api('/api/daily?kind=' + k).then(function (r) {
      if (k !== DK) return;
      if (ok(r)) { DL = r.data; renderDaily(); }
      else toast('读取失败：' + ((r.data || {}).message || '未知错误'));
    })
      .catch(function () { $('dlAi').innerHTML = '<div class="empty">加载失败，请点击顶栏「刷新」或切换页签重试</div>'; });
  }
  window.loadDaily = loadDaily;
  function renderDaily() {
    var d = DL; if (!d) return;
    var doc = d.today_doc, cfg = d.config || {};
    var c = {}; try { c = (doc && JSON.parse(doc.counts_json || '{}')) || {}; } catch (e) { c = {}; }
    $('dlAiTitle').textContent = '今日 AI 总结 · ' + d.today;
    $('dlToday').textContent = d.today;
    if (doc && doc.summary_md) {
      $('dlAi').innerHTML = mdToHtml(doc.summary_md);
      $('dlAiTime').textContent = '生成于 ' + fmtTs(doc.generated_at);
      $('dlAiNote').textContent = DK === 'mil'
        ? '由 DeepSeek 归纳当日军事要闻，并单列「地缘风险与黄金」与「对黄金积存买卖的参考」；只给条件化提示，不构成投资建议。'
        : '由 DeepSeek 根据当日清单归纳，仅陈述事实，不构成投资建议。';
    } else {
      $('dlAi').innerHTML = '<div class="empty">' + (doc && doc.ai_reason && !doc.summary_md ? '今日未生成 AI 总结：' + esc(doc.ai_reason)
        : (cfg.enabled === false ? '每日' + (DK === 'news' ? '新闻' : '资讯') + '已在设置中关闭' : '今日尚未生成，可点右上角按钮立即生成')) + '</div>';
      $('dlAiTime').textContent = doc ? fmtTs(doc.generated_at) : '—';
      $('dlAiNote').textContent = '';
    }
    var per = periodText(cfg);
    var stats = DK === 'news'
      ? [['央视新闻', c.cctv === undefined ? '—' : c.cctv, '条'], ['互联网热搜', c.hot === undefined ? '—' : c.hot, '条'],
         ['热搜平台', c.hot_sources ? c.hot_sources.length : '—', '个'], ['文档', doc ? '已生成' : '未生成', '']]
      : DK === 'mil'
      ? [['国内军事', c.mil_domestic === undefined ? '—' : c.mil_domestic, '条'], ['国际军事', c.mil_world === undefined ? '—' : c.mil_world, '条'],
         ['文档', doc ? '已生成' : '未生成', ''], ['AI 总结', doc ? (doc.ai_available ? '已生成' : '未生成') : '—', '']]
      : [['当日要闻', c.brief === undefined ? '—' : c.brief, '条'], ['当日 AI 研判', c.signal ? '已纳入' : '暂无', ''],
         ['文档', doc ? '已生成' : '未生成', ''], ['AI 总结', doc ? (doc.ai_available ? '已生成' : '未生成') : '—', '']];
    $('dlSummary').innerHTML = '<div class="gtstats" style="grid-template-columns:repeat(4,1fr)">' +
      stats.map(function (s) { return '<div class="st"><small>' + s[0] + '</small><b>' + s[1] + '</b> ' + s[2] + '</div>'; }).join('') + '</div>' +
      '<div class="kv"><b>正文本机保存</b><span>' + (d.article
        ? (d.article.with_content + ' / ' + d.article.total + ' 条' + (d.article.exists ? ' · 已写入 ' + String(d.article.path || '').split(/[\\/]/).pop() : ' · 文件未生成'))
        : '—') + '</span></div>' +
      '<div class="kv"><b>下一次生成</b><span>' + esc(d.next_daily) + '</span></div>' +
      '<div class="kv"><b>下一次汇总</b><span>' + esc(d.next_digest) + '</span></div>' +
      '<div class="kv"><b>汇总周期</b><span>' + esc(per) + ' · ' + (cfg.digest_send_mail ? '汇总后发邮箱' : '汇总后不发邮箱') +
        ' · 每日' + (cfg.send_mail ? '发邮箱' : '不发邮箱') + '</span></div>' +
      '<div class="kv" title="' + esc(d.doc_dir) + '"><b>文档目录</b><span style="word-break:break-all">' + esc(d.doc_dir) + '</span></div>';
    var gs = d.digests || [];
    $('dlDigest').innerHTML = gs.length ? gs.slice(0, 5).map(function (g) {
      return '<div class="kv"><b>' + esc(String(g.start_date).slice(2)) + '</b><span>' + esc(g.label) + ' · ' + g.days + ' 天' +
        (g.ai_available ? '' : ' · AI 未生成') + (g.mailed ? ' · 已发邮箱' : ' · 未发邮箱') + '</span></div>'; }).join('')
      : '<div class="empty">尚无汇总记录</div>';
    $('dlDigestBtn').textContent = cfg.digest_send_mail ? '立即汇总并发信' : '立即汇总（不发邮箱）';
    renderDailyList();
    DLPATHS = [];
    var docs = (d.docs || []).slice(0, 48).map(function (x) {
      DLPATHS.push(x.path); var i = DLPATHS.length - 1, xc = x.counts || {};
      return '<div class="dc" onclick="viewDoc(' + i + ')" title="' + esc(x.path) + '"><b>' + esc(x.day) + '</b><small>' +
        (x.ai_available ? 'AI 总结' : '无 AI 总结') + ' · ' + (DK === 'news' ? (xc.cctv || 0) + '+' + (xc.hot || 0) + ' 条'
          : (DK === 'mil' ? (xc.mil_domestic || 0) + '+' + (xc.mil_world || 0) + ' 条' : (xc.brief || 0) + ' 条')) +
        (xc.content ? ' · 正文 ' + xc.content + ' 篇' : '') + '</small></div>';
    }).join('');
    var dgs = gs.slice(0, 12).map(function (g) {
      DLPATHS.push(g.path); var i = DLPATHS.length - 1;
      return '<div class="dc dg" onclick="viewDoc(' + i + ')" title="' + esc(g.path) + '"><b>' + esc(g.label) + '</b><small>' + g.days + ' 天' +
        (g.mailed ? ' · 已发邮箱' : ' · 未发邮箱') + '</small></div>';
    }).join('');
    $('dlDocs').innerHTML = '<div class="hint" style="margin-bottom:8px">每日文档（每天一份）</div><div class="docgrid">' +
      (docs || '<div class="empty" style="grid-column:1/-1">尚无每日文档</div>') + '</div>' +
      (dgs ? '<div class="hint" style="margin:14px 0 8px">周期汇总（AI 二次归纳）</div><div class="docgrid">' + dgs + '</div>' : '');
  }
  function renderDailyList() {
    var items = (DL && DL.items) || [], names = {}, srcs = [];
    items.forEach(function (it) { if (names[it.src] === undefined) { names[it.src] = it.src_name; srcs.push(it.src); } });
    srcs.sort(function (a, b) { return DL_ORDER.indexOf(a) - DL_ORDER.indexOf(b); });
    if (DLSRC !== 'all' && names[DLSRC] === undefined) DLSRC = 'all';
    var html = '';
    var one = function (it, i) {
      var hv = it.hot_value ? '<span class="meta"> · 热度 ' + (it.hot_value >= 10000 ? Math.round(it.hot_value / 1000) / 10 + ' 万' : it.hot_value) + '</span>' : '';
      var pub = String(it.published_at_bj || '').slice(11, 16);
      var key = it.akey || '';
      var title = key
        ? '<a href="javascript:void(0)" onclick="viewArticle(\'' + key + '\')" title="看本机保存的正文">' + esc(it.title) + '</a>'
        : (it.url ? '<a href="' + esc(it.url) + '" target="_blank" rel="noopener">' + esc(it.title) + '</a>' : esc(it.title));
      var mark = (key && it.has_content) ? '<span class="meta"> · 本机正文</span>'
        : (key && it.art_status === 'skip' ? '<span class="meta"> · 热搜词条无正文</span>' : '');
      return '<li>' + title +
        (pub ? '<span class="meta"> · ' + pub + '</span>' : '') + (DK === 'brief' ? '<span class="meta"> · ' + esc(it.source || '—') + '</span>' : '') + mark + hv +
        (it.summary ? '<div class="sum">' + esc(it.summary) + '</div>' : '') + '</li>';
    };
    if (DK === 'brief') {
      html = items.length ? '<ol class="nlist">' + items.map(one).join('') + '</ol>'
        : '<div class="empty">今日暂无已采集的财经要闻，可到「资讯与 AI 研判」页点「立即获取资讯」，或点右上角按钮</div>';
      $('dlSrcTabs').innerHTML = '';
    } else {
      $('dlSrcTabs').innerHTML = ['all'].concat(srcs).map(function (s) {
        return '<button class="ktab' + (s === DLSRC ? ' on' : '') + '" onclick="setDailySrc(\'' + s + '\')">' +
          esc(s === 'all' ? '全部' : names[s]) + '</button>'; }).join('');
      var groups = srcs.filter(function (s) { return DLSRC === 'all' || s === DLSRC; });
      if (!groups.length) html = '<div class="empty">今日尚未采集，点右上角「立即采集并 AI 总结」</div>';
      groups.forEach(function (s) {
        html += '<div class="ch" style="margin-top:12px"><h3>' + esc(names[s]) + '</h3><span class="sub">' +
          items.filter(function (it) { return it.src === s; }).length + ' 条</span></div><ol class="nlist">' +
          items.filter(function (it) { return it.src === s; }).map(one).join('') + '</ol>';
      });
    }
    $('dlList').innerHTML = html;
    $('dlListSub').textContent = DK === 'news'
      ? '按栏目 / 平台排名排列，点标题看本机保存的正文'
      : (DK === 'mil' ? '国内（中国军网 / 人民网军事）+ 国际（央视国际军事），点标题看本机保存的正文'
        : '「资讯与 AI 研判」页当日要闻，点标题看本机保存的正文');
  }
  function viewDocPath(p) {
    if (!p) { toast('这份文档没有路径'); return; }
    api('/api/daily?kind=' + DK + '&path=' + encodeURIComponent(p)).then(function (r) {
      var d = (r && r.data) || {};
      if (!ok(r)) { toast(d.message || '读取失败：文件可能被移动或删除了'); return; }
      $('docTitle').textContent = String(p).split(/[\\/]/).pop();
      $('docPath').textContent = p;
      $('docBody').innerHTML = mdToHtml(d.markdown || '');
      $('docModal').className = 'modal-mask show';
    }).catch(function () { toast('读取失败，请检查文档目录是否可访问'); });
  }
  function viewDoc(i) { viewDocPath(DLPATHS[i]); }
  window.viewDoc = viewDoc;
  /* 今日 AI 总结卡点击 → 打开今天这份完整文档 */
  function viewTodayDoc() {
    var doc = DL && DL.today_doc;
    if (!doc || !doc.path) { toast('今日还没有生成文档，可点右上角按钮立即生成'); return; }
    var i = DLPATHS.indexOf(doc.path);
    if (i >= 0) viewDoc(i); else viewDocPath(doc.path);
  }
  window.viewTodayDoc = viewTodayDoc;
  /* 列表点标题 → 读本机保存的正文（没存过就让后端按需抓一次） */
  var ART = null;
  function articleItem(key) {
    var items = (DL && DL.items) || [];
    for (var i = 0; i < items.length; i++) if (items[i].akey === key) return items[i];
    return {};
  }
  function viewArticle(key) {
    var it = articleItem(key) || {};
    ART = null;
    $('artTitle').textContent = it.title || '正文';
    $('artMeta').textContent = '正在读取本机正文…';
    $('artBody').innerHTML = '<div class="empty">正在读取…</div>';
    $('artOpen').style.display = 'none';
    $('artModal').className = 'modal-mask show';
    api('/api/daily/article?key=' + encodeURIComponent(key) +
        '&url=' + encodeURIComponent(it.url || '') +
        '&title=' + encodeURIComponent(it.title || '')).then(function (r) {
      var d = (r && r.data) || {};
      ART = d;
      if (!ok(r)) {
        $('artMeta').textContent = '';
        $('artBody').innerHTML = '<div class="empty">' + esc(d.message || '读取失败') + '</div>';
        return;
      }
      var meta = [it.src_name || it.source || '', it.published_at_bj ? String(it.published_at_bj).slice(11, 16) : '']
        .filter(function (x) { return !!x; }).join(' · ');
      $('artMeta').textContent = meta + (d.chars ? ' · ' + d.chars + ' 字' : '') +
        (d.fetched_at ? ' · 本机保存于 ' + fmtTs(d.fetched_at) : '');
      $('artBody').innerHTML = (d.status === 'ok' && d.content)
        ? d.content.split(/\n{2,}/).map(function (p) { return '<p>' + esc(p) + '</p>'; }).join('')
        : '<div class="empty">' + esc(d.message || '这篇没抓到正文') + '</div>';
      if (d.url) { $('artOpen').href = d.url; $('artOpen').style.display = ''; }
    }).catch(function () {
      $('artMeta').textContent = '';
      $('artBody').innerHTML = '<div class="empty">读取失败，请稍后重试</div>';
    });
  }
  window.viewArticle = viewArticle;
  function closeArticle() { $('artModal').className = 'modal-mask'; }
  window.closeArticle = closeArticle;
  function openArticleFile() {
    api('/api/daily/article/open', 'POST', { kind: DK, day: (DL && DL.today) || '' }).then(function (r) {
      var d = (r && r.data) || {};
      toast(ok(r) ? '已用默认浏览器打开本地正文文件' : (d.message || '打开失败'));
    });
  }
  window.openArticleFile = openArticleFile;
  function closeDoc() { $('docModal').className = 'modal-mask'; }
  window.closeDoc = closeDoc;
  function runDaily() {
    var b = $('dlRunBtn'); b.disabled = true; b.textContent = '正在生成…';
    toast('正在' + (DK === 'news' ? '采集央视新闻与热搜' : (DK === 'mil' ? '采集国内与国际军事要闻' : '获取当日资讯')) + '并做 AI 总结，约需 10–40 秒');
    api('/api/daily/run', 'POST', { kind: DK }).then(function (r) {
      b.disabled = false; b.textContent = DK === 'news' ? '立即采集并 AI 总结'
        : (DK === 'mil' ? '立即采集军事要闻并 AI 总结' : '立即获取资讯并 AI 总结');
      var d = r.data || {};
      if (!ok(r)) { toast('失败：' + (d.message || '未知错误')); }
      else {
        toast(DLK[DK] + ' 已生成（' + ((d.items) || 0) + ' 条' + (d.ai && d.ai.available ? '，AI 总结完成' : '，AI 未生成：' + (d.ai && d.ai.reason || '—')) + '）');
      }
      loadDaily(); loadState();
    }).catch(function (e) { b.disabled = false; toast('请求失败：' + e); });
  }
  window.runDaily = runDaily;
  function runDigestNow() {
    var cfg = (DL && DL.config) || {}, ps = periodsOf(cfg);
    if (!ps.length) { toast('未勾选任何汇总周期，请先到设置页选择'); return; }
    if (cfg.digest_send_mail && !confirm('将按已勾选的周期（' + periodText(cfg) + '）各做一次 AI 汇总，并各发一封邮件，继续？')) return;
    var b = $('dlDigestBtn'); b.disabled = true; b.textContent = '正在汇总…';
    api('/api/daily/digest', 'POST', { kind: DK, force: true }).then(function (r) {
      b.disabled = false; b.textContent = '立即汇总';
      var d = r.data || {};
      if (!ok(r)) { toast(d.not_due ? (d.message || '未到汇总周期') : '失败：' + (d.message || '未知错误')); }
      else {
        var rs = d.results || [d], names = rs.filter(function (x) { return x.ok; }).map(function (x) {
          return (PERIOD_CN[x.period] || x.period) + ' ' + x.days + ' 天'; });
        toast('汇总完成 ' + names.length + ' 份：' + names.join('、') +
          (d.ai && d.ai.available ? '，AI 完成' : '，AI 未生成') +
          (d.delivery ? (d.delivery.ok ? '，邮件已发送' : '，邮件发送失败') : ''));
      }
      loadDaily(); loadState();
    }).catch(function (e) { b.disabled = false; b.textContent = '立即汇总'; toast('请求失败：' + e); });
  }
  window.runDigestNow = runDigestNow;
  function openDocFolder() {
    api('/api/daily/open', 'POST', { kind: DK }).then(function (r) {
      var d = r.data || {}; if (!ok(r)) toast('打开失败：' + (d.message || '未知错误'));
    }).catch(function (e) { toast('请求失败：' + e); });
  }
  window.openDocFolder = openDocFolder;

  /* ---------------- 启动 ---------------- */
  function clock() {
    var d = new Date();
    $('clock').textContent = ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2) + ':' + ('0' + d.getSeconds()).slice(-2);
  }
  clock(); setInterval(clock, 1000);
  setHistoryPreset('6m', true);
  initWindowControls();
  loadState();
  loadHistoryRuns();
  setTimeout(loadConfig, 300);
  setInterval(function () { if (document.visibilityState === 'visible') loadState(); }, 30000);
  // 支持 ?page=xxx 直接打开指定模块（便于快捷方式与分享链接）
  var pm = /[?&]page=([a-z0-9-]+)/i.exec(location.search);
  if (pm) go(pm[1].toLowerCase());
  // 支持 ?page=daily&kind=mil 直达某个每日模块页签
  var km = /[?&]kind=(news|brief|mil)/i.exec(location.search);
  if (km) setDailyKind(km[1].toLowerCase());
})();
