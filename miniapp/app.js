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
  if (tg) { tg.ready(); tg.expand(); }

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
      document.getElementById("m-strategy").textContent = s.active_strategy || "—";
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

  // ═════════════════════ НАСТРОЙКИ БОТА (общие) ══════════════════════════
  // Только общие настройки. Indicator params живут в «Стратегия → Настройки».
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
      // Handlers — single-key updates
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
            flashSetting(el, true);
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

  // ═════════════════════ INITIAL LOAD + AUTO-POLL ═══════════════════════
  loadStatus();
  setInterval(() => {
    const active = document.querySelector(".tab.active")?.dataset.tab;
    if (active === "status") loadStatus();
  }, 5000);
})();
