"use strict";
(() => {
  let guild = "", channel = "", generation = 0;
  const select = document.getElementById("persona-channel");
  const status = document.getElementById("persona-status");
  const refresh = document.getElementById("persona-refresh");
  const names = {style:"Speaking style", interests:"Interests", humor:"Humor", conversation_habits:"Conversation habits", group_culture:"Group culture", curiosity:"Curiosity"};
  async function load() {
    if (!guild || !channel) return;
    const current = ++generation;
    select.disabled = true; refresh.disabled = true;
    status.textContent = "Loading development...";
    try {
      const data = await api(`/persona?guild_id=${encodeURIComponent(guild)}&channel_id=${encodeURIComponent(channel)}`);
      if (current !== generation) return;
      const phases = {getting_acquainted:"Getting acquainted", settling:"Settling", stable:"Stable"};
      status.textContent = `${data.enabled ? phases[data.phase] : "Learning paused"} · ${data.total} messages observed · review after about ${data.messages_per_review} new messages, at least ${Math.ceil(data.review_interval_seconds / 60)} minutes apart.${data.error ? " Recent review failed; existing habits are retained." : ""}`;
      const notes = document.getElementById("persona-notes"); notes.replaceChildren();
      for (const row of data.notes) {
        const line = document.createElement("p"), label = document.createElement("strong");
        label.textContent = (names[row.slot] || row.slot) + ": "; line.append(label, document.createTextNode(row.text)); notes.append(line);
      }
      if (!data.notes.length) notes.textContent = "No learned habits yet. The core personality is used as written.";
      const history = document.getElementById("persona-history"); history.replaceChildren();
      const latest = new Set();
      for (const row of data.history) {
        const line = document.createElement("p");
        line.textContent = `${new Date(row.created * 1000).toLocaleString()} · ${names[row.slot] || row.slot} · ${row.after || row.reason}`;
        if (!latest.has(row.slot) && row.kind === "learn" && data.notes.some(note => note.slot === row.slot && note.text === row.after)) {
          const button = document.createElement("button"); button.type = "button"; button.textContent = "Revert this change";
          button.addEventListener("click", async () => {
            button.disabled = true;
            try {
              await api("/persona/revert", "POST", {guild_id:guild, channel_id:channel, revision_id:row.id});
              await load();
            } catch (error) {status.textContent = error.message; button.disabled = false;}
          });
          line.append(document.createTextNode(" "), button);
        }
        latest.add(row.slot); history.append(line);
      }
      if (!data.history.length) history.textContent = "No revisions yet. Accepted changes will appear here.";
    } catch (error) {if (current === generation) status.textContent = "Could not load development. " + error.message;}
    finally {if (current === generation) {select.disabled = false; refresh.disabled = false;}}
  }
  window.loadPersona = async (nextGuild, channels, allowed) => {
    if (nextGuild !== guild) channel = "";
    guild = nextGuild;
    if (!channels.some(item => item.id === channel)) channel = allowed[0] || channels[0]?.id || "";
    select.replaceChildren(...channels.map(item => new Option(item.name, item.id)));
    select.value = channel; select.disabled = !channel;
    await load();
  };
  select.addEventListener("change", () => {channel = select.value; load();});
  refresh.addEventListener("click", load);
})();
