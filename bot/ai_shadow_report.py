"""Read-only operational report; never sends data or changes classifications."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def report(db_path: str, limit: int = 30) -> dict:
    with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        counts = [
            dict(row)
            for row in db.execute(
                "SELECT state,category,COUNT(*) AS count FROM ai_shadow GROUP BY state,category"
            )
        ]
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT ticket_id,source,state,category,service,error_code,model,prompt_version,"
                "queued_at,input_tokens,output_tokens,latency_ms FROM ai_shadow "
                "ORDER BY queued_at DESC,ticket_id DESC LIMIT ?",
                (limit,),
            )
        ]
    return {"mode": "shadow", "counts": counts, "recent": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="/var/lib/glpi-tgbot/glpi-tgbot.sqlite3")
    parser.add_argument("--limit", type=int, choices=range(1, 501), default=30)
    args = parser.parse_args()
    print(json.dumps(report(args.db, args.limit), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
