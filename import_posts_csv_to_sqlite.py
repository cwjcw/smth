#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser(description="Import posts CSV into sqlite posts table")
    p.add_argument("--csv", type=Path, required=True)
    p.add_argument("--db", type=Path, default=Path("data/smth_stock.db"))
    p.add_argument("--batch", type=int, default=2000)
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
          post_id INTEGER PRIMARY KEY,
          author TEXT,
          board TEXT,
          title TEXT,
          post_time TEXT,
          body TEXT
        )
        """
    )
    sql = """
    INSERT INTO posts(post_id,author,board,title,post_time,body)
    VALUES(?,?,?,?,?,?)
    ON CONFLICT(post_id) DO UPDATE SET
      author=excluded.author,
      board=excluded.board,
      title=excluded.title,
      post_time=excluded.post_time,
      body=excluded.body
    """

    buf = []
    with args.csv.open("r", newline="", encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        for row in r:
            buf.append(
                (
                    int(row["post_id"]),
                    row["author"],
                    row["board"],
                    row["title"],
                    row["post_time"],
                    row["body"],
                )
            )
            if len(buf) >= args.batch:
                conn.executemany(sql, buf)
                conn.commit()
                buf = []
    if buf:
        conn.executemany(sql, buf)
        conn.commit()
    conn.close()
    print("import_done")


if __name__ == "__main__":
    main()

