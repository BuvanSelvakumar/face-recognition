(() => {
  const $ = (id) => document.getElementById(id);
  const views = ["loading", "error", "wait", "start", "confirm", "search", "gallery"];
  const show = (name) => views.forEach((v) => $("v-" + v).classList.toggle("hidden", v !== name));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch {} },
  };
  const setName = (name) => {
    document.querySelectorAll(".ev-name").forEach((el) => (el.textContent = name));
    document.title = `${name} · Find your photos`;
  };
  const toast = (msg) => {
    const t = document.createElement("div");
    t.className = "toast";
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  };
  const fail = (msg) => { $("error-msg").textContent = msg; show("error"); };

  async function api(url, opts) {
    const res = await fetch(url, opts);
    let body = null;
    try { body = await res.json(); } catch {}
    if (!res.ok) throw new Error((body && body.detail) || "Something went wrong. Please try again.");
    return body;
  }

  // ------------------------------------------------------------ selfie page
  let slug = null, event = null, selfieBlob = null, addTo = null;

  async function startEvent(s) {
    slug = s;
    addTo = new URLSearchParams(location.search).get("add");
    try {
      event = await api(`/api/e/${encodeURIComponent(slug)}`);
    } catch (e) { return fail(e.message); }
    setName(event.name);
    if (!event.ready) return show("wait");
    if (event.needs_pin) {
      $("pin-box").classList.remove("hidden");
      $("pin").value = store.get(`wff:pin:${slug}`) || "";
    }
    const last = store.get(`wff:session:${slug}`);
    if (last && !addTo) {
      const p = document.createElement("p");
      p.className = "small";
      p.style.marginTop = "14px";
      p.innerHTML = `<a href="/g/${encodeURIComponent(last)}">See photos from your last selfie →</a>`;
      $("start-err").after(p);
    }
    show("start");
  }

  function startErr(msg) {
    const el = $("start-err");
    el.textContent = msg || "";
    el.classList.toggle("hidden", !msg);
  }

  function pickFile(input) {
    if (!$("consent").checked) return startErr("Please tick the box above to continue.");
    if (event.needs_pin && !$("pin").value.trim()) return startErr("Please enter the event PIN.");
    startErr("");
    input.value = "";
    input.click();
  }

  // Shrink the selfie on the phone before upload (faster on mobile data).
  async function shrink(file, maxSide = 1280) {
    let bmp;
    try {
      bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
    } catch {
      bmp = await new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => resolve(img);
        img.onerror = reject;
        img.src = URL.createObjectURL(file);
      });
    }
    const scale = Math.min(1, maxSide / Math.max(bmp.width, bmp.height));
    const c = document.createElement("canvas");
    c.width = Math.round(bmp.width * scale);
    c.height = Math.round(bmp.height * scale);
    c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
    return new Promise((resolve) => c.toBlob(resolve, "image/jpeg", 0.9));
  }

  async function onFile(e) {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    try {
      selfieBlob = await shrink(file);
    } catch {
      selfieBlob = file; // let the server try (e.g. HEIC on some browsers)
    }
    $("selfie-img").src = URL.createObjectURL(selfieBlob);
    $("confirm-err").classList.add("hidden");
    show("confirm");
  }

  async function search() {
    show("search");
    const fd = new FormData();
    fd.append("selfie", selfieBlob, "selfie.jpg");
    fd.append("pin", $("pin").value.trim());
    if (addTo) fd.append("session_id", addTo);
    try {
      const r = await api(`/api/e/${encodeURIComponent(slug)}/match`, { method: "POST", body: fd });
      if (event.needs_pin) store.set(`wff:pin:${slug}`, $("pin").value.trim());
      store.set(`wff:session:${slug}`, r.session_id);
      history.replaceState(null, "", `/g/${r.session_id}`);
      if (addTo) toast(r.added ? `Found ${r.added} more photo${r.added > 1 ? "s" : ""}` : "No new photos found");
      openGallery(r.session_id);
    } catch (e) {
      $("confirm-err").textContent = e.message;
      $("confirm-err").classList.remove("hidden");
      show("confirm");
    }
  }

  $("btn-camera").onclick = () => pickFile($("file-camera"));
  $("btn-upload").onclick = () => pickFile($("file-upload"));
  $("file-camera").onchange = onFile;
  $("file-upload").onchange = onFile;
  $("btn-retake").onclick = () => show("start");
  $("btn-search").onclick = search;

  // ------------------------------------------------------------ gallery
  let photos = [], sid = null, current = 0;

  async function openGallery(s) {
    sid = s;
    let g;
    try {
      g = await api(`/api/s/${encodeURIComponent(sid)}`);
    } catch (e) { return fail(e.message); }
    photos = g.photos;
    slug = g.event_slug;
    setName(g.event_name);
    store.set(`wff:session:${slug}`, sid);

    const grid = $("grid");
    grid.innerHTML = "";
    photos.forEach((p, i) => {
      const b = document.createElement("button");
      b.setAttribute("aria-label", `Open photo ${i + 1}`);
      const img = document.createElement("img");
      img.loading = "lazy";
      img.decoding = "async";
      img.src = p.thumb;
      img.alt = "";
      b.appendChild(img);
      b.onclick = () => openLightbox(i);
      grid.appendChild(b);
    });
    const n = photos.length;
    $("g-count").textContent = n ? `${n} photo${n > 1 ? "s" : ""} of you` : "";
    $("g-empty").classList.toggle("hidden", n > 0);
    $("btn-zip").classList.toggle("hidden", n === 0);
    $("btn-zip").href = `/api/s/${encodeURIComponent(sid)}/zip`;
    $("g-expiry").textContent =
      `Bookmark this page to come back later. Available until ${new Date(g.expires_at * 1000).toLocaleDateString()}.`;
    show("gallery");
  }

  const addSelfie = () => (location.href = `/e/${encodeURIComponent(slug)}?add=${encodeURIComponent(sid)}`);
  $("btn-more").onclick = addSelfie;
  $("btn-retry").onclick = addSelfie;
  $("btn-share").onclick = async () => {
    const url = location.href;
    if (navigator.share) {
      try { await navigator.share({ title: document.title, url }); return; } catch {}
    }
    try { await navigator.clipboard.writeText(url); toast("Link copied"); } catch { prompt("Copy this link:", url); }
  };

  // ------------------------------------------------------------ lightbox
  function openLightbox(i) {
    current = (i + photos.length) % photos.length;
    const p = photos[current];
    $("lb-img").src = p.preview;
    $("lb-download").href = p.download;
    $("lb-download").setAttribute("download", p.name);
    $("lb-count").textContent = `${current + 1} / ${photos.length}`;
    $("lightbox").classList.remove("hidden");
    document.body.style.overflow = "hidden";
    [current + 1, current - 1].forEach((j) => { // preload neighbours
      const q = photos[(j + photos.length) % photos.length];
      if (q) new Image().src = q.preview;
    });
  }
  function closeLightbox() {
    $("lightbox").classList.add("hidden");
    document.body.style.overflow = "";
  }
  $("lb-close").onclick = closeLightbox;
  $("lb-prev").onclick = () => openLightbox(current - 1);
  $("lb-next").onclick = () => openLightbox(current + 1);
  document.addEventListener("keydown", (e) => {
    if ($("lightbox").classList.contains("hidden")) return;
    if (e.key === "Escape") closeLightbox();
    if (e.key === "ArrowLeft") openLightbox(current - 1);
    if (e.key === "ArrowRight") openLightbox(current + 1);
  });
  let touchX = null;
  $("lightbox").addEventListener("touchstart", (e) => (touchX = e.touches[0].clientX), { passive: true });
  $("lightbox").addEventListener("touchend", (e) => {
    if (touchX === null) return;
    const dx = e.changedTouches[0].clientX - touchX;
    if (Math.abs(dx) > 50) openLightbox(current + (dx < 0 ? 1 : -1));
    touchX = null;
  });

  // ------------------------------------------------------------ route
  const [, kind, id] = location.pathname.split("/");
  if (kind === "e" && id) startEvent(decodeURIComponent(id));
  else if (kind === "g" && id) openGallery(decodeURIComponent(id));
  else fail("This link is not valid.");
})();
