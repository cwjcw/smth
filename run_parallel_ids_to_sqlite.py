#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import telnetlib3

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
DT_RE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(\d{1,2})\s+\d{2}:\d{2}:\d{2}\s+(\d{4})\b"
)
MONTH = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06", "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12"}

K_AUTHOR = "发信人:"
K_BOARD = "信区:"
K_TITLE = "题:"
K_INNER = "站内"
K_QUOTE = "【 "


@dataclass
class Account:
    username: str
    password: str


def clean(s: str) -> str:
    return ANSI_RE.sub("", s).replace("\r", "")


async def rd(reader: telnetlib3.TelnetReader, sec: float = 0.8) -> str:
    end = time.time() + sec
    out: list[str] = []
    while time.time() < end:
        try:
            d = await asyncio.wait_for(reader.read(1), timeout=0.1)
        except asyncio.TimeoutError:
            continue
        if d:
            out.append(d)
            end = min(end + 0.08, time.time() + 0.35)
    return clean("".join(out))


async def send(writer: telnetlib3.TelnetWriter, s: str) -> None:
    writer.write(s)
    await writer.drain()


def parse_post(raw: str, pid: int) -> tuple | None:
    author = ""
    board = ""
    title = ""
    post_time = ""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    for ln in lines:
        if (not author or not board) and K_AUTHOR in ln and K_BOARD in ln:
            author = ln.split(K_AUTHOR, 1)[1].split(K_BOARD, 1)[0].strip()
            board = ln.split(K_BOARD, 1)[1].strip()
        if not title and ("标" in ln) and K_TITLE in ln:
            title = ln.split(K_TITLE, 1)[1].strip()

    m = DT_RE.search(raw)
    if m:
        mon, day, year = m.group(1), m.group(2), m.group(3)
        post_time = f"{year}-{MONTH[mon]}-{day.zfill(2)}"

    body = ""
    if K_INNER in raw:
        rest = raw.split(K_INNER, 1)[1]
        stop = len(rest)
        for marker in ["\n" + K_QUOTE, "\n--\n", "\n--"]:
            idx = rest.find(marker)
            if idx >= 0:
                stop = min(stop, idx)
        body = rest[:stop].strip()

    if not (author and board and title and post_time):
        return None
    if "(" in author:
        author = author.split("(", 1)[0].strip()
    if author.endswith(","):
        author = author[:-1].strip()
    return (pid, author, board, title, post_time, body)


def normalize_title(s: str) -> str:
    t = s.strip()
    t = re.sub(r"^[●\*\s]+", "", t)
    return t


def make_content_hash(author: str, title: str, post_time: str, body: str) -> str:
    base = "\n".join(
        [
            author.strip(),
            title.strip(),
            post_time.strip(),
            body.strip(),
        ]
    )
    return hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()


def read_checkpoint(path: Path) -> Optional[int]:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        return int(raw)
    except Exception:
        return None


