"""Command-line helpers.

  python -m app.cli list                                  # events and their status
  python -m app.cli index  <slug>                         # (re)index an event in the foreground
  python -m app.cli match  <slug> path/to/selfie.jpg      # show similarity scores, to tune MATCH_THRESHOLD
"""
import argparse
import logging
import sys
from pathlib import Path

from . import config, db, faces, indexer, matching


def _event(slug: str):
    with db.get_conn() as conn:
        ev = conn.execute("SELECT * FROM events WHERE slug=?", (slug,)).fetchone()
    if not ev:
        sys.exit(f"No event with slug {slug!r}. Run `python -m app.cli list`.")
    return ev


def cmd_list(_):
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM events ORDER BY created_at DESC").fetchall()
    if not rows:
        print("No events yet.")
    for r in rows:
        print(f"{r['slug']:10}  {r['status']:9}  {r['done_photos']:>5}/{r['total_photos']:<5} photos  "
              f"{r['face_count']:>6} faces  {r['name']}")


def cmd_index(args):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    indexer.sync_event(_event(args.slug)["id"])


def cmd_match(args):
    ev = _event(args.slug)
    emb = faces.selfie_embedding(Path(args.selfie).read_bytes())
    scored = sorted(matching.scores(ev["id"], emb).items(), key=lambda x: -x[1])
    with db.get_conn() as conn:
        names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM photos WHERE event_id=?", (ev["id"],))}
    t = config.MATCH_THRESHOLD
    print(f"Threshold = {t}   (photos at or above it are shown to the guest)\n")
    shown = 0
    for i, (pid, s) in enumerate(scored[: args.top]):
        if shown == 0 and s < t:
            print("   ---------------- threshold ----------------")
            shown = 1
        mark = "✔" if s >= t else " "
        print(f" {mark} {s:.3f}  {names.get(pid, pid)}")
    print(f"\n{sum(1 for _, s in scored if s >= t)} photos match out of {len(scored)} with faces.")
    print("Tip: if wrong people sit just above the line, raise MATCH_THRESHOLD; "
          "if the person's own photos sit just below it, lower it.")


def main():
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    s = sub.add_parser("index"); s.add_argument("slug"); s.set_defaults(fn=cmd_index)
    s = sub.add_parser("match"); s.add_argument("slug"); s.add_argument("selfie")
    s.add_argument("--top", type=int, default=40); s.set_defaults(fn=cmd_match)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
