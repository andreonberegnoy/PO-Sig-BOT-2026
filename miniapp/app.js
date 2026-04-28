/* Mini App logic. Vanilla JS, no build step. */
(() => {
  const tg = window.Telegram?.WebApp;
  const initData = tg?.initData || "";
  if (tg) { tg.ready(); tg.expand(); }

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

  // ─── Tab switching ───
  // Note: /api/status auto-poll every 5s while Status tab is active is set up
  // at the bottom of this file (search for `setInterval`). No need to wire it here.
  document.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      document.getElementById(`tab-${t.dataset.tab}`).classList.add("active");
      if (t.dataset.tab === "settings") loadSettings();
      if (t.dataset.tab === "strategies") loadStrategies();
      if (t.dataset.tab === "status") loadStatus();
      if (t.dataset.tab === "hourly") loadHourly();
      // expiry tab — explicit click of "Запустить анализ", не auto-load
    });
  });

  // ─── STATUS ───
  function showActionMsg(text, kind = "info") {
    const el = document.getElementById("action-msg");
    if (!el) return;
    el.textContent = text;
    el.className = `action-msg ${kind}`;
    if (text) {
      clearTimeout(showActionMsg._t);
      showActionMsg._t = setTimeout(() => { el.textContent = ""; el.className = "action-msg"; }, 6000);
    }
  }

  async function loadStatus() {
    try {
      const s = await api("/api/status");
      document.getElementById("m-mode").textContent = s.mode || "—";
      document.getElementById("m-balance").textContent = s.balance != null ? `$${(+s.balance).toFixed(2)}` : "—";
      document.getElementById("m-strategy").textContent = s.active_strategy || "—";
      document.getElementById("m-tracked").textContent = s.tracked_pairs ?? "—";
      document.getElementById("m-active").textContent = s.active_syms ?? "—";
      document.getElementById("m-banned").textContent = s.banned_pairs ?? "—";
      // In MG cycle but no pair locked → bot is searching all eligible pairs
      const inSearchMode = (s.mg_step ?? 0) > 0 && !s.current_pair;
      document.getElementById("m-pair").textContent = inSearchMode
        ? "🔍 поиск сигнала на всех допустимых"
        : (s.current_pair || "—");
      document.getElementById("m-base").textContent = s.base_amount != null ? `$${(+s.base_amount).toFixed(2)}` : "—";
      document.getElementById("m-expiry").textContent = s.expiry_seconds != null ? `${s.expiry_seconds} сек` : "—";
      document.getElementById("m-mg").textContent = s.mg_step ?? 0;
      document.getElementById("m-loss").textContent = `$${(+(s.session_loss || 0)).toFixed(2)}`;
      document.getElementById("m-paused").textContent = s.paused ? "ДА" : "нет";
      // Cycle-only buttons (switch pair, reset cycle): show whenever MG cycle is
      // active — either locked on a pair OR in search mode (current_pair=None).
      // Reset cycle is useful in both states; switch pair only when locked.
      const inCycle = (s.mg_step ?? 0) > 0;
      const ca = document.getElementById("cycle-actions");
      if (ca) ca.style.display = inCycle ? "" : "none";
      // Hide "switch pair" specifically in search mode (no pair to switch from)
      const switchBtn = document.getElementById("btn-switch-pair");
      if (switchBtn) switchBtn.style.display = inSearchMode ? "none" : "";
    } catch (e) {
      console.error(e);
    }
  }
  document.getElementById("btn-refresh").onclick = loadStatus;
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
    showActionMsg("🔀 Ищу лучшую пару…", "info");
    try {
      const r = await api("/api/control/switch_pair", { method: "POST" });
      if (r && r.ok && r.new) {
        showActionMsg(`🔀 Сменена пара: ${r.old} → ${r.new}`, "ok");
      } else {
        showActionMsg("⚠️ Нет доступных пар для смены. Жду сигнал на текущей.", "warn");
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

  // ─── SETTINGS ───
  // Global (cross-strategy) settings only. Indicator parameters are now
  // per-strategy and rendered dynamically below.
  const GLOBAL_SCHEMA = {
    "🔍 Фильтр пар": [
      { k: "filter.asset_categories", t: "multi",
        options: ["forex", "crypto", "stocks", "indices", "commodities", "other"],
        label: "Категории активов (пусто = все)" },
      { k: "filter.min_payout", t: "int", min: 50, max: 95, label: "Минимум payout (%)" },
      { k: "filter.payout_floor", t: "int", min: 50, max: 90, label: "Порог смены пары (%)" },
      { k: "filter.max_losses_in_row", t: "int", min: 1, max: 10, label: "Макс. минусов до бана" },
      { k: "filter.min_wr1", t: "int", min: 0, max: 100, step: 5, label: "Мин. % 1-й сделки за 1000 свечей" },
      { k: "filter.min_wr1_recent", t: "int", min: 0, max: 100, step: 5, label: "Мин. % 1-й сделки за 200 свечей" },
      { k: "filter.recent_lookback_bars", t: "int", min: 50, max: 500, step: 50, label: "Окно recent (свечей)" },
      { k: "filter.history_candles", t: "int", min: 200, max: 2000, label: "Размер истории" },
      { k: "filter.ban_hours", t: "int", min: 1, max: 72, label: "Бан пары (часов)" },
      { k: "filter.pause_hours", t: "int", min: 1, max: 24, label: "Пауза за низкий recent WR1 (часов)" },
      { k: "filter.day_off_hours", t: "int", min: 1, max: 24, label: "День-офф (часов)" },
    ],
    "💰 Торговля": [
      { k: "trading.base_amount", t: "float", min: 0.5, max: 100, step: 0.5, label: "Базовая ставка ($)" },
      { k: "trading.expiry_seconds", t: "int", min: 30, max: 600, label: "Экспирация (сек)" },
      { k: "trading.limit_trades_per_pair_enabled", t: "bool", label: "Лимит сделок на паре (вкл/выкл)" },
      { k: "trading.max_trades_on_pair", t: "int", min: 1, max: 10, label: "Макс. сделок на паре" },
      { k: "trading.max_pair_switch_per_cycle", t: "int", min: 0, max: 5, label: "Смен пары за цикл" },
    ],
    "🎰 Мартингейл": [
      { k: "martingale.enabled", t: "bool", label: "Включить мартингейл" },
      { k: "martingale.coefficient", t: "float", min: 1.5, max: 5, step: 0.1, label: "Множитель" },
      { k: "martingale.max_steps", t: "int", min: 1, max: 20, label: "Макс. шагов" },
      { k: "martingale.stop_sum", t: "float", min: 10, max: 10000, step: 50, label: "Стоп-сумма ($)" },
    ],
    "⏰ Расписание работы": [
      { k: "schedule.enabled", t: "bool", label: "Работать по расписанию (снять = 24/7 круглосуточно)" },
      { k: "schedule.start_hour", t: "int", min: 0, max: 23, label: "Час начала (0-23)" },
      { k: "schedule.end_hour", t: "int", min: 0, max: 24, label: "Час конца (0-24)" },
    ],
    "📋 Периодический отчёт": [
      { k: "periodic_report.enabled", t: "bool", label: "Присылать сводку (по окончании торгов или утром при 24/7)" },
      { k: "periodic_report.hour_when_24_7", t: "int", min: 0, max: 23, label: "Час отправки в режиме 24/7" },
    ],
  };

  function getDeep(o, path) { return path.split(".").reduce((a, p) => a?.[p], o); }

  function inferType(value) {
    if (typeof value === "boolean") return "bool";
    if (typeof value === "number") return Number.isInteger(value) ? "int" : "float";
    return "string";
  }

  async function loadSettings() {
    const cont = document.getElementById("settings-list");
    cont.innerHTML = "Загрузка…";
    try {
      const [cfg, stratData] = await Promise.all([
        api("/api/settings"),
        api("/api/strategies"),
      ]);
      cont.innerHTML = "";

      // ── Strategy-specific section ──
      const active = stratData.strategies.find((s) => s.active) || stratData.strategies[0];
      if (active) {
        const stratDiv = document.createElement("div");
        stratDiv.className = "category";
        stratDiv.innerHTML = `<div class="category-title">🧠 Параметры стратегии: <b>${active.name}</b></div>`;
        const paramKeys = Object.keys(active.default_params || {});
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
          stratDiv.appendChild(row);
        }
        cont.appendChild(stratDiv);
      }

      // ── Global sections ──
      for (const [cat, items] of Object.entries(GLOBAL_SCHEMA)) {
        const div = document.createElement("div");
        div.className = "category";
        div.innerHTML = `<div class="category-title">${cat}</div>`;
        for (const it of items) {
          const v = getDeep(cfg, it.k);
          const row = document.createElement("div");
          row.className = "setting-row";
          let input;
          if (it.t === "bool") {
            input = `<input type="checkbox" data-k="${it.k}" data-t="bool" ${v ? "checked" : ""}/>`;
          } else if (it.t === "choice") {
            input = `<select data-k="${it.k}" data-t="choice">` +
              it.options.map((o) => `<option value="${o}" ${o === v ? "selected" : ""}>${o}</option>`).join("") +
              `</select>`;
          } else if (it.t === "multi") {
            // Multi-select via checkboxes. Value is an array.
            const arr = Array.isArray(v) ? v : [];
            const opts = it.options.map((o) =>
              `<label class="multi-opt"><input type="checkbox" data-multi="${it.k}" data-opt="${o}" ${arr.includes(o) ? "checked" : ""}/> ${o}</label>`
            ).join("");
            input = `<div class="multi-row">${opts}</div>`;
          } else {
            const step = it.step || (it.t === "int" ? 1 : 0.1);
            input = `<input type="number" data-k="${it.k}" data-t="${it.t}"
                            min="${it.min ?? ""}" max="${it.max ?? ""}" step="${step}" value="${v ?? ""}"/>`;
          }
          row.innerHTML = `<label>${it.label}<span class="hint">${it.k}</span></label>${input}`;
          div.appendChild(row);
        }
        cont.appendChild(div);
      }

      // ── handlers ──
      const flash = (el, ok) => {
        el.style.outline = `2px solid ${ok ? "#22c55e" : "#ef4444"}`;
        setTimeout(() => (el.style.outline = ""), 600);
      };

      cont.querySelectorAll("[data-k]").forEach((el) => {
        el.addEventListener("change", async () => {
          const key = el.dataset.k;
          const t = el.dataset.t;
          let value;
          if (t === "bool") value = el.checked;
          else if (t === "int") value = parseInt(el.value);
          else if (t === "float") value = parseFloat(el.value);
          else value = el.value;
          try {
            await api("/api/settings", { method: "PUT", body: JSON.stringify({ [key]: value }) });
            flash(el, true);
          } catch (e) { flash(el, false); console.error(e); }
        });
      });
      // Multi-select handlers (asset_categories etc.)
      cont.querySelectorAll("[data-multi]").forEach((el) => {
        el.addEventListener("change", async () => {
          const key = el.dataset.multi;
          // Collect all checked options for this key
          const opts = [];
          cont.querySelectorAll(`[data-multi="${key}"]:checked`).forEach((c) =>
            opts.push(c.dataset.opt)
          );
          try {
            await api("/api/settings", { method: "PUT", body: JSON.stringify({ [key]: opts }) });
            flash(el, true);
          } catch (e) { flash(el, false); console.error(e); }
        });
      });
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
            flash(el, true);
          } catch (e) { flash(el, false); console.error(e); }
        });
      });
    } catch (e) {
      cont.innerHTML = `<div class="status-line err">Ошибка: ${e.message}</div>`;
    }
  }

  // ─── STRATEGIES ───
  async function loadStrategies() {
    const list = document.getElementById("strategies-list");
    list.innerHTML = "<li>Загрузка…</li>";
    try {
      const data = await api("/api/strategies");
      list.innerHTML = "";
      for (const s of data.strategies) {
        const li = document.createElement("li");
        li.classList.toggle("strategy-active", s.active);
        const activeBadge = s.active
          ? `<span class="badge active">✓ Активна</span>`
          : `<span class="badge source-${s.source}">${s.source === "user" ? "своя" : "встроенная"}</span>`;
        // For active strategies — show "Switch to consensus" (so user can disable
        // a custom strategy and fall back to builtin). For inactive — "Активировать".
        let actionBtn = "";
        if (!s.active) {
          actionBtn = `<button class="btn primary" data-act="activate" data-name="${s.name}">▶ Активировать</button>`;
        } else if (s.name !== "consensus") {
          actionBtn = `<button class="btn" data-act="activate" data-name="consensus">⏹ Выключить (на consensus)</button>`;
        }
        const delBtn = (s.source === "user" && !s.active)
          ? `<button class="btn" data-act="delete" data-name="${s.name}">🗑 Удалить</button>`
          : "";
        li.innerHTML = `
          <div class="strategy-row">
            <div class="strategy-name">${s.name} ${activeBadge}</div>
            <div class="strategy-actions">${actionBtn}${delBtn}</div>
          </div>
        `;
        list.appendChild(li);
      }
      list.querySelectorAll("[data-act]").forEach((b) => {
        b.addEventListener("click", async () => {
          const name = b.dataset.name;
          const act = b.dataset.act;
          if (act === "activate") {
            await api(`/api/strategies/${encodeURIComponent(name)}/activate`, { method: "POST" });
          } else if (act === "delete") {
            if (!confirm(`Удалить стратегию "${name}"?`)) return;
            await api(`/api/strategies/${encodeURIComponent(name)}`, { method: "DELETE" });
          }
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

  // ─── Analytics tab ───
  let analyticsState = { range: "7d", sortKey: "score", sortDir: "desc", data: [], workHoursOnly: false };

  function fmt(v, digits = 0, suffix = "") {
    if (v === undefined || v === null || Number.isNaN(v)) return "—";
    return (typeof v === "number" ? v.toFixed(digits) : v) + suffix;
  }
  function pctClass(v, good = 60, bad = 45) {
    if (v === undefined || v === null) return "";
    if (v >= good) return "cell-good";
    if (v < bad) return "cell-bad";
    return "cell-warn";
  }
  function streakClass(v) {
    if (v === undefined || v === null) return "";
    if (v <= 2) return "cell-good";
    if (v >= 4) return "cell-bad";
    return "cell-warn";
  }

  function renderAnalyticsTable() {
    const wrap = document.getElementById("analytics-table");
    if (!analyticsState.data || analyticsState.data.length === 0) {
      wrap.innerHTML = '<div class="status-line">Данных пока нет — снапшоты пишутся каждые 5 мин (выплаты) и каждый час (бэктест). Подожди и обнови.</div>';
      return;
    }
    const cols = [
      { key: "symbol",         label: "Пара" },
      { key: "current_payout", label: "Выплата сейчас" },
      { key: "avg_payout",     label: "Средняя выплата" },
      { key: "pct_above_min",  label: "% времени ≥ min" },
      { key: "pct_above_floor",label: "% времени ≥ floor" },
      { key: "last_wr1",       label: "1-я WR" },
      { key: "avg_wr1",        label: "1-я WR (средн.)" },
      { key: "last_wr",        label: "Общая WR" },
      { key: "last_max_streak",label: "Max минусов" },
      { key: "max_max_streak", label: "Max мин подряд" },
      { key: "avg_signals",    label: "Кол сигнал" },
      { key: "n_snapshots",    label: "Снапшотов" },
    ];
    // Sort
    const dir = analyticsState.sortDir === "asc" ? 1 : -1;
    const key = analyticsState.sortKey;
    const score = (r) => (r.last_wr1 || 0) * 0.5 + (r.pct_above_min || 0) * 0.3 - (r.last_max_streak || 0) * 5;
    const sorted = [...analyticsState.data].sort((a, b) => {
      let av = key === "score" ? score(a) : a[key];
      let bv = key === "score" ? score(b) : b[key];
      if (typeof av === "string") return av.localeCompare(bv) * dir;
      av = av ?? -Infinity; bv = bv ?? -Infinity;
      return (av - bv) * dir;
    });
    const head = cols.map(c => {
      const cls = c.key === key ? `sort-${analyticsState.sortDir}` : "";
      return `<th class="${cls}" data-sort="${c.key}">${c.label}</th>`;
    }).join("");
    const rows = sorted.map(r => {
      const wr1 = r.last_wr1, streak = r.last_max_streak;
      return `<tr>
        <td><b>${r.symbol}</b></td>
        <td>${fmt(r.current_payout, 0, "%")}</td>
        <td>${fmt(r.avg_payout, 1, "%")}</td>
        <td>${fmt(r.pct_above_min, 1, "%")}</td>
        <td>${fmt(r.pct_above_floor, 1, "%")}</td>
        <td class="${pctClass(wr1)}">${fmt(wr1, 0, "%")}</td>
        <td class="${pctClass(r.avg_wr1)}">${fmt(r.avg_wr1, 0, "%")}</td>
        <td class="${pctClass(r.last_wr)}">${fmt(r.last_wr, 0, "%")}</td>
        <td class="${streakClass(streak)}">${fmt(streak)}</td>
        <td class="${streakClass(r.max_max_streak)}">${fmt(r.max_max_streak)}</td>
        <td>${fmt(r.avg_signals, 0)}</td>
        <td>${fmt(r.n_snapshots, 0)}</td>
      </tr>`;
    }).join("");
    wrap.innerHTML = `
      <div class="analytics-wrap">
        <table class="analytics">
          <thead><tr>${head}</tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
    wrap.querySelectorAll("th[data-sort]").forEach((th) => {
      th.addEventListener("click", () => {
        const k = th.dataset.sort;
        if (analyticsState.sortKey === k) {
          analyticsState.sortDir = analyticsState.sortDir === "asc" ? "desc" : "asc";
        } else {
          analyticsState.sortKey = k;
          analyticsState.sortDir = "desc";
        }
        renderAnalyticsTable();
      });
    });
  }

  async function loadAnalytics() {
    const wrap = document.getElementById("analytics-table");
    wrap.innerHTML = '<div class="status-line">Загрузка…</div>';
    try {
      const wh = analyticsState.workHoursOnly ? 1 : 0;
      const r = await api(`/api/pair_stats?range=${encodeURIComponent(analyticsState.range)}&work_hours_only=${wh}`);
      analyticsState.data = r.pairs || [];
      renderAnalyticsTable();
    } catch (e) {
      wrap.innerHTML = `<div class="status-line err">Ошибка: ${e.message}</div>`;
    }
  }
  document.querySelectorAll(".range-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".range-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      analyticsState.range = btn.dataset.range;
      loadAnalytics();
    });
  });
  const workHoursToggle = document.getElementById("analytics-workhours");
  if (workHoursToggle) {
    workHoursToggle.addEventListener("change", () => {
      analyticsState.workHoursOnly = workHoursToggle.checked;
      loadAnalytics();
    });
  }
  document.querySelector('.tab[data-tab="analytics"]').addEventListener("click", loadAnalytics);

  // ─── HOURLY analytics ───
  const hourlyState = { range: "7d", sortKey: "total", sortAsc: false };

  async function loadHourly() {
    const sumEl = document.getElementById("hourly-summary");
    const pairsEl = document.getElementById("hourly-pairs");
    if (!sumEl || !pairsEl) return;
    sumEl.innerHTML = '<tr><td>Загрузка…</td></tr>';
    pairsEl.innerHTML = '';
    try {
      const r = await api(`/api/hourly_stats?range=${encodeURIComponent(hourlyState.range)}`);
      _lastHourlyData = r;   // cache for CSV export / apply-filter actions
      const summary = r.summary_by_hour || [];
      const buckets = r.buckets || [];

      // ─── Summary table (24 rows, one per hour) ─────────────────────────
      const sumHeaders = `
        <thead><tr>
          <th>Час</th><th>Сделок</th><th>WIN</th><th>LOSS</th>
          <th>WR</th><th>Avg payout WIN</th><th>Profit</th>
        </tr></thead>`;
      const sumBody = summary.map(s => {
        if (s.total === 0) return '';   // skip empty hours
        const wrCls = s.wr >= 70 ? 'cell-good' : s.wr >= 55 ? '' : 'cell-bad';
        const profCls = s.profit >= 0 ? 'cell-good' : 'cell-bad';
        const payCls = s.avg_win_payout == null ? '' :
          s.avg_win_payout >= 90 ? 'cell-good' :
          s.avg_win_payout >= 80 ? '' : 'cell-bad';
        const payTxt = s.avg_win_payout == null ? '—' :
          `${s.avg_win_payout.toFixed(1)}%` +
          (s.min_win_payout != null && s.min_win_payout !== s.max_win_payout
            ? ` <span class="hint" style="font-size:10px;">(${s.min_win_payout}-${s.max_win_payout})</span>` : '');
        return `<tr>
          <td><b>${String(s.hour).padStart(2,'0')}:00</b></td>
          <td>${s.total}</td>
          <td class="cell-good">${s.wins}</td>
          <td class="cell-bad">${s.losses}</td>
          <td class="${wrCls}">${s.wr.toFixed(1)}%</td>
          <td class="${payCls}">${payTxt}</td>
          <td class="${profCls}">$${s.profit.toFixed(2)}</td>
        </tr>`;
      }).join('');
      sumEl.innerHTML = sumHeaders + `<tbody>${sumBody || '<tr><td colspan="7">Нет сделок за выбранный период</td></tr>'}</tbody>`;

      // ─── Pairs × hours table ──────────────────────────────────────────
      // Sort by current sortKey
      const sortedBuckets = [...buckets].sort((a, b) => {
        const k = hourlyState.sortKey;
        const av = a[k], bv = b[k];
        if (typeof av === 'string') return hourlyState.sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
        return hourlyState.sortAsc ? av - bv : bv - av;
      });
      const arrow = (key) => key === hourlyState.sortKey
        ? (hourlyState.sortAsc ? ' ▲' : ' ▼') : '';
      const pairsHeaders = `
        <thead><tr>
          <th data-sort="symbol">Пара${arrow('symbol')}</th>
          <th data-sort="hour">Час${arrow('hour')}</th>
          <th data-sort="total">Сделок${arrow('total')}</th>
          <th data-sort="wins">WIN${arrow('wins')}</th>
          <th data-sort="losses">LOSS${arrow('losses')}</th>
          <th data-sort="wr">WR${arrow('wr')}</th>
          <th data-sort="avg_win_payout">Avg payout WIN${arrow('avg_win_payout')}</th>
          <th data-sort="avg_min_win_exp" title="Среднее: какая минимальная экспирация (1-5 баров) закрыла бы каждую сделку в плюс. Накапливается по реальным сделкам.">Min ✓ exp${arrow('avg_min_win_exp')}</th>
          <th data-sort="profit">Profit${arrow('profit')}</th>
        </tr></thead>`;
      const pairsBody = sortedBuckets.map(p => {
        const star = (p.wr >= 70 && p.total >= 5) ? ' ⭐' : '';
        const wrCls = p.wr >= 70 ? 'cell-good' : p.wr >= 55 ? '' : 'cell-bad';
        const profCls = p.profit >= 0 ? 'cell-good' : 'cell-bad';
        const payCls = p.avg_win_payout == null ? '' :
          p.avg_win_payout >= 90 ? 'cell-good' :
          p.avg_win_payout >= 80 ? '' : 'cell-bad';
        const payTxt = p.avg_win_payout == null ? '—' :
          `${p.avg_win_payout.toFixed(1)}%` +
          (p.min_win_payout != null && p.min_win_payout !== p.max_win_payout
            ? ` <span class="hint" style="font-size:10px;">(${p.min_win_payout}-${p.max_win_payout})</span>` : '');
        const expTxt = (p.avg_min_win_exp == null) ? '—' :
          `${p.avg_min_win_exp.toFixed(1)}` +
          (p.exp_data_trades ? ` <span class="hint" style="font-size:10px;">(n=${p.exp_data_trades})</span>` : '');
        const expCls = (p.avg_min_win_exp != null && p.avg_min_win_exp <= 2.5) ? 'cell-good' :
                       (p.avg_min_win_exp != null && p.avg_min_win_exp >= 4) ? 'cell-bad' : '';
        return `<tr>
          <td>${p.symbol}${star}</td>
          <td>${String(p.hour).padStart(2,'0')}:00</td>
          <td>${p.total}</td>
          <td class="cell-good">${p.wins}</td>
          <td class="cell-bad">${p.losses}</td>
          <td class="${wrCls}">${p.wr.toFixed(1)}%</td>
          <td class="${payCls}">${payTxt}</td>
          <td class="${expCls}">${expTxt}</td>
          <td class="${profCls}">$${p.profit.toFixed(2)}</td>
        </tr>`;
      }).join('');
      pairsEl.innerHTML = pairsHeaders + `<tbody>${pairsBody || '<tr><td colspan="9">Нет данных</td></tr>'}</tbody>`;

      // Hook column-header sort
      pairsEl.querySelectorAll('th[data-sort]').forEach(th => {
        th.addEventListener('click', () => {
          const key = th.dataset.sort;
          if (hourlyState.sortKey === key) hourlyState.sortAsc = !hourlyState.sortAsc;
          else { hourlyState.sortKey = key; hourlyState.sortAsc = false; }
          loadHourly();
        });
      });
    } catch (e) {
      sumEl.innerHTML = `<tr><td>Ошибка: ${e.message || e}</td></tr>`;
      pairsEl.innerHTML = '';
    }
    // Update active-filter status indicator (separate endpoint)
    try { await refreshHourlyFilterStatus(); } catch (e) { /* non-fatal */ }
  }

  // Period buttons for hourly tab
  document.querySelectorAll(".hour-range-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".hour-range-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      hourlyState.range = btn.dataset.range;
      loadHourly();
    });
  });

  // Cache last-loaded hourly data for CSV export and apply-filter actions
  let _lastHourlyData = null;

  async function refreshHourlyFilterStatus() {
    try {
      const r = await api("/api/hour_whitelist");
      const status = document.getElementById("hourly-filter-status");
      const clearBtn = document.getElementById("btn-hourly-clear");
      if (r.count > 0) {
        const pairCount = Object.keys(r.whitelist).length;
        status.className = "action-msg ok";
        status.textContent = `⭐ Активный фильтр: ${pairCount} пар × ${r.count} комбинаций пара/час. Бот торгует только в этих окнах.`;
        if (clearBtn) clearBtn.style.display = "";
      } else {
        status.className = "action-msg";
        status.textContent = "";
        if (clearBtn) clearBtn.style.display = "none";
      }
    } catch (e) { /* ignore — endpoint may not exist on older server */ }
  }

  // Helper: trigger CSV download
  function downloadCSV(filename, headers, rows) {
    const escape = (v) => {
      if (v == null) return "";
      const s = String(v);
      return s.includes(",") || s.includes('"') || s.includes("\n")
        ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const csv = [headers.join(",")]
      .concat(rows.map(r => r.map(escape).join(",")))
      .join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // ─── Export CSV ───
  const exportBtn = document.getElementById("btn-hourly-export");
  if (exportBtn) exportBtn.addEventListener("click", () => {
    if (!_lastHourlyData) {
      alert("Загрузи данные сначала (клик на 24ч/7д/30д/всё)");
      return;
    }
    const ts = new Date().toISOString().slice(0, 10);
    const range = hourlyState.range;
    // 1. Summary CSV
    downloadCSV(
      `hourly_summary_${range}_${ts}.csv`,
      ["hour", "total", "wins", "losses", "draws", "wr",
       "avg_win_payout", "min_win_payout", "max_win_payout", "profit"],
      (_lastHourlyData.summary_by_hour || []).filter(s => s.total > 0).map(s =>
        [s.hour, s.total, s.wins, s.losses, s.draws, s.wr,
         s.avg_win_payout ?? "", s.min_win_payout ?? "", s.max_win_payout ?? "",
         s.profit]
      )
    );
    // 2. Pairs × hours CSV
    downloadCSV(
      `hourly_pairs_${range}_${ts}.csv`,
      ["symbol", "hour", "total", "wins", "losses", "draws", "wr",
       "avg_win_payout", "min_win_payout", "max_win_payout", "profit"],
      (_lastHourlyData.buckets || []).map(p =>
        [p.symbol, p.hour, p.total, p.wins, p.losses, p.draws, p.wr,
         p.avg_win_payout ?? "", p.min_win_payout ?? "", p.max_win_payout ?? "",
         p.profit]
      )
    );
  });

  // ─── Apply hour filter ───
  const applyBtn = document.getElementById("btn-hourly-apply");
  if (applyBtn) applyBtn.addEventListener("click", async () => {
    const minWrStr = prompt("Минимальный WR % (по умолчанию 70):", "70");
    if (minWrStr === null) return;
    const minTradesStr = prompt("Минимум сделок в окне (по умолчанию 5):", "5");
    if (minTradesStr === null) return;
    const minWr = parseFloat(minWrStr) || 70;
    const minTrades = parseInt(minTradesStr, 10) || 5;
    const range = hourlyState.range;
    if (!confirm(
      `Применить фильтр?\n\n` +
      `Будут торговаться ТОЛЬКО (пара × час) с WR ≥ ${minWr}% и сделок ≥ ${minTrades} ` +
      `за период "${range}".\n\n` +
      `Применяется сразу. Можешь снять кнопкой "🔓 Снять фильтр".`
    )) return;
    try {
      const r = await api("/api/apply_hour_whitelist", {
        method: "POST",
        body: JSON.stringify({ min_wr: minWr, min_trades: minTrades, range }),
      });
      alert(`✅ Применено. ${r.count} комбинаций пара/час в фильтре. Бот сразу применяет фильтр.`);
      refreshHourlyFilterStatus();
    } catch (e) {
      alert(`❌ Ошибка: ${e.message || e}`);
    }
  });

  // ─── Clear hour filter ───
  const clearBtn = document.getElementById("btn-hourly-clear");
  if (clearBtn) clearBtn.addEventListener("click", async () => {
    if (!confirm("Снять фильтр по часам? Бот вернётся к торговле по всем подходящим парам без ограничения по времени.")) return;
    try {
      await api("/api/clear_hour_whitelist", { method: "POST" });
      refreshHourlyFilterStatus();
    } catch (e) {
      alert(`❌ Ошибка: ${e.message || e}`);
    }
  });

  // ─── Reset stats baseline (small button, double-confirm) ───
  const resetBtn = document.getElementById("btn-hourly-reset");
  if (resetBtn) resetBtn.addEventListener("click", async () => {
    if (!confirm(
      "⤺ Сбросить статистику по часам?\n\n" +
      "Подсчёт начнётся с НУЛЯ с этого момента.\n" +
      "Старые сделки в БД сохраняются — это просто метка времени для аналитики.\n\n" +
      "Используй когда меняешь стратегию и хочешь чистый старт."
    )) return;
    if (!confirm("Точно? Это последнее подтверждение.")) return;
    try {
      const r = await api("/api/reset_hourly_stats", { method: "POST" });
      const date = new Date(r.baseline_ts * 1000).toLocaleString("ru-RU");
      alert(`✅ Статистика сброшена. Подсчёт пойдёт с ${date}.`);
      loadHourly();
    } catch (e) {
      alert(`❌ Ошибка: ${e.message || e}`);
    }
  });

  // ─── EXPIRY analysis ───
  let _lastExpiryData = null;
  let _expiryScope = "tracked";

  // Scope selector buttons
  document.querySelectorAll(".exp-scope-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".exp-scope-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      _expiryScope = btn.dataset.scope || "tracked";
    });
  });

  async function loadExpiry() {
    const tbl = document.getElementById("expiry-table");
    const info = document.getElementById("expiry-info");
    const exportBtn = document.getElementById("btn-expiry-export");
    const loadBtn = document.getElementById("btn-expiry-load");
    if (!tbl) return;

    info.className = "action-msg info";
    info.textContent = _expiryScope === "all"
      ? "⏳ Анализирую ВСЕ OTC пары (это может занять 10-60 сек)..."
      : "⏳ Анализирую tracked пары...";
    tbl.innerHTML = "";
    if (loadBtn) loadBtn.disabled = true;

    try {
      const r = await api(`/api/expiry_stats?scope=${encodeURIComponent(_expiryScope)}`);
      _lastExpiryData = r;
      const pairs = r.pairs || [];
      const expiries = r.expiries || [1, 2, 3, 4, 5];
      const overall = r.overall || {};
      const overallBest = r.overall_best;

      if (pairs.length === 0) {
        info.className = "action-msg warn";
        info.textContent = r.note || "Нет данных. Бот ещё не загрузил свечи.";
        return;
      }

      info.className = "action-msg ok";
      info.textContent = `✅ ${r.note}`;
      if (exportBtn) exportBtn.style.display = "";

      // Header: Pair | Payout | Свечей/Сигналов | for each expiry: WR (sigs) | best
      let header = `<thead><tr>
        <th>Пара</th>
        <th>Payout</th>
        <th>Свечей / Сигналов<br/><span class="hint" style="font-size:10px;">в окне 1000</span></th>`;
      for (const exp of expiries) {
        header += `<th>exp=${exp}<br/><span class="hint" style="font-size:10px;">WR / W·L</span></th>`;
      }
      header += `<th>⭐ Лучшая</th></tr></thead>`;

      // Summary row — average WR per expiry across all pairs
      let summaryRow = `<tr style="background:rgba(255,200,0,0.07); font-weight:600;">
        <td colspan="2">📊 Среднее по всем парам</td>
        <td class="hint">—</td>`;
      for (const exp of expiries) {
        const a = overall[exp];
        if (!a || a.pairs_with_data === 0) {
          summaryRow += `<td class="hint">—</td>`;
        } else {
          const cls = a.avg_wr >= 70 ? 'cell-good' : a.avg_wr >= 55 ? '' : 'cell-bad';
          const star = (exp === overallBest) ? ' ⭐' : '';
          summaryRow += `<td class="${cls}">${a.avg_wr.toFixed(1)}%${star}<br/>
            <span class="hint" style="font-size:10px;">${a.pairs_with_data} пар · ${a.total_signals} сигн.</span></td>`;
        }
      }
      if (overallBest !== null && overallBest !== undefined) {
        const bestAvg = overall[overallBest]?.avg_wr ?? 0;
        const cls = bestAvg >= 70 ? 'cell-good' : bestAvg >= 55 ? '' : '';
        summaryRow += `<td class="${cls}"><b>exp=${overallBest}</b><br/>${bestAvg.toFixed(1)}%</td>`;
      } else {
        summaryRow += `<td class="hint">мало<br/>данных</td>`;
      }
      summaryRow += `</tr>`;

      const body = pairs.map(p => {
        let row = `<tr>
          <td><b>${p.symbol}</b></td>
          <td>${p.payout}%</td>
          <td>${p.candles_used}<br/>
              <span class="hint" style="font-size:10px;">${p.completed_1000} сделок</span></td>`;
        for (const exp of expiries) {
          const d = p.expiries[exp];
          if (!d || d.signals === 0) {
            row += `<td class="hint">—</td>`;
          } else {
            const isBest = (exp === p.best_expiry);
            const wrCls = d.wr >= 70 ? 'cell-good' : d.wr >= 55 ? '' : 'cell-bad';
            const star = isBest ? ' ⭐' : '';
            const dim = (d.signals < (r.min_signals_for_score || 5)) ? 'opacity:0.5;' : '';
            row += `<td class="${wrCls}" style="${dim}">${d.wr.toFixed(1)}%${star}<br/>
                    <span class="hint" style="font-size:10px;">✓${d.wins} ✗${d.losses}</span></td>`;
          }
        }
        if (p.best_expiry !== null) {
          const bestCls = p.best_wr >= 70 ? 'cell-good' : p.best_wr >= 55 ? '' : '';
          row += `<td class="${bestCls}"><b>exp=${p.best_expiry}</b><br/>${p.best_wr.toFixed(1)}%</td>`;
        } else {
          row += `<td class="hint">мало<br/>сигналов</td>`;
        }
        row += "</tr>";
        return row;
      }).join("");

      tbl.innerHTML = header + `<tbody>${summaryRow}${body}</tbody>`;
    } catch (e) {
      info.className = "action-msg err";
      info.textContent = `❌ Ошибка: ${e.message || e}`;
    } finally {
      if (loadBtn) loadBtn.disabled = false;
    }
  }

  // Кнопки
  const expLoadBtn = document.getElementById("btn-expiry-load");
  if (expLoadBtn) expLoadBtn.addEventListener("click", loadExpiry);

  const expExportBtn = document.getElementById("btn-expiry-export");
  if (expExportBtn) expExportBtn.addEventListener("click", () => {
    if (!_lastExpiryData) {
      alert("Запусти анализ сначала");
      return;
    }
    const expiries = _lastExpiryData.expiries;
    const headers = ["symbol", "payout", "candles_used", "completed_1000"];
    for (const e of expiries) {
      headers.push(`exp${e}_signals`, `exp${e}_wins`, `exp${e}_losses`,
                   `exp${e}_wr`, `exp${e}_wr1`);
    }
    headers.push("best_expiry", "best_wr");

    const rows = (_lastExpiryData.pairs || []).map(p => {
      const r = [p.symbol, p.payout, p.candles_used, p.completed_1000];
      for (const e of expiries) {
        const d = p.expiries[e] || {};
        r.push(d.signals ?? "", d.wins ?? "", d.losses ?? "",
               d.wr ?? "", d.wr1 ?? "");
      }
      r.push(p.best_expiry ?? "", p.best_wr ?? "");
      return r;
    });

    // Append summary row
    const overall = _lastExpiryData.overall || {};
    const sumRow = ["__OVERALL__", "", "", ""];
    for (const e of expiries) {
      const a = overall[e] || {};
      sumRow.push(a.total_signals ?? "", a.total_wins ?? "", a.total_losses ?? "",
                  a.avg_wr ?? "", "");
    }
    sumRow.push(_lastExpiryData.overall_best ?? "", "");
    rows.push(sumRow);

    const ts = new Date().toISOString().slice(0, 10);
    downloadCSV(`expiry_analysis_${_lastExpiryData.scope || "tracked"}_${ts}.csv`, headers, rows);
  });

  // initial load
  loadStatus();
  setInterval(() => {
    const active = document.querySelector(".tab.active")?.dataset.tab;
    if (active === "status") loadStatus();
  }, 5000);
})();
