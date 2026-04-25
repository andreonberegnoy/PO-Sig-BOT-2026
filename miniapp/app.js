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
  document.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      document.getElementById(`tab-${t.dataset.tab}`).classList.add("active");
      if (t.dataset.tab === "settings") loadSettings();
      if (t.dataset.tab === "strategies") loadStrategies();
      if (t.dataset.tab === "status") loadStatus();
    });
  });

  // ─── STATUS ───
  async function loadStatus() {
    try {
      const s = await api("/api/status");
      document.getElementById("m-mode").textContent = s.mode || "—";
      document.getElementById("m-balance").textContent = s.balance != null ? `$${(+s.balance).toFixed(2)}` : "—";
      document.getElementById("m-strategy").textContent = s.active_strategy || "—";
      document.getElementById("m-tracked").textContent = s.tracked_pairs ?? "—";
      document.getElementById("m-active").textContent = s.active_syms ?? "—";
      document.getElementById("m-banned").textContent = s.banned_pairs ?? "—";
      document.getElementById("m-pair").textContent = s.current_pair || "—";
      document.getElementById("m-base").textContent = s.base_amount != null ? `$${(+s.base_amount).toFixed(2)}` : "—";
      document.getElementById("m-expiry").textContent = s.expiry_seconds != null ? `${s.expiry_seconds} сек` : "—";
      document.getElementById("m-mg").textContent = s.mg_step ?? 0;
      document.getElementById("m-loss").textContent = `$${(+(s.session_loss || 0)).toFixed(2)}`;
      document.getElementById("m-paused").textContent = s.paused ? "ДА" : "нет";
    } catch (e) {
      console.error(e);
    }
  }
  document.getElementById("btn-refresh").onclick = loadStatus;
  document.getElementById("btn-pause").onclick = () => api("/api/control/pause", { method: "POST" }).then(loadStatus);
  document.getElementById("btn-resume").onclick = () => api("/api/control/resume", { method: "POST" }).then(loadStatus);

  // ─── SETTINGS ───
  // Global (cross-strategy) settings only. Indicator parameters are now
  // per-strategy and rendered dynamically below.
  const GLOBAL_SCHEMA = {
    "🔍 Фильтр пар": [
      { k: "filter.min_payout", t: "int", min: 50, max: 95, label: "Минимум payout (%)" },
      { k: "filter.payout_floor", t: "int", min: 50, max: 90, label: "Порог смены пары (%)" },
      { k: "filter.max_losses_in_row", t: "int", min: 1, max: 10, label: "Макс. минусов до бана" },
      { k: "filter.history_candles", t: "int", min: 200, max: 2000, label: "Размер истории" },
      { k: "filter.ban_hours", t: "int", min: 1, max: 72, label: "Бан пары (часов)" },
      { k: "filter.day_off_hours", t: "int", min: 1, max: 24, label: "День-офф (часов)" },
    ],
    "💰 Торговля": [
      { k: "trading.base_amount", t: "float", min: 0.5, max: 100, step: 0.5, label: "Базовая ставка ($)" },
      { k: "trading.expiry_seconds", t: "int", min: 30, max: 600, label: "Экспирация (сек)" },
      { k: "trading.max_pair_switch_per_cycle", t: "int", min: 0, max: 5, label: "Смен пары за цикл" },
    ],
    "🎰 Мартингейл": [
      { k: "martingale.coefficient", t: "float", min: 1.5, max: 5, step: 0.1, label: "Множитель" },
      { k: "martingale.max_steps", t: "int", min: 1, max: 20, label: "Макс. шагов" },
      { k: "martingale.stop_sum", t: "float", min: 10, max: 10000, step: 50, label: "Стоп-сумма ($)" },
    ],
    "⏰ Расписание работы": [
      { k: "schedule.enabled", t: "bool", label: "Работать по расписанию (снять = 24/7 круглосуточно)" },
      { k: "schedule.start_hour", t: "int", min: 0, max: 23, label: "Час начала (0-23)" },
      { k: "schedule.end_hour", t: "int", min: 0, max: 24, label: "Час конца (0-24)" },
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

  // initial load
  loadStatus();
  setInterval(() => {
    const active = document.querySelector(".tab.active")?.dataset.tab;
    if (active === "status") loadStatus();
  }, 5000);
})();
