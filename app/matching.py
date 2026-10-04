"""Find the photos a selfie's face appears in."""
import threading

import numpy as np

from . import config, db

_cache: dict[int, tuple[np.ndarray, np.ndarray]] = {}  # event_id -> (embeddings, photo_ids)
_cache_lock = threading.Lock()


def invalidate(event_id: int) -> None:
    with _cache_lock:
        _cache.pop(event_id, None)


def _event_matrix(event_id: int) -> tuple[np.ndarray, np.ndarray]:
    with _cache_lock:
        if event_id in _cache:
            return _cache[event_id]
    with db.get_conn() as conn:
        rows = conn.execute("SELECT photo_id, embedding FROM faces WHERE event_id=?", (event_id,)).fetchall()
    if rows:
        emb = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        pids = np.fromiter((r["photo_id"] for r in rows), dtype=np.int64, count=len(rows))
    else:
        emb, pids = np.zeros((0, 512), dtype=np.float32), np.zeros(0, dtype=np.int64)
    with _cache_lock:
        _cache[event_id] = (emb, pids)
    return emb, pids


def scores(event_id: int, selfie: np.ndarray) -> dict[int, float]:
    """Best similarity per photo, for every photo in the event that has faces."""
    emb, pids = _event_matrix(event_id)
    if len(pids) == 0:
        return {}
    sims = emb @ selfie
    best: dict[int, float] = {}
    for pid, s in zip(pids.tolist(), sims.tolist()):
        if s > best.get(pid, -1.0):
            best[pid] = s
    return best


def match(event_id: int, selfie: np.ndarray, threshold: float | None = None) -> list[tuple[int, float]]:
    t = config.MATCH_THRESHOLD if threshold is None else threshold
    found = [(pid, s) for pid, s in scores(event_id, selfie).items() if s >= t]
    found.sort(key=lambda x: -x[1])
    return found
