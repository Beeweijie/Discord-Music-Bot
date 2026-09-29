"use strict";
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const titles = {overview:"Overview", music:"Music", settings:"Settings", logs:"Logs"};
let state = {}, selected = null, dashboardReady = false, pending = false, polling = false, settingsLoaded = false;
let toastTimer, loadGeneration = 0;
let settingsGuild = "", settingsRequest = 0;
let queuePage = 1;
let logGuild = null, logRequest = 0;
const settingsSections = ["music", "welcome", "voice_moderation"];
const scopedSettings = (path, guild = settingsGuild) => path + "?guild_id=" + encodeURIComponent(guild);
const dirty = new Set();

async function api(path, method="GET", body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch("/api/v1" + path, {method, credentials:"same-origin", signal:controller.signal,
      headers:{"Content-Type":"application/json", "X-Requested-With":"BotDashboard"},
      body:body === undefined ? undefined : JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(result.error?.message || "Request failed");
      error.status = response.status; throw error;
    }
    return result;
  } catch(error) {
    if (error.name === "AbortError") throw new Error("Request timed out. Check the server connection.");
    throw error;
  } finally {clearTimeout(timer);}
}
function toast(message) {
  $("toast").textContent = message; $("toast").hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $("toast").hidden = true, 4200);
}
async function enter() {
  dashboardReady = true; loadGeneration++; dirty.clear();
  $("app").hidden = false;
  route(); await refresh();
  if (dashboardReady) await loadSettings();
}
function route() {
  const page = location.hash.slice(1) in titles ? location.hash.slice(1) : "overview";
  for (const name of Object.keys(titles)) $("page-"+name).hidden = page !== name;
  document.querySelectorAll("nav a").forEach(a => {a.classList.toggle("active", a.dataset.page === page); a.setAttribute("aria-current", a.dataset.page === page ? "page" : "false");});
  $("breadcrumb").textContent = titles[page];
  if (dashboardReady && page === "logs") refreshLogs();
}
window.addEventListener("hashchange", route);
function ago(value) {
  const elapsed = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 1000));
  if (!Number.isFinite(elapsed)) return "—";
  if (elapsed < 60) return elapsed + "s ";
  if (elapsed < 3600) return Math.floor(elapsed/60) + "m ";
  return Math.floor(elapsed/3600) + "h " + Math.floor(elapsed%3600/60) + "m ";
}
function entries() {return Object.entries(state.music_sessions || {});}
function emptyMarkup(compact=false) {
  return `<div class="empty ${compact ? "compact" : ""}"><h2>No playback sessions</h2><p>Join a voice channel and use <code>/play</code> in Discord. Your session will appear here.</p></div>`;
}
function renderState() {
  const stale = !state.updated_at || Date.now()-Date.parse(state.updated_at)>45000;
  const online = state.state === "online" && !stale;
  const labels = {online:"Online", starting:"Starting", reconnecting:"Reconnecting", offline:"Offline", error:"Error"};
  const label = stale ? "Waiting for an update" : (labels[state.state] || "Waiting for connection");
  $("connection").textContent = label; $("connection").classList.toggle("online", online);
  $("hero-badge").textContent = (online ? "● " : "○ ") + label;
  $("hero-detail").textContent = state.last_error || (online ? "Connected to Discord." : "Dashboard ready. Waiting for the bot to connect. ");
  $("bot-name").textContent = state.bot_user || "喵酱";
  $("workspace-name").textContent = (state.bot_user || "喵酱").split("#")[0];
  $("uptime").textContent = state.started_at ? ago(state.started_at) : "—";
  $("guild-count").textContent = state.guild_count ?? "—";
  $("session-count").textContent = entries().length;
  $("latency").textContent = state.latency_ms ?? "—";
  $("updated").textContent = state.updated_at ? ago(state.updated_at)+"ago" : "Waiting for heartbeat";
  $("queue-summary").textContent = "Queued tracks: " + entries().reduce((n,[,s]) => n+(s.queue_size||0),0) + " tracks";
  $("overview-sessions").innerHTML = entries().length ? entries().map(([id,s]) => `<a class="session-row" href="#music" data-session="${esc(id)}"><div><h3>${esc(s.current_song || "Waiting for a track")}</h3><p>${esc(s.guild_name)} · ${esc(s.channel_name)}</p></div><span class="pill">${s.is_paused ? "Paused" : s.is_playing ? "Playing" : "Idle"}</span></a>`).join("") : emptyMarkup(true);
  renderMusic();
}
function renderMusic() {
  const sessions = entries();
  $("music-count").textContent = sessions.length + (sessions.length === 1 ? " session" : " sessions");
  $("music-empty").hidden = sessions.length > 0; $("music-content").hidden = !sessions.length;
  if (!sessions.length) {selected = null; return;}
  if (!sessions.some(([id]) => id === selected)) selected = sessions[0][0];
  $("session-picker").innerHTML = sessions.map(([id,s]) => `<button class="${id===selected ? "selected" : ""}" data-session="${esc(id)}">${esc(s.guild_name)} / ${esc(s.channel_name)}</button>`).join("");
  const s = state.music_sessions[selected];
  $("track-title").textContent = s.current_song || (s.restored ? "Saved queue, ready to resume" : "Waiting for a track");
  $("track-meta").textContent = s.restored ? "Join the original voice channel, then click Resume or use /resume in Discord." : (s.requester ? "Requested by:"+s.requester+" · " : "") + (s.is_paused ? "Paused" : s.is_playing ? "Playing" : "Idle");
  if (!$("volume-form").dataset.dirty) {$("volume").value = s.volume ?? 40; $("volume-value").textContent = $("volume").value + "%";}
  if (!$("loop-form").dataset.dirty) $("loop").value = s.loop_mode || "off";
  document.querySelector('[data-action="pause"]').hidden = !s.is_playing;
  document.querySelector('[data-action="resume"]').hidden = !(s.is_paused || s.restored || s.sleeping || (!s.is_playing && s.queue_size));
  document.querySelector('[data-action="skip"]').disabled = pending || !s.is_connected;
  document.querySelector('[data-action="replay"]').disabled = pending || !(s.is_playing || s.is_paused);
  for (const input of $("seek-form").querySelectorAll("input,button")) input.disabled = pending || !(s.is_playing || s.is_paused);
  $("sleep-status").textContent = s.sleep_until ? "Pauses at " + new Date(s.sleep_until * 1000).toLocaleTimeString() + ". Enter 0 to cancel." : s.sleeping ? "Sleep timer paused playback. Use Resume to continue." : "No sleep timer. Enter 0 to cancel.";
  $("history-list").innerHTML = (s.history || []).map(q => `<li>${esc(q.title)}</li>`).join("") || "<li>No recently played tracks.</li>";
  $("queue-count").textContent = (s.queue_size || 0) + " tracks";
  const filter = $("queue-filter").value.trim().toLocaleLowerCase();
  const matches = (s.queue || []).map((q,i) => ({...q, index:i+1})).filter(q => (q.title+" "+q.requester).toLocaleLowerCase().includes(filter));
  const pages = Math.max(1, Math.ceil(matches.length / 15));
  queuePage = Math.min(queuePage, pages);
  $("queue-page").textContent = `${queuePage} / ${pages} · ${matches.length} ${matches.length === 1 ? "track" : "tracks"}`;
  $("queue-prev").disabled = pending || queuePage <= 1;
  $("queue-next").disabled = pending || queuePage >= pages;
  $("queue-list").innerHTML = matches.slice((queuePage-1)*15,queuePage*15).map(q => `<li><span>${q.index}</span><div><strong>${esc(q.title)}</strong><small>${esc(q.requester || "")}</small></div><button data-queue-action="nextup" data-index="${q.index}" data-version="${esc(s.queue_version)}" ${pending ? "disabled" : ""}>Play next</button><button data-queue-action="remove" data-index="${q.index}" data-version="${esc(s.queue_version)}" ${pending ? "disabled" : ""}>Remove</button></li>`).join("") || '<li>No matching tracks.</li>';
}
document.addEventListener("click", event => {
  const choice = event.target.closest("[data-session]");
  if (choice && !pending) {selected = choice.dataset.session; queuePage = 1; delete $("volume-form").dataset.dirty; delete $("loop-form").dataset.dirty; renderMusic();}
  const queueAction = event.target.closest("[data-queue-action]");
  if (queueAction) control(queueAction.dataset.queueAction, {index:Number(queueAction.dataset.index), queue_version:queueAction.dataset.version});
  const action = event.target.closest("[data-action]");
  if (action) control(action.dataset.action);
});
async function refresh() {
  if (polling || !dashboardReady) return;
  polling = true; const generation = loadGeneration;
  try {
    const next = await api("/status");
    if (generation !== loadGeneration) return;
    state = next; $("network-error").hidden = true; renderState();
    if (settingsLoaded && !$("page-settings").hidden) await refreshEffects();
  } catch(error) {
    if (dashboardReady) {$("network-error").textContent = "Could not update status:" + error.message + ". Showing the last known state while reconnecting."; $("network-error").hidden = false; $("connection").textContent = "Disconnected"; $("connection").classList.remove("online");}
  } finally {polling = false;}
}
async function control(action, extra={}) {
  if (pending || !selected) return;
  if (["stop","clear"].includes(action) && !confirm(action === "stop" ? "Stop playback, clear the queue, and disconnect?" : "Clear all waiting tracks? The current track will keep playing.")) return;
  pending = true;
  const targetSession = selected;
  const buttons = [...$("music-content").querySelectorAll("button")]; buttons.forEach(b => b.disabled = true);
  $("control-result").textContent = "Sending command...";
  try {
    const result = await api("/sessions/"+encodeURIComponent(targetSession)+"/controls", "POST", {action,...extra});
    $("control-result").textContent = "Command sent. Waiting for confirmation...";
    let finished = false;
    for (let i=0; i<65 && dashboardReady; i++) {
      await new Promise(resolve => setTimeout(resolve,1000));
      try {
        const ack = await api("/controls/"+result.id);
        if (!ack.ok) throw new Error(ack.message || "Operation failed");
        $("control-result").textContent = "✓ " + ack.message;
        delete $("volume-form").dataset.dirty; delete $("loop-form").dataset.dirty;
        toast("Done"); finished = true; break;
      } catch(error) {if (error.status !== 404) throw error;}
    }
    if (!finished) throw new Error("No confirmation received. Check the bot status before retrying.");
    await refresh();
  } catch(error) {$("control-result").textContent = "Operation failed:"+error.message; toast(error.message);}
  finally {pending = false; buttons.forEach(b => b.disabled = false); renderMusic();}
}
$("volume").addEventListener("input", () => {$("volume-value").textContent = $("volume").value + "%"; $("volume-form").dataset.dirty = "true";});
$("loop").addEventListener("change", () => $("loop-form").dataset.dirty = "true");
$("volume-form").addEventListener("submit", e => {e.preventDefault(); control("volume", {volume:Number($("volume").value)});});
$("loop-form").addEventListener("submit", e => {e.preventDefault(); control("loop", {mode:$("loop").value});});
function effect(section, report) {
  $(section+"-effect").textContent = report.effect === "stored_only" ? "Stored only" : report.requires_restart ? "Restart required" : report.applied ? "Applied" : "Pending";
  if (!dirty.has(section)) $(section+"-save-status").textContent = report.reason || "Saved";
}
function setSettingsDisabled(disabled) {
  for (const section of settingsSections) {
    for (const input of $(section+"-settings").querySelectorAll("input,select,button")) input.disabled = disabled;
  }
}
async function refreshEffects() {
  if (!settingsGuild) return;
  const guild = settingsGuild, request = settingsRequest;
  const reports = await api(scopedSettings("/settings", guild));
  if (guild !== settingsGuild || request !== settingsRequest) return;
  for (const section of settingsSections) effect(section, reports[section]);
}
async function loadSettings() {
  const request = ++settingsRequest;
  settingsLoaded = false; setSettingsDisabled(true); $("settings-guild").disabled = true;
  try {
    const guilds = await api("/guilds");
    if (request !== settingsRequest) return;
    if (!guilds.some(g => g.id === settingsGuild)) settingsGuild = guilds[0]?.id || "";
    $("settings-guild").innerHTML = guilds.length ? guilds.map(g => '<option value="'+esc(g.id)+'">'+esc(g.name)+'</option>').join("") : '<option value="">The bot has not joined any servers</option>';
    $("settings-guild").value = settingsGuild;
    if (!settingsGuild) return;
    const reports = await api(scopedSettings("/settings"));
    if (request !== settingsRequest) return;
    for (const section of settingsSections) {
      const form = $(section+"-settings");
      for (const [key,value] of Object.entries(reports[section].settings)) {
        const input = form.elements.namedItem(key); if (!input) continue;
        if (input.type === "checkbox") input.checked = value; else input.value = value;
      }
      dirty.delete(section); effect(section,reports[section]);
    }
    $("channels").innerHTML = guilds.find(g => g.id === settingsGuild).channels.map(c => '<option value="'+esc(c.id)+'">#'+esc(c.name)+'</option>').join("");
    settingsLoaded = true; setSettingsDisabled(false);
  } catch(error) {toast("Could not load settings:"+error.message);}
  finally {if (request === settingsRequest) $("settings-guild").disabled = !settingsGuild;}
}
$("settings-guild").addEventListener("change", () => {
  if (dirty.size && !confirm("Discard unsaved changes and switch servers?")) {$("settings-guild").value = settingsGuild; return;}
  settingsGuild = $("settings-guild").value; dirty.clear(); loadSettings();
});

