"""Background worker: pulls photos from the event's folder, saves thumbnails, and
stores an embedding for every face it finds. Re-running ("Sync") only processes
photos that are new since the last run, and drops photos removed from the folder."""
import logging
import queue
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from . import config, db, faces, matching, sources

log = logging.getLogger("indexer")

_queue: "queue.Queue[int]" = queue.Queue()
_queued: set[int] = set()
_queued_lock = threading.Lock()
PREFETCH = 6  # photos downloaded/decoded ahead while the face model works


def media_path(event_id: int, kind: str, photo_id: int) -> Path:
    return config.MEDIA_DIR / str(event_id) / kind / f"{photo_id}.jpg"


def delete_event_media(event_id: int) -> None:
    shutil.rmtree(config.MEDIA_DIR / str(event_id), ignore_errors=True)


def enqueue(event_id: int) -> None:
    with _queued_lock:
        if event_id in _queued:
            return
        _queued.add(event_id)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE events SET status='pending', status_msg='Waiting in queue' WHERE id=?", (event_id,)
        )
    _queue.put(event_id)


def _set_status(event_id: int, status: str, msg: str | None = None) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE events SET status=?, status_msg=? WHERE id=?", (status, msg, event_id))


def _refresh_counts(conn, event_id: int) -> None:
    conn.execute(
        """UPDATE events SET
             total_photos = (SELECT COUNT(*) FROM photos WHERE event_id=:e),
             done_photos  = (SELECT COUNT(*) FROM photos WHERE event_id=:e AND status != 'pending'),
             face_count   = (SELECT COUNT(*) FROM faces  WHERE event_id=:e)
           WHERE id=:e""",
        {"e": event_id},
    )


