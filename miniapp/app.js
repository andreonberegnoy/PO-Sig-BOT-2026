/* Mini App logic. Vanilla JS, no build step.
 *
 * Структура (после рефакторинга):
 *   🏠 Главная (Status)
 *   ⚙️ Настройки бота (общие — base_amount, MG, schedule, weekends, filter)
 *   🧠 Стратегия
 *      ├─ Список стратегий + загрузка
 *      ├─ Настройки стратегии (indicator params: RSI, EMA, BB, ATR, QQE, HTF)
 *      └─ Аналитика (placeholder, наполнится в этапе 2 рефакторинга)
 */
(() => {
  const tg = window.Telegram?.WebApp;
  const initData = tg?.initData || "";
  if (tg) {
    tg.ready();
    tg.expand();
    // Закрепить Mini App: запретить вертикальный свайп-вниз который сворачивает
    // приложение. BotAPI 7.7+ — у старых клиентов метода нет, ловим try/catch.
    try { if (typeof tg.disableVerticalSwipes === "function") tg.disableVerticalSwipes(); } catch (e) {}
    // Подтверждение закрытия — чтобы случайный свайп вниз не закрывал случайно.
    try { if (typeof tg.enableClosingConfirmation === "function") tg.enableClosingConfirmation(); } catch (e) {}
  }

  // ─── Fullscreen toggle (на весь экран в TG Mini App) ────────────────
  // Telegram WebApp API: requestFullscreen / exitFullscreen (BotAPI 8.0+).
  // Для старых клиентов — фолбэк на tg.expand() (на максимальную высоту).
  const fsBtn = document.getElementById("btn-fullscreen");
  if (fsBtn && tg) {
    const updateFsButton = () => {
      const isFs = tg.isFullscreen === true;
      fsBtn.textContent = isFs ? "⛗" : "⛶";
      fsBtn.setAttribute("aria-label",
        isFs ? "Свернуть из полного экрана" : "Развернуть на весь экран");
    };
    fsBtn.addEventListener("click", () => {
      try {
        if (tg.isFullscreen) {
          if (typeof tg.exitFullscreen === "function") tg.exitFullscreen();
          else tg.expand();   // fallback для старых клиентов
        } else {
          if (typeof tg.requestFullscreen === "function") tg.requestFullscreen();
          else tg.expand();
        }
      } catch (e) {
        // На случай если client не поддерживает API — просто expand
        try { tg.expand(); } catch (_) {}
      }
      // updateFsButton будет вызвана автоматически через event,
      // но дублируем на случай отсутствия события
      setTimeout(updateFsButton, 100);
    });
    // Подписываемся на события смены полноэкранного режима
    if (typeof tg.onEvent === "function") {
      tg.onEvent("fullscreenChanged", updateFsButton);
      tg.onEvent("fullscreenFailed", updateFsButton);
    }
    updateFsButton();
  } else if (fsBtn && !tg) {
    // Открыто не через Telegram (dev-режим в браузере) — кнопка скрыта
    fsBtn.style.display = "none";
  }

  // ─── Auto-dismiss keyboard on tap outside input ───────────────────────
  document.addEventListener("pointerdown", (e) => {
    const t = e.target;
    if (!t || !(t instanceof Element)) return;
    if (t.closest("input, textarea, select, button, label, [contenteditable]")) return;
    const a = document.activeElement;
    if (a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT")) {
      a.blur();
    }
  }, { passive: true });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      const a = document.activeElement;
      if (a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA")) a.blur();
    }
  });

  // ─── Info-popover для ⓘ кнопок в настройках ────────────────────
  // Один экземпляр на страницу, переиспользуется для всех ⓘ.
  const infoPopover = document.createElement("div");
  infoPopover.className = "info-popover";
  infoPopover.innerHTML = `<div class="info-popover-body"></div><button class="info-popover-close">✕</button>`;
  document.body.appendChild(infoPopover);
  const infoPopBody = infoPopover.querySelector(".info-popover-body");
  const closeInfoPop = () => infoPopover.classList.remove("show");
  infoPopover.querySelector(".info-popover-close").addEventListener("click", (e) => {
    e.stopPropagation();
    closeInfoPop();
  });
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".info-btn");
    if (btn) {
      e.stopPropagation();
      infoPopBody.innerHTML = `<div class="info-popover-key">${btn.dataset.key || ""}</div>` +
                              `<div>${btn.dataset.info || ""}</div>`;
      // позиционирование: рядом с кнопкой, но clamp в viewport
      const r = btn.getBoundingClientRect();
      const vw = window.innerWidth;
      infoPopover.classList.add("show");
      // чуть позже измерим высоту и спозиционируем
      const pw = Math.min(280, vw - 24);
      infoPopover.style.maxWidth = pw + "px";
      let left = r.left;
      if (left + pw > vw - 12) left = vw - pw - 12;
      if (left < 12) left = 12;
      infoPopover.style.left = left + "px";
      infoPopover.style.top = (r.bottom + 6) + "px";
      return;
    }
    if (!e.target.closest(".info-popover")) closeInfoPop();
  });

  const api = async (path, opts = {}) => {
    const res = await fetch(path, {
      ...opts,
      headers: {
        "Content-Type": "application/json",
        "X-Init-Data": initData,
        ...(opts.headers || {}),
      },
    });
    if (!res.ok) {
      const t = await res.text();
      throw new Error(`HTTP ${res.status}: ${t}`);
    }
    return res.json();
  };

  // ─── Top-level tab switching ───────────────────────────────────────────
  document.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      document.getElementById(`tab-${t.dataset.tab}`).classList.add("active");
      if (t.dataset.tab === "settings") loadGlobalSettings();
      if (t.dataset.tab === "strategy") {
        // При входе в Стратегия → всегда возвращаемся на уровень «Список»
        showStrategyLevel("list");
        loadStrategies();
      }
      if (t.dataset.tab === "status") loadStatus();
    });
  });

  // ─── Drill-down navigation в «Стратегия» ───────────────────────────────
  // Уровни: list (default) → detail (Настройки + Аналитика подвкладки)
  // Между уровнями — кнопка «← Назад».
  function showStrategyLevel(level) {
    document.querySelectorAll(".strategy-level").forEach((x) => x.classList.remove("active"));
    const target = document.getElementById(`strategy-level-${level}`);
    if (target) target.classList.add("active");
  }
  function gotoSubtab(name) {
    // Внутри уровня detail — переключение между Настройки/Аналитика
    document.querySelectorAll(".subtab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".subtab-panel").forEach((x) => x.classList.remove("active"));
    const tabBtn = document.querySelector(`.subtab[data-subtab="${name}"]`);
    if (tabBtn) tabBtn.classList.add("active");
    const panelId = name === "strategy-settings" ? "sub-strategy-settings"
      : name === "analytics" ? "sub-analytics" : null;
    if (panelId) document.getElementById(panelId)?.classList.add("active");
    if (name === "strategy-settings") loadStrategyParams();
    if (name === "analytics") loadAnalytics();
  }
  document.querySelectorAll(".subtab").forEach((sub) => {
    sub.addEventListener("click", () => gotoSubtab(sub.dataset.subtab));
  });
  // Кнопка «← Назад к списку» — возврат на уровень list
  const backBtn = document.getElementById("btn-back-to-list");
  if (backBtn) {
    backBtn.addEventListener("click", () => {
      showStrategyLevel("list");
      loadStrategies();
    });
  }

  // ═════════════════════ ГЛАВНАЯ (STATUS) ════════════════════════════════
  function showActionMsg(text, kind = "info") {
    const el = document.getElementById("action-msg");
    if (!el) return;
    el.textContent = text;
    el.className = `action-msg ${kind}`;
    if (text) {
      clearTimeout(showActionMsg._t);
      showActionMsg._t = setTimeout(() => {
        el.textContent = "";
        el.className = "action-msg";
      }, 6000);
    }
  }

  async function loadStatus() {
    try {
      const s = await api("/api/status");
      document.getElementById("m-mode").textContent = s.mode || "—";
      document.getElementById("m-balance").textContent = s.balance != null ? `$${(+s.balance).toFixed(2)}` : "—";
      // m-strategy: текст + бейдж 🎯 если активен фильтр (этап 3)
      const sBadge = document.getElementById("m-strategy");
      if (sBadge && s.active_strategy) {
        sBadge.innerHTML = `${s.active_strategy} ${s.filter_active ? '<span class="badge active" style="font-size:10px">🎯 фильтр</span>' : ''}`;
      } else if (sBadge) {
        sBadge.textContent = "—";
      }
      // Этап 3 виджеты — пишем сразу, без второго запроса /api/status
      loadActiveCycle(s);
      loadDayOff(s);
      loadProfitToday();
      // Tracked пары: показать count + список имён (compact comma-separated)
      const tList = Array.isArray(s.tracked_pairs_list) ? s.tracked_pairs_list : [];
      const tCount = s.tracked_pairs ?? tList.length;
      document.getElementById("m-tracked").innerHTML = tCount > 0 && tList.length > 0
        ? `<b>${tCount}</b>: <span style="font-size:12px; color:var(--hint); word-break:break-all;">${tList.join(", ")}</span>`
        : (tCount === 0 ? "0 (нет подходящих пар)" : (tCount ?? "—"));
      document.getElementById("m-active").textContent = s.active_syms ?? "—";
      document.getElementById("m-banned").textContent = s.banned_pairs ?? "—";
      const inSearchMode = (s.mg_step ?? 0) > 0 && !s.current_pair;
      document.getElementById("m-pair").textContent = inSearchMode
        ? "🔍 поиск сигнала на всех допустимых"
        : (s.current_pair || "—");
      document.getElementById("m-base").textContent = s.base_amount != null ? `$${(+s.base_amount).toFixed(2)}` : "—";
      document.getElementById("m-expiry").textContent = s.expiry_seconds != null ? `${s.expiry_seconds} сек` : "—";
      document.getElementById("m-mg").textContent = s.mg_step ?? 0;
      document.getElementById("m-loss").textContent = `$${(+(s.session_loss || 0)).toFixed(2)}`;
      document.getElementById("m-paused").textContent = s.paused ? "ДА" : "нет";
      const inCycle = (s.mg_step ?? 0) > 0;
      const ca = document.getElementById("cycle-actions");
      if (ca) ca.style.display = inCycle ? "" : "none";
      const switchBtn = document.getElementById("btn-switch-pair");
      if (switchBtn) switchBtn.style.display = inSearchMode ? "none" : "";
    } catch (e) {
      console.error(e);
    }
  }
  async function loadActiveCycle(s) {
    const card = document.getElementById("active-cycle-card");
    if (!card) return;
    const inCycle = (s.mg_step ?? 0) > 0 || s.current_pair;
    card.style.display = inCycle ? "" : "none";
    if (!inCycle) return;
    document.getElementById("ac-current").textContent = s.current_pair || "🔍 поиск";
    document.getElementById("ac-original").textContent = s.original_pair || "—";
    document.getElementById("ac-direction").textContent = s.direction
      ? (s.direction.toUpperCase() + (s.direction === "call" ? " ⬆" : " ⬇"))
      : "—";
    document.getElementById("ac-trades-on-pair").textContent = s.trades_on_pair ?? 0;
    document.getElementById("ac-switches").textContent = s.cycle_switches ?? 0;
    document.getElementById("ac-carry").textContent = s.cycle_unused_carry ?? 0;
    const sp = s.switched_pairs || [];
    document.getElementById("ac-switched-pairs").textContent = sp.length ? sp.join(", ") : "—";
  }

  function loadDayOff(s) {
    const card = document.getElementById("day-off-card");
    if (!card) return;
    const until = s.day_off_until || 0;
    const now = Math.floor(Date.now() / 1000);
    if (until > now) {
      card.style.display = "";
      const remainMin = Math.round((until - now) / 60);
      const endLocal = new Date(until * 1000).toLocaleString();
      document.getElementById("day-off-meta").textContent =
        `Осталось ~${remainMin} мин. Конец: ${endLocal}.`;
    } else {
      card.style.display = "none";
    }
  }

  async function loadProfitToday() {
    try {
      const p = await api("/api/profit_today");
      const amt = Number(p.profit || 0);
      const el = document.getElementById("profit-amt");
      if (el) {
        el.textContent = `${amt >= 0 ? "+" : ""}$${amt.toFixed(2)}`;
        el.className = `profit-amt ${amt > 0 ? "ok" : amt < 0 ? "err" : ""}`;
      }
      const tEl = document.getElementById("profit-trades");
      if (tEl) tEl.textContent = p.trades ?? 0;
      const wEl = document.getElementById("profit-wins");
      if (wEl) wEl.textContent = p.wins ?? 0;
      const lEl = document.getElementById("profit-losses");
      if (lEl) lEl.textContent = p.losses ?? 0;
    } catch (e) { /* silent */ }
  }
  // 🔄 «Обновить» — форсит немедленный rescan на VPS (не ждать 60с тика)
  // и сразу читает свежий /api/status. Полезно после изменения настроек,
  // чтобы tracked-список и фильтр обновились мгновенно.
  document.getElementById("btn-refresh").onclick = async () => {
    const btn = document.getElementById("btn-refresh");
    const orig = btn.textContent;
    btn.disabled = true; btn.textContent = "⏳ Обновляю…";
    try {
      await api("/api/control/rescan", { method: "POST" });
      // дать боту 1-2 сек чтобы прогнать rescan (он у нас лёгкий)
      await new Promise((r) => setTimeout(r, 1500));
      await loadStatus();
      showActionMsg("✓ Обновлено", "ok");
    } catch (e) {
      showActionMsg(`❌ ${e.message || e}`, "err");
    } finally {
      btn.disabled = false; btn.textContent = orig;
    }
  };
  document.getElementById("btn-pause").onclick = async () => {
    try { await api("/api/control/pause", { method: "POST" }); showActionMsg("⏸ Пауза включена", "ok"); }
    catch (e) { showActionMsg(`❌ ${e.message || e}`, "err"); }
    loadStatus();
  };
  document.getElementById("btn-resume").onclick = async () => {
    try { await api("/api/control/resume", { method: "POST" }); showActionMsg("▶ Возобновлено", "ok"); }
    catch (e) { showActionMsg(`❌ ${e.message || e}`, "err"); }
    loadStatus();
  };
  document.getElementById("btn-switch-pair").onclick = async () => {
    showActionMsg("🔀 Перехожу в SEARCH режим…", "info");
    try {
      const r = await api("/api/control/switch_pair", { method: "POST" });
      if (r && r.ok && (r.new === "SEARCH" || r.mode === "SEARCH")) {
        showActionMsg(`🔍 SEARCH режим: ${r.old || "пара"} исключена из цикла. Войду на первый CONSENSUS-сигнал.`, "ok");
      } else if (r && r.ok && r.new) {
        showActionMsg(`🔀 Сменена пара: ${r.old} → ${r.new}`, "ok");
      } else {
        showActionMsg("⚠️ Нет активного цикла или tracked-пар.", "warn");
      }
    } catch (e) {
      showActionMsg(`❌ ${e.message || e}`, "err");
    }
    loadStatus();
  };
  document.getElementById("btn-reset-cycle").onclick = async () => {
    if (!confirm("Сбросить текущий цикл и вернуться в FREE-режим?\n\nМГ-шаг и пара сбросятся, бот начнёт искать новый сигнал с базовой ставки.")) return;
    try {
      const r = await api("/api/control/reset_cycle", { method: "POST" });
      const old = (r && r.old) ? r.old : "цикл";
      showActionMsg(`♻️ Сброшен ${old} → FREE. Ищу новый сигнал…`, "ok");
    } catch (e) {
      showActionMsg(`❌ ${e.message || e}`, "err");
    }
    loadStatus();
  };

  // ═════════════════════ ЧАРТ-ПАНЕЛЬ (этап 3+) ═══════════════════════════
  // Сворачиваемая панель с лайв-графиком + HUD карточкой как у PoSignals.
  // Открывается кликом по строке «Tracked пары» в карточке статуса.
  let chart = null, candleSeries = null, currentChartSymbol = null;
  let chartFilterMode = "tracked";  // tracked | payout | all
  let chartAutoRefreshTimer = null;

  function initChart() {
    if (chart) return;
    if (typeof LightweightCharts === "undefined") {
      console.error("LightweightCharts не загружен");
      return;
    }
    const container = document.getElementById("chart-container");
    if (!container) return;
    chart = LightweightCharts.createChart(container, {
      width: container.clientWidth || 320,
      height: 360,
      layout: { background: { type: "solid", color: "#0f1115" }, textColor: "#e8eaed" },
      grid:   { vertLines: { color: "rgba(255,255,255,0.04)" }, horzLines: { color: "rgba(255,255,255,0.04)" } },
      crosshair: { mode: 0 },
      rightPriceScale: { borderColor: "rgba(255,255,255,0.08)" },
      timeScale: {
        timeVisible: true, secondsVisible: false,
        borderColor: "rgba(255,255,255,0.08)",
        rightOffset: 5,
      },
    });
    candleSeries = chart.addCandlestickSeries({
      upColor: "#22c55e", downColor: "#ef4444",
      borderUpColor: "#22c55e", borderDownColor: "#ef4444",
      wickUpColor: "#22c55e", wickDownColor: "#ef4444",
    });
    // Resize-observer чтобы график подстраивался при разворачивании панели
    const ro = new ResizeObserver(() => {
      if (chart && container.clientWidth > 0) {
        chart.applyOptions({ width: container.clientWidth });
      }
    });
    ro.observe(container);
  }

  async function fetchPairsForFilter() {
    if (chartFilterMode === "tracked") {
      const s = await api("/api/status");
      const list = s.tracked_pairs_list || [];
      // дополним payout-инфой через /api/payout_pairs (без отсечки)
      const all = await api("/api/payout_pairs?min_payout=0").catch(() => ({ pairs: [] }));
      const payoutMap = {};
      (all.pairs || []).forEach((p) => { payoutMap[p.symbol] = p.payout; });
      return list.map((sym) => ({ symbol: sym, payout: payoutMap[sym] || 0 }));
    }
    if (chartFilterMode === "payout") {
      const data = await api("/api/payout_pairs");
      // Обновим лейбл фильтра текущим порогом
      if (data.min_payout != null) {
        const el = document.getElementById("filter-min-payout");
        if (el) el.textContent = data.min_payout;
      }
      return data.pairs || [];
    }
    // all
    const data = await api("/api/payout_pairs?min_payout=0");
    return data.pairs || [];
  }

  async function populateChartPairsList() {
    const select = document.getElementById("chart-pair-select");
    if (!select) return;
    let pairs = [];
    try { pairs = await fetchPairsForFilter(); } catch (e) { console.error(e); }
    select.innerHTML = pairs.length
      ? pairs.map((p) =>
          `<option value="${p.symbol}">${p.symbol}${p.payout ? ` — ${p.payout}%` : ""}</option>`
        ).join("")
      : `<option value="">— нет пар —</option>`;
    if (currentChartSymbol && pairs.find((p) => p.symbol === currentChartSymbol)) {
      select.value = currentChartSymbol;
    } else if (pairs.length) {
      currentChartSymbol = pairs[0].symbol;
      select.value = currentChartSymbol;
    } else {
      currentChartSymbol = null;
    }
  }

  async function loadCandlesForChart(symbol) {
    if (!symbol || !chart || !candleSeries) return;
    try {
      const data = await api(`/api/candles?symbol=${encodeURIComponent(symbol)}&limit=1100`);
      const cs = (data.candles || []).map((c) => ({
        time: c.time, open: c.open, high: c.high, low: c.low, close: c.close,
      }));
      candleSeries.setData(cs);
      // Show last ~60 bars by default; user может проскроллить назад до самого начала
      if (cs.length > 60) {
        chart.timeScale().setVisibleLogicalRange({
          from: cs.length - 60, to: cs.length - 1,
        });
      } else {
        chart.timeScale().fitContent();
      }
    } catch (e) {
      console.error("loadCandles failed", e);
    }
  }

  async function updateChartHUD(symbol) {
    if (!symbol) return;
    const hud = document.getElementById("chart-hud");
    if (!hud) return;
    try {
      const s = await api(`/api/pair_score?symbol=${encodeURIComponent(symbol)}`);
      const wr = s.wr ?? 0;
      const wr1 = s.wr1 ?? 0;
      const wr1r = s.wr1_recent ?? 0;
      const recent = (s.recent_results || []).slice(-30);
      const recentStr = recent.map((r) => r === 1 ? "✓" : "✗").join("");
      const wrColor = wr >= 60 ? "#22c55e" : wr >= 50 ? "#fdf647" : "#ef4444";
      const wr1rColor = wr1r >= 70 ? "#22c55e" : wr1r >= 60 ? "#fdf647" : "#ef4444";
      const streakWarn = (s.max_loss_streak_before_win || 0) > 3 ? " ⚠️" : "";
      let stateBadge = "";
      if (s.ban) stateBadge = ` <span style="color:#ef4444">🚫 BAN</span>`;
      else if (s.pause) stateBadge = ` <span style="color:#fdf647">⏸ ПАУЗА</span>`;
      else if (s.allowed) stateBadge = ` <span style="color:#22c55e">✓ tracked</span>`;
      hud.innerHTML = `
        <div>🧠 CONSENSUS 4/5 | ⏱ Экспир: ${s.expiry_bars} бара${stateBadge}</div>
        <div>📊 Payout: <b>${s.payout || "?"}%</b></div>
        <div style="color:${wrColor}">🏁 Общая: ${wr.toFixed(0)}% | ✅ ${s.wins} : ❌ ${s.losses} (всего сигналов: ${s.signals_count || 0})</div>
        <div style="color:${wr1rColor}">🎯 Проходимость 1-го входа за 200 св: ${wr1r.toFixed(0)}%</div>
        <div>⚡ WR1 (вся ист.): ${wr1.toFixed(0)}%</div>
        <div>📉 Макс. минусов до ✅: ${s.max_loss_streak_before_win}${streakWarn} | всего: ${s.max_loss_streak}</div>
        ${recentStr ? `<div>📈 Последние ${recent.length}: ${recentStr}</div>` : ""}
        ${s.reason ? `<div class="hint" style="margin-top:6px; font-size:10px">${s.reason}</div>` : ""}
      `;
    } catch (e) {
      hud.innerHTML = `<div class="err">Ошибка: ${e.message || e}</div>`;
    }
  }

  async function refreshChart() {
    if (!currentChartSymbol) return;
    await loadCandlesForChart(currentChartSymbol);
    await updateChartHUD(currentChartSymbol);
  }

  async function openChartPanel() {
    const panel = document.getElementById("chart-panel");
    if (!panel) return;
    panel.style.display = "";
    initChart();
    await populateChartPairsList();
    if (currentChartSymbol) await refreshChart();
    // авто-обновление каждые 10 сек пока панель открыта
    if (chartAutoRefreshTimer) clearInterval(chartAutoRefreshTimer);
    chartAutoRefreshTimer = setInterval(() => {
      const p = document.getElementById("chart-panel");
      if (p && p.style.display !== "none") refreshChart();
    }, 10000);
  }

  function closeChartPanel() {
    const panel = document.getElementById("chart-panel");
    if (!panel) return;
    panel.style.display = "none";
    if (chartAutoRefreshTimer) {
      clearInterval(chartAutoRefreshTimer);
      chartAutoRefreshTimer = null;
    }
  }

  function toggleChartPanel() {
    const panel = document.getElementById("chart-panel");
    if (!panel) return;
    if (panel.style.display === "none") openChartPanel();
    else closeChartPanel();
  }

  // Привязки чарт-панели
  document.getElementById("row-tracked")?.addEventListener("click", toggleChartPanel);
  document.getElementById("btn-chart-close")?.addEventListener("click", closeChartPanel);
  document.getElementById("chart-pair-select")?.addEventListener("change", (e) => {
    currentChartSymbol = e.target.value;
    refreshChart();
  });
  document.getElementById("btn-chart-prev")?.addEventListener("click", () => {
    const sel = document.getElementById("chart-pair-select");
    if (!sel || sel.options.length === 0) return;
    const idx = sel.selectedIndex;
    sel.selectedIndex = (idx - 1 + sel.options.length) % sel.options.length;
    currentChartSymbol = sel.value;
    refreshChart();
  });
  document.getElementById("btn-chart-next")?.addEventListener("click", () => {
    const sel = document.getElementById("chart-pair-select");
    if (!sel || sel.options.length === 0) return;
    const idx = sel.selectedIndex;
    sel.selectedIndex = (idx + 1) % sel.options.length;
    currentChartSymbol = sel.value;
    refreshChart();
  });
  document.querySelectorAll(".chart-filter-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      document.querySelectorAll(".chart-filter-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      chartFilterMode = btn.dataset.filter;
      await populateChartPairsList();
      await refreshChart();
    });
  });

  // ═════════════════════ НАСТРОЙКИ БОТА (общие) ══════════════════════════
  // Только общие настройки. Indicator params живут в «Стратегия → Настройки».
  const GLOBAL_SCHEMA = {
    "🔍 Фильтр пар": [
      { k: "filter.asset_categories", t: "multi",
        options: ["forex", "crypto", "stocks", "indices", "commodities", "other"],
        label: "Категории активов (пусто = все)",
        desc: "какие типы активов разрешены к торговле; пусто = все доступные" },
      { k: "filter.min_payout", t: "int", min: 50, max: 95, label: "Мин. payout для первой сделки (%)",
        desc: "минимальная выплата чтобы зайти в ПЕРВУЮ сделку нового цикла" },
      { k: "filter.payout_floor", t: "int", min: 50, max: 90, label: "Порог смены пары при падении payout (%)",
        desc: "если выплата на текущей паре упала ниже — бот меняет пару (не для последней пары цикла)" },
      { k: "filter.max_losses_in_row", t: "int", min: 1, max: 10, label: "Макс. минусов до бана",
        desc: "если у пары серия минусов подряд > этого числа — 12ч бан" },
      { k: "filter.min_wr1", t: "int", min: 0, max: 100, step: 5, label: "Мин. % 1-й сделки за 1000 свечей",
        desc: "долгосрочный фильтр: пара с WR1 ниже не торгуется (skip, не бан)" },
      { k: "filter.min_wr1_recent", t: "int", min: 0, max: 100, step: 5, label: "Мин. % 1-й сделки за 200 свечей",
        desc: "фильтр свежей формы: пара ниже порога → пауза (не бан)" },
      { k: "filter.recent_lookback_bars", t: "int", min: 50, max: 500, step: 50, label: "Окно recent (свечей)",
        desc: "размер «свежего» окна для расчёта recent-статистики (default 200 свечей)" },
      { k: "filter.history_candles", t: "int", min: 200, max: 2000, label: "Размер истории",
        desc: "сколько свечей грузить на пару при старте (нужно ≥1000 для статистики)" },
      { k: "filter.ban_hours", t: "int", min: 1, max: 72, label: "Бан пары (часов)",
        desc: "длительность бана пары при провале max_losses_in_row" },
      { k: "filter.pause_minutes", t: "int", min: 5, max: 1440, step: 5, label: "Пауза за низкий recent WR1 (мин.)",
        desc: "короткая пауза ОДНОЙ пары при провале recent WR1 (остальные торгуются)" },
      { k: "filter.day_off_hours", t: "int", min: 1, max: 24, label: "День-офф (часов)",
        desc: "ГЛОБАЛЬНАЯ пауза всего бота если ни одна пара не прошла фильтр" },
    ],
    "💰 Торговля": [
      { k: "trading.base_amount", t: "float", min: 0.5, max: 100, step: 0.5, label: "Базовая ставка ($)",
        desc: "сумма первой сделки нового цикла; от неё считаются все перекрытия" },
      { k: "trading.expiry_seconds", t: "int", min: 30, max: 600, label: "Экспирация (сек)",
        desc: "длительность открытой сделки в секундах (60 = 1 мин, 120 = 2 мин)" },
    ],
    "🎰 Мартингейл": [
      { k: "martingale.enabled", t: "bool", label: "Включить мартингейл",
        desc: "вкл = удваивать после минуса; выкл = после LOSS сразу новый поиск с base" },
      { k: "martingale.coefficient", t: "float", min: 1.5, max: 5, step: 0.1, label: "Множитель", parent: "martingale.enabled",
        desc: "коэффициент удвоения ставки на каждое следующее перекрытие (2.1 = ×2.1)" },
      { k: "martingale.cycle_total_limit", t: "int", min: 1, max: 20, label: "Общий лимит сделок в цикле (РОВНО N сделок)", parent: "martingale.enabled",
        desc: "жёсткий потолок сделок в одном цикле; при достижении — стоп до /resume" },
      { k: "martingale.stop_sum", t: "float", min: 10, max: 10000, step: 50, label: "Стоп-сумма ($)", parent: "martingale.enabled",
        desc: "потолок потерь $ в цикле; при достижении — стоп до /resume" },
      { k: "martingale.pair_limits", t: "intlist", label: "Сделок на каждой паре через запятую (длина = число пар)", parent: "martingale.enabled",
        desc: "«3,3,2» = 3 пары (на 1-й до 3 сделок, на 2-й до 3, на последней до 2 + перенос резерва)" },
      { k: "martingale.consecutive_losses_switch", t: "int", min: 0, max: 10, label: "Минусов подряд для switch (0 = выкл)", parent: "martingale.enabled",
        desc: "после N минусов подряд на не-последней паре — переход с переносом резерва" },
      { k: "martingale.carry_unused", t: "bool", label: "Переносить неиспользованные перекрытия в резерв", parent: "martingale.enabled",
        desc: "если ушли с пары не исчерпав лимит — остаток отдадут последней паре цикла" },
      { k: "martingale.last_pair_until_stop_sum", t: "bool", label: "На последней паре цикла торговать до stop-sum", parent: "martingale.enabled",
        desc: "на последней паре игнорить лимит/payout/серии и торговать до WIN или stop-sum" },
      { k: "martingale.manual_switch_counts", t: "bool", label: "Ручная смена пары засчитывается в счётчик", parent: "martingale.enabled",
        desc: "если меняешь пару руками через UI/TG — считается как cycle_switches" },
    ],
    "⏰ Расписание работы": [
      { k: "schedule.enabled", t: "bool", label: "Работать по расписанию (снять = 24/7 круглосуточно)",
        desc: "вкл = торговать только в указанные часы; аналитика пишется 24/7 в любом случае" },
      { k: "schedule.start_hour", t: "int", min: 0, max: 23, label: "Час начала (0-23)", parent: "schedule.enabled",
        desc: "час открытия торгового окна в локальной TZ (telegram.daily_report_timezone)" },
      { k: "schedule.end_hour", t: "int", min: 0, max: 24, label: "Час конца (0-24)", parent: "schedule.enabled",
        desc: "час закрытия торгового окна; активный цикл доводится до WIN даже после конца" },
      { k: "schedule.no_weekends", t: "bool", label: "📅 Не торговать на выходных (Сб/Вс)", parent: "schedule.enabled",
        desc: "вкл = пропускать субботу и воскресенье; аналитика всё равно пишется" },
    ],
    "🗄 Хранение аналитики": [
      { k: "retention.signals_days", t: "int", min: 30, max: 365, step: 30, label: "Хранить signals (дней, 30-365)",
        desc: "сколько дней держать историю сигналов в БД; старше — удаляются раз в сутки" },
    ],
    "📋 Периодический отчёт": [
      { k: "periodic_report.enabled", t: "bool", label: "Присылать сводку раз в сутки",
        desc: "ежедневная сводка в TG: баланс, профит, минусы подряд, выплаты" },
      { k: "periodic_report.hour", t: "int", min: 0, max: 23, label: "Час отправки (0–23, локальная TZ)", parent: "periodic_report.enabled",
        desc: "час отправки; если в этот час идёт мартингейл-цикл — ждёт его закрытия" },
    ],
  };

  function getDeep(o, path) { return path.split(".").reduce((a, p) => a?.[p], o); }

  function inferType(value) {
    if (typeof value === "boolean") return "bool";
    if (typeof value === "number") return Number.isInteger(value) ? "int" : "float";
    return "string";
  }

  function flashSetting(el, ok) {
    el.style.outline = `2px solid ${ok ? "#22c55e" : "#ef4444"}`;
    setTimeout(() => (el.style.outline = ""), 600);
  }

  // Загружает только GLOBAL_SCHEMA в #settings-list (без strategy params)
  async function loadGlobalSettings() {
    const cont = document.getElementById("settings-list");
    cont.innerHTML = "Загрузка…";
    try {
      const cfg = await api("/api/settings");
      cont.innerHTML = "";
      for (const [cat, items] of Object.entries(GLOBAL_SCHEMA)) {
        const div = document.createElement("div");
        div.className = "category";
        div.innerHTML = `<div class="category-title">${cat}</div>`;
        // Карта parent → DOM-контейнер для детей (создаём пустые групы заранее)
        const childGroups = new Map();
        for (const it of items) {
          const v = getDeep(cfg, it.k);
          const row = document.createElement("div");
          row.className = "setting-row";
          if (it.parent) row.dataset.parent = it.parent;
          let input;
          if (it.t === "bool") {
            input = `<input type="checkbox" data-k="${it.k}" data-t="bool" ${v ? "checked" : ""}/>`;
          } else if (it.t === "choice") {
            input = `<select data-k="${it.k}" data-t="choice">` +
              it.options.map((o) => `<option value="${o}" ${o === v ? "selected" : ""}>${o}</option>`).join("") +
              `</select>`;
          } else if (it.t === "multi") {
            const arr = Array.isArray(v) ? v : [];
            const opts = it.options.map((o) =>
              `<label class="multi-opt"><input type="checkbox" data-multi="${it.k}" data-opt="${o}" ${arr.includes(o) ? "checked" : ""}/> ${o}</label>`
            ).join("");
            input = `<div class="multi-row">${opts}</div>`;
          } else if (it.t === "intlist") {
            const csv = Array.isArray(v) ? v.join(",") : (v ?? "");
            input = `<input type="text" data-k="${it.k}" data-t="intlist" placeholder="3,3,2" value="${csv}"/>`;
          } else {
            const step = it.step || (it.t === "int" ? 1 : 0.1);
            input = `<input type="number" data-k="${it.k}" data-t="${it.t}"
                            min="${it.min ?? ""}" max="${it.max ?? ""}" step="${step}" value="${v ?? ""}"/>`;
          }
          // ⓘ button — отдельный flex-item ВНЕ <label>, чтобы он не
          // растягивался по ширине лейбла на мобильном. flex: 0 0 auto
          // в CSS гарантирует фиксированный 18×18 размер.
          const infoBtn = it.desc
            ? `<button class="info-btn" type="button" data-info="${it.desc.replace(/"/g, "&quot;")}" data-key="${it.k}" title="Что это">ⓘ</button>`
            : "";
          row.innerHTML = `<label>${it.label}</label>${infoBtn}${input}`;

          if (it.parent) {
            // append в child-группу parent'а
            let group = childGroups.get(it.parent);
            if (!group) {
              group = document.createElement("div");
              group.className = "setting-children";
              group.dataset.childrenOf = it.parent;
              childGroups.set(it.parent, group);
            }
            group.appendChild(row);
          } else {
            div.appendChild(row);
            // Если этот item — toggle (bool), создадим под него child-группу заранее
            if (it.t === "bool") {
              const group = document.createElement("div");
              group.className = "setting-children";
              group.dataset.childrenOf = it.k;
              childGroups.set(it.k, group);
              div.appendChild(group);
            }
          }
        }
        // Если родитель сам с parent=другой_родитель (вложенность), нашёл его group и закинул туда.
        // Скрыть child-группы у выключенных toggle'ов сразу при первом рендере.
        for (const [parentKey, group] of childGroups) {
          const parentInput = div.querySelector(`[data-k="${parentKey}"][data-t="bool"]`);
          if (parentInput && !parentInput.checked) group.style.display = "none";
        }
        cont.appendChild(div);
      }
      // Handlers — single-key updates + parent/children visibility
      cont.querySelectorAll("[data-k]").forEach((el) => {
        el.addEventListener("change", async () => {
          const key = el.dataset.k;
          const t = el.dataset.t;
          let value;
          if (t === "bool") value = el.checked;
          else if (t === "int") value = parseInt(el.value);
          else if (t === "float") value = parseFloat(el.value);
          else if (t === "intlist") {
            value = el.value.split(",").map((s) => parseInt(s.trim())).filter((n) => Number.isFinite(n) && n > 0);
            if (!value.length) { flashSetting(el, false); return; }
          } else value = el.value;
          try {
            await api("/api/settings", { method: "PUT", body: JSON.stringify({ [key]: value }) });
            flashSetting(el, true);
            // Если toggle — show/hide группу детей
            if (t === "bool") {
              const group = cont.querySelector(`[data-children-of="${key}"]`);
              if (group) group.style.display = el.checked ? "" : "none";
            }
          } catch (e) { flashSetting(el, false); console.error(e); }
        });
      });
      // Handlers — multi-select (asset_categories)
      cont.querySelectorAll("[data-multi]").forEach((el) => {
        el.addEventListener("change", async () => {
          const key = el.dataset.multi;
          const opts = [];
          cont.querySelectorAll(`[data-multi="${key}"]:checked`).forEach((c) =>
            opts.push(c.dataset.opt)
          );
          try {
            await api("/api/settings", { method: "PUT", body: JSON.stringify({ [key]: opts }) });
            flashSetting(el, true);
          } catch (e) { flashSetting(el, false); console.error(e); }
        });
      });
    } catch (e) {
      cont.innerHTML = `<div class="status-line err">Ошибка: ${e.message}</div>`;
    }
  }

  // ═════════════════════ СТРАТЕГИЯ → НАСТРОЙКИ ══════════════════════════
  // Загружает ТОЛЬКО indicator params активной стратегии
  async function loadStrategyParams() {
    const cont = document.getElementById("strategy-params-list");
    cont.innerHTML = "Загрузка…";
    try {
      const stratData = await api("/api/strategies");
      cont.innerHTML = "";
      const active = stratData.strategies.find((s) => s.active) || stratData.strategies[0];
      if (!active) {
        cont.innerHTML = `<div class="status-line">Нет активной стратегии. Выбери в подвкладке «Список».</div>`;
        return;
      }
      const div = document.createElement("div");
      div.className = "category";
      div.innerHTML = `<div class="category-title">Параметры: <b>${active.name}</b></div>`;
      const paramKeys = Object.keys(active.default_params || {});
      if (paramKeys.length === 0) {
        div.innerHTML += `<div class="status-line">У этой стратегии нет настраиваемых параметров.</div>`;
        cont.appendChild(div);
        return;
      }
      for (const key of paramKeys) {
        const schema = (active.param_schema || {})[key] || {};
        const value = (active.params || {})[key] ?? active.default_params[key];
        const t = schema.type || inferType(value);
        const label = schema.label || key;
        const row = document.createElement("div");
        row.className = "setting-row";
        let input;
        if (t === "bool") {
          input = `<input type="checkbox" data-strat-k="${key}" data-strat="${active.name}" data-t="bool" ${value ? "checked" : ""}/>`;
        } else if (t === "choice" && Array.isArray(schema.options)) {
          input = `<select data-strat-k="${key}" data-strat="${active.name}" data-t="choice">` +
            schema.options.map((o) => `<option value="${o}" ${o === value ? "selected" : ""}>${o}</option>`).join("") +
            `</select>`;
        } else if (t === "string") {
          input = `<input type="text" data-strat-k="${key}" data-strat="${active.name}" data-t="string" value="${value ?? ""}"/>`;
        } else {
          const step = schema.step || (t === "int" ? 1 : 0.1);
          input = `<input type="number" data-strat-k="${key}" data-strat="${active.name}" data-t="${t}"
                          min="${schema.min ?? ""}" max="${schema.max ?? ""}" step="${step}" value="${value ?? ""}"/>`;
        }
        row.innerHTML = `<label>${label}<span class="hint">${key}</span></label>${input}`;
        div.appendChild(row);
      }
      cont.appendChild(div);
      cont.querySelectorAll("[data-strat-k]").forEach((el) => {
        el.addEventListener("change", async () => {
          const key = el.dataset.stratK;
          const strat = el.dataset.strat;
          const t = el.dataset.t;
          let value;
          if (t === "bool") value = el.checked;
          else if (t === "int") value = parseInt(el.value);
          else if (t === "float") value = parseFloat(el.value);
          else value = el.value;
          try {
            await api(`/api/strategies/${encodeURIComponent(strat)}/params`,
                      { method: "PUT", body: JSON.stringify({ [key]: value }) });
            flashSetting(el, true);
          } catch (e) { flashSetting(el, false); console.error(e); }
        });
      });
    } catch (e) {
      cont.innerHTML = `<div class="status-line err">Ошибка: ${e.message}</div>`;
    }
  }

  // ═════════════════════ СТРАТЕГИЯ → СПИСОК ════════════════════════════════
  // UX-flow: клик по карточке стратегии =
  //   1) активирует её (если не активна)
  //   2) переходит на уровень «detail» (Настройки + Аналитика подвкладки)
  // Из detail-уровня → кнопка «← Назад к списку» возвращает на list.
  // 🗑 — только для пользовательских неактивных, не триггерит drill-down.

  async function loadStrategies() {
    const list = document.getElementById("strategies-list");
    list.innerHTML = "<li>Загрузка…</li>";
    try {
      const data = await api("/api/strategies");
      list.innerHTML = "";
      // Подсказка пользователю — что делать
      const hint = document.createElement("li");
      hint.className = "strategy-hint";
      hint.innerHTML = `<span class="hint">Клик на карточку → активация + переход к настройкам этой стратегии.</span>`;
      list.appendChild(hint);
      for (const s of data.strategies) {
        const li = document.createElement("li");
        li.classList.toggle("strategy-active", s.active);
        li.classList.add("strategy-card");
        li.dataset.name = s.name;
        const activeBadge = s.active
          ? `<span class="badge active">✓ Активна</span>`
          : `<span class="badge source-${s.source}">${s.source === "user" ? "своя" : "встроенная"}</span>`;
        const arrow = `<span class="strategy-arrow">→</span>`;
        const delBtn = (s.source === "user" && !s.active)
          ? `<button class="btn-mini" data-act="delete" data-name="${s.name}" title="Удалить">🗑</button>`
          : "";
        li.innerHTML = `
          <div class="strategy-row">
            <div class="strategy-name">${s.name} ${activeBadge}</div>
            <div class="strategy-actions">${delBtn}${arrow}</div>
          </div>
        `;
        list.appendChild(li);
      }
      // Клик по карточке → активация (если надо) + drill-down в detail
      list.querySelectorAll(".strategy-card").forEach((card) => {
        card.addEventListener("click", async (e) => {
          if (e.target.closest("[data-act='delete']")) return;
          const name = card.dataset.name;
          try {
            if (!card.classList.contains("strategy-active")) {
              await api(`/api/strategies/${encodeURIComponent(name)}/activate`, { method: "POST" });
              loadStatus();
            }
            // Заполняем имя стратегии в шапке detail-уровня
            const nameEl = document.getElementById("strategy-detail-name");
            if (nameEl) nameEl.textContent = name;
            // Переход на detail-уровень → дефолтная подвкладка «Настройки»
            showStrategyLevel("detail");
            gotoSubtab("strategy-settings");
          } catch (err) {
            alert(`Не удалось активировать «${name}»: ${err.message || err}`);
          }
        });
      });
      // Удаление — отдельным обработчиком, не передаёт клик карточке
      list.querySelectorAll("[data-act='delete']").forEach((b) => {
        b.addEventListener("click", async (e) => {
          e.stopPropagation();
          const name = b.dataset.name;
          if (!confirm(`Удалить стратегию "${name}"?`)) return;
          await api(`/api/strategies/${encodeURIComponent(name)}`, { method: "DELETE" });
          loadStrategies();
          loadStatus();
        });
      });
    } catch (e) {
      list.innerHTML = `<li class="err">${e.message}</li>`;
    }
  }

  // Load template into textarea
  document.getElementById("btn-load-template").onclick = async () => {
    try {
      const r = await fetch("/strategy_template");
      const code = await r.text();
      document.getElementById("new-strat-code").value = code;
      if (!document.getElementById("new-strat-name").value) {
        document.getElementById("new-strat-name").value = "my_strategy";
      }
    } catch (e) {
      console.error(e);
    }
  };

  document.getElementById("btn-upload-strat").onclick = async () => {
    const name = document.getElementById("new-strat-name").value.trim();
    const code = document.getElementById("new-strat-code").value;
    const status = document.getElementById("strat-status");
    status.textContent = "Загружаю…";
    status.className = "status-line";
    try {
      const r = await api("/api/strategies", { method: "POST", body: JSON.stringify({ name, code }) });
      await api(`/api/strategies/${encodeURIComponent(r.name)}/activate`, { method: "POST" });
      status.textContent = `✓ Стратегия "${r.name}" сохранена и активирована`;
      status.className = "status-line ok";
      loadStrategies();
    } catch (e) {
      status.textContent = `❌ ${e.message}`;
      status.className = "status-line err";
    }
  };

  // ═════════════════════ АНАЛИТИКА (этап 2) ═══════════════════════════
  // Колонки таблицы: ключ → заголовок + опциональная функция форматирования
  const AN_COLS = [
    { k: "symbol",                  h: "Пара",          fmt: (v) => `<b>${v}</b>` },
    { k: "signals",                 h: "Сигналов" },
    { k: "entered",                 h: "Вошёл" },
    { k: "wr_first",                h: "WR 1 бар %",    cls: wrClass },
    { k: "wr_chosen",               h: "WR exp %",      cls: wrClass },
    { k: "wr_best",                 h: "Best exp %",    cls: wrClass },
    { k: "pluses",                  h: "+" },
    { k: "minuses",                 h: "-" },
    { k: "max_loss_streak_to_win",  h: "Макс minus→plus" },
    { k: "avg_payout",              h: "Avg payout %" },
    { k: "pct_payout_optimal",      h: "% 85-92" },
    { k: "wr_real",                 h: "WR real %",     cls: wrClass },
    { k: "wins_real",               h: "WIN" },
    { k: "losses_real",             h: "LOSS" },
    { k: "profit_real",             h: "Profit $",      cls: profitClass },
    // ─── разделитель ─── market snapshots
    { k: "_sep_",                   h: "│" },
    { k: "avg_votes_total",         h: "Avg votes" },
    { k: "avg_atr_ratio",           h: "Avg ATR ratio" },
    { k: "avg_bb_position",         h: "Avg BB pos" },
    { k: "avg_candle_atr_ratio",    h: "Avg candle/ATR" },
    { k: "avg_rsi_ma",              h: "Avg RSI MA" },
    { k: "avg_qqe_trailing",        h: "Avg QQE" },
    { k: "avg_wr1_long",            h: "WR1-1000 %" },
    { k: "avg_wr1_recent",          h: "WR1-200 %" },
  ];

  function wrClass(v) {
    if (v == null) return "";
    if (v >= 70) return "wr-green";
    if (v >= 60) return "wr-yellow";
    if (v >= 50) return "wr-orange";
    return "wr-red";
  }
  function profitClass(v) {
    if (v == null || v === 0) return "";
    return v > 0 ? "profit-pos" : "profit-neg";
  }
  function fmtCell(v, k) {
    if (v == null) return "—";
    if (k === "profit_real") return `${v >= 0 ? "+" : ""}${(+v).toFixed(2)}`;
    if (typeof v === "number" && !Number.isInteger(v)) return v.toFixed(2);
    return v;
  }

  let _anSortKey = "signals";
  let _anSortDir = "desc";

  function getAnFilters() {
    const days = parseInt(document.querySelector(".period-btn.active")?.dataset.days || "7");
    const hf = document.getElementById("an-hour-from").value;
    const ht = document.getElementById("an-hour-to").value;
    const dows = [];
    document.querySelectorAll('.dow-filter input[type="checkbox"]:checked').forEach((c) =>
      dows.push(c.dataset.dow)
    );
    const params = new URLSearchParams({ period_days: days });
    if (hf !== "" && ht !== "") {
      params.set("hour_from", hf);
      params.set("hour_to", ht);
    }
    if (dows.length > 0 && dows.length < 7) params.set("dow", dows.join(","));
    return { params, days, hf, ht, dows };
  }

  async function loadAnalytics() {
    const tbody = document.querySelector("#an-table tbody");
    const thead = document.querySelector("#an-table thead");
    const meta = document.getElementById("an-meta");
    tbody.innerHTML = `<tr><td colspan="${AN_COLS.length}">Загрузка…</td></tr>`;
    try {
      const { params, days, hf, ht } = getAnFilters();
      const data = await api(`/api/analytics/pairs?${params.toString()}`);
      meta.textContent = `Период: ${days}д · Стратегия: ${data.strategy || "—"} · Экспирация по умолчанию: ${data.expiry_bars} бар(а)` +
        (hf !== "" && ht !== "" ? ` · Часы: ${hf}-${ht}` : "");
      // header
      thead.innerHTML = "<tr>" + AN_COLS.map((c) =>
        c.k === "_sep_"
          ? `<th class="sep">${c.h}</th>`
          : `<th data-sort="${c.k}" class="sortable ${_anSortKey === c.k ? "sorted-" + _anSortDir : ""}">${c.h}</th>`
      ).join("") + "</tr>";
      thead.querySelectorAll("[data-sort]").forEach((th) => {
        th.addEventListener("click", () => {
          const k = th.dataset.sort;
          if (_anSortKey === k) _anSortDir = _anSortDir === "asc" ? "desc" : "asc";
          else { _anSortKey = k; _anSortDir = "desc"; }
          loadAnalytics();
        });
      });
      // sort
      const rows = (data.rows || []).slice().sort((a, b) => {
        const av = a[_anSortKey], bv = b[_anSortKey];
        if (av == null && bv == null) return 0;
        if (av == null) return 1;
        if (bv == null) return -1;
        if (typeof av === "string") return _anSortDir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
        return _anSortDir === "asc" ? av - bv : bv - av;
      });
      if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="${AN_COLS.length}" class="hint" style="padding:20px;text-align:center">Нет данных за выбранный период. Сигналы пишутся в реальном времени — подожди пока бот наберёт историю.</td></tr>`;
        return;
      }
      tbody.innerHTML = rows.map((r) => {
        return `<tr data-symbol="${r.symbol}">` + AN_COLS.map((c) => {
          if (c.k === "_sep_") return `<td class="sep">│</td>`;
          const v = r[c.k];
          const cls = c.cls ? c.cls(v) : "";
          return `<td class="${cls}">${fmtCell(v, c.k)}</td>`;
        }).join("") + "</tr>";
      }).join("");
      // click row → drill-down 24h
      tbody.querySelectorAll("tr[data-symbol]").forEach((tr) => {
        tr.addEventListener("click", () => loadHourly(tr.dataset.symbol));
      });
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="${AN_COLS.length}" class="err">Ошибка: ${e.message}</td></tr>`;
    }
  }

  async function loadHourly(symbol, group = "hour") {
    const wrap = document.getElementById("an-hourly");
    wrap.style.display = "";
    wrap.innerHTML = `<div class="card"><h3>📊 ${symbol} — drill-down</h3><div>Загрузка…</div></div>`;
    try {
      const { params } = getAnFilters();
      params.set("symbol", symbol);
      params.set("group", group);
      const data = await api(`/api/analytics/hourly?${params.toString()}`);
      const rows = data.rows || [];
      const byKey = {};
      rows.forEach((r) => { byKey[group === "hour" ? r.hour : r.dow] = r; });
      const dowNames = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"];
      const labelFor = (i) => group === "hour"
        ? `${String(i).padStart(2, "0")}:00`
        : dowNames[i] || "?";
      const range = group === "hour" ? 24 : 7;
      const groupLabel = group === "hour" ? "24ч" : "по дням недели";
      const otherGroup = group === "hour" ? "dow" : "hour";
      const otherLabel = group === "hour" ? "📅 По дням" : "🕒 По часам";
      // Drill-down показывает ТЕ ЖЕ колонки что и общая таблица (AN_COLS),
      // только без "Пара" (drill-down уже привязан к одному символу).
      // Прокрутка горизонтальная — .analytics-table-wrap (overflow-x:auto).
      const drillCols = AN_COLS.filter((c) => c.k !== "symbol");
      let html = `<div class="card">
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:8px">
          <h3 style="margin:0; flex:1">📊 ${symbol} — ${groupLabel} (${data.period_days}д)</h3>
          <button id="btn-drill-toggle" class="btn">${otherLabel}</button>
          <button id="btn-hourly-close" class="btn">✕</button>
        </div>
        <p class="hint" style="font-size:11px;margin:0 0 6px">Свайпни таблицу влево — там ещё индикаторы (ATR, BB, RSI, QQE, votes).</p>
        <div class="analytics-table-wrap">
        <table class="analytics-table"><thead><tr>
          <th>${group === "hour" ? "Час" : "День"}</th>` +
          drillCols.map((c) => c.k === "_sep_"
            ? `<th class="sep">${c.h}</th>`
            : `<th>${c.h}</th>`
          ).join("") +
        `</tr></thead><tbody>`;
      for (let i = 0; i < range; i++) {
        const r = byKey[i];
        if (!r) {
          html += `<tr class="hint"><td>${labelFor(i)}</td><td colspan="${drillCols.length}">—</td></tr>`;
          continue;
        }
        html += `<tr><td>${labelFor(i)}</td>` +
          drillCols.map((c) => {
            if (c.k === "_sep_") return `<td class="sep">│</td>`;
            const v = r[c.k];
            const cls = c.cls ? c.cls(v) : "";
            return `<td class="${cls}">${fmtCell(v, c.k)}</td>`;
          }).join("") +
        `</tr>`;
      }
      html += `</tbody></table></div></div>`;
      wrap.innerHTML = html;
      document.getElementById("btn-hourly-close").addEventListener("click", () => {
        wrap.style.display = "none"; wrap.innerHTML = "";
      });
      document.getElementById("btn-drill-toggle").addEventListener("click", () => {
        loadHourly(symbol, otherGroup);
      });
    } catch (e) {
      wrap.innerHTML = `<div class="card err">Ошибка: ${e.message}</div>`;
    }
  }

  // NOTE: per-strategy signal filter UI убран до накопления истории.
  // Backend /api/strategies/{name}/filter/* остаётся — вернём UI позже.

  // period buttons
  document.querySelectorAll(".period-btn").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll(".period-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      loadAnalytics();
    });
  });
  document.getElementById("btn-an-apply")?.addEventListener("click", loadAnalytics);
  document.getElementById("btn-an-csv")?.addEventListener("click", () => {
    const { params } = getAnFilters();
    // download via plain anchor (CSV is a non-JSON response, нужен auth header
    // — но FastAPI игнорирует X-Init-Data в plain GET если auth выкл; в TG
    // окружении токен передаётся, и initData есть в location). Используем
    // initData как query param.
    const qs = params.toString() + (initData ? `&initData=${encodeURIComponent(initData)}` : "");
    const a = document.createElement("a");
    a.href = `/api/analytics/csv?${qs}`;
    a.download = `analytics_${getAnFilters().days}d.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  });

  // ═════════════════════ INITIAL LOAD + AUTO-POLL ═══════════════════════
  loadStatus();
  setInterval(() => {
    const active = document.querySelector(".tab.active")?.dataset.tab;
    if (active === "status") loadStatus();
  }, 5000);
})();