for (const section of settingsSections) {
  const form = $(section+"-settings");
  form.addEventListener("input", () => {dirty.add(section); $(section+"-save-status").textContent = "Unsaved changes";});
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (!settingsLoaded || !settingsGuild) return;
    const guild = settingsGuild, request = settingsRequest;
    const button = form.querySelector('button[type="submit"]'); button.disabled = true; $("settings-guild").disabled = true;
    try {
      const values = {};
      for (const input of form.querySelectorAll("input[name],select[name]")) values[input.name] = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value.trim();
      const report = await api(scopedSettings("/settings/"+section, guild), "PUT", values);
      if (guild !== settingsGuild || request !== settingsRequest) return;
      dirty.delete(section); effect(section,report); $(section+"-save-status").textContent = "Saved. Waiting for the bot to apply settings..."; toast("Settings saved");
      setTimeout(() => {if(dashboardReady) refreshEffects().catch(e=>toast(e.message));}, 2500);
    } catch(error) {$(section+"-save-status").textContent = "Save failed:"+error.message; toast(error.message);}
    finally {button.disabled = false; $("settings-guild").disabled = !settingsGuild;}
  });
}
async function refreshLogs() {
  const request = ++logRequest;
  try {
    const guilds = await api("/guilds");
    if (request !== logRequest) return;
    if (logGuild === null) logGuild = settingsGuild || guilds[0]?.id || "";
    if (logGuild && !guilds.some(g => g.id === logGuild)) logGuild = "";
    $("log-guild").innerHTML = '<option value="">System</option>' + guilds.map(g => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join("");
    $("log-guild").value = logGuild;
    const query = new URLSearchParams({limit:"80", level:$("log-level").value, details:String($("log-details").checked)});
    if (logGuild) query.set("guild_id", logGuild);
    const value = await api("/logs?"+query);
    if (request !== logRequest) return;
    $("log-scope").textContent = logGuild ? "Only events from this server. Choose System for startup and shared connection issues." : "Startup and shared connection events. Select a server to see its activity.";
    const output = $("log-output"), nearEnd = output.scrollTop + output.clientHeight >= output.scrollHeight - 30;
    const text = value.lines.join("\n") || "No matching log entries yet.";
    if (output.textContent !== text) {output.textContent = text; if (nearEnd && $("auto-logs").checked) output.scrollTop = output.scrollHeight;}
    $("log-updated").textContent = new Date().toLocaleTimeString("en") + " updated";
  } catch(error) {if (request === logRequest) $("log-output").textContent = "Could not load logs: "+error.message;}
}
$("log-guild").addEventListener("change", () => {logGuild=$("log-guild").value; $("log-output").textContent="Loading logs..."; refreshLogs();});
for (const id of ["log-level", "log-details"]) $(id).addEventListener("change", refreshLogs);
$("refresh").addEventListener("click", refresh);
$("refresh-logs").addEventListener("click", refreshLogs);
$("reload-settings").addEventListener("click", () => {if(!dirty.size || confirm("Discard changes and reload saved settings?")) loadSettings();});
window.addEventListener("beforeunload", event => {if(dirty.size){event.preventDefault(); event.returnValue="";}});
setInterval(() => {if(dashboardReady && !document.hidden){refresh(); if(!$("page-logs").hidden && $("auto-logs").checked) refreshLogs();}},3000);
enter().catch(error => toast(error.message));

$("seek-form").addEventListener("submit", e => {e.preventDefault(); control("seek", {position:$("seek-position").value.trim()});});
$("sleep-form").addEventListener("submit", e => {e.preventDefault(); control("sleep", {minutes:Number($("sleep-minutes").value)});});
$("queue-filter").addEventListener("input", () => {queuePage=1; renderMusic();});
$("queue-prev").addEventListener("click", () => {queuePage=Math.max(1,queuePage-1); renderMusic();});
$("queue-next").addEventListener("click", () => {queuePage++; renderMusic();});
