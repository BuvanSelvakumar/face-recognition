(() => {
  const $ = (id) => document.getElementById(id);
  let info = {}, sourceType = "drive", pollTimer = null;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toast = (msg) => {
    const t = document.createElement("div");
    t.className = "toast";
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  };

  async function api(url, opts = {}) {
    const res = await fetch(url, { credentials: "same-origin", ...opts });
    let body = null;
    try { body = await res.json(); } catch {}
    if (!res.ok) throw new Error((body && body.detail && (typeof body.detail === "string" ? body.detail : body.detail[0]?.msg)) || `Error ${res.status}`);
    return body;
  }

  function setType(t) {
    sourceType = t;
    document.querySelectorAll("#f-type button").forEach((b) => b.classList.toggle("on", b.dataset.v === t));
    if (t === "drive") {
      $("f-source-label").textContent = "Google Drive folder link";
      $("f-source").placeholder = "https://drive.google.com/drive/folders/…";
      $("f-source-hint").innerHTML = info.service_account_email
        ? `First share the folder (Viewer) with <code>${esc(info.service_account_email)}</code>`
        : `<span style="color:var(--err)">Google Drive isn't set up yet: add <code>service-account.json</code> (see README).</span>`;
    } else {
      $("f-source-label").textContent = "Folder path on this computer";
      $("f-source").placeholder = "/Users/you/Pictures/Priya-Arjun-Wedding";
      $("f-source-hint").textContent = "Only works while the app runs on the same computer as the photos.";
    }
  }

  function fmtDate(ts) { return new Date(ts * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }); }

  function render(events) {
    $("no-events").classList.toggle("hidden", events.length > 0);
    $("events").innerHTML = events.map((e) => {
      const pct = e.total_photos ? Math.round((e.done_photos / e.total_photos) * 100) : 0;
      const busy = e.status === "indexing" || e.status === "pending";
      return `
      <div class="card event" data-id="${e.id}">
        <div class="event-top">
          <div>
            <h3>${esc(e.name)}</h3>
            <div class="small muted">${e.source_type === "drive" ? "Google Drive" : "Local folder"} · created ${fmtDate(e.created_at)} · deletes on ${fmtDate(e.expires_at)}${e.pin ? ` · PIN <b>${esc(e.pin)}</b>` : ""}</div>
          </div>
          <span class="badge ${e.status}">${e.status === "pending" ? "queued" : e.status}</span>
        </div>
        ${busy ? `<div class="progress"><div style="width:${pct}%"></div></div>` : ""}
        <div class="small ${e.status === "error" ? "err" : "muted"}" style="margin:0">${esc(e.status_msg || "")}
          ${!busy && e.status !== "error" ? "" : ` · ${e.done_photos}/${e.total_photos} photos · ${e.face_count} faces`}</div>
        <div class="link-row">Guest link: <a href="${esc(e.url)}" target="_blank">${esc(e.url)}</a>
          <button class="btn ghost small" data-act="copy" data-url="${esc(e.url)}">Copy</button></div>
        <div class="event-actions">
          <a class="btn small" href="/api/admin/events/${e.id}/card" target="_blank">🖨 QR card</a>
          <a class="btn ghost small" href="/api/admin/events/${e.id}/qr.png" download="QR - ${esc(e.name)}.png">⬇ QR image</a>
          <button class="btn ghost small" data-act="sync" ${busy ? "disabled" : ""}>↻ Sync new photos</button>
          <button class="btn ghost small" data-act="pin">PIN</button>
          <button class="btn ghost small" data-act="extend">Extend</button>
          <button class="btn danger small" data-act="delete">Delete</button>
        </div>
      </div>`;
    }).join("");
  }

  async function load() {
    try {
      const events = await api("/api/admin/events");
      render(events);
      clearTimeout(pollTimer);
      if (events.some((e) => e.status === "indexing" || e.status === "pending")) pollTimer = setTimeout(load, 3000);
    } catch (e) { toast(e.message); }
  }

  $("events").addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const id = btn.closest(".event").dataset.id;
    const name = btn.closest(".event").querySelector("h3").textContent;
    try {
      switch (btn.dataset.act) {
        case "copy":
          await navigator.clipboard.writeText(btn.dataset.url);
          toast("Guest link copied");
          return;
        case "sync":
          await api(`/api/admin/events/${id}/sync`, { method: "POST" });
          toast("Checking the folder for new photos…");
          break;
        case "pin": {
          const pin = prompt("Guest PIN (leave empty to remove):", "");
          if (pin === null) return;
          await api(`/api/admin/events/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pin }) });
          toast(pin ? "PIN updated, reprint the QR card" : "PIN removed");
          break;
        }
        case "extend": {
          const days = prompt("Keep this gallery for how many more days (from today)?", String(info.default_days || 60));
          if (!days) return;
          await api(`/api/admin/events/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ days: Number(days) }) });
          toast("Expiry updated");
          break;
        }
        case "delete":
          if (!confirm(`Delete "${name}"?\n\nGuests will lose access, and face data and thumbnails are removed. Photos in your Drive are NOT touched.`)) return;
          await api(`/api/admin/events/${id}`, { method: "DELETE" });
          toast("Event deleted");
          break;
      }
      load();
    } catch (e) { toast(e.message); }
  });

  $("btn-new").onclick = () => {
    $("new-form").classList.remove("hidden");
    $("f-days").value = info.default_days || 60;
    $("f-name").focus();
  };
  $("f-cancel").onclick = () => $("new-form").classList.add("hidden");
  document.querySelectorAll("#f-type button").forEach((b) => (b.onclick = () => setType(b.dataset.v)));

  $("new-form").onsubmit = async (e) => {
    e.preventDefault();
    $("f-err").textContent = "";
    $("f-submit").disabled = true;
    $("f-submit").textContent = "Checking folder…";
    try {
      await api("/api/admin/events", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: $("f-name").value,
          source_type: sourceType,
          source: $("f-source").value,
          pin: $("f-pin").value || null,
          days: Number($("f-days").value) || info.default_days,
        }),
      });
      $("new-form").reset();
      $("new-form").classList.add("hidden");
      toast("Event created, indexing started");
      load();
    } catch (err) {
      $("f-err").textContent = err.message;
    } finally {
      $("f-submit").disabled = false;
      $("f-submit").textContent = "Create & start indexing";
    }
  };

  (async () => {
    try { info = await api("/api/admin/info"); } catch {}
    if (!info.allow_local) document.querySelector('#f-type [data-v="local"]').remove();
    setType("drive");
    load();
  })();
})();
