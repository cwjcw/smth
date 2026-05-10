#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

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


async def csv_writer(csv_path: Path, q: asyncio.Queue, flush_every: int) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if csv_path.stat().st_size == 0:
            w.writerow(["post_id", "author", "board", "title", "post_time", "body"])
        n = 0
        while True:
            item = await q.get()
            if item is None:
                break
            w.writerow(item)
            n += 1
            if n >= flush_every:
                f.flush()
                n = 0
        f.flush()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Parallel SMTH ID scraper -> sqlite")
    p.add_argument("--start-id", type=int, required=True)
    p.add_argument("--end-id", type=int, required=True)
    p.add_argument("--board", default=os.getenv("SMTH_BOARD", "stock"))
    p.add_argument("--csv", type=Path, default=Path("data/smth_stock_posts.csv"))
    p.add_argument("--accounts", default=os.getenv("SMTH_ACCOUNTS", ""))
    p.add_argument("--sessions-per-account", type=int, default=3)
    p.add_argument("--flush-every", type=int, default=500)
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--short-wait", type=float, default=0.12)
    p.add_argument("--long-wait", type=float, default=0.45)
    p.add_argument("--split-mode", choices=["round_robin", "chunk"], default="chunk")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    if args.end_id < args.start_id:
        raise SystemExit("end-id must be >= start-id")
    accounts = parse_accounts(args.accounts)
    if not accounts:
        raise SystemExit("missing accounts. set --accounts or SMTH_ACCOUNTS")

    workers_n = len(accounts) * args.sessions_per_account
    ids = list(range(args.start_id, args.end_id + 1))
    groups = split_ids_chunk(ids, workers_n) if args.split_mode == "chunk" else split_ids_round_robin(ids, workers_n)

    q: asyncio.Queue = asyncio.Queue(maxsize=5000)
    writer_task = asyncio.create_task(csv_writer(args.csv, q, args.flush_every))
    progress = {"done": 0, "ok_base": 0, "miss_base": 0, "fail_base": 0}

    tasks = []
    wid = 0
    for acc in accounts:
        for _ in range(args.sessions_per_account):
            if wid >= len(groups):
                break
            tasks.append(
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

    await asyncio.gather(*tasks)
    await q.put(None)
    await writer_task
    print(f"SUMMARY total={len(ids)} ok={progress['ok_base']} miss={progress['miss_base']} fail={progress['fail_base']}")


if __name__ == "__main__":
    asyncio.run(main())
