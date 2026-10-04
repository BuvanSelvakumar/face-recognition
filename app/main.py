"""Web server: admin dashboard, guest selfie page, and the photo/download API."""
import base64
import io
import logging
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from zipfile import ZIP_STORED

import qrcode
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, faces, indexer, matching, sources

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
STATIC = Path(__file__).parent / "static"
MAX_SELFIE_BYTES = 15 * 1024 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    if config.ADMIN_PASSWORD == "change-me":
        log.warning("Using the default ADMIN_PASSWORD; set it in .env before going public!")
    threading.Thread(target=faces.warm_up, name="warmup", daemon=True).start()
    indexer.start()
    yield


app = FastAPI(title="Wedding Face Finder", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# ------------------------------------------------------------------ helpers

_basic = HTTPBasic(realm="Photographer admin")


def require_admin(creds: HTTPBasicCredentials = Depends(_basic)) -> None:
    ok_user = secrets.compare_digest(creds.username.encode(), config.ADMIN_USERNAME.encode())
    ok_pass = secrets.compare_digest(creds.password.encode(), config.ADMIN_PASSWORD.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(401, "Wrong username or password", headers={"WWW-Authenticate": "Basic"})


_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = threading.Lock()


def client_ip(request: Request) -> str:
    # Behind Caddy / Cloudflare Tunnel the real IP is the last hop they append.
    fwd = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request) -> None:
    ip, now = client_ip(request), time.time()
    with _hits_lock:
        q = _hits[ip]
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= config.MATCH_RATE_LIMIT_PER_MIN:
            raise HTTPException(429, "Too many tries. Please wait a minute and try again.")
        q.append(now)


def event_url(slug: str) -> str:
    return f"{config.PUBLIC_BASE_URL}/e/{slug}"


def event_json(ev) -> dict:
    return {
        "id": ev["id"],
        "slug": ev["slug"],
        "name": ev["name"],
        "source_type": ev["source_type"],
        "source_ref": ev["source_ref"],
        "pin": ev["pin"],
        "status": ev["status"],
        "status_msg": ev["status_msg"],
        "total_photos": ev["total_photos"],
        "done_photos": ev["done_photos"],
        "face_count": ev["face_count"],
        "created_at": ev["created_at"],
        "expires_at": ev["expires_at"],
        "url": event_url(ev["slug"]),
    }


def get_event_by_slug(slug: str):
    with db.get_conn() as conn:
        ev = conn.execute("SELECT * FROM events WHERE slug=? AND expires_at > ?", (slug, db.now())).fetchone()
    if not ev:
        raise HTTPException(404, "This gallery link is not valid or has expired.")
    return ev


def get_session(sid: str):
    with db.get_conn() as conn:
        s = conn.execute(
            """SELECT s.*, e.name AS event_name, e.slug AS event_slug, e.source_type, e.source_ref
               FROM guest_sessions s JOIN events e ON e.id = s.event_id
               WHERE s.id=? AND s.expires_at > ? AND e.expires_at > ?""",
            (sid, db.now(), db.now()),
        ).fetchone()
    if not s:
        raise HTTPException(404, "This gallery has expired. Scan the QR code again.")
    return s


def session_photo(sid: str, photo_id: int):
    s = get_session(sid)
    with db.get_conn() as conn:
        p = conn.execute(
            """SELECT p.* FROM session_photos sp JOIN photos p ON p.id = sp.photo_id
               WHERE sp.session_id=? AND p.id=?""",
            (sid, photo_id),
        ).fetchone()
    if not p:
        raise HTTPException(404, "Photo not found")
    return s, p


def download_name(name: str) -> str:
    return Path(name).name.replace('"', "")


def disposition(kind: str, filename: str) -> str:
    """Content-Disposition that works for non-English names too."""
    ascii_name = filename.encode("ascii", "ignore").decode().replace('"', "") or "photo"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


# ------------------------------------------------------------------ pages

@app.get("/", include_in_schema=False)
def home():
    return HTMLResponse(
        "<!doctype html><meta name=viewport content='width=device-width'>"
        "<body style='font-family:system-ui;text-align:center;padding:4rem 1rem'>"
        "<h2>📸 Scan the QR code at the event to find your photos.</h2></body>"
    )


@app.get("/admin", include_in_schema=False, dependencies=[Depends(require_admin)])
def admin_page():
    return FileResponse(STATIC / "admin.html")


@app.get("/e/{slug}", include_in_schema=False)
@app.get("/g/{sid}", include_in_schema=False)
def guest_page():
    return FileResponse(STATIC / "guest.html", headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------------ admin API

class EventIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source_type: str = Field(pattern="^(drive|local)$")
    source: str = Field(min_length=1)
    pin: str | None = Field(default=None, max_length=12)
    days: int = Field(default=config.DEFAULT_EVENT_DAYS, ge=1, le=730)


class EventPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    pin: str | None = Field(default=None, max_length=12)
    days: int | None = Field(default=None, ge=1, le=730)


@app.get("/api/admin/info", dependencies=[Depends(require_admin)])
def admin_info():
    return {
        "service_account_email": sources.service_account_email(),
        "allow_local": config.ALLOW_LOCAL_FOLDERS,
        "public_base_url": config.PUBLIC_BASE_URL,
        "default_days": config.DEFAULT_EVENT_DAYS,
    }


@app.get("/api/admin/events", dependencies=[Depends(require_admin)])
def list_events():
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM events ORDER BY created_at DESC").fetchall()
    return [event_json(r) for r in rows]


@app.post("/api/admin/events", dependencies=[Depends(require_admin)])
def create_event(body: EventIn):
    try:
        ref = sources.parse_drive_folder_id(body.source) if body.source_type == "drive" else str(
            Path(body.source).expanduser().resolve()
        )
        sources.check_folder(body.source_type, ref)
    except sources.SourceError as e:
        raise HTTPException(400, str(e))
    slug = secrets.token_urlsafe(6)
    with db.get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO events (slug, name, source_type, source_ref, pin, created_at, expires_at)
               VALUES (?,?,?,?,?,?,?)""",
            (slug, body.name.strip(), body.source_type, ref, (body.pin or "").strip() or None,
             db.now(), db.now() + body.days * 86400),
        )
        event_id = cur.lastrowid
    indexer.enqueue(event_id)
    with db.get_conn() as conn:
        return event_json(conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())


@app.patch("/api/admin/events/{event_id}", dependencies=[Depends(require_admin)])
def update_event(event_id: int, body: EventPatch):
    with db.get_conn() as conn:
        if body.name is not None:
            conn.execute("UPDATE events SET name=? WHERE id=?", (body.name.strip(), event_id))
        if body.pin is not None:
            conn.execute("UPDATE events SET pin=? WHERE id=?", (body.pin.strip() or None, event_id))
        if body.days is not None:
            conn.execute("UPDATE events SET expires_at=? WHERE id=?", (db.now() + body.days * 86400, event_id))
        ev = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not ev:
        raise HTTPException(404)
    return event_json(ev)


@app.post("/api/admin/events/{event_id}/sync", dependencies=[Depends(require_admin)])
def sync_event(event_id: int):
    with db.get_conn() as conn:
        if not conn.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone():
            raise HTTPException(404)
    indexer.enqueue(event_id)
    return {"ok": True}


@app.delete("/api/admin/events/{event_id}", dependencies=[Depends(require_admin)])
def delete_event(event_id: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM events WHERE id=?", (event_id,))
    indexer.delete_event_media(event_id)
    matching.invalidate(event_id)
    return {"ok": True}


def _qr_png(data: str) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=14, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, "PNG")
    return buf.getvalue()


def _admin_event(event_id: int):
    with db.get_conn() as conn:
        ev = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not ev:
        raise HTTPException(404)
    return ev


@app.get("/api/admin/events/{event_id}/qr.png", dependencies=[Depends(require_admin)])
def event_qr(event_id: int):
    ev = _admin_event(event_id)
    return Response(_qr_png(event_url(ev["slug"])), media_type="image/png",
                    headers={"Content-Disposition": disposition("inline", f"QR - {ev['name']}.png")})


@app.get("/api/admin/events/{event_id}/card", dependencies=[Depends(require_admin)])
def event_card(event_id: int):
    ev = _admin_event(event_id)
    url = event_url(ev["slug"])
    qr_b64 = base64.b64encode(_qr_png(url)).decode()
    pin_line = f"<p class=pin>PIN: <b>{ev['pin']}</b></p>" if ev["pin"] else ""
    name = (ev["name"].replace("&", "&amp;").replace("<", "&lt;"))
    return HTMLResponse(f"""<!doctype html><html><head><meta charset=utf-8><title>QR card – {name}</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
 body{{margin:0;background:#f4f1ec;font-family:Inter,system-ui,sans-serif;color:#2b2622}}
 .card{{width:420px;margin:40px auto;background:#fff;border-radius:18px;padding:40px 36px;text-align:center;
        box-shadow:0 10px 40px rgba(0,0,0,.08);border:1px solid #e8e1d6}}
 h1{{font-family:'Cormorant Garamond',serif;font-weight:600;font-size:34px;margin:6px 0 4px}}
 .sub{{color:#8a7d6e;letter-spacing:.18em;text-transform:uppercase;font-size:11px}}
 img{{width:300px;height:300px;margin:24px auto 12px;display:block}}
 .how{{font-size:15px;line-height:1.5}} .pin{{font-size:16px}} .url{{font-size:11px;color:#9a8f82;word-break:break-all}}
 button{{display:block;margin:0 auto 40px;padding:10px 22px;border:0;border-radius:999px;background:#2b2622;color:#fff;font:inherit;cursor:pointer}}
 @media print{{body{{background:#fff}} .card{{box-shadow:none;margin:0 auto}} button{{display:none}}}}
</style></head><body>
<div class=card><div class=sub>Find your photos</div><h1>{name}</h1>
<img src="data:image/png;base64,{qr_b64}" alt="QR code">
<p class=how>📷 Scan with your phone camera,<br>take a selfie, and get every photo you're in.</p>{pin_line}
<p class=url>{url}</p></div>
<button onclick="print()">Print</button></body></html>""")


# ------------------------------------------------------------------ guest API

@app.get("/api/e/{slug}")
def event_public(slug: str):
    ev = get_event_by_slug(slug)
    return {
        "name": ev["name"],
        "ready": ev["status"] == "ready" or ev["face_count"] > 0,
        "indexing": ev["status"] in ("pending", "indexing"),
        "needs_pin": bool(ev["pin"]),
    }


@app.post("/api/e/{slug}/match")
def match_selfie(
    slug: str,
    request: Request,
    selfie: UploadFile = File(...),
    pin: str = Form(""),
    session_id: str = Form(""),
):
    rate_limit(request)
    ev = get_event_by_slug(slug)
    if ev["pin"] and not secrets.compare_digest(pin.strip().encode(), ev["pin"].encode()):
        raise HTTPException(403, "Wrong PIN. It's printed on the QR card.")

    data = selfie.file.read(MAX_SELFIE_BYTES + 1)
    if len(data) > MAX_SELFIE_BYTES:
        raise HTTPException(413, "That photo is too large.")
    try:
        emb = faces.selfie_embedding(data)  # the selfie itself is never saved
    except faces.NoFaceError as e:
        raise HTTPException(422, str(e))
    found = matching.match(ev["id"], emb)

    with db.transaction() as conn:
        sid = None
        if session_id:
            row = conn.execute(
                "SELECT id FROM guest_sessions WHERE id=? AND event_id=? AND expires_at > ?",
                (session_id, ev["id"], db.now()),
            ).fetchone()
            sid = row["id"] if row else None
        if sid is None:
            sid = secrets.token_urlsafe(24)
            conn.execute(
                "INSERT INTO guest_sessions (id, event_id, created_at, expires_at) VALUES (?,?,?,?)",
                (sid, ev["id"], db.now(), db.now() + config.GUEST_SESSION_DAYS * 86400),
            )
        before = conn.execute("SELECT COUNT(*) FROM session_photos WHERE session_id=?", (sid,)).fetchone()[0]
        conn.executemany(
            """INSERT INTO session_photos (session_id, photo_id, score) VALUES (?,?,?)
               ON CONFLICT(session_id, photo_id) DO UPDATE SET score = MAX(score, excluded.score)""",
            [(sid, pid, score) for pid, score in found],
        )
        after = conn.execute("SELECT COUNT(*) FROM session_photos WHERE session_id=?", (sid,)).fetchone()[0]
    log.info("event %s: selfie matched %d photos (%d new)", ev["id"], len(found), after - before)
    return {"session_id": sid, "matched": len(found), "added": after - before, "total": after}


@app.get("/api/s/{sid}")
def session_gallery(sid: str):
    s = get_session(sid)
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT p.id, p.name, p.width, p.height FROM session_photos sp JOIN photos p ON p.id = sp.photo_id
               WHERE sp.session_id=? AND p.status='done' ORDER BY p.name""",
            (sid,),
        ).fetchall()
    base = f"/api/s/{sid}/photo"
    return {
        "event_name": s["event_name"],
        "event_slug": s["event_slug"],
        "expires_at": s["expires_at"],
        "photos": [
            {
                "id": r["id"],
                "name": download_name(r["name"]),
                "width": r["width"],
                "height": r["height"],
                "thumb": f"{base}/{r['id']}/thumb",
                "preview": f"{base}/{r['id']}/preview",
                "download": f"{base}/{r['id']}/original",
            }
            for r in rows
        ],
    }


@app.get("/api/s/{sid}/photo/{photo_id}/{kind}")
def session_photo_file(sid: str, photo_id: int, kind: str):
    s, p = session_photo(sid, photo_id)
    if kind in ("thumb", "preview"):
        path = indexer.media_path(s["event_id"], kind, photo_id)
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
    if kind == "original":
        try:
            data = sources.download(s["source_type"], s["source_ref"], p["source_id"])
        except Exception:
            log.exception("original download failed")
            raise HTTPException(502, "Couldn't fetch the original right now. Please try again.")
        return Response(data, media_type=p["mime"] or "image/jpeg",
                        headers={"Content-Disposition": disposition("attachment", download_name(p["name"]))})
    raise HTTPException(404)


@app.get("/api/s/{sid}/zip")
def session_zip(sid: str):
    from zipstream import ZipStream

    s = get_session(sid)
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT p.source_id, p.name FROM session_photos sp JOIN photos p ON p.id = sp.photo_id
               WHERE sp.session_id=? AND p.status='done' ORDER BY p.name""",
            (sid,),
        ).fetchall()
    if not rows:
        raise HTTPException(404, "No photos to download")

    def lazy(source_id: str):
        # each original is fetched only when the ZIP stream reaches it
        yield sources.download(s["source_type"], s["source_ref"], source_id)

    zs = ZipStream(compress_type=ZIP_STORED)  # JPEGs are already compressed
    used: set[str] = set()
    for r in rows:
        name = download_name(r["name"])
        stem, suffix, i = Path(name).stem, Path(name).suffix, 1
        while name in used:
            i += 1
            name = f"{stem} ({i}){suffix}"
        used.add(name)
        zs.add(lazy(r["source_id"]), name)
    return StreamingResponse(zs, media_type="application/zip",
                             headers={"Content-Disposition": disposition("attachment", f"{s['event_name']} - my photos.zip")})


@app.get("/healthz", include_in_schema=False)
def health():
    return {"ok": True}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return RedirectResponse("/static/favicon.svg")