def write_checkpoint(path: Path, post_id: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(str(post_id), encoding="utf-8")
    tmp.replace(path)


async def enter_stock(writer: telnetlib3.TelnetWriter, reader: telnetlib3.TelnetReader, account: Account, board: str) -> None:
    await rd(reader, 2.8)
    await send(writer, account.username + "\r\n")
    await rd(reader, 1.2)
    await send(writer, account.password + "\r\n")
    t = await rd(reader, 1.8)
    if "窗口数过多" in t or "踢除" in t:
        await send(writer, "1\r\n")
        await rd(reader, 1.2)

    await send(writer, "\r\n")
    await rd(reader, 0.8)
    await send(writer, "\r\n")
    await rd(reader, 0.8)
    for _ in range(4):
        await send(writer, " ")
        await rd(reader, 0.8)

    await send(writer, "f")
    await rd(reader, 0.6)
    await send(writer, "\r\n")
    await rd(reader, 0.8)
    await send(writer, "\r\n")
    await rd(reader, 1.2)


async def read_by_id(writer: telnetlib3.TelnetWriter, reader: telnetlib3.TelnetReader, pid: int, short_wait: float, long_wait: float) -> tuple[str | None, str]:
    await send(writer, f"{pid}")
    await rd(reader, short_wait)
    await send(writer, "\r\n")
    await rd(reader, long_wait)
    await send(writer, "\r\n")
    t_open = await rd(reader, long_wait)

    chunks: list[str] = [t_open] if t_open else []
    merged = "\n".join(chunks)

    if any(x in merged for x in ["没有这篇", "不存在", "找不到", "No such"]):
        await send(writer, "\x1b[D")
        await rd(reader, short_wait)
        return None, "miss"
    if "FROM:" in merged or "[阅读文章]" in merged:
        await send(writer, "\x1b[D")
        await rd(reader, short_wait)
        return merged, "ok"

    for _ in range(20):
        t = await rd(reader, short_wait)
        if t:
            chunks.append(t)
        merged = "\n".join(chunks)

        if any(x in merged for x in ["没有这篇", "不存在", "找不到", "No such"]):
            await send(writer, "\x1b[D")
            await rd(reader, short_wait)
            return None, "miss"
        if "FROM:" in merged or "[阅读文章]" in merged:
            await send(writer, "\x1b[D")
            await rd(reader, short_wait)
            return merged, "ok"
        if ("下面还有喔" in merged) or ("按空格" in merged) or ("(SPACE)" in merged):
            await send(writer, " ")
            continue

    await send(writer, "\x1b[D")
    await rd(reader, short_wait)
    return "\n".join(chunks), "partial"


def parse_accounts(raw: str) -> list[Account]:
    out: list[Account] = []
    for pair in raw.split(","):
        p = pair.strip()
        if not p or ":" not in p:
            continue
        u, pw = p.split(":", 1)
        out.append(Account(u.strip(), pw.strip()))
    return out



async def worker(
    wid: int,
    account: Account,
    board: str,
    ids: list[int],
    out_q: asyncio.Queue,
    retries: int,
    short_wait: float,
    long_wait: float,
    progress: dict,
    quiet: bool,
) -> None:
    reader = None
    writer = None
    ok = miss = fail = 0
    try:
        for pid in ids:
            if progress.get("stop"):
                break
            if pid > progress["max_seen_id"]:
                progress["max_seen_id"] = pid
            success = False
            for i in range(retries + 1):
                try:
                    if writer is None:
                        await asyncio.sleep(random.uniform(0.03, 0.2))
                        reader, writer = await telnetlib3.open_connection(
                            "bbs.mysmth.net",
                            23,
                            connect_minwait=0.1,
                            connect_maxwait=1.0,
                            encoding="gb18030",
                            encoding_errors="ignore",
                            shell=None,
                        )
                        await enter_stock(writer, reader, account, board)
                    raw, st = await read_by_id(writer, reader, pid, short_wait, long_wait)
                except Exception as e:
                    if not quiet:
                        print(f"worker#{wid} reconnect on pid={pid}: {e}")
                    try:
                        if writer is not None:
                            writer.close()
                    except Exception:
                        pass
                    reader, writer = None, None
                    backoff = 0.25 * (2 ** min(i, 3)) + random.uniform(0, 0.25)
                    await asyncio.sleep(backoff)
                    continue

                if st == "miss" or raw is None:
                    miss += 1
                    success = True
                    break
                rec = parse_post(raw, pid)
                if rec:
                    await out_q.put(rec)
                    ok += 1
                    if progress.get("until_title_norm"):
                        title_norm = normalize_title(rec[3])
                        if title_norm == progress["until_title_norm"]:
                            progress["stop"] = True
                            progress["stop_post_id"] = pid
                            progress["stop_title"] = rec[3]
                    success = True
                    break

            if not success:
                fail += 1

            progress["done"] += 1
            if (not quiet) and progress["done"] % 50 == 0:
                print(f"progress done={progress['done']} ok={progress['ok_base'] + ok} miss={progress['miss_base'] + miss} fail={progress['fail_base'] + fail}")
    finally:
        try:
            if writer is not None:
                writer.close()
        except Exception:
            pass

    progress["ok_base"] += ok
    progress["miss_base"] += miss
    progress["fail_base"] += fail
    if not quiet:
        print(f"worker#{wid} done ok={ok} miss={miss} fail={fail}")


def split_ids_chunk(ids: list[int], n: int) -> list[list[int]]:
    buckets = [[] for _ in range(n)]
    if n <= 0:
        return buckets
    size = (len(ids) + n - 1) // n
    for i in range(n):
        s = i * size
        e = min(len(ids), (i + 1) * size)
        if s < len(ids):
            buckets[i] = ids[s:e]
    return buckets


def split_ids_round_robin(ids: list[int], n: int) -> list[list[int]]:
    buckets = [[] for _ in range(n)]
    for i, pid in enumerate(ids):
        buckets[i % n].append(pid)
    return buckets


def init_sqlite(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          post_id INTEGER,
          content_hash TEXT UNIQUE,
          author TEXT,
          board TEXT,
          title TEXT,
          post_time TEXT,
          body TEXT,
          first_seen_at TEXT,
          last_seen_at TEXT,
          seen_count INTEGER DEFAULT 1
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_post_id ON posts(post_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_post_time ON posts(post_time)")
    return conn


def upsert_posts(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    if not rows:
        return 0
    sql = """
    INSERT INTO posts(
      post_id, content_hash, author, board, title, post_time, body, first_seen_at, last_seen_at, seen_count
    )
    VALUES(?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(content_hash) DO UPDATE SET
      last_seen_at=excluded.last_seen_at,
      seen_count=posts.seen_count + 1
    """
    before = conn.total_changes
    now = datetime.now(timezone.utc).isoformat()
    payload = [
        (
            post_id,
            make_content_hash(author, title, post_time, body),
            author,
            board,
            title,
            post_time,
            body,
            now,
            now,
            1,
        )
        for (post_id, author, board, title, post_time, body) in rows
    ]
    conn.executemany(sql, payload)
    conn.commit()
    # sqlite total_changes includes both inserts and updates.
    # Keep this as "affected rows" metric.
    return conn.total_changes - before


async def sink_writer(
    q: asyncio.Queue,
    db_path: Path,
    sqlite_batch: int,
    csv_path: Path | None,
    csv_flush_every: int,
) -> int:
    conn = init_sqlite(db_path)
    csv_file = None
    csv_writer_obj = None
    if csv_path is not None:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_file = csv_path.open("a", newline="", encoding="utf-8-sig")
        csv_writer_obj = csv.writer(csv_file)
        if csv_path.stat().st_size == 0:
            csv_writer_obj.writerow(["post_id", "author", "board", "title", "post_time", "body"])

    sqlite_buf: list[tuple] = []
    csv_n = 0
    total = 0
    try:
        while True:
            item = await q.get()
            if item is None:
                break
            sqlite_buf.append(item)
            if csv_writer_obj is not None:
                csv_writer_obj.writerow(item)
                csv_n += 1
                if csv_n >= csv_flush_every:
                    csv_file.flush()
                    csv_n = 0
            if len(sqlite_buf) >= sqlite_batch:
                total += upsert_posts(conn, sqlite_buf)
                sqlite_buf = []
        if sqlite_buf:
            total += upsert_posts(conn, sqlite_buf)
        if csv_file is not None:
            csv_file.flush()
    finally:
        conn.close()
        if csv_file is not None:
            csv_file.close()
    return total


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Parallel SMTH ID scraper -> sqlite")
    p.add_argument("--start-id", type=int, required=True)
    p.add_argument("--end-id", type=int, default=None)
    p.add_argument("--board", default=os.getenv("SMTH_BOARD", "stock"))
    p.add_argument("--csv", type=Path, default=Path("data/smth_stock_posts.csv"))
    p.add_argument("--no-csv", action="store_true")
    p.add_argument("--db", type=Path, default=Path("data/smth_stock.db"))
    p.add_argument("--sqlite-batch", type=int, default=2000)
    p.add_argument("--accounts", default=os.getenv("SMTH_ACCOUNTS", ""))
    p.add_argument("--sessions-per-account", type=int, default=1)
    p.add_argument("--flush-every", type=int, default=500, help="CSV flush interval when CSV is enabled")
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--short-wait", type=float, default=0.12)
    p.add_argument("--long-wait", type=float, default=0.45)
    p.add_argument("--split-mode", choices=["round_robin", "chunk"], default="chunk")
    p.add_argument("--until-title", default=None, help="持续抓取直到命中该标题（忽略 --end-id）")
    p.add_argument("--batch-size", type=int, default=300, help="until 模式每轮分配的ID数量")
    p.add_argument("--checkpoint-file", type=Path, default=Path("data/smth_stock.last_id"))
    p.add_argument("--no-resume", action="store_true", help="忽略 checkpoint，从 --start-id 开始")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    if args.until_title is None:
        if args.end_id is None:
            raise SystemExit("missing --end-id when --until-title is not set")
        if args.end_id < args.start_id:
            raise SystemExit("end-id must be >= start-id")
    elif args.batch_size <= 0:
        raise SystemExit("batch-size must be > 0")
    accounts = parse_accounts(args.accounts)
    if not accounts:
        raise SystemExit("missing accounts. set --accounts or SMTH_ACCOUNTS")

    if not args.no_resume:
        last_id = read_checkpoint(args.checkpoint_file)
        if last_id is not None and last_id >= args.start_id:
            args.start_id = last_id + 1
            if not args.quiet:
                print(f"resume from checkpoint last_id={last_id} -> start_id={args.start_id}")

    workers_n = len(accounts) * args.sessions_per_account

    q: asyncio.Queue = asyncio.Queue(maxsize=5000)
    csv_path = None if args.no_csv else args.csv
    writer_task = asyncio.create_task(
        sink_writer(q, args.db, args.sqlite_batch, csv_path, args.flush_every)
    )
    progress = {
        "done": 0,
        "ok_base": 0,
        "miss_base": 0,
        "fail_base": 0,
        "stop": False,
        "stop_post_id": None,
        "stop_title": None,
        "until_title_norm": normalize_title(args.until_title) if args.until_title else None,
        "max_seen_id": args.start_id - 1,
    }

    def build_tasks(groups: list[list[int]]) -> list[asyncio.Task]:
        tasks_local: list[asyncio.Task] = []
        wid = 0
        for acc in accounts:
            for _ in range(args.sessions_per_account):
                if wid >= len(groups):
                    break
                tasks_local.append(
                    asyncio.create_task(
                        worker(
                            wid,
                            acc,
                            args.board,
                            groups[wid],
                            q,
                            args.retries,
                            args.short_wait,
                            args.long_wait,
                            progress,
                            args.quiet,
                        )
                    )
                )
                wid += 1
        return tasks_local

    if args.until_title is None:
        if args.end_id < args.start_id:
            await q.put(None)
            await writer_task
            print("SUMMARY total=0 ok=0 miss=0 fail=0")
            print(f"SQLITE db={args.db} imported=0")
            return
        ids = list(range(args.start_id, args.end_id + 1))
        groups = split_ids_chunk(ids, workers_n) if args.split_mode == "chunk" else split_ids_round_robin(ids, workers_n)
        await asyncio.gather(*build_tasks(groups))
        if progress["max_seen_id"] >= args.start_id:
            write_checkpoint(args.checkpoint_file, progress["max_seen_id"])
        target_total = len(ids)
    else:
        cursor = args.start_id
        target_total = 0
        while not progress["stop"]:
            ids = list(range(cursor, cursor + args.batch_size))
            # In until mode, prefer round-robin to reduce overshoot after stop signal.
            groups = split_ids_round_robin(ids, workers_n)
            await asyncio.gather(*build_tasks(groups))
            cursor += args.batch_size
            if progress["max_seen_id"] >= args.start_id:
                write_checkpoint(args.checkpoint_file, progress["max_seen_id"])
            if not args.quiet:
                print(
                    f"until-progress scanned_to={progress['max_seen_id']} "
                    f"ok={progress['ok_base']} miss={progress['miss_base']} fail={progress['fail_base']}"
                )
            if progress["stop"]:
                break
        if progress["max_seen_id"] >= args.start_id:
            target_total = progress["max_seen_id"] - args.start_id + 1

    await q.put(None)
    imported = await writer_task
    print(f"SUMMARY total={target_total} ok={progress['ok_base']} miss={progress['miss_base']} fail={progress['fail_base']}")
    print(f"SQLITE db={args.db} imported={imported}")
    if not args.no_csv:
        print(f"CSV file={args.csv}")
    if progress["stop"]:
        print(f"STOP matched_post_id={progress['stop_post_id']} matched_title={progress['stop_title']}")


if __name__ == "__main__":
    asyncio.run(main())
