# Project Notes: Wedding Face Finder

What this project is, how it was built, what was learned along the way, and the problems hit while building it. For setup and usage, see [README.md](README.md).

---

## 1. What it is

A self-hosted web app for wedding photographers:

1. The photographer uploads all photos of a wedding to a **Google Drive folder**.
2. In the admin page they create an **event** for that folder. The app indexes every face in every photo and generates a **QR code** (plus a printable card).
3. A guest **scans the QR code**, takes a **selfie**, and gets a private gallery with **only the photos their face appears in**. They can view them, download single originals, or download all as a ZIP.

Constraints it was designed for:
- **Free / open source only.** No paid APIs, no per-use cost.
- **Scale:** about 10 weddings a month, 500–1,000 photos each.
- Runs on a **laptop** for testing and on **Oracle Cloud Free Tier** for real use.
- **Privacy first:** face data is biometric data (India's DPDP Act), so selfies are never stored and event data expires.

---

## 2. How it works

```
                ┌──────────────────────── Admin (/admin, password) ───────────────────────┐
Photographer ──>│ create event (Drive link, PIN, expiry) · sync · QR card · delete         │
                └───────────────┬─────────────────────────────────────────────────────────┘
                                │ enqueue
                                ▼
          ┌─────────────── Indexer (background thread) ───────────────┐
Drive ───>│ list images (recursive) → download (6 in parallel)        │
folder    │ → fix EXIF rotation → save thumbnail (400px) + preview    │
          │   (1600px) → detect faces (YuNet) → embed (Facenet512)    │──> SQLite
          │ → store one 512-number vector per face                    │    data/app.db
          └───────────────────────────────────────────────────────────┘    data/media/
                                                                              ▲
Guest phone ── scan QR ──> /e/<slug> ── selfie ──> embed largest face         │
                                                   → dot product vs all faces of the event
                                                   → photos with similarity ≥ 0.60
                                                   → private session /g/<token> ──┘
                                                   → thumbnails/previews from disk,
                                                     originals streamed from Drive
```

### Key pieces
| File | Role |
|---|---|
| `app/main.py` | FastAPI app: admin API (HTTP Basic auth), guest API, QR/PNG + printable card, single + streaming ZIP downloads, rate limit |
| `app/indexer.py` | Queue + worker thread; incremental sync (new photos only, removed ones dropped, failed ones retried); resumes after restart; hourly cleanup of expired events |
| `app/faces.py` | DeepFace wrapper: image decoding (EXIF, HEIC, truncated files), detection + embedding, selfie handling |
| `app/matching.py` | Per-event embedding matrix cached in memory; cosine similarity via numpy |
| `app/sources.py` | Google Drive (service account, read-only, shared drives, sub-folders) and local-folder sources |
| `app/db.py` | SQLite schema: events, photos, faces, guest_sessions, session_photos |
| `app/cli.py` | `list`, `index`, `match` (prints scores to tune the threshold) |
| `app/static/` | Plain HTML/CSS/JS for the guest and admin pages (no build step) |
| `Dockerfile`, `docker-compose.yml`, `deploy/Caddyfile` | Cloud deployment with automatic HTTPS |

### Data model in one line each
- **events**: one wedding: source folder, random slug for the QR link, optional PIN, status/progress, expiry.
- **photos**: one row per image file in the folder, with status `pending`, `done` or `error`.
- **faces**: one row per detected face: 512-float32 embedding blob, bounding box, confidence.
- **guest_sessions / session_photos**: a guest's matched gallery, behind an unguessable 32-character token.

---

## 3. How it was made

### Technology choices
| Need | Chosen | Alternatives considered | Reason |
|---|---|---|---|
| Face embeddings | **DeepFace + Facenet512** | InsightFace (ArcFace), dlib `face_recognition`, CompreFace | InsightFace is the most accurate, but its pretrained weights are *non-commercial research only*, which is risky for a photography business even if guests pay nothing. Facenet512 is free to use and accurate enough. |
| Face detector | **YuNet** (OpenCV) | RetinaFace, MTCNN | ~28× faster than RetinaFace with equal or better results on our test (see Issues). |
| Vector search | **numpy dot product over SQLite blobs** | pgvector, FAISS, Qdrant | A wedding has ≤ ~10k faces, so brute-force search takes milliseconds. No extra database server to install or run on a free VM. |
| Backend | **FastAPI** + uvicorn | Flask, Node | Async-friendly, simple file uploads/streaming, plus a Python ML ecosystem. |
| Background jobs | **In-process thread + queue** | Celery/RQ + Redis | The load is a few events a month; one process is easier to run. Trade-off: run exactly **one** uvicorn worker. |
| Frontend | **Vanilla HTML/JS** | React/Next.js | Two small pages; no build tooling, loads fast on phones at the venue. |
| QR | `qrcode` (high error correction) | segno | Still scans if a printed card gets a little damaged. |
| ZIP download | `zipstream-ng`, stored (no compression) | Build ZIP in memory | Streams originals one by one from Drive, so memory stays flat even for hundreds of photos. JPEGs don't compress anyway. |
| Hosting | Laptop → **Oracle Cloud Free Tier** (Docker + Caddy) | Render/Fly/Railway free tiers | Oracle's free ARM VM (4 cores, 24 GB) is big enough for TensorFlow and always on; the others sleep or have too little RAM. |

### Build process
1. **Plan:** confirmed the use case, scale and licensing constraints with the photographer and picked the stack.
2. **Backend core:** config, SQLite schema, Drive and local sources, face wrapper, indexer, matching, then the API.
3. **Frontend:** a mobile-first guest flow (consent → selfie → gallery/lightbox → downloads) and the admin dashboard.
4. **Testing:** used the public DeepFace test dataset as a fake wedding: 62 photos as the folder and one photo of a person as the "selfie". Ground truth comes from the dataset's `master.csv`. Tested every API path with curl, checked the UI with headless-Chrome screenshots, and decoded the generated QR with OpenCV.
5. **Tuning:** benchmarked detectors and calibrated the match threshold from real score distributions.
6. **Packaging:** Dockerfile (weights baked in at build time), docker-compose with Caddy HTTPS, README and these notes. Verified the container reproduces the same match results.

### Test results (final configuration)
| Check | Result |
|---|---|
| Correct photos found for the test person | **9 / 9**, including 2 group photos |
| Wrong photos shown | **0** |
| Same-person scores | 0.64 – 0.77 |
| Highest different-person score | 0.46 |
| Indexing speed (M2 laptop) | 62 photos in 21 s (~0.35 s/photo incl. thumbnails) |
| Selfie match time | < 2 s |
| Edge cases | wrong PIN, no face, rate limit (429 on the 15th try/min), expired/deleted links, photo not in session (404), Tamil event name in filenames |

---

## 4. Issues faced and how they were solved

### 4.1 RetinaFace was far too slow (~5 s per photo)
- **Symptom:** the first full test indexed 62 photos in **625 s**. Extrapolated, a 1,000-photo wedding would take about 2 hours.
- **Cause:** RetinaFace (TensorFlow) took ~5 s per image **even for small 400px images**. The cost is per call, not per pixel: the graph is effectively re-traced for every new input size.
- **Fix:** benchmarked the detectors on the same images. YuNet took **0.18 s/photo**, and the full re-test found *more* correct matches (9/9 vs 7/9). Made YuNet the default; RetinaFace stays available via `FACE_DETECTOR`.

### 4.2 Slightly truncated JPEGs failed completely
- **Symptom:** 5 of the 62 test photos failed with `image file is truncated (43 bytes not processed)`.
- **Cause:** Pillow refuses images missing even a few trailing bytes. This happens with interrupted uploads and some camera/phone exports.
- **Fix:** `ImageFile.LOAD_TRUNCATED_IMAGES = True`. All 62 photos then indexed. Failed photos are also retried automatically on the next **Sync**.

### 4.3 Choosing the match threshold
- DeepFace's documented Facenet512 cosine cut-off (distance 0.30, so similarity 0.70) was **too strict** for selfie-vs-candid photos: two genuine photos at 0.65 were missed.
- Measured distributions: same person ≥ 0.64, different people ≤ 0.47. Set the default to **0.60**, with margin on both sides.
- Built `python -m app.cli match <slug> selfie.jpg` so the photographer can see the scores on a real wedding and tune `MATCH_THRESHOLD` without guessing.

### 4.4 DeepFace returns a fake "face" when there is none
- With `enforce_detection=False` (needed so photos without faces don't crash indexing), DeepFace returns the **whole image** as one face with confidence 0.
- **Fix:** filter by `face_confidence ≥ 0.90` and a minimum face size of 40px, which also drops tiny background faces that would add noise.

### 4.5 Thread safety
- TensorFlow models are not safe to call concurrently, so a global lock wraps model calls. Indexing and guest selfies share it, and a guest waits at most one photo's worth of time.
- The Google API client (`httplib2`) is not thread-safe either, so a fresh Drive client is built per call. Downloads run in 6 parallel threads while the face model works on the previous photo.

### 4.6 Non-English names broke download headers
- HTTP headers are Latin-1, so an event name like `Priya & Arjun · திருமணம்` would crash `Content-Disposition`.
- **Fix:** send both an ASCII `filename=` fallback and an RFC 5987 `filename*=UTF-8''…` value. Verified with a Tamil event name.

### 4.7 Deleting an event while it was being indexed
- In-flight download threads could recreate the thumbnail folder after deletion.
- **Fix:** the worker checks the event still exists before each write; if it's gone, it cancels pending work and removes any files written by downloads already in progress.

### 4.8 Test tooling gotchas (not app bugs)
- The DeepFace sample dataset had moved from `tests/dataset` to `tests/unit/dataset`; the first download attempt got 404s.
- In headless-Chrome screenshots at 400px, the page looked cut off. Headless Chrome enforces a ~500px minimum viewport; at 600px the layout was correct.
- The admin list looked empty in headless Chrome because login details in the URL (`user:pass@host`) aren't passed to `fetch()` calls. A small local proxy that adds the login header showed it renders correctly; real browsers reuse the login after the password prompt.

### 4.9 Pushing to GitHub
- The first push failed: the GitHub login stored in the macOS keychain was rejected, and there was no SSH key or `gh` CLI. A personal access token was then used for the push without saving it to the git settings.
- **Lesson:** use `gh auth login` (or SSH keys) on dev machines, and never paste tokens into chats or commit them. Revoke any token that was shared.

---

## 5. Learnings

1. **Benchmark the defaults.** The "most accurate" detector was both slower *and* less accurate on this data. Measuring the real pipeline beat trusting reputation.
2. **Calibrate thresholds on score distributions, not documentation.** Printing the ranked scores made the right threshold obvious, and the CLI keeps that possible on real weddings.
3. **Check licences early.** The most popular face library's weights are non-commercial only; finding that during planning avoided a rewrite later.
4. **Don't over-build infrastructure.** SQLite plus numpy replaced pgvector + Redis + Celery for this scale. One process and one data folder make it easy to run on a free VM.
5. **Privacy by design is mostly simple choices:** don't save the selfie, keep the Drive folder private and serve only matched files, give every event an expiry, use unguessable links, and rate-limit the selfie endpoint.
6. **Real-world files are messy:** truncated JPEGs, EXIF rotation, HEIC from iPhones, sub-folders, Unicode names. Handle them in one place (`faces.open_image`, `sources`).
7. **Stream large things.** Downloading originals on demand and streaming the ZIP keeps memory flat, so a cheap VM can serve big galleries.

---

## 6. Known limitations and next ideas

- **Spoofing:** holding up a printed photo of someone else would return their photos. A per-event PIN reduces the risk; liveness detection would be the proper fix.
- **Hard faces:** strong side profiles, sunglasses, masks and very small faces in big group shots may be missed. "Add another selfie" helps.
- **Single process:** fine for this scale; moving to Postgres + pgvector and a separate worker would be the path to multi-studio scale.
- Not yet tested against a real Google Drive folder and a real wedding (needs the service-account key).
- **Ideas:** watermark downloads, studio branding, "browse people" face clustering for the couple, WhatsApp share, email the gallery link.
