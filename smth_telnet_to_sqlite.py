#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import telnetlib3

ESC_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

SENDER_RE = re.compile(r"发信人[:：]\s*([^\s(]+)")
BOARD_RE = re.compile(r"信区[:：]\s*(\S+)")
TITLE_RE = re.compile(r"标\s*题[:：]\s*(.+)")
TIME_RE = re.compile(r"发信站[:：]\s*(.+)")


@dataclass
class Post:
    post_id: int
    author: str
    board: str
    title: str
    post_time: str
    body: str


class SmthTelnetScraper:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        board: str,
        timeout: float,
        read_pause: float,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.board = board
        self.timeout = timeout
        self.read_pause = read_pause
        self.reader: Optional[telnetlib3.TelnetReader] = None
        self.writer: Optional[telnetlib3.TelnetWriter] = None

    async def connect_and_login(self) -> None:
        self.reader, self.writer = await telnetlib3.open_connection(
            host=self.host,
            port=self.port,
            connect_minwait=0.1,
            connect_maxwait=1.0,
            encoding="gb18030",
            encoding_errors="ignore",
            shell=None,
        )
        await self._read_until_any(["login", "用户名", "帐号", "账号", "代号", "请输入代号"])
        await self._sendline(self.username)
        await self._read_until_any(["Password", "密码", "请输入密码"])
        await self._sendline(self.password)
        await self._wait_settle(1.2)
        await self._post_login_dismiss()

    async def enter_board(self) -> None:
        await self._post_login_dismiss()
        for _ in range(10):
            await self._send("q")
            await asyncio.sleep(0.3)
            text = await self._drain_text(max_wait=1.0)
            if "主选单" in text or "讨论区 [Test]" in text:
                break

        # Single-key command first, then board name + Enter.
        await self._send("s")
        await asyncio.sleep(0.5)
        await self._drain_text(max_wait=1.5)
        await self._sendline(self.board)
        await asyncio.sleep(0.5)

        # Handle shuttle/progress/continue pages until likely entering board.
        merged = ""
        for _ in range(80):
            text = await self._drain_text(max_wait=0.8)
            if text:
                merged += text
            if "窗口数过多" in merged or "踢除" in merged:
                await self._sendline("1")
                merged = ""
                continue
            if "按空格键继续" in merged or "按任何键继续" in merged:
                await self._send(" ")
                merged = ""
                continue
            if (
                f"[{self.board.capitalize()}]" in merged
                or f"[{self.board.upper()}]" in merged
                or f"[{self.board.lower()}]" in merged
                or ("讨论区" in merged and self.board.lower() in merged.lower())
            ):
                break

    async def fetch_post(self, post_id: int, max_scroll: int = 200) -> Optional[str]:
        await self._sendline(f"{post_id}")
        chunks = []
        no_growth_rounds = 0
        last_len = 0
        for _ in range(max_scroll):
            text = await self._drain_text(max_wait=1.2)
            if text:
                chunks.append(text)

            merged = "\n".join(chunks)
            if len(merged) < 20 and "找不到" not in merged and "不存在" not in merged:
                # Fallback for contexts that require explicit read command.
                await self._sendline(f"read {post_id}")
                await asyncio.sleep(0.2)
                continue
            if any(x in merged for x in ["不存在", "没有这篇", "找不到", "No such"]):
                await self._sendline("q")
                await self._wait_settle(0.2)
                return None

            if self._need_more(merged):
                await self._send(" ")
                await asyncio.sleep(self.read_pause)
                continue

            current_len = len(merged)
            if current_len <= last_len:
                no_growth_rounds += 1
            else:
                no_growth_rounds = 0
            last_len = current_len

            if self._looks_like_post_page(merged):
                if no_growth_rounds >= 2:
                    break
                # If metadata is visible and no pagination marker, usually full page is loaded.
                if not self._need_more(merged):
                    break

            if no_growth_rounds >= 4 and current_len > 0:
                break

        await self._sendline("q")
        await self._wait_settle(0.2)
        return "\n".join(chunks)

    async def close(self) -> None:
        if not self.writer:
            return
        try:
            await self._sendline("quit")
            await self._wait_settle(0.2)
        except Exception:
            pass
        self.writer.close()

    async def _send(self, s: str) -> None:
        assert self.writer is not None
        self.writer.write(s)
        await self.writer.drain()

    async def _sendline(self, s: str) -> None:
        await self._send(s + "\r\n")

    async def _read_until_any(self, needles: list[str]) -> str:
        deadline = time.time() + self.timeout
        buf = ""
        while time.time() < deadline:
            buf += await self._drain_text(max_wait=0.8)
            lower = buf.lower()
            if any(n.lower() in lower for n in needles):
                return buf
        raise TimeoutError(f"等待提示超时: {needles}")

    async def _wait_settle(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            await self._drain_text(max_wait=0.2)
            await asyncio.sleep(0.05)

    async def _drain_text(self, max_wait: float) -> str:
        assert self.reader is not None
        end = time.time() + max_wait
        chunks: list[str] = []
        while time.time() < end:
            try:
                data = await asyncio.wait_for(self.reader.read(1), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            if not data:
                continue
            chunks.append(data)
            end = min(end + 0.15, time.time() + 0.3)
        if not chunks:
            return ""
        text = "".join(chunks).replace("\r", "")
        text = ESC_RE.sub("", text)
        text = text.replace("\x08", "")
        return text

    async def _post_login_dismiss(self) -> None:
        # If too many online sessions, kick one existing session first.
        for _ in range(3):
            text = await self._drain_text(max_wait=0.8)
            if "窗口数过多" in text or "踢除" in text:
                await self._sendline("1")
                await asyncio.sleep(0.5)
                await self._drain_text(max_wait=1.2)
                break
        # User-confirmed fixed sequence:
        # Enter x2, then any key x4.
        for _ in range(2):
            await self._sendline("")
            await asyncio.sleep(0.45)
            await self._drain_text(max_wait=1.0)
        for _ in range(4):
            await self._send(" ")
            await asyncio.sleep(0.45)
            await self._drain_text(max_wait=1.0)

    @staticmethod
    def _need_more(text: str) -> bool:
        markers = ["--More--", "更多", "继续", "[按空格]", "(SPACE)"]
        return any(m in text for m in markers)

    @staticmethod
    def _looks_like_post_page(text: str) -> bool:
        return "发信人" in text and "标" in text and "发信站" in text


def clean_body_lines(lines: list[str]) -> str:
    cleaned: list[str] = []
    in_quote_block = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if not in_quote_block:
                cleaned.append("")
            continue

        if stripped.startswith("【 在") and "大作中提到" in stripped:
            in_quote_block = True
            continue

        if in_quote_block:
            if stripped.startswith(":") or stripped.startswith(">"):
                continue
            in_quote_block = False

        if stripped.startswith(":") or stripped.startswith(">"):
            continue
        if stripped.startswith("※ 来源") or stripped.startswith("--"):
            continue
        cleaned.append(line.rstrip())

    text = "\n".join(cleaned)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def parse_post(post_id: int, raw: str) -> Optional[Post]:
    lines = [ln.rstrip("\n") for ln in raw.split("\n")]
    sender = ""
    board = ""
    title = ""
    post_time = ""

    meta_end = -1
    for i, line in enumerate(lines):
        if not sender:
            m = SENDER_RE.search(line)
            if m:
                sender = m.group(1).strip()
        if not board:
            m = BOARD_RE.search(line)
            if m:
                board = m.group(1).strip()
        if not title:
            m = TITLE_RE.search(line)
            if m:
                title = m.group(1).strip()
        if not post_time:
            m = TIME_RE.search(line)
            if m:
                post_time = m.group(1).strip()
                meta_end = i

    if not sender or not board or not title or not post_time:
        return None

    body_lines = lines[meta_end + 1 :] if meta_end >= 0 else []
    body = clean_body_lines(body_lines)
    return Post(post_id=post_id, author=sender, board=board, title=title, post_time=post_time, body=body)


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
            post_id INTEGER PRIMARY KEY,
            author TEXT NOT NULL,
            board TEXT NOT NULL,
            title TEXT NOT NULL,
            post_time TEXT NOT NULL,
            body TEXT NOT NULL,
            raw_text TEXT
        )
        """
    )
    conn.commit()


def upsert_post(conn: sqlite3.Connection, post: Post, raw_text: str) -> None:
    conn.execute(
        """
        INSERT INTO posts (post_id, author, board, title, post_time, body, raw_text)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(post_id) DO UPDATE SET
            author=excluded.author,
            board=excluded.board,
            title=excluded.title,
            post_time=excluded.post_time,
            body=excluded.body,
            raw_text=excluded.raw_text
        """,
        (post.post_id, post.author, post.board, post.title, post.post_time, post.body, raw_text),
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SMTH telnet scraper -> sqlite")
    p.add_argument("--host", default=os.getenv("SMTH_HOST", "bbs.newsmth.net"))
    p.add_argument("--port", type=int, default=int(os.getenv("SMTH_PORT", "23")))
    p.add_argument("--username", default=os.getenv("SMTH_USERNAME"))
    p.add_argument("--password", default=os.getenv("SMTH_PASSWORD"))
    p.add_argument("--board", default=os.getenv("SMTH_BOARD", "stock"))
    p.add_argument("--start-id", type=int, required=True)
    p.add_argument("--end-id", type=int, required=True)
    p.add_argument("--db", type=Path, default=Path("data/smth_stock.db"))
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--read-pause", type=float, default=0.25)
    p.add_argument("--sleep", type=float, default=0.2)
    p.add_argument("--commit-every", type=int, default=20)
    return p.parse_args()


async def run(args: argparse.Namespace) -> None:
    if not args.username or not args.password:
        raise SystemExit("缺少用户名或密码。请通过参数传入，或在环境变量 SMTH_USERNAME/SMTH_PASSWORD 中设置。")
    if args.end_id < args.start_id:
        raise SystemExit("end-id 必须大于等于 start-id")

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    init_db(conn)

    scraper = SmthTelnetScraper(
        host=args.host,
        port=args.port,
        username=args.username,
        password=args.password,
        board=args.board,
        timeout=args.timeout,
        read_pause=args.read_pause,
    )

    ok = 0
    miss = 0
    fail = 0
    try:
        await scraper.connect_and_login()
        await scraper.enter_board()

        for idx, post_id in enumerate(range(args.start_id, args.end_id + 1), start=1):
            try:
                raw = await scraper.fetch_post(post_id)
                if not raw:
                    miss += 1
                else:
                    post = parse_post(post_id, raw)
                    if not post:
                        fail += 1
                    else:
                        upsert_post(conn, post, raw)
                        ok += 1

                if idx % args.commit_every == 0:
                    conn.commit()
                    print(f"progress: {post_id} ok={ok} miss={miss} fail={fail}")

                await asyncio.sleep(max(0.0, args.sleep))
            except Exception as exc:
                fail += 1
                print(f"error post_id={post_id}: {exc}")

        conn.commit()
    finally:
        await scraper.close()
        conn.close()

    print(f"done. ok={ok} miss={miss} fail={fail} db={args.db}")


def main() -> None:
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
