"use strict";
(() => {
  let guilds = [], guild = "", config, generation = 0, changed = false, loading = false;
  const form = document.getElementById("chat-form");
  const advanced = [
    ["model", "DeepSeek model", "text"], ["debounce_seconds", "Combine messages for (seconds)", "number"],
    ["max_wait_seconds", "Maximum message-combining wait (seconds)", "number"], ["cooldown_seconds", "Base reply interval (Active adapts up to 8 seconds, longer in busy channels)", "number"],
    ["hourly_limit", "Spontaneous replies per hour", "number"], ["daily_limit", "Extra reply-count limit per 24 hours (0: budget only)", "number"],
    ["daily_model_calls", "Extra model-call limit per day (0: budget only)", "number"], ["context_messages", "Recent messages to consider", "number"],
    ["context_chars", "Maximum context characters", "number"], ["max_reply_chars", "Maximum reply characters", "number"],
    ["reply_max_tokens", "Normal reply token limit (all message parts combined)", "number"],
    ["decision_persona", "Brief persona for reply decisions (optional)", "text"],
    ["quiet_hours", "Quiet hours (HH:MM-HH:MM)", "text"], ["timezone", "Time zone", "text"],
    ["excluded_users", "Excluded member IDs (comma separated)", "text"],
    ["web_max_calls", "Web requests per reply", "number"], ["web_daily_calls", "Extra web-call limit per day (0: budget only)", "number"]
  ];
  for (const [name, title, type] of advanced) {
    const label = document.createElement("label"); label.textContent = title;
    const input = document.createElement("input"); input.name = name; input.type = type;
    if (type === "number") {input.min = "0"; input.step = "1"; input.required = true;}
    label.append(input); document.getElementById("chat-advanced").append(label);
  }
  function disable(value) {for (const element of form.elements) element.disabled = value;}
  const query = () => "?guild_id=" + encodeURIComponent(guild);
  function report(error) {document.getElementById("chat-save-status").textContent = error.message;}
  async function decisions() {
    if (!guild) return;
    const selectedGuild = guild, result = await api("/chat/decisions" + query());
    if (selectedGuild !== guild) return;
    const root = document.getElementById("chat-decisions"); root.replaceChildren();
    if (!result.decisions.length) {root.textContent = "No decisions yet. Enable Observe mode and allow a test channel to start."; return;}
    for (const row of result.decisions) {
      const item = document.createElement("p"); item.className = "decision-row";
      let outcome = "";
      try {const detail = JSON.parse(row.detail || "{}"); if (row.action === "tool" && detail.status) outcome = " · " + detail.status;} catch {}
      item.textContent = `${new Date(row.created * 1000).toLocaleString()} · ${row.scope.split(":").at(-1)} · ${row.action} · ${row.reason}${outcome}`;
      root.append(item);
    }
  }
  async function load() {
    if (loading) return;
    loading = true; const current = ++generation; disable(true);
    document.getElementById("chat-guild").disabled = true;
    try {
      guilds = await api("/guilds"); if (current !== generation) return;
      if (!guilds.some(item => item.id === guild)) guild = guilds[0]?.id || "";
      const select = document.getElementById("chat-guild");
      select.replaceChildren(...guilds.map(item => new Option(item.name, item.id)));
      select.disabled = !guild; select.value = guild;
      if (!guild) {document.getElementById("chat-health").textContent = "No server connected. Start the bot and invite it to a test server."; return;}
      const values = await api("/settings/chat" + query());
      if (current !== generation) return;
      config = values;
      for (const element of form.elements) {
        if (!element.name || !(element.name in config)) continue;
        if (element.type === "checkbox") element.checked = config[element.name];
        else element.value = Array.isArray(config[element.name]) ? config[element.name].join(", ") : config[element.name];
      }
      const options = document.getElementById("chat-channels"); options.replaceChildren();
      const overrides = document.getElementById("chat-overrides"); overrides.replaceChildren();
      const heading = document.createElement("h3"); heading.textContent = "Channel participation overrides"; overrides.append(heading);
      for (const channel of guilds.find(item => item.id === guild).channels) {
        const label = document.createElement("label"); label.className = "inline-check";
        const input = document.createElement("input"); input.type = "checkbox"; input.dataset.channel = channel.id;
        input.checked = config.channels.includes(channel.id); label.append(input, document.createTextNode(channel.name)); options.append(label);
        const overrideLabel = document.createElement("label"); overrideLabel.textContent = channel.name;
        const override = document.createElement("select"); override.dataset.override = channel.id;
        override.add(new Option("Use server default", "")); override.add(new Option("Observe", "observe")); override.add(new Option("Participate", "participate"));
        override.value = config.channel_overrides[channel.id]?.mode || ""; overrideLabel.append(override); overrides.append(overrideLabel);
      }
      changed = false; document.getElementById("chat-save-status").textContent = "Loaded. Changes apply to new messages.";
      const health = await api("/ai/status" + query()); if (current !== generation) return;
      const labels = {ready:"Ready", missing_api_key:"API key required", not_running:"Not running", unavailable:"Unavailable; check Logs"};
      const webLabels = {ready:"Ready", not_used:"Not used yet", unavailable:"Recent lookup failed"};
      const surfLabels = {waiting:"Waiting for a suitable gap",shared:"Shared a discovery",preserving_budget:"Preserving chat budget",unavailable:"Recent browsing attempt stopped"};
      document.getElementById("chat-health").textContent = `Model: ${labels[health.state] || health.state}. Web search: ${config.web_enabled ? (webLabels[health.web_search] || "Ready when needed") : "Off"}. Interest browsing: ${config.surf_enabled ? (surfLabels[health.surfing] || "Waiting") : "Off"}. ${health.chat_error ? "Recent request failed; check Logs." : "No recent chat errors."}`;
      const budget = health.budget;
      document.getElementById("chat-budget").textContent = budget ? `¥${budget.spent_cny.toFixed(4)} used of ¥${budget.limit_cny.toFixed(2)} · ¥${budget.reserved_cny.toFixed(4)} reserved · resets at midnight ${budget.timezone} (${budget.day})` : "Budget available when the bot is running.";
      if (window.loadPersona) await window.loadPersona(guild, guilds.find(item => item.id === guild).channels, config.channels);
      const usage = document.getElementById("chat-usage"); usage.replaceChildren();
      for (const row of health.usage || []) {
        const hit = row.cache_hit_tokens || 0, miss = row.cache_miss_tokens || 0;
        const rate = hit + miss ? (100 * hit / (hit + miss)).toFixed(1) + "%" : "—";
        const cache = row.cache_reported ? `cache hit ${rate} (${hit} hit / ${miss} uncached input tokens; ${row.cache_reported}/${row.requests} requests reported)` : "cache usage not reported";
        const line = document.createElement("p"); line.textContent = `${row.day} · ${row.model || ""} · ${row.stage} · ${row.requests} requests · ${row.input_tokens || 0} input / ${row.output_tokens || 0} output tokens (${row.reasoning_tokens || 0} thinking tokens included) · ${cache} · ${row.status}`; usage.append(line);
      }
      if (!usage.childElementCount) usage.textContent = "No model requests recorded.";
      await decisions();
    } catch (error) {document.getElementById("chat-health").textContent = "Could not load chat: " + error.message; report(error);}
    finally {loading = false; document.getElementById("chat-guild").disabled = !guild; if (config && guild) disable(false);}
  }
  form.addEventListener("input", () => {changed = true; document.getElementById("chat-save-status").textContent = "Unsaved changes";});
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (!config || !guild || loading) return;
    const next = structuredClone(config), scope = guild;
    for (const element of form.elements) {
      if (!element.name || !(element.name in next)) continue;
      next[element.name] = element.type === "checkbox" ? element.checked : element.type === "number" ? Number(element.value) : element.value.trim();
    }
    for (const key of ["aliases", "excluded_users"]) next[key] = next[key].split(/[,，\n]/).map(item => item.trim()).filter(Boolean);
    next.channels = [...form.querySelectorAll("[data-channel]:checked")].map(item => item.dataset.channel);
    for (const select of form.querySelectorAll("[data-override]")) {
      const entry = {...next.channel_overrides[select.dataset.override]};
      if (select.value) entry.mode = select.value; else delete entry.mode;
      if (Object.keys(entry).length) next.channel_overrides[select.dataset.override] = entry; else delete next.channel_overrides[select.dataset.override];
    }
    disable(true);
    document.getElementById("chat-guild").disabled = true;
    try {
      const result = await api("/settings/chat?guild_id=" + encodeURIComponent(scope), "PUT", next);
      if (scope !== guild) return;
      config = next; changed = false;
      document.getElementById("chat-save-status").textContent = result.reason || "Saved. New messages use these settings.";
      toast("Chat settings saved");
    } catch (error) {report(error);} finally {disable(false); document.getElementById("chat-guild").disabled = !guild;}
  });
  document.getElementById("chat-guild").addEventListener("change", event => {
    if (changed && !confirm("Discard unsaved chat changes?")) {event.target.value = guild; return;}
    guild = event.target.value; config = null; load();
  });
  document.getElementById("chat-refresh").addEventListener("click", () => {if (!changed || confirm("Discard unsaved chat changes?")) load();});
  document.getElementById("chat-decisions-refresh").addEventListener("click", () => decisions().catch(report));
  window.addEventListener("beforeunload", event => {if (changed) {event.preventDefault(); event.returnValue = "";}});
  window.loadChatPage = () => {if (!config) load();};
  if (location.hash === "#chat") load();
})();
