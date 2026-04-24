// ======================== 🧠 CONSENSUS 4/5 — Claude's own design ========================
// Концепция: не ловить максимум сигналов, а брать только идеальные условия.
// Сигнал проходит ТОЛЬКО если минимум 4 из 5 независимых систем согласны.
// Плюс защита от просадки: HUD краснеет и ставит STOP при плохом рынке.
// ========================================================================================

export const meta = {
  name: "CONSENSUS 4/5",
  defaultParams: {
    // ===== ЭКСПИРАЦИЯ =====
    expiryBars: 2,
    entryMode: "nextBarOpen",

    // ===== ДОГОНЫ (МАРТИНГЕЙЛ) =====
    martingaleSteps: 0,
    drawBehavior: "continue",

    // ===== КОНСЕНСУС =====
    minConsensus: 4,
    requireAll5OnWeekend: true,

    // ===== 1. RSI-QQE =====
    rsiPeriod: 14,
    rsiSmoothing: 5,
    qqeFactor: 4.238,

    // ===== 2. СТАРШИЙ ТАЙМФРЕЙМ =====
    htfMultiplier: 5,
    htfMaType: "EMA",
    htfMaPeriod: 20,

    // ===== 3. ВОЛАТИЛЬНОСТЬ (ATR) =====
    atrPeriod: 14,
    atrAvgWindow: 100,
    atrMinRatio: 0.7,
    atrMaxRatio: 2.0,

    // ===== 4. BOLLINGER BANDS =====
    bbPeriod: 20,
    bbStdDev: 2.0,
    bbZoneDepth: 0.3,

    // ===== 5. ФИЛЬТР СВЕЧИ =====
    candleMaxAtrMult: 2.0,
    candleReqAlign: true,

    // ===== КУЛДАУН =====
    cooldownBars: 3,

    // ===== ЗАЩИТА ОТ ПРОСАДКИ =====
    drawdownEnabled: true,
    drawdownWindow: 30,
    drawdownStopWR: 40,

    // ===== СТАТИСТИКА =====
    statsLookbackBars: 1000,

    // ===== ВИД СИГНАЛОВ =====
    buyText: "BUY",
    sellText: "SELL",
    buyColor: "#22c55e",
    sellColor: "#ef4444",
    buyTextColor: "#ecfdf5",
    sellTextColor: "#fef2f2",

    // ===== МАРКЕРЫ РЕЗУЛЬТАТА =====
    showResultMarkers: true,
    winMarkerText: "✅",
    lossMarkerText: "❌",
    winMarkerColor: "#22c55e",
    lossMarkerColor: "#ef4444",

    // ===== HUD =====
    showHudCard: true,
    hudAnchor: "TR",
    hudTop: 10, hudRight: 10, hudBottom: 10, hudLeft: 10,
    hudFontPx: 13,
    hudMaxWidth: 380,
    hudPadX: 12, hudPadY: 8,
    hudLineGap: 4, hudRadius: 10,
    hudBg: "rgba(17,24,39,.90)",
    hudBorder: "rgba(148,163,184,.28)",
    hudNeutral: "#fff"
  },

  paramMeta: {
    expiryBars:   { label: "🎏 Экспирация (баров)", type: "number", min: 1, max: 50, step: 1 },
    entryMode: {
      label: "📍 Вход в сделку", type: "select",
      options: [
        { label: "На закрытии сигнальной свечи", value: "signalBarClose" },
        { label: "На открытии следующей свечи",  value: "nextBarOpen"    }
      ]
    },
    martingaleSteps: { label: "🎏 Кол-во догонов", type: "number", min: 0, max: 10, step: 1 },
    drawBehavior: {
      label: "🎏 Ничья (=): поведение", type: "select",
      options: [
        { label: "Продолжить догон",   value: "continue" },
        { label: "Считать победой",    value: "win"      },
        { label: "Считать поражением", value: "loss"     }
      ]
    },
    minConsensus: { label: "🧠 Мин. согласных систем (из 5)", type: "number", min: 2, max: 5, step: 1 },
    requireAll5OnWeekend: { label: "🧠 На выходных требовать все 5", type: "boolean" },

    rsiPeriod:    { label: "① RSI период", type: "number", min: 2, max: 100, step: 1 },
    rsiSmoothing: { label: "① RSI сглаживание", type: "number", min: 1, max: 50, step: 1 },
    qqeFactor:    { label: "① QQE фактор", type: "number", min: 1, max: 10, step: 0.001 },

    htfMultiplier: { label: "② Старший TF множитель", type: "number", min: 2, max: 20, step: 1 },
    htfMaType: {
      label: "② Тип MA старшего TF", type: "select",
      options: [{ label: "SMA", value: "SMA" }, { label: "EMA", value: "EMA" }, { label: "WMA", value: "WMA" }]
    },
    htfMaPeriod: { label: "② Период MA старшего TF", type: "number", min: 5, max: 200, step: 1 },

    atrPeriod:    { label: "③ ATR период", type: "number", min: 2, max: 50, step: 1 },
    atrAvgWindow: { label: "③ Окно усреднения ATR", type: "number", min: 20, max: 500, step: 10 },
    atrMinRatio:  { label: "③ Мин. ATR / средний", type: "number", min: 0.1, max: 1.5, step: 0.05 },
    atrMaxRatio:  { label: "③ Макс. ATR / средний", type: "number", min: 1.0, max: 5.0, step: 0.1 },

    bbPeriod:    { label: "④ BB период", type: "number", min: 5, max: 100, step: 1 },
    bbStdDev:    { label: "④ BB отклонение", type: "number", min: 0.5, max: 4.0, step: 0.1 },
    bbZoneDepth: { label: "④ Глубина зоны BB (0-0.5)", type: "number", min: 0.1, max: 0.5, step: 0.05 },

    candleMaxAtrMult: { label: "⑤ Макс. размер свечи (×ATR)", type: "number", min: 1.0, max: 5.0, step: 0.1 },
    candleReqAlign:   { label: "⑤ Свеча должна совпадать с сигналом", type: "boolean" },

    cooldownBars: { label: "⏱ Кулдаун (баров)", type: "number", min: 0, max: 20, step: 1 },

    drawdownEnabled:  { label: "🛡 Защита от просадки: вкл", type: "boolean" },
    drawdownWindow:   { label: "🛡 Окно последних сделок", type: "number", min: 3, max: 50, step: 1 },
    drawdownStopWR:   { label: "🛡 STOP если WR в окне ниже %", type: "number", min: 20, max: 60, step: 1 },

    statsLookbackBars: { label: "📊 Окно статистики (баров)", type: "number", min: 50, max: 5000, step: 10 },

    buyText:       { label: "🎨 Текст BUY",  type: "text", maxLength: 12 },
    sellText:      { label: "🎨 Текст SELL", type: "text", maxLength: 12 },
    buyColor:      { label: "🎨 Цвет BUY",   type: "color" },
    sellColor:     { label: "🎨 Цвет SELL",  type: "color" },
    buyTextColor:  { label: "🎨 Цвет текста BUY",  type: "color" },
    sellTextColor: { label: "🎨 Цвет текста SELL", type: "color" },

    showResultMarkers: { label: "✅ Показывать маркеры результата", type: "boolean" },
    winMarkerText:     { label: "✅ Текст плюса", type: "text", maxLength: 6 },
    lossMarkerText:    { label: "❌ Текст минуса", type: "text", maxLength: 6 },
    winMarkerColor:    { label: "✅ Цвет плюса", type: "color" },
    lossMarkerColor:   { label: "❌ Цвет минуса", type: "color" },

    showHudCard:  { label: "🪪 HUD: включить", type: "boolean" },
    hudAnchor: {
      label: "Позиция HUD", type: "select",
      options: [
        { label: "Верх-право", value: "TR" }, { label: "Верх-лево", value: "TL" },
        { label: "Низ-право", value: "BR" }, { label: "Низ-лево", value: "BL" }
      ]
    },
    hudTop:      { label: "HUD сверху (px)", type: "number", min: 0, max: 300, step: 1 },
    hudRight:    { label: "HUD справа (px)", type: "number", min: 0, max: 300, step: 1 },
    hudBottom:   { label: "HUD снизу (px)",  type: "number", min: 0, max: 300, step: 1 },
    hudLeft:     { label: "HUD слева (px)",  type: "number", min: 0, max: 300, step: 1 },
    hudFontPx:   { label: "HUD шрифт (px)", type: "number", min: 8, max: 30, step: 1 },
    hudMaxWidth: { label: "HUD макс. ширина (px)", type: "number", min: 120, max: 800, step: 10 },
    hudPadX:     { label: "HUD отступ X",  type: "number", min: 4, max: 40, step: 1 },
    hudPadY:     { label: "HUD отступ Y",  type: "number", min: 2, max: 40, step: 1 },
    hudLineGap:  { label: "HUD интервал", type: "number", min: 0, max: 20, step: 1 },
    hudRadius:   { label: "HUD скругление", type: "number", min: 0, max: 24, step: 1 },
    hudBg:       { label: "HUD фон", type: "color" },
    hudBorder:   { label: "HUD рамка", type: "color" },
    hudNeutral:  { label: "HUD нейтральный", type: "color" }
  }
};