def _prepare(source_type: str, ref: str, photo: dict) -> dict:
    """Runs in a helper thread: download + decode + make thumbnail/preview."""
    data = sources.download(source_type, ref, photo["source_id"])
    img = faces.open_image(data)
    event_id, pid = photo["event_id"], photo["id"]
    for kind, size, quality in (("thumb", config.THUMB_MAX_SIDE, 80), ("preview", config.PREVIEW_MAX_SIDE, 86)):
        path = media_path(event_id, kind, pid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(faces.to_jpeg(faces.resized(img, size), quality))
    return {"img": faces.resized(img, config.DETECT_MAX_SIDE), "width": img.width, "height": img.height}


def sync_event(event_id: int) -> None:
    with db.get_conn() as conn:
        ev = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not ev:
        return
    _set_status(event_id, "indexing", "Listing photos in folder…")
    files = sources.list_images(ev["source_type"], ev["source_ref"])
    current = {f.source_id for f in files}

    with db.transaction() as conn:
        existing = {
            r["source_id"]: r["id"]
            for r in conn.execute("SELECT id, source_id FROM photos WHERE event_id=?", (event_id,))
        }
        removed = [pid for sid, pid in existing.items() if sid not in current]
        for pid in removed:
            conn.execute("DELETE FROM photos WHERE id=?", (pid,))
        conn.executemany(
            "INSERT OR IGNORE INTO photos (event_id, source_id, name, mime) VALUES (?,?,?,?)",
            [(event_id, f.source_id, f.name, f.mime) for f in files],
        )
        # retry photos that failed last time
        conn.execute("UPDATE photos SET status='pending', error=NULL WHERE event_id=? AND status='error'", (event_id,))
        _refresh_counts(conn, event_id)
        pending = [
            dict(r)
            for r in conn.execute(
                "SELECT id, event_id, source_id, name FROM photos WHERE event_id=? AND status='pending' ORDER BY name",
                (event_id,),
            )
        ]
    for pid in removed:
        for kind in ("thumb", "preview"):
            media_path(event_id, kind, pid).unlink(missing_ok=True)

    total = len(files)
    log.info("event %s: %d photos, %d to process, %d removed", event_id, total, len(pending), len(removed))
    started = time.time()
    errors = 0
    deleted = False

    with ThreadPoolExecutor(max_workers=PREFETCH) as pool:
        futures: list[tuple[dict, Future]] = []
        it = iter(pending)

        def top_up():
            while len(futures) < PREFETCH:
                p = next(it, None)
                if p is None:
                    return
                futures.append((p, pool.submit(_prepare, ev["source_type"], ev["source_ref"], p)))

        top_up()
        n = 0
        while futures:
            photo, fut = futures.pop(0)
            top_up()
            n += 1
            try:
                prepared = fut.result()
                found = faces.detect_faces(prepared["img"])
                with db.transaction() as conn:
                    if not conn.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone():
                        deleted = True
                        for _, f in futures:
                            f.cancel()
                        futures.clear()
                        continue
                    conn.execute("DELETE FROM faces WHERE photo_id=?", (photo["id"],))
                    conn.executemany(
                        "INSERT INTO faces (event_id, photo_id, embedding, x, y, w, h, confidence) VALUES (?,?,?,?,?,?,?,?)",
                        [(event_id, photo["id"], f.embedding.tobytes(), f.x, f.y, f.w, f.h, f.confidence) for f in found],
                    )
                    conn.execute(
                        "UPDATE photos SET status='done', width=?, height=? WHERE id=?",
                        (prepared["width"], prepared["height"], photo["id"]),
                    )
            except Exception as e:  # one bad photo shouldn't stop the wedding
                errors += 1
                log.warning("photo %s (%s) failed: %s", photo["id"], photo["name"], e)
                with db.get_conn() as conn:
                    conn.execute("UPDATE photos SET status='error', error=? WHERE id=?", (str(e)[:500], photo["id"]))

            if not deleted and (n % 5 == 0 or not futures):
                elapsed = time.time() - started
                eta = elapsed / n * (len(pending) - n)
                with db.get_conn() as conn:
                    _refresh_counts(conn, event_id)
                    conn.execute(
                        "UPDATE events SET status_msg=? WHERE id=?",
                        (f"Processing {n}/{len(pending)} new photos · ~{int(eta // 60)}m {int(eta % 60)}s left", event_id),
                    )

    if deleted:  # event was deleted mid-run; remove files written by in-flight downloads
        log.info("event %s deleted during indexing; stopped", event_id)
        delete_event_media(event_id)
        return

    with db.get_conn() as conn:
        _refresh_counts(conn, event_id)
        ev = conn.execute("SELECT total_photos, face_count FROM events WHERE id=?", (event_id,)).fetchone()
    matching.invalidate(event_id)
    msg = f"{ev['total_photos']} photos · {ev['face_count']} faces"
    if errors:
        msg += f" · {errors} couldn't be read (Sync retries them)"
    _set_status(event_id, "ready", msg)
    log.info("event %s ready in %.0fs: %s", event_id, time.time() - started, msg)


def _worker() -> None:
    while True:
        event_id = _queue.get()
        with _queued_lock:
            _queued.discard(event_id)
        try:
            sync_event(event_id)
        except Exception as e:
            log.exception("event %s failed", event_id)
            _set_status(event_id, "error", str(e)[:500])
        finally:
            _queue.task_done()


def _cleanup_loop() -> None:
    """Deletes expired events (photos, faces, thumbnails) and expired guest sessions."""
    while True:
        try:
            with db.get_conn() as conn:
                expired = [r["id"] for r in conn.execute("SELECT id FROM events WHERE expires_at < ?", (db.now(),))]
                for eid in expired:
                    conn.execute("DELETE FROM events WHERE id=?", (eid,))
                conn.execute("DELETE FROM guest_sessions WHERE expires_at < ?", (db.now(),))
            for eid in expired:
                delete_event_media(eid)
                matching.invalidate(eid)
                log.info("deleted expired event %s", eid)
        except Exception:
            log.exception("cleanup failed")
        time.sleep(3600)


def start() -> None:
    threading.Thread(target=_worker, name="indexer", daemon=True).start()
    threading.Thread(target=_cleanup_loop, name="cleanup", daemon=True).start()
    # resume anything interrupted by a restart
    with db.get_conn() as conn:
        unfinished = [r["id"] for r in conn.execute("SELECT id FROM events WHERE status IN ('pending','indexing')")]
    for eid in unfinished:
        enqueue(eid)
