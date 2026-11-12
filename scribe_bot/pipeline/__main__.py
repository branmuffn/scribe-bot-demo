"""`python -m scribe_bot.pipeline <command>` on the M14 (until the web app's Review page exists).

    speakers <conversation id>             list its speakers: id, cluster, name, talk time, match score
    enroll <speaker id> <name>             name a speaker; adds their voice to that person's voiceprint
    backlog                                list recordings held back by the first scan (ids and lengths only)
    process <recording id> [...]           queue chosen backlog recordings
    rerun <conversation id> [--speakers N] process again (e.g. --speakers 2 for "This was a 1:1")
Prints ids, names, and numbers only, never transcript text.
"""
import argparse
import sys

from scribe_bot.core import db, settings
from scribe_bot.pipeline import identify, ingest, recording


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m scribe_bot.pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("speakers").add_argument("conversation_id", type=int)
    e = sub.add_parser("enroll")
    e.add_argument("speaker_id", type=int)
    e.add_argument("name")
    sub.add_parser("backlog")
    sub.add_parser("process").add_argument("recording_ids", type=int, nargs="+")
    r = sub.add_parser("rerun")
    r.add_argument("conversation_id", type=int)
    r.add_argument("--speakers", type=int)
    args = ap.parse_args()

    cfg = settings.load()
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    if args.cmd == "speakers":
        for s in conn.execute("SELECT s.id, s.cluster, p.name, s.talk_seconds, s.match_score FROM speaker s "
                              "LEFT JOIN person p ON p.id = s.person_id WHERE s.conversation_id = ? ORDER BY s.cluster",
                              (args.conversation_id,)):
            print(f"speaker {s['id']}: cluster {s['cluster']}, {s['name'] or 'unknown'}, "
                  f"{round(s['talk_seconds'] / 60, 1)} min, match {s['match_score']}")
    elif args.cmd == "enroll":
        print(f"person {identify.enroll(conn, args.speaker_id, name=args.name, cfg=cfg)}")
    elif args.cmd == "backlog":
        for r in conn.execute("SELECT id, duration_s FROM recording WHERE status = 'backlog' ORDER BY id"):
            print(f"recording {r['id']}: {round((r['duration_s'] or 0) / 60, 1)} min")
    elif args.cmd == "process":
        print(f"queued {ingest.process_backlog(conn, args.recording_ids, cfg)}")
    elif args.cmd == "rerun":
        print(f"job {recording.rerun(conn, args.conversation_id, args.speakers)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
