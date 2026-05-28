#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import atexit
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

ROOT = Path(__file__).resolve().parent
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
LOGIN_FAIL_MARKERS = [
    "密码错误",
    "密码不正确",
    "登录失败",
    "登陆失败",
    "错误的密码",
    "用户不存在",
    "请输入正确",
    "请重新输入",
    "Login incorrect",
    "incorrect",
]
LOGIN_SUCCESS_MARKERS = [
    "主选单",
    "讨论区",
    "窗口数过多",
    "踢除",
    "目前选择",
    "按任意键继续",
    "上次在",
    "积分",
    "信箱",
    "等级",
    "身份",
]
AUDIT_BLOCK_MARKERS = [
    "全站审核中",
    "暂不能查看本文内容",
]


@dataclass
class Account:
    username: str
    password: str


class LoginFailed(RuntimeError):
    pass


class BoardEnterFailed(RuntimeError):
    pass


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            os.environ.setdefault(key, value)


def clean(s: str) -> str:
    return ANSI_RE.sub("", s).replace("\r", "")


async def rd(
    reader: telnetlib3.TelnetReader,
    sec: float = 0.8,
    idle_timeout: float = 0.06,
) -> str:
    start = time.monotonic()
    end = start + sec
    hard_end = start + max(sec * 2, sec + 1.0)
    out: list[str] = []
    saw_data = False
    while time.monotonic() < end:
        timeout = idle_timeout if saw_data else min(0.1, end - time.monotonic())
        try:
            d = await asyncio.wait_for(reader.read(4096), timeout=max(timeout, 0.001))
        except asyncio.TimeoutError:
            if saw_data:
                break
            continue
        if d:
            out.append(d)
            saw_data = True
            end = min(max(end, time.monotonic() + idle_timeout), hard_end)
    return clean("".join(out))


async def send(writer: telnetlib3.TelnetWriter, s: str) -> None:
    writer.write(s)
    await writer.drain()


def contains_any(s: str, markers: list[str]) -> bool:
    return any(marker in s for marker in markers)


def has_board_marker(s: str, board: str) -> bool:
    low = s.lower()
    board_low = board.lower()
    markers = [
        f"讨论区 [{board_low}]",
        f"信区: {board_low}",
        f"信区：{board_low}",
    ]
    return any(marker in low for marker in markers)


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
    t = t.replace("　", " ")
    t = re.sub(r"\s+", "", t)
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


