"use strict";
(() => {
  let guilds = [], guild = "", channel = "", config, offset = 0, changed = false, generation = 0;
  const form = document.getElementById("memory-form");
  const advanced = [["model", "Extraction model", "text"], ["batch_messages", "Messages per extraction batch", "number"],
    ["batch_seconds", "Maximum batch wait (seconds)", "number"], ["retention_days", "Retain original messages (days)", "number"],
    ["recall_limit", "Memories per reply", "number"], ["daily_model_calls", "Extra extraction-call limit per day (0: budget only)", "number"]];
  for (const [name, title, type] of advanced) {
    const label = document.createElement("label"); label.textContent = title;
    const input = document.createElement("input"); input.name = name; input.type = type; input.required = true;
    if (type === "number") {input.min = name === "daily_model_calls" ? "0" : "1"; input.step = "1";}
    label.append(input); document.getElementById("memory-advanced").append(label);
  }
  const query = () => new URLSearchParams({guild_id:guild, channel_id:channel, subject_id:document.getElementById("memory-subject").value.trim(), history:document.getElementById("memory-history").checked ? "1" : "0", offset:String(offset), limit:"50"});
  const body = () => ({guild_id:guild, channel_id:channel});
  function report(error) {document.getElementById("memory-save-status").textContent = error.message;}
  function disable(value) {for (const element of form.elements) element.disabled = value;}
  async function records() {
    const target = document.getElementById("memory-records");
    if (!guild || !channel) {target.textContent = "Select a channel to inspect its memories."; return;}
    const scope = guild + ":" + channel, page = offset;
    target.textContent = "Loading memories...";
    const result = await api("/memory?" + query());
    if (scope !== guild + ":" + channel || page !== offset) return;
    target.replaceChildren();
    for (const row of result.memories) {
      const article = document.createElement("article"); article.className = "memory-record";
      const meta = document.createElement("p"); meta.className = "muted";
      meta.textContent = `${row.kind} · ${row.state} · ${row.subject || "shared event"} · ${new Date(row.updated * 1000).toLocaleString()}${row.supersedes ? " · replaces " + row.supersedes : ""}`;
      const text = document.createElement("p"); text.textContent = row.content || "Record forgotten; text removed.";
      const sources = document.createElement("details"), summary = document.createElement("summary"); summary.textContent = "Source evidence"; sources.append(summary);
      for (const source of row.sources) {
        const line = document.createElement("p"), link = document.createElement("a");
        link.textContent = `${source.name} · ${new Date(source.created * 1000).toLocaleString()}`;
        link.href = `https://discord.com/channels/${encodeURIComponent(guild)}/${encodeURIComponent(channel)}/${encodeURIComponent(source.message_id)}`;
        link.target = "_blank"; link.rel = "noopener noreferrer";
        line.append(link, document.createTextNode(" — " + (source.content || "Original text reached its retention limit."))); sources.append(line);
      }
      const actions = document.createElement("div"); actions.className = "play-controls";
      const remove = document.createElement("button"); remove.type = "button"; remove.className = "danger"; remove.textContent = "Forget record";
      const selectedScope = body();
      remove.addEventListener("click", async () => {
        if (!confirm("Forget this record, its retained source messages and derived memories? Pending replies will be cancelled.")) return;
        remove.disabled = true;
        try {await api("/memory/" + encodeURIComponent(row.id), "DELETE", selectedScope); await records();} catch (error) {report(error); remove.disabled = false;}
      }); actions.append(remove);
      if (row.state === "candidate") {
        const approve = document.createElement("button"); approve.type = "button"; approve.textContent = "Approve candidate";
        approve.addEventListener("click", async () => {approve.disabled = true; try {await api("/memory/" + row.id + "/approve", "POST", selectedScope); await records();} catch (error) {report(error); approve.disabled = false;}}); actions.append(approve);
      }
      const edit = document.createElement("details"), editTitle = document.createElement("summary"); editTitle.textContent = "Correct this memory";
      const editForm = document.createElement("form"), label = document.createElement("label"); label.textContent = "Corrected statement";
      const input = document.createElement("textarea"); input.rows = 3; input.maxLength = 1800; input.required = true; input.value = row.content;
      const save = document.createElement("button"); save.type = "submit"; save.textContent = "Save correction";
      label.append(input); editForm.append(label, save); edit.append(editTitle, editForm);
      editForm.addEventListener("submit", async event => {event.preventDefault(); save.disabled = true; try {await api("/memory/" + row.id + "/correct", "POST", {...selectedScope, text:input.value}); await records();} catch (error) {report(error); save.disabled = false;}});
      article.append(meta, text, sources);
      if (row.state !== "deleted") article.append(actions);
      if (["active", "candidate"].includes(row.state)) article.append(edit);
      target.append(article);
    }
    if (!result.memories.length) target.textContent = "No memories in this channel yet. Enable memory and allow this channel in Group chat. Observe mode creates review candidates.";
    document.getElementById("memory-page").textContent = "Page " + (offset / 50 + 1);
    document.getElementById("memory-prev").disabled = offset === 0;
    document.getElementById("memory-next").disabled = result.memories.length < 50;
  }
  async function load() {
    const current = ++generation; disable(true);
    document.getElementById("memory-guild").disabled = true;
    document.getElementById("memory-channel").disabled = true;
    try {
      guilds = await api("/guilds"); if (current !== generation) return;
      if (!guilds.some(item => item.id === guild)) guild = guilds[0]?.id || "";
      const select = document.getElementById("memory-guild"); select.replaceChildren(...guilds.map(item => new Option(item.name, item.id))); select.value = guild; select.disabled = !guild;
      if (!guild) {document.getElementById("memory-health").textContent = "No server connected. Start the bot to manage memory."; return;}
      const channels = guilds.find(item => item.id === guild).channels;
      if (!channels.some(item => item.id === channel)) channel = channels[0]?.id || "";
      const picker = document.getElementById("memory-channel"); picker.replaceChildren(...channels.map(item => new Option(item.name, item.id))); picker.value = channel; picker.disabled = !channel;
      config = await api("/settings/memory?guild_id=" + encodeURIComponent(guild)); if (current !== generation) return;
      for (const element of form.elements) {if (!element.name) continue; if (element.type === "checkbox") element.checked = config[element.name]; else element.value = config[element.name];}
      const health = await api("/ai/status?guild_id=" + encodeURIComponent(guild)); if (current !== generation) return;
      const labels = {ready:"Ready", empty:"No memories yet", error:"Needs attention", disabled:"Off", missing_embedding_configuration:"Embedding service required", unavailable_using_keywords:"Unavailable; using keyword recall"};
      document.getElementById("memory-health").textContent = `Memory: ${labels[health.memory?.state] || "Not running"}. Semantic recall: ${labels[health.semantic_search] || health.semantic_search || "Off"}.`;
      document.getElementById("memory-save-status").textContent = "Loaded. Changes apply to new operations.";
      changed = false; await records();
    } catch (error) {document.getElementById("memory-health").textContent = "Could not load memory: " + error.message; report(error);}
    finally {document.getElementById("memory-guild").disabled = !guild; document.getElementById("memory-channel").disabled = !channel; if (config && guild) disable(false);}
  }
  form.addEventListener("input", () => {changed = true; document.getElementById("memory-save-status").textContent = "Unsaved changes";});
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (!config || !guild) return;
    const next = {...config}, selectedGuild = guild;
    for (const element of form.elements) {if (!element.name) continue; next[element.name] = element.type === "checkbox" ? element.checked : element.type === "number" ? Number(element.value) : element.value.trim();}
    disable(true);
    document.getElementById("memory-guild").disabled = true;
    try {const result = await api("/settings/memory?guild_id=" + encodeURIComponent(selectedGuild), "PUT", next); if (guild !== selectedGuild) return; config = next; changed = false; document.getElementById("memory-save-status").textContent = result.reason || "Memory settings saved."; toast("Memory settings saved");}
    catch (error) {report(error);} finally {disable(false); document.getElementById("memory-guild").disabled = !guild;}
  });
  document.getElementById("memory-guild").addEventListener("change", event => {if (changed && !confirm("Discard unsaved memory changes?")) {event.target.value = guild; return;} guild = event.target.value; channel = ""; config = null; offset = 0; load();});
  document.getElementById("memory-channel").addEventListener("change", event => {channel = event.target.value; offset = 0; records().catch(report);});
  document.getElementById("memory-refresh").addEventListener("click", () => {if (!changed || confirm("Discard unsaved memory changes?")) load();});
  document.getElementById("memory-search").addEventListener("click", () => {offset = 0; records().catch(report);});
  document.getElementById("memory-history").addEventListener("change", () => {offset = 0; records().catch(report);});
  document.getElementById("memory-prev").addEventListener("click", () => {offset = Math.max(0, offset - 50); records().catch(report);});
  document.getElementById("memory-next").addEventListener("click", () => {offset += 50; records().catch(report);});
  document.getElementById("memory-forget").addEventListener("submit", async event => {
    event.preventDefault(); if (!guild || !channel || !confirm("Forget this member's messages and derived memories in the selected channel?")) return;
    const button = event.target.querySelector("button"); button.disabled = true;
    try {await api("/memory/forget", "POST", {...body(), subject_id:event.target.elements.subject_id.value}); document.getElementById("memory-forget-status").textContent = "Member forgotten in this channel."; await records();}
    catch (error) {document.getElementById("memory-forget-status").textContent = error.message;} finally {button.disabled = false;}
  });
  document.getElementById("memory-export").addEventListener("click", async event => {
    if (!guild || !channel) return; const button = event.target; button.disabled = true;
    const selectedScope = body(), all = [];
    try {
      for (let page = 0; ; page += 1000) {
        const params = new URLSearchParams({...selectedScope, history:"1", offset:String(page), limit:"1000"});
        const result = await api("/memory?" + params); all.push(...result.memories); if (result.memories.length < 1000) break;
      }
      const url = URL.createObjectURL(new Blob([JSON.stringify({scope:selectedScope, memories:all}, null, 2)], {type:"application/json"}));
      const link = document.createElement("a"); link.href = url; link.download = `memories-${selectedScope.guild_id}-${selectedScope.channel_id}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {report(error);} finally {button.disabled = false;}
  });
  window.addEventListener("beforeunload", event => {if (changed) {event.preventDefault(); event.returnValue = "";}});
  window.loadMemoryPage = () => {if (!config) load();};
  if (location.hash === "#memory") load();
})();
