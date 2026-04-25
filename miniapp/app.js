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
      document.getElementById("m-pair").textContent = s.current_pair || "—";
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
  // Schema mirrors tg/settings_ui.py (must stay in sync)
  const SCHEMA = {
    "📊 Индикатор: правила": [
      { k: "indicator.minConsensus", t: "int", min: 3, max: 5, label: "Минимум голосов (4 или 5)" },
      { k: "indicator.requireAll5OnWeekend", t: "bool", label: "На выходных требовать 5/5" },
      { k: "indicator.cooldownBars", t: "int", min: 0, max: 30, label: "Cooldown между сигналами (бары)" },
      { k: "indicator.expiryBars", t: "int", min: 1, max: 10, label: "Экспирация в барах (бэктест)" },
      { k: "indicator.entryMode", t: "choice", options: ["nextBarOpen", "signalBarClose"], label: "Режим входа" },
    ],
    "📈 Индикатор: RSI-QQE": [
      { k: "indicator.rsiPeriod", t: "int", min: 2, max: 50, label: "RSI period" },
      { k: "indicator.rsiSmoothing", t: "int", min: 1, max: 30, label: "RSI smoothing" },
      { k: "indicator.qqeFactor", t: "float", min: 1, max: 10, step: 0.1, label: "QQE factor" },
    ],
    "🌐 Индикатор: HTF": [
      { k: "indicator.htfMultiplier", t: "int", min: 2, max: 60, label: "HTF multiplier (M1×N)" },
      { k: "indicator.htfMaPeriod", t: "int", min: 5, max: 200, label: "HTF MA period" },
      { k: "indicator.htfMaType", t: "choice", options: ["EMA", "SMA", "WMA", "RMA"], label: "HTF MA type" },
    ],
    "📉 Индикатор: ATR": [
      { k: "indicator.atrPeriod", t: "int", min: 5, max: 50, label: "ATR period" },
      { k: "indicator.atrAvgWindow", t: "int", min: 20, max: 500, label: "ATR avg window" },
      { k: "indicator.atrMinRatio", t: "float", min: 0.1, max: 5, step: 0.05, label: "ATR min ratio" },
      { k: "indicator.atrMaxRatio", t: "float", min: 0.5, max: 10, step: 0.1, label: "ATR max ratio" },
    ],
    "📊 Индикатор: Bollinger": [
      { k: "indicator.bbPeriod", t: "int", min: 5, max: 100, label: "BB period" },
      { k: "indicator.bbStdDev", t: "float", min: 0.5, max: 5, step: 0.1, label: "BB std dev" },
      { k: "indicator.bbZoneDepth", t: "float", min: 0.05, max: 0.95, step: 0.05, label: "BB zone depth (0-1)" },
    ],
    "🕯 Индикатор: свеча": [
      { k: "indicator.candleMaxAtrMult", t: "float", min: 0.5, max: 10, step: 0.1, label: "Max body / ATR" },
      { k: "indicator.candleReqAlign", t: "bool", label: "Свеча в направлении сигнала" },
    ],
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
  };

  function getDeep(o, path) { return path.split(".").reduce((a, p) => a?.[p], o); }

  async function loadSettings() {
    const cont = document.getElementById("settings-list");
    cont.innerHTML = "Загрузка…";
    try {
      const cfg = await api("/api/settings");
      cont.innerHTML = "";
      for (const [cat, items] of Object.entries(SCHEMA)) {
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
      // attach onchange
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
            el.style.outline = "2px solid #22c55e";
            setTimeout(() => (el.style.outline = ""), 600);
          } catch (e) {
            el.style.outline = "2px solid #ef4444";
            console.error(e);
          }
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
        li.innerHTML = `
          <span class="name">${s.name} <span class="badge ${s.active ? "active" : ""}">${s.active ? "АКТИВНА" : s.source}</span></span>
          ${s.active ? "" : `<button class="btn" data-act="activate" data-name="${s.name}">Использовать</button>`}
          ${s.source === "user" ? `<button class="btn" data-act="delete" data-name="${s.name}">Удалить</button>` : ""}
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
            if (!confirm(`Удалить ${name}?`)) return;
            await api(`/api/strategies/${encodeURIComponent(name)}`, { method: "DELETE" });
          }
          loadStrategies();
        });
      });
    } catch (e) {
      list.innerHTML = `<li class="err">${e.message}</li>`;
    }
  }

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

  // initial load
  loadStatus();
  setInterval(() => {
    const active = document.querySelector(".tab.active")?.dataset.tab;
    if (active === "status") loadStatus();
  }, 5000);
})();