def append_fail_log(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def format_fail_preview(raw: str | None, limit: int = 160) -> str:
    if not raw:
        return ""
    preview = clean(raw)
    preview = re.sub(r"\s+", " ", preview).strip()
    return preview[:limit]


def acquire_lock(lock_file: Path) -> int:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
    os.close(fd)
    return os.getpid()


def release_lock(lock_file: Path) -> None:
    try:
        if lock_file.exists():
            lock_file.unlink()
    except Exception:
        pass


async def enter_stock(
    writer: telnetlib3.TelnetWriter,
    reader: telnetlib3.TelnetReader,
    account: Account,
    board: str,
    idle_timeout: float,
) -> None:
    seen: list[str] = []
    seen.append(await rd(reader, 2.8, idle_timeout))
    await send(writer, account.username + "\r\n")
    seen.append(await rd(reader, 1.2, idle_timeout))
    await send(writer, account.password + "\r\n")
    t = await rd(reader, 1.8, idle_timeout)
    seen.append(t)
    merged = "\n".join(seen)
    if contains_any(merged, LOGIN_FAIL_MARKERS):
        raise LoginFailed(format_fail_preview(merged))
    if "窗口数过多" in t or "踢除" in t:
        await send(writer, "1\r\n")
        seen.append(await rd(reader, 1.2, idle_timeout))

    await send(writer, "\r\n")
    seen.append(await rd(reader, 0.8, idle_timeout))
    await send(writer, "\r\n")
    seen.append(await rd(reader, 0.8, idle_timeout))
    for _ in range(4):
        await send(writer, " ")
        seen.append(await rd(reader, 0.8, idle_timeout))

    await send(writer, "f")
    seen.append(await rd(reader, 0.6, idle_timeout))
    await send(writer, "\r\n")
    seen.append(await rd(reader, 0.8, idle_timeout))
    await send(writer, "\r\n")
    seen.append(await rd(reader, 1.2, idle_timeout))

    merged = "\n".join(seen)
    if contains_any(merged, LOGIN_FAIL_MARKERS):
        raise LoginFailed(format_fail_preview(merged))
    if not has_board_marker(merged, board):
        raise BoardEnterFailed(format_fail_preview(merged))


async def read_by_id(
    writer: telnetlib3.TelnetWriter,
    reader: telnetlib3.TelnetReader,
    pid: int,
    short_wait: float,
    long_wait: float,
    idle_timeout: float,
) -> tuple[str | None, str]:
    await send(writer, f"{pid}")
    await rd(reader, short_wait, idle_timeout)
    await send(writer, "\r\n")
    await rd(reader, long_wait, idle_timeout)
    await send(writer, "\r\n")
    t_open = await rd(reader, long_wait, idle_timeout)

    chunks: list[str] = [t_open] if t_open else []
    merged = "\n".join(chunks)

    if any(x in merged for x in ["没有这篇", "不存在", "找不到", "No such"]):
        await send(writer, "\x1b[D")
        await rd(reader, short_wait, idle_timeout)
        return None, "miss"
    if "FROM:" in merged or "[阅读文章]" in merged:
        await send(writer, "\x1b[D")
        await rd(reader, short_wait, idle_timeout)
        return merged, "ok"

    for _ in range(20):
        t = await rd(reader, short_wait, idle_timeout)
        if t:
            chunks.append(t)
        merged = "\n".join(chunks)

        if any(x in merged for x in ["没有这篇", "不存在", "找不到", "No such"]):
            await send(writer, "\x1b[D")
            await rd(reader, short_wait, idle_timeout)
            return None, "miss"
        if "FROM:" in merged or "[阅读文章]" in merged:
            await send(writer, "\x1b[D")
            await rd(reader, short_wait, idle_timeout)
            return merged, "ok"
        if ("下面还有喔" in merged) or ("按空格" in merged) or ("(SPACE)" in merged):
            await send(writer, " ")
            continue

    await send(writer, "\x1b[D")
    await rd(reader, short_wait, idle_timeout)
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


def parse_account_names(raw: str) -> set[str]:
    return {name.strip() for name in raw.split(",") if name.strip()}


def filter_disabled_accounts(accounts: list[Account], disabled: set[str]) -> list[Account]:
    if not disabled:
        return accounts
    return [account for account in accounts if account.username not in disabled]


def dedupe_accounts(accounts: list[Account]) -> tuple[list[Account], list[str]]:
    out: list[Account] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    for account in accounts:
        if account.username in seen:
            duplicates.append(account.username)
            continue
        seen.add(account.username)
        out.append(account)
    return out, duplicates



async def worker(
    wid: int,
    account: Account,
    board: str,
    ids: list[int] | asyncio.Queue,
    out_q: asyncio.Queue,
    retries: int,
    short_wait: float,
    long_wait: float,
    host: str,
    port: int,
    idle_timeout: float,
    per_account_interval: float,
    request_jitter: float,
    audit_slowdown_multiplier: float,
    max_per_account_interval: float,
    recovery_successes: int,
    audit_block_cooldown: float,
    reconnect_after_short_partial: int,
    min_reconnect_interval: float,
    login_fail_sleep: float,
    progress: dict,
    quiet: bool,
) -> None:
    reader = None
    writer = None
    ok = miss = fail = 0
    short_partial_streak = 0
    id_queue = ids if isinstance(ids, asyncio.Queue) else None
    id_iter = iter(ids) if id_queue is None else None

    async def wait_for_connect_slot() -> None:
        while True:
            async with progress["connect_lock"]:
                now = time.monotonic()
                next_connect_at = progress["next_connect_at_by_account"].get(
                    account.username,
                    0.0,
                )
                wait_for = next_connect_at - now
                if wait_for <= 0:
                    progress["next_connect_at_by_account"][account.username] = (
                        now + min_reconnect_interval
                    )
                    return
            if not quiet:
                print(
                    f"account={account.username} throttle reconnect "
                    f"sleep={wait_for:.1f}s"
                )
            await asyncio.sleep(wait_for)

    async def wait_for_request_slot() -> None:
        while True:
            async with progress["request_lock"]:
                now = time.monotonic()
                current_interval = progress["request_interval_by_account"].get(
                    account.username,
                    per_account_interval,
                )
                next_request_at = progress["next_request_at_by_account"].get(
                    account.username,
                    0.0,
                )
                wait_for = next_request_at - now
                if wait_for <= 0:
                    delay = current_interval
                    if request_jitter > 0:
                        delay += random.uniform(0, request_jitter)
                    progress["next_request_at_by_account"][account.username] = now + delay
                    return
            if (not quiet) and progress.get("verbose_throttle"):
                print(
                    f"account={account.username} throttle request "
                    f"sleep={wait_for:.1f}s"
                )
            await asyncio.sleep(wait_for)

    async def record_healthy_read() -> None:
        async with progress["request_lock"]:
            current_interval = progress["request_interval_by_account"].get(
                account.username,
                per_account_interval,
            )
            if current_interval <= per_account_interval:
                return
            successes = progress["healthy_reads_by_account"].get(account.username, 0) + 1
            if recovery_successes <= 0 or successes < recovery_successes:
                progress["healthy_reads_by_account"][account.username] = successes
                return
            new_interval = max(per_account_interval, current_interval / audit_slowdown_multiplier)
            progress["request_interval_by_account"][account.username] = new_interval
            progress["healthy_reads_by_account"][account.username] = 0
        if not quiet:
            print(
                f"account={account.username} recovered request interval "
                f"{current_interval:.1f}s -> {new_interval:.1f}s"
            )

    async def record_audit_slowdown() -> None:
        async with progress["request_lock"]:
            current_interval = progress["request_interval_by_account"].get(
                account.username,
                per_account_interval,
            )
            new_interval = min(
                max_per_account_interval,
                max(per_account_interval, current_interval) * audit_slowdown_multiplier,
            )
            progress["request_interval_by_account"][account.username] = new_interval
            progress["healthy_reads_by_account"][account.username] = 0
        if not quiet:
            print(
                f"account={account.username} slow request interval "
                f"{current_interval:.1f}s -> {new_interval:.1f}s"
            )

    async def wait_for_account_audit_cooldown() -> None:
        while True:
            async with progress["audit_cooldown_lock"]:
                cooldown_until = progress["audit_cooldown_until_by_account"].get(
                    account.username,
                    0.0,
                )
                wait_for = cooldown_until - time.monotonic()
            if wait_for <= 0:
                return
            if not quiet:
                print(
                    f"account={account.username} audit-block cooldown "
                    f"sleep={wait_for:.1f}s"
                )
            await asyncio.sleep(wait_for)

    async def start_account_audit_cooldown(pid: int, raw_len: int) -> None:
        async with progress["audit_cooldown_lock"]:
            now = time.monotonic()
            until = now + audit_block_cooldown
            current_until = progress["audit_cooldown_until_by_account"].get(
                account.username,
                0.0,
            )
            if until > current_until:
                progress["audit_cooldown_until_by_account"][account.username] = until
            wait_for = progress["audit_cooldown_until_by_account"][account.username] - now
        if not quiet:
            print(
                f"account={account.username} audit-block pid={pid} raw_len={raw_len}; "
                f"pause this account {wait_for:.1f}s then retry same pid"
            )
        await asyncio.sleep(wait_for)

    async def mark_completed(post_id: int) -> None:
        async with progress["checkpoint_lock"]:
            progress["completed_ids"].add(post_id)
            next_id = progress["checkpoint_contiguous_id"] + 1
            while next_id in progress["completed_ids"]:
                progress["completed_ids"].remove(next_id)
                progress["checkpoint_contiguous_id"] = next_id
                next_id += 1

    try:
        while True:
            if id_queue is None:
                try:
                    pid = next(id_iter)
                except StopIteration:
                    break
                if progress.get("stop"):
                    break
            else:
                pid = await id_queue.get()
                if pid is None:
                    break
                if progress.get("stop"):
                    continue
            if pid > progress["max_seen_id"]:
                progress["max_seen_id"] = pid
            success = False
            fail_reason = ""
            raw = None
            i = 0
            while i <= retries:
                try:
                    await wait_for_account_audit_cooldown()
                    if writer is None:
                        await wait_for_connect_slot()
                        await asyncio.sleep(random.uniform(0.03, 0.2))
                        reader, writer = await telnetlib3.open_connection(
                            host,
                            port,
                            connect_minwait=0.1,
                            connect_maxwait=1.0,
                            encoding="gb18030",
                            encoding_errors="ignore",
                            shell=None,
                        )
                        await enter_stock(writer, reader, account, board, idle_timeout)
                        if not quiet:
                            print(f"account={account.username} entered board={board}")
                    await wait_for_request_slot()
                    raw, st = await read_by_id(
                        writer,
                        reader,
                        pid,
                        short_wait,
                        long_wait,
                        idle_timeout,
                    )
                    # Unexpectedly fell back to menu/list screen; force reconnect and retry.
                    if raw and ("主选单" in raw or "讨论区 [Test]" in raw):
                        try:
                            writer.close()
                        except Exception:
                            pass
                        reader, writer = None, None
                        await asyncio.sleep(max(0.15, min_reconnect_interval))
                        fail_reason = "returned_to_menu"
                        i += 1
                        continue
                except LoginFailed as e:
                    fail_reason = f"login_failed:{e}"
                    append_fail_log(
                        progress["fail_log_file"],
                        f"post_id={pid}\taccount={account.username}"
                        f"\treason={fail_reason}\traw_preview=",
                    )
                    if not quiet:
                        print(
                            f"account={account.username} login failed; "
                            f"sleep {login_fail_sleep:.0f}s before retry"
                        )
                    try:
                        if writer is not None:
                            writer.close()
                    except Exception:
                        pass
                    reader, writer = None, None
                    await asyncio.sleep(login_fail_sleep)
                    continue
                except BoardEnterFailed as e:
                    fail_reason = f"board_enter_failed:{e}"
                    append_fail_log(
                        progress["fail_log_file"],
                        f"post_id={pid}\taccount={account.username}"
                        f"\treason={fail_reason}\traw_preview=",
                    )
                    if not quiet:
                        print(
                            f"account={account.username} did not enter "
                            f"{board}; reconnect"
                        )
                    try:
                        if writer is not None:
                            writer.close()
                    except Exception:
                        pass
                    reader, writer = None, None
                    await asyncio.sleep(min_reconnect_interval + random.uniform(0, 0.5))
                    i += 1
                    continue
                except Exception as e:
                    fail_reason = f"exception:{type(e).__name__}:{e}"
                    if not quiet:
                        print(f"account={account.username} reconnect on pid={pid}: {e}")
                    try:
                        if writer is not None:
                            writer.close()
                    except Exception:
                        pass
                    reader, writer = None, None
                    backoff = max(
                        min_reconnect_interval,
                        0.25 * (2 ** min(i, 3)) + random.uniform(0, 0.25),
                    )
                    await asyncio.sleep(backoff)
                    i += 1
                    continue

                if st == "miss" or raw is None:
                    miss += 1
                    progress["miss"] += 1
                    await record_healthy_read()
                    success = True
                    break
                rec = parse_post(raw, pid)
                if rec:
                    short_partial_streak = 0
                    await out_q.put(rec)
                    ok += 1
                    progress["ok"] += 1
                    await record_healthy_read()
                    if progress.get("until_title_norm"):
                        title_norm = normalize_title(rec[3])
                        if progress["until_title_norm"] in title_norm:
                            progress["stop"] = True
                            progress["stop_post_id"] = pid
                            progress["stop_title"] = rec[3]
                    success = True
                    break
                raw_len = len(raw) if raw else 0
                fail_reason = f"parse_failed status={st} raw_len={raw_len}"
                if raw and contains_any(raw, AUDIT_BLOCK_MARKERS):
                    fail_reason = f"audit_blocked cooldown={audit_block_cooldown:.1f} raw_len={raw_len}"
                    await record_audit_slowdown()
                    try:
                        if writer is not None:
                            writer.close()
                    except Exception:
                        pass
                    reader, writer = None, None
                    await start_account_audit_cooldown(pid, raw_len)
                    continue
                if st == "partial":
                    short_partial_streak += 1
                    if (
                        reconnect_after_short_partial > 0
                        and short_partial_streak >= reconnect_after_short_partial
                    ):
                        try:
                            if writer is not None:
                                writer.close()
                        except Exception:
                            pass
                        reader, writer = None, None
                        short_partial_streak = 0
                        if not quiet:
                            print(
                                f"account={account.username} reconnect after partial "
                                f"pid={pid} raw_len={raw_len}; "
                                f"sleep at least {min_reconnect_interval:.0f}s"
                            )
                else:
                    short_partial_streak = 0
                i += 1

            if not success:
                fail += 1
                progress["fail"] += 1
                append_fail_log(
                    progress["fail_log_file"],
                    f"post_id={pid}\taccount={account.username}"
                    f"\treason={fail_reason or 'unknown'}"
                    f"\traw_preview={format_fail_preview(raw)}",
                )
            progress["done"] += 1
            await mark_completed(pid)
            if (not quiet) and progress["done"] % 50 == 0:
                print(
                    f"progress done={progress['done']} ok={progress['ok']} "
                    f"miss={progress['miss']} fail={progress['fail']}"
                )
    finally:
        try:
            if writer is not None:
                writer.close()
        except Exception:
            pass

    if not quiet:
        print(f"account={account.username} done ok={ok} miss={miss} fail={fail}")


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
    load_env_file(ROOT / ".env")
    p = argparse.ArgumentParser(description="Parallel SMTH ID scraper -> sqlite")
    p.add_argument("--host", default=os.getenv("SMTH_HOST", "bbs.mysmth.net"))
    p.add_argument("--port", type=int, default=int(os.getenv("SMTH_PORT", "23")))
    p.add_argument("--start-id", type=int, default=None)
    p.add_argument("--end-id", type=int, default=None)
    p.add_argument("--board", default=os.getenv("SMTH_BOARD", "stock"))
    p.add_argument("--csv", type=Path, default=Path("data/smth_stock_posts.csv"))
    p.add_argument("--no-csv", action="store_true")
    p.add_argument("--db", type=Path, default=Path("data/smth_stock.db"))
    p.add_argument("--sqlite-batch", type=int, default=2000)
    p.add_argument("--accounts", default=os.getenv("SMTH_ACCOUNTS", ""))
    p.add_argument(
        "--disabled-accounts",
        default=os.getenv("SMTH_DISABLED_ACCOUNTS", ""),
        help="逗号分隔的禁用账号名，即使出现在 --accounts/SMTH_ACCOUNTS 中也不会登录",
    )
    p.add_argument("--sessions-per-account", type=int, default=1, help="必须为 1；每个账号严禁多个同时连接")
    p.add_argument("--flush-every", type=int, default=500, help="CSV flush interval when CSV is enabled")
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--short-wait", type=float, default=0.08)
    p.add_argument("--long-wait", type=float, default=0.25)
    p.add_argument(
        "--idle-timeout",
        type=float,
        default=float(os.getenv("SMTH_IDLE_TIMEOUT", "0.06")),
        help="读取到数据后，连续空闲多少秒即认为本次响应结束，默认 0.06",
    )
    p.add_argument(
        "--per-account-interval",
        type=float,
        default=float(os.getenv("SMTH_PER_ACCOUNT_INTERVAL", "3")),
        help="同一账号两次读帖之间的最小间隔秒数，默认 3",
    )
    p.add_argument(
        "--request-jitter",
        type=float,
        default=float(os.getenv("SMTH_REQUEST_JITTER", "1")),
        help="每次读帖间隔额外随机抖动秒数，默认 1",
    )
    p.add_argument(
        "--audit-slowdown-multiplier",
        type=float,
        default=float(os.getenv("SMTH_AUDIT_SLOWDOWN_MULTIPLIER", "2")),
        help="账号遇到审核提示后读帖间隔放大倍数，默认 2",
    )
    p.add_argument(
        "--max-per-account-interval",
        type=float,
        default=float(os.getenv("SMTH_MAX_PER_ACCOUNT_INTERVAL", "15")),
        help="账号自适应降速后的最大读帖间隔秒数，默认 15",
    )
    p.add_argument(
        "--recovery-successes",
        type=int,
        default=int(os.getenv("SMTH_RECOVERY_SUCCESSES", "500")),
        help="账号连续成功读取多少条后尝试恢复一档速度，默认 500",
    )
    p.add_argument(
        "--reconnect-after-short-partial",
        type=int,
        default=int(os.getenv("SMTH_RECONNECT_AFTER_SHORT_PARTIAL", "0")),
        help="连续 partial 解析失败后的自动重连阈值；0 表示不因 partial 主动重连",
    )
    p.add_argument(
        "--min-reconnect-interval",
        type=float,
        default=float(os.getenv("SMTH_MIN_RECONNECT_INTERVAL", "30")),
        help="同一账号两次连接尝试之间的最小间隔秒数，默认 30",
    )
    p.add_argument(
        "--max-audit-blocks",
        type=int,
        default=int(os.getenv("SMTH_MAX_AUDIT_BLOCKS", "3")),
        help="兼容旧参数；当前不再因审核提示停用账号",
    )
    p.add_argument(
        "--audit-block-retries",
        type=int,
        default=int(os.getenv("SMTH_AUDIT_BLOCK_RETRIES", "3")),
        help="兼容旧参数；当前遇到审核提示会触发账号冷却后重试同一帖",
    )
    p.add_argument(
        "--audit-block-wait",
        type=float,
        default=float(os.getenv("SMTH_AUDIT_BLOCK_WAIT", "2")),
        help="兼容旧参数；当前遇到审核提示会触发账号冷却后重试同一帖",
    )
    p.add_argument(
        "--audit-block-cooldown",
        type=float,
        default=float(os.getenv("SMTH_AUDIT_BLOCK_COOLDOWN", "300")),
        help="单个账号遇到全站审核提示后的暂停秒数，默认 300",
    )
    p.add_argument("--login-fail-sleep", type=float, default=600, help="登录失败后等待多少秒再重试")
    p.add_argument("--split-mode", choices=["round_robin", "chunk"], default="chunk")
    p.add_argument("--until-title", default=None, help="持续抓取直到命中该标题（忽略 --end-id）")
    p.add_argument("--batch-size", type=int, default=300, help="until 模式每轮分配的ID数量")
    p.add_argument("--checkpoint-file", type=Path, default=Path("data/smth_stock.last_id"))
    p.add_argument("--fail-log-file", type=Path, default=Path("data/smth_stock.fail.log"))
    p.add_argument("--lock-file", type=Path, default=Path("data/smth_stock.run.lock"))
    p.add_argument("--no-lock", action="store_true", help="不启用单实例锁")
    p.add_argument("--no-resume", action="store_true", help="忽略 checkpoint，从 --start-id 开始")
    p.add_argument("--verbose-throttle", action="store_true", help="打印每次读帖节流等待日志")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    lock_acquired = False
    if not args.no_lock:
        try:
            acquire_lock(args.lock_file)
            lock_acquired = True
            atexit.register(release_lock, args.lock_file)
        except FileExistsError:
            raise SystemExit(
                f"lock exists: {args.lock_file} (another run may be active). "
                "Use --no-lock to bypass."
            )
    if args.sessions_per_account != 1:
        raise SystemExit("sessions-per-account must be 1; one account may only have one connection")

    accounts = parse_accounts(args.accounts)
    disabled_accounts = parse_account_names(args.disabled_accounts)
    before_filter = len(accounts)
    accounts = filter_disabled_accounts(accounts, disabled_accounts)
    accounts, duplicate_accounts = dedupe_accounts(accounts)
    if not accounts:
        raise SystemExit("missing enabled accounts. set --accounts or SMTH_ACCOUNTS")
    if not args.quiet:
        if disabled_accounts:
            disabled_used = sorted(disabled_accounts)
            print(f"disabled accounts: {', '.join(disabled_used)}")
            print(f"enabled accounts: {len(accounts)}/{before_filter}")
        if duplicate_accounts:
            print("ignored duplicate accounts: " + ", ".join(sorted(set(duplicate_accounts))))
        print("using accounts: " + ", ".join(account.username for account in accounts))

    if not args.no_resume:
        last_id = read_checkpoint(args.checkpoint_file)
        if last_id is not None and args.start_id is None:
            args.start_id = last_id + 1
            if not args.quiet:
                print(f"resume from checkpoint last_id={last_id} -> start_id={args.start_id}")
        elif last_id is not None and args.start_id is not None and last_id >= args.start_id:
            args.start_id = last_id + 1
            if not args.quiet:
                print(f"resume from checkpoint last_id={last_id} -> start_id={args.start_id}")

    if args.start_id is None:
        raise SystemExit("missing --start-id and no checkpoint found")
    if args.until_title is None:
        if args.end_id is None:
            raise SystemExit("missing --end-id when --until-title is not set")
        if args.end_id < args.start_id:
            raise SystemExit("end-id must be >= start-id")
    elif args.batch_size <= 0:
        raise SystemExit("batch-size must be > 0")
    if args.per_account_interval < 0:
        raise SystemExit("per-account-interval must be >= 0")
    if args.request_jitter < 0:
        raise SystemExit("request-jitter must be >= 0")
    if args.audit_slowdown_multiplier <= 1:
        raise SystemExit("audit-slowdown-multiplier must be > 1")
    if args.max_per_account_interval < args.per_account_interval:
        raise SystemExit("max-per-account-interval must be >= per-account-interval")
    if args.recovery_successes < 0:
        raise SystemExit("recovery-successes must be >= 0")

    workers_n = len(accounts)

    q: asyncio.Queue = asyncio.Queue(maxsize=5000)
    csv_path = None if args.no_csv else args.csv
    writer_task = asyncio.create_task(
        sink_writer(q, args.db, args.sqlite_batch, csv_path, args.flush_every)
    )
    progress = {
        "done": 0,
        "ok": 0,
        "miss": 0,
        "fail": 0,
        "stop": False,
        "stop_post_id": None,
        "stop_title": None,
        "until_title_norm": normalize_title(args.until_title) if args.until_title else None,
        "max_seen_id": args.start_id - 1,
        "fail_log_file": args.fail_log_file,
        "connect_lock": asyncio.Lock(),
        "next_connect_at_by_account": {},
        "request_lock": asyncio.Lock(),
        "next_request_at_by_account": {},
        "request_interval_by_account": {},
        "healthy_reads_by_account": {},
        "verbose_throttle": args.verbose_throttle,
        "audit_cooldown_lock": asyncio.Lock(),
        "audit_cooldown_until_by_account": {},
        "checkpoint_lock": asyncio.Lock(),
        "checkpoint_contiguous_id": args.start_id - 1,
        "completed_ids": set(),
    }

    args.fail_log_file.parent.mkdir(parents=True, exist_ok=True)
    args.fail_log_file.write_text("", encoding="utf-8")

    def build_tasks(groups: list[list[int]]) -> list[asyncio.Task]:
        tasks_local: list[asyncio.Task] = []
        wid = 0
        for acc in accounts:
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
                        args.host,
                        args.port,
                        args.idle_timeout,
                        args.per_account_interval,
                        args.request_jitter,
                        args.audit_slowdown_multiplier,
                        args.max_per_account_interval,
                        args.recovery_successes,
                        args.audit_block_cooldown,
                        args.reconnect_after_short_partial,
                        args.min_reconnect_interval,
                        args.login_fail_sleep,
                        progress,
                        args.quiet,
                    )
                )
            )
            wid += 1
        return tasks_local

    def build_queue_tasks(id_q: asyncio.Queue) -> list[asyncio.Task]:
        tasks_local: list[asyncio.Task] = []
        for wid, acc in enumerate(accounts):
            tasks_local.append(
                asyncio.create_task(
                    worker(
                        wid,
                        acc,
                        args.board,
                        id_q,
                        q,
                        args.retries,
                        args.short_wait,
                        args.long_wait,
                        args.host,
                        args.port,
                        args.idle_timeout,
                        args.per_account_interval,
                        args.request_jitter,
                        args.audit_slowdown_multiplier,
                        args.max_per_account_interval,
                        args.recovery_successes,
                        args.audit_block_cooldown,
                        args.reconnect_after_short_partial,
                        args.min_reconnect_interval,
                        args.login_fail_sleep,
                        progress,
                        args.quiet,
                    )
                )
            )
        return tasks_local

    async def feed_until_ids(id_q: asyncio.Queue) -> None:
        cursor = args.start_id
        while not progress["stop"]:
            await id_q.put(cursor)
            cursor += 1
        for _ in range(workers_n):
            await id_q.put(None)

    async def monitor_until_progress(done_event: asyncio.Event) -> None:
        last_checkpoint = args.start_id - 1
        last_report_done = 0
        while not done_event.is_set():
            try:
                await asyncio.wait_for(done_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            async with progress["checkpoint_lock"]:
                checkpoint_id = progress["checkpoint_contiguous_id"]
            if checkpoint_id > last_checkpoint:
                write_checkpoint(args.checkpoint_file, checkpoint_id)
                last_checkpoint = checkpoint_id
            should_report = (
                progress["done"] - last_report_done >= args.batch_size
                or (done_event.is_set() and progress["done"] > last_report_done)
            )
            if (not args.quiet) and should_report:
                print(
                    f"until-progress scanned_to={progress['max_seen_id']} "
                    f"checkpoint_to={checkpoint_id} "
                    f"ok={progress['ok']} miss={progress['miss']} fail={progress['fail']}"
                )
                last_report_done = progress["done"]

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
        async with progress["checkpoint_lock"]:
            checkpoint_id = progress["checkpoint_contiguous_id"]
        if checkpoint_id >= args.start_id:
            write_checkpoint(args.checkpoint_file, checkpoint_id)
        target_total = len(ids)
    else:
        id_q: asyncio.Queue = asyncio.Queue(maxsize=args.batch_size)
        until_done = asyncio.Event()
        worker_tasks = build_queue_tasks(id_q)
        monitor_task = asyncio.create_task(monitor_until_progress(until_done))
        producer_task = asyncio.create_task(feed_until_ids(id_q))
        await producer_task
        await asyncio.gather(*worker_tasks)
        until_done.set()
        await monitor_task
        if progress["max_seen_id"] >= args.start_id:
            target_total = progress["max_seen_id"] - args.start_id + 1
        else:
            target_total = 0

    try:
        await q.put(None)
        imported = await writer_task
        print(f"SUMMARY total={target_total} ok={progress['ok']} miss={progress['miss']} fail={progress['fail']}")
        print(f"SQLITE db={args.db} imported={imported}")
        print(f"FAIL_LOG file={args.fail_log_file}")
        if not args.no_csv:
            print(f"CSV file={args.csv}")
        if progress["stop"]:
            print(f"STOP matched_post_id={progress['stop_post_id']} matched_title={progress['stop_title']}")
    finally:
        if lock_acquired:
            release_lock(args.lock_file)


if __name__ == "__main__":
    asyncio.run(main())