// =================== HUD Primitive ===================
class HudPrim {
  constructor(s, o) { this._s = s; this._o = { ...o }; this._d = {}; this._pv = new HudPaneView(() => this._d, () => this._o); }
  setData(d) { this._d = d || {}; }
  setOptions(o) { Object.assign(this._o, o || {}); }
  paneViews() { return [this._pv]; } priceAxisViews() { return []; } timeAxisViews() { return []; }
}
class HudPaneView {
  constructor(dg, og) { this._r = new HudRenderer(dg, og); }
  renderer() { return this._r; } zOrder() { return "top"; } update() {}
}
class HudRenderer {
  constructor(dg, og) { this._dg = dg; this._og = og; }
  draw(target) {
    const opts = this._og(), data = this._dg() || {};
    const lines = ["title","stats","stats2","stats3","stats4","stats5"].map(k => data[k]).filter(Boolean);
    if (!lines.length || !target.useBitmapCoordinateSpace) return;
    target.useBitmapCoordinateSpace(scope => {
      const ctx = scope.context;
      const hr = scope.horizontalPixelRatio || 1, vr = scope.verticalPixelRatio || 1;
      const cssW = scope.cssWidth || scope.bitmapSize?.width || 0;
      const cssH = scope.cssHeight || ((scope.bitmapSize?.height || 0) / vr);
      const padX = (opts.paddingX || 10) * hr, padY = (opts.paddingY || 7) * vr;
      const gap = (opts.lineGap || 3) * vr, maxW = (opts.maxWidth || 360) * hr;
      const radius = (opts.radius ?? 10) * hr, fontPx = (opts.fontSize || 13) * vr;
      ctx.save();
      ctx.font = `${fontPx}px system-ui,-apple-system,Segoe UI,Roboto`;
      ctx.textBaseline = "top";
      let w = 0, h = padY * 2 - gap;
      for (const ln of lines) {
        w = Math.min(Math.max(w, ctx.measureText(ln.text || "").width), maxW);
        h += Math.ceil(fontPx * 1.2) + gap;
      }
      const anc = String(opts.anchor || "TR").toUpperCase();
      let x = anc.includes("R") ? cssW - (w + padX * 2) - (opts.right || 10) * hr : (opts.left || 10) * hr;
      let y = anc.includes("T") ? (opts.top || 10) * vr : cssH - h - (opts.bottom || 10) * vr;
      roundRect(ctx, x, y, w + padX * 2, h, radius);
      ctx.fillStyle = opts.background || "rgba(17,24,39,.90)"; ctx.fill();
      ctx.strokeStyle = opts.border || "rgba(148,163,184,.28)"; ctx.lineWidth = 1 * hr; ctx.stroke();
      let cy = y + padY;
      for (const ln of lines) {
        ctx.fillStyle = ln.color || opts.neutral || "#fff";
        ctx.fillText(ln.text || "", x + padX, cy, maxW);
        cy += Math.ceil(fontPx * 1.2) + gap;
      }
      ctx.restore();
    });
  }
  hitTest() { return null; }
}
function roundRect(ctx, x, y, w, h, r) {
  const rr = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

// =================== BOT CORE ===================
export function init(ctx) {
  const { candleSeries, params, createSeriesMarkers } = ctx;
  const p = { ...meta.defaultParams, ...(params || {}) };
  const markersApi = createSeriesMarkers(candleSeries, []);

  let hudPrim = candleSeries.__consensusHud || null;

  function ensureHud() {
    if (!p.showHudCard || typeof candleSeries.attachPrimitive !== "function") {
      if (hudPrim && typeof candleSeries.detachPrimitive === "function") candleSeries.detachPrimitive(hudPrim);
      candleSeries.__consensusHud = hudPrim = null;
      return null;
    }
    const opt = {
      anchor: p.hudAnchor, top: p.hudTop, right: p.hudRight, bottom: p.hudBottom, left: p.hudLeft,
      maxWidth: p.hudMaxWidth, fontSize: p.hudFontPx, paddingX: p.hudPadX, paddingY: p.hudPadY,
      lineGap: p.hudLineGap, radius: p.hudRadius,
      background: p.hudBg, border: p.hudBorder, neutral: p.hudNeutral
    };
    if (!hudPrim) {
      hudPrim = new HudPrim(candleSeries, opt);
      candleSeries.attachPrimitive(hudPrim);
      candleSeries.__consensusHud = hudPrim;
    } else hudPrim.setOptions(opt);
    return hudPrim;
  }

  // ====== МАТЕМАТИКА ======
  function sma(v, p) { const o = new Array(v.length).fill(null); let s = 0;
    for (let i = 0; i < v.length; i++) { s += v[i]; if (i >= p) s -= v[i - p]; if (i >= p - 1) o[i] = s / p; }
    return o;
  }
  function ema(v, p) { const o = new Array(v.length).fill(null); const k = 2 / (p + 1); let pr = null;
    for (let i = 0; i < v.length; i++) { if (v[i] == null) continue; pr = pr == null ? v[i] : v[i] * k + pr * (1 - k); o[i] = pr; }
    return o;
  }
  function wma(v, p) { const o = new Array(v.length).fill(null); const d = (p * (p + 1)) / 2;
    for (let i = p - 1; i < v.length; i++) { let s = 0; for (let j = 0; j < p; j++) s += v[i - j] * (p - j); o[i] = s / d; }
    return o;
  }
  function rma(v, p) { const o = new Array(v.length).fill(null); let pr = null, s = 0;
    for (let i = 0; i < v.length; i++) { const x = v[i] || 0;
      if (i < p) { s += x; if (i === p - 1) { pr = s / p; o[i] = pr; } }
      else { pr = (pr * (p - 1) + x) / p; o[i] = pr; } }
    return o;
  }
  function ma(v, per, type) {
    switch (type) { case "SMA": return sma(v, per); case "WMA": return wma(v, per); default: return ema(v, per); }
  }
  function stdev(v, period) {
    const means = sma(v, period);
    const out = new Array(v.length).fill(null);
    for (let i = period - 1; i < v.length; i++) {
      let s = 0;
      for (let j = 0; j < period; j++) { const d = v[i - j] - means[i]; s += d * d; }
      out[i] = Math.sqrt(s / period);
    }
    return out;
  }

  // ====== RSI / QQE ======
  function rsi(closes, p) {
    const out = new Array(closes.length).fill(null);
    const g = new Array(closes.length).fill(0), l = new Array(closes.length).fill(0);
    for (let i = 1; i < closes.length; i++) {
      const d = closes[i] - closes[i - 1];
      g[i] = d > 0 ? d : 0; l[i] = d < 0 ? -d : 0;
    }
    const aG = rma(g, p), aL = rma(l, p);
    for (let i = 0; i < closes.length; i++) {
      if (aG[i] == null || aL[i] == null) continue;
      if (aL[i] === 0) { out[i] = 100; continue; }
      out[i] = 100 - 100 / (1 + aG[i] / aL[i]);
    }
    return out;
  }
  function qqe(src, rp, sm, f) {
    const rsiRaw = rsi(src, rp), rsiMa = ema(rsiRaw, sm);
    const wl = rp * 2 - 1;
    const diff = new Array(rsiMa.length).fill(0);
    for (let i = 1; i < rsiMa.length; i++) {
      if (rsiMa[i] != null && rsiMa[i - 1] != null) diff[i] = Math.abs(rsiMa[i] - rsiMa[i - 1]);
    }
    const dar = rma(rma(diff, wl), wl).map(v => v == null ? null : v * f);
    const tr = new Array(rsiMa.length).fill(null);
    for (let i = 0; i < rsiMa.length; i++) {
      if (rsiMa[i] == null || dar[i] == null) continue;
      const pr = i > 0 ? tr[i - 1] : null, prRsi = i > 0 ? rsiMa[i - 1] : null;
      const up = rsiMa[i] + dar[i], dn = rsiMa[i] - dar[i];
      if (pr == null) { tr[i] = rsiMa[i] > 50 ? dn : up; continue; }
      if (rsiMa[i] > pr && prRsi != null && prRsi > pr) tr[i] = Math.max(pr, dn);
      else if (rsiMa[i] < pr && prRsi != null && prRsi < pr) tr[i] = Math.min(pr, up);
      else tr[i] = rsiMa[i] > pr ? dn : up;
    }
    return { rsiMa, trailing: tr };
  }

  function htfTrend(opens, highs, lows, closes, mult, maPeriod, maType) {
    const n = closes.length;
    const htfCloses = [];
    const htfMap = new Array(n).fill(-1);
    for (let i = 0; i < n; i++) {
      const group = Math.floor(i / mult);
      if (htfCloses.length <= group) htfCloses.push(null);
      htfCloses[group] = closes[i];
      htfMap[i] = group;
    }
    const htfMA = ma(htfCloses, maPeriod, maType);
    const trend = new Array(n).fill(0);
    for (let i = 0; i < n; i++) {
      const hg = htfMap[i];
      if (hg < 0 || htfMA[hg] == null) continue;
      const c = htfCloses[hg];
      if (c > htfMA[hg]) trend[i] = +1;
      else if (c < htfMA[hg]) trend[i] = -1;
    }
    return trend;
  }

  function atrArr(highs, lows, closes, period) {
    const tr = new Array(closes.length).fill(0);
    for (let i = 1; i < closes.length; i++) {
      const h = highs[i], l = lows[i], pc = closes[i - 1];
      tr[i] = Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc));
    }
    return rma(tr, period);
  }

  function bollinger(closes, period, mult) {
    const basis = sma(closes, period);
    const sd = stdev(closes, period);
    const upper = new Array(closes.length).fill(null);
    const lower = new Array(closes.length).fill(null);
    for (let i = 0; i < closes.length; i++) {
      if (basis[i] == null || sd[i] == null) continue;
      upper[i] = basis[i] + sd[i] * mult;
      lower[i] = basis[i] - sd[i] * mult;
    }
    return { upper, lower, basis };
  }

  function candleIsAligned(open, close, side) {
    if (side === "buy") return close >= open;
    return close <= open;
  }

  function isWeekend(unixTime) {
    const d = new Date(unixTime * 1000).getUTCDay();
    return d === 0 || d === 6;
  }

  // ====== ГЕНЕРАТОР СИГНАЛОВ ======
  function generateSignals(candles, params, diag) {
    const n = candles.length;
    const times = new Array(n), opens = new Array(n), highs = new Array(n), lows = new Array(n), closes = new Array(n);
    for (let i = 0; i < n; i++) {
      times[i] = candles[i].time; opens[i] = candles[i].open;
      highs[i] = candles[i].high; lows[i] = candles[i].low;  closes[i] = candles[i].close;
    }

    const { rsiMa, trailing } = qqe(closes, params.rsiPeriod, params.rsiSmoothing, params.qqeFactor);
    const htf = htfTrend(opens, highs, lows, closes, params.htfMultiplier, params.htfMaPeriod, params.htfMaType);
    const atr = atrArr(highs, lows, closes, params.atrPeriod);
    const atrAvg = sma(atr, params.atrAvgWindow);
    const bb = bollinger(closes, params.bbPeriod, params.bbStdDev);

    const events = [];
    let lastSigIdx = -Infinity;
    let rawCount = 0, rejected = { htf: 0, atr: 0, bb: 0, candle: 0, cooldown: 0 };

    for (let i = 1; i < n; i++) {
      if (rsiMa[i] == null || trailing[i] == null || rsiMa[i - 1] == null || trailing[i - 1] == null) continue;

      const crossUp   = rsiMa[i - 1] <= trailing[i - 1] && rsiMa[i] >  trailing[i];
      const crossDown = rsiMa[i - 1] >= trailing[i - 1] && rsiMa[i] <  trailing[i];
      if (!crossUp && !crossDown) continue;
      rawCount++;

      const side = crossUp ? "buy" : "sell";
      const votes = { rsi: 1 };

      const htfOk = side === "buy" ? htf[i] === +1 : htf[i] === -1;
      votes.htf = htfOk ? 1 : 0;

      let volOk = false;
      if (atr[i] != null && atrAvg[i] != null && atrAvg[i] > 0) {
        const ratio = atr[i] / atrAvg[i];
        volOk = ratio >= params.atrMinRatio && ratio <= params.atrMaxRatio;
      }
      votes.vol = volOk ? 1 : 0;

      let bbOk = false;
      if (bb.upper[i] != null && bb.lower[i] != null) {
        const range = bb.upper[i] - bb.lower[i];
        if (range > 0) {
          const posInBand = (closes[i] - bb.lower[i]) / range;
          if (side === "buy")  bbOk = posInBand <= params.bbZoneDepth;
          else                 bbOk = posInBand >= (1 - params.bbZoneDepth);
        }
      }
      votes.bb = bbOk ? 1 : 0;

      let candleOk = true;
      if (params.candleReqAlign && !candleIsAligned(opens[i], closes[i], side)) candleOk = false;
      if (candleOk && atr[i] != null && atr[i] > 0) {
        const size = Math.abs(closes[i] - opens[i]);
        if (size > atr[i] * params.candleMaxAtrMult) candleOk = false;
      }
      votes.candle = candleOk ? 1 : 0;

      const total = votes.rsi + votes.htf + votes.vol + votes.bb + votes.candle;
      const required = (params.requireAll5OnWeekend && isWeekend(times[i])) ? 5 : params.minConsensus;

      if (!votes.htf)    rejected.htf++;
      if (!votes.vol)    rejected.atr++;
      if (!votes.bb)     rejected.bb++;
      if (!votes.candle) rejected.candle++;

      if (total < required) continue;

      if (i - lastSigIdx <= params.cooldownBars) { rejected.cooldown++; continue; }
      lastSigIdx = i;

      events.push({ side, i, votes, total });
    }

    if (diag) { diag.rawCount = rawCount; diag.rejected = rejected; }
    return events;
  }

  function evaluateTrade(opens, closes, signal, params, n) {
    const steps = Math.max(0, Math.floor(params.martingaleSteps || 0));
    const exp = Math.max(1, Math.floor(params.expiryBars || 1));
    const entryMode = String(params.entryMode || "nextBarOpen");

    let firstWin = false;
    let lastExitIndex = -1;

    for (let step = 0; step <= steps; step++) {
      let entryIndex, entryPrice;
      const sigIdx = signal.i + step * exp;

      if (entryMode === "signalBarClose") {
        entryIndex = sigIdx;
        if (entryIndex >= n) return { completed: false, firstWin, totalWin: false, stepsUsed: step, exitIndex: lastExitIndex };
        entryPrice = closes[entryIndex];
      } else {
        entryIndex = sigIdx + 1;
        if (entryIndex >= n) return { completed: false, firstWin, totalWin: false, stepsUsed: step, exitIndex: lastExitIndex };
        entryPrice = opens[entryIndex];
      }

      const exitIndex = entryIndex + exp - 1;
      if (exitIndex >= n) return { completed: false, firstWin, totalWin: false, stepsUsed: step, exitIndex: lastExitIndex };

      lastExitIndex = exitIndex;
      const exitPrice = closes[exitIndex];
      let result;
      if (exitPrice === entryPrice) result = "draw";
      else if (signal.side === "buy") result = exitPrice > entryPrice ? "win" : "loss";
      else                            result = exitPrice < entryPrice ? "win" : "loss";

      if (result === "draw") {
        if (params.drawBehavior === "win")  result = "win";
        if (params.drawBehavior === "loss") result = "loss";
      }

      if (step === 0) firstWin = (result === "win");

      if (result === "win") {
        return { completed: true, firstWin, totalWin: true, stepsUsed: step + 1, stepsToWin: step, exitIndex };
      }
      if (result === "draw" && params.drawBehavior === "continue") continue;
    }

    return { completed: true, firstWin, totalWin: false, stepsUsed: steps + 1, stepsToWin: -1, exitIndex: lastExitIndex };
  }

  // ====== UPDATE ======
  function update(candles) {
    const n = candles.length;
    if (n < 100) { markersApi.setMarkers([]); return []; }

    const opens = candles.map(c => c.open), highs = candles.map(c => c.high);
    const lows = candles.map(c => c.low), closes = candles.map(c => c.close);
    const times = candles.map(c => c.time);

    const diag = {};
    const events = generateSignals(candles, p, diag);

    const lookback = Math.max(1, Math.floor(p.statsLookbackBars));
    const fromBar = Math.max(0, (n - 1) - lookback);

    let wins = 0, losses = 0, draws = 0, completed = 0;
    let wins1 = 0;
    const mgSteps = Math.max(0, Math.floor(p.martingaleSteps || 0));
    const stepWins = new Array(mgSteps + 1).fill(0);
    const sigOutcome = new Map();
    const recentResults = [];

    let currentLossStreak = 0;
    let maxLossStreakOverall = 0;
    let maxLossStreakBeforeWin = 0;

    for (const ev of events) {
      if (ev.i < fromBar) continue;
      const r = evaluateTrade(opens, closes, ev, p, n);
      sigOutcome.set(ev.i, r);
      if (!r.completed) continue;
      completed++;

      if (r.firstWin) wins1++;

      if (r.totalWin) {
        wins++;
        recentResults.push(1);
        const idx = Math.min(r.stepsToWin, mgSteps);
        stepWins[idx]++;
        if (currentLossStreak > maxLossStreakBeforeWin) {
          maxLossStreakBeforeWin = currentLossStreak;
        }
        currentLossStreak = 0;
      } else {
        losses++;
        recentResults.push(0);
        currentLossStreak++;
        if (currentLossStreak > maxLossStreakOverall) {
          maxLossStreakOverall = currentLossStreak;
        }
      }
    }
    const wr = completed ? (wins / (wins + losses)) * 100 : 0;
    const wr1 = completed ? (wins1 / completed) * 100 : 0;

    const ddWindow = Math.min(p.drawdownWindow, recentResults.length);
    const recent = recentResults.slice(-ddWindow);
    const recentWR = recent.length ? (recent.filter(x => x === 1).length / recent.length) * 100 : 100;
    const drawdownActive = p.drawdownEnabled && recent.length >= p.drawdownWindow && recentWR < p.drawdownStopWR;

    const markers = [];
    for (const ev of events) {
      const out = sigOutcome.get(ev.i);
      const side = ev.side;
      const text = (side === "buy" ? p.buyText : p.sellText) + ` ${ev.total}/5`;
      markers.push({
        name: side,
        time: times[ev.i],
        position: side === "buy" ? "belowBar" : "aboveBar",
        shape: side === "buy" ? "arrowUp" : "arrowDown",
        color: side === "buy" ? p.buyColor : p.sellColor,
        text,
        textColor: side === "buy" ? p.buyTextColor : p.sellTextColor,
        price: side === "buy" ? lows[ev.i] : highs[ev.i]
      });
      if (p.showResultMarkers && out?.completed) {
        const ri = out.exitIndex;
        if (ri < 0 || ri >= n) continue;
        let markerText, markerColor, shape;
        if (out.totalWin) {
          const stepLabel = out.stepsToWin === 0 ? p.winMarkerText : `${p.winMarkerText}${out.stepsToWin}`;
          markerText = stepLabel;
          markerColor = p.winMarkerColor;
          shape = "circle";
        } else {
          markerText = p.lossMarkerText;
          markerColor = p.lossMarkerColor;
          shape = "square";
        }
        markers.push({
          name: out.totalWin ? "win" : "loss",
          time: times[ri],
          position: side === "buy" ? "aboveBar" : "belowBar",
          shape,
          color: markerColor,
          text: markerText,
          textColor: "#ffffff",
          price: side === "buy" ? highs[ri] : lows[ri]
        });
      }
    }

    const prim = ensureHud();
    if (prim) {
      const titleColor = "#fde047";
      const wrColor = wr >= 60 ? "#86feac" : wr >= 53 ? "#fdf647" : "#fff";
      const neutral = p.hudNeutral;

      if (drawdownActive) {
        prim.setOptions({ background: "rgb(123, 38, 38)", border: "rgba(239,68,68,.95)", neutral: "#fff" });
      } else if (wr >= 60 && completed >= 20) {
        prim.setOptions({ background: "rgb(5, 60, 12)", border: "rgba(34,197,94,.95)", neutral: "#fff" });
      } else {
        prim.setOptions({ background: p.hudBg, border: p.hudBorder, neutral: p.hudNeutral });
      }

      const title = `🧠 CONSENSUS 4/5 | ⏱ Экспир: ${p.expiryBars} бара${mgSteps ? ` +${mgSteps} догона` : ""}`;
      const stats = drawdownActive
        ? `🛑 STOP: WR последних ${ddWindow} сделок = ${recentWR.toFixed(0)}%`
        : `🏁 Общая: ${wr.toFixed(0)}% | ✅ ${wins} : ❌ ${losses}${draws ? ` | = ${draws}` : ""}`;

      let stepsStatsText = null;
      if (mgSteps > 0 && completed > 0) {
        const parts = [];
        for (let s = 0; s <= mgSteps; s++) {
          const label = s === 0 ? "1" : `d${s}`;
          parts.push(`${label}:${stepWins[s]}`);
        }
        parts.push(`LOSS:${losses}`);
        stepsStatsText = `⚡ WR1: ${wr1.toFixed(0)}% | 🏆 ${parts.join(" | ")}`;
      } else if (completed > 0) {
        stepsStatsText = `⚡ 1-я сделка: ${wr1.toFixed(0)}%`;
      }

      const streakWarn = maxLossStreakBeforeWin > mgSteps ? " ⚠️" : "";
      const lossStreakText = `📉 Макс. минусов до ✅: ${maxLossStreakBeforeWin}${streakWarn} | всего: ${maxLossStreakOverall}`;
      const diagText = ``;
      const recentText = recent.length >= 3
        ? `📈 Последние ${recent.length}: ${recent.map(x => x === 1 ? "✓" : "✗").join("")}`
        : null;

      prim.setData({
        title:  { text: title,  color: titleColor },
        stats:  { text: stats,  color: drawdownActive ? "#fff" : wrColor },
        stats2: stepsStatsText ? { text: stepsStatsText, color: neutral } : null,
        stats3: { text: lossStreakText, color: neutral },
        stats4: { text: diagText, color: neutral },
        stats5: recentText ? { text: recentText, color: neutral } : null
      });
    }

    markersApi.setMarkers(markers);

    return markers;
  }

  function destroy() {
    markersApi.setMarkers([]);
    if (candleSeries.__consensusHud && typeof candleSeries.detachPrimitive === "function") {
      candleSeries.detachPrimitive(candleSeries.__consensusHud);
    }
    candleSeries.__consensusHud = null;
  }

  return { update, destroy };
}
