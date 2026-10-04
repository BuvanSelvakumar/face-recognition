# Wedding Face Finder

Upload a wedding's photos to Google Drive, and guests scan a QR code, take a selfie, and see **only the photos they're in**. They can view or download them, one at a time or all together as a ZIP.

Everything used is free and open source: FastAPI, DeepFace (Facenet512 + YuNet), SQLite, and a plain HTML/JS frontend.

```
You ──upload──> Google Drive folder
                     │  (shared read-only with the app)
Admin page ──> app indexes every face (≈0.3 s/photo on a laptop)
                     │
            QR card printed for the venue
                     │
Guest phone ──scan──> selfie ──> only their photos ──> view / download / ZIP
```

---

## 1. Run it on your laptop (5 minutes)

Requires Python 3.11+ (macOS: `brew install python` if needed).

```bash
cd agent-classification
cp .env.example .env        # then open .env and set ADMIN_PASSWORD
./run.sh                    # first run installs everything (~1 GB, a few minutes)
```

Open **http://localhost:8000/admin** and log in (username `admin`, your password).

The first time photos are indexed, the face model weights (~100 MB) download automatically.

> Quick try without Google Drive: on the admin page choose **Folder on this computer** and paste a
> folder path, e.g. `/Users/you/Pictures/Test-Wedding`.

---

## 2. Google Drive setup (once)

The app reads your Drive through a **service account**, a free Google "robot" account. You share each wedding folder with it as *Viewer*, and it can never change or delete anything.

1. Go to <https://console.cloud.google.com/> and create a project (e.g. "wedding-photos").
2. **APIs & Services → Library** → search **Google Drive API** → **Enable**.
3. **APIs & Services → Credentials → Create credentials → Service account**. Give it any name, then click **Done**.
4. Open the new service account → **Keys → Add key → Create new key → JSON**. A file downloads.
5. Rename that file to `service-account.json` and put it in this project folder (next to `run.sh`).
6. Restart the app. The admin page now shows the service account's email, something like
   `wedding-photos@your-project.iam.gserviceaccount.com`.

**For every wedding:** in Google Drive, right-click the wedding folder → **Share** → paste that email → *Viewer* → Send.

---

## 3. Using it (each wedding)

1. Upload the photos to a Drive folder (sub-folders are fine) and share it with the service-account email.
2. In **/admin** click **New event**, enter a name, paste the folder link and optionally set a **PIN**.
3. Wait for indexing. Progress shows live; 1,000 photos take about 5–10 minutes on a laptop.
4. Click **🖨 QR card** to get a printable card with the QR code, or **⬇ QR image** for your own designs.
5. Uploaded more photos later? Click **↻ Sync new photos**. Only new photos are processed, and deleted ones are removed.

**What guests see:** scan → tick consent → take a selfie → their gallery. They can tap **+ Add another selfie** if some photos are missing (it merges results), **Download all** for a ZIP of originals, or bookmark the page to come back later.

---

## 4. Letting guests reach it

Guests' phones need to reach the app over the internet. Pick one option:

### Option A: Laptop + Cloudflare Tunnel (free, quick, laptop must stay on)

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:8000
```
It prints a URL like `https://random-words.trycloudflare.com`. Put it in `.env` as `PUBLIC_BASE_URL`, restart the app, and print the QR card. The quick-tunnel URL changes every time you restart it. For a permanent address, set up a [named tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/) with your own domain.

### Option B: Oracle Cloud Free Tier (recommended, always on, ₹0)

1. Sign up at <https://www.oracle.com/cloud/free/>. Create a VM: **Ampere A1 (ARM), 4 OCPU / 24 GB**, Ubuntu 22.04+.
   Tip: upgrade the account to *Pay As You Go*. It still costs ₹0 inside free limits, and stops Oracle from reclaiming idle VMs.
2. In the VM's subnet **Security List**, allow inbound TCP **80** and **443**. On the VM itself, run:
   ```bash
   sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT && sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
   sudo netfilter-persistent save
   ```
3. Point a domain at the VM's public IP. A free subdomain from <https://www.duckdns.org> works.
4. On the VM:
   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   git clone https://github.com/BuvanSelvakumar/face-recognition.git && cd face-recognition
   cp .env.example .env     # set ADMIN_PASSWORD and PUBLIC_BASE_URL=https://your.domain
   # copy service-account.json here (e.g. with scp)
   sudo DOMAIN=your.domain docker compose --profile https up -d --build
   ```
   Caddy gets a free HTTPS certificate automatically. Admin is at `https://your.domain/admin`.

To update later: `git pull && sudo DOMAIN=your.domain docker compose --profile https up -d --build`.

---

## 5. Tuning accuracy

Every face gets a similarity score from 0 to 1, and photos scoring at least `MATCH_THRESHOLD` (default **0.60**) are shown. To check on a real wedding:

```bash
.venv/bin/python -m app.cli list                         # shows each event's slug
.venv/bin/python -m app.cli match <slug> my-selfie.jpg   # lists photos with scores
```

- If guests see **other people's** photos, raise the threshold to 0.63–0.65.
- If guests **miss their own** photos, lower it to 0.55–0.57.

Set the value in `.env` and restart. In testing, the same person scored 0.64–0.77 and different people scored below 0.47.

`FACE_DETECTOR=yunet` is the default (fast, ~0.2 s/photo). `retinaface` sometimes finds a few more tiny or side-on faces but is ~25× slower.

---

## 6. Privacy & safety (built in)

- **Selfies are never stored.** They're processed in memory and discarded.
- Guests must tick a consent box before taking a selfie. Face data is biometric data under India's DPDP Act.
- **Your Drive folder stays private.** Guests only ever receive photos matched to their face, served through the app.
- **Auto-delete:** each event's face data and thumbnails are deleted after N days (set per event; *Extend* to keep longer). Your Drive is never touched.
- An optional **PIN** per event stops people who only have the link. The selfie endpoint is rate-limited (15 tries/min per device).
- Guest gallery links are long random tokens that expire after 30 days.

Known limit: someone holding up another person's photo to the camera would get that person's photos. Use a PIN for high-profile events.

---

## Project layout

```
app/
  main.py       web server: admin API, guest API, QR code, downloads/ZIP
  indexer.py    background worker: download → thumbnails → faces → database
  faces.py      DeepFace wrapper (detect + 512-d embedding)
  matching.py   selfie vs event faces (cosine similarity in numpy)
  sources.py    Google Drive + local-folder readers
  db.py         SQLite schema
  cli.py        list / index / match helpers
  static/       admin.html, guest.html, JS, CSS
Dockerfile, docker-compose.yml, deploy/Caddyfile   cloud deployment
data/           database + thumbnails (created at runtime; back this up)
```

Run with **one** worker process only, because the indexer runs inside the web process.
