#!/usr/bin/env python3
"""
Scrape the FamilyLife board on newsmth.net and extract author IDs/IPs.

The script walks the board pages, downloads every visible thread (optionally
filtering by ID), follows the thread pagination, and writes the collected
records to a CSV file.  Configuration (page range + target IDs) can be kept
inside scraper.config.json so the crawler can be restarted without editing
the code.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests import Response, Session

BASE_URL = "https://www.newsmth.net"
BOARD_PATH = "/nForum/board/FamilyLife"
BOARD_URL_PATTERN = BASE_URL + BOARD_PATH + "?p={page}"
THREAD_ID_RE = re.compile(r"/article/FamilyLife/(\d+)")
AUTHOR_RE = re.compile(r"发信人:\s*([^(]+)")
POST_TIME_RE = re.compile(r"发信站:\s*(.+)")
FROM_RE = re.compile(r"\[FROM:\s*([^\]]+)\]")
DelayRange = Tuple[float, float]
DEFAULT_DELAY_RANGE: DelayRange = (2.0, 5.0)


@dataclass
class ThreadLink:
    """Metadata collected from a board page."""

    title: str
    url: str
    board_page: int

    @property
    def thread_id(self) -> Optional[str]:
        match = THREAD_ID_RE.search(self.url)
        return match.group(1) if match else None


@dataclass
class PostRecord:
    """Single post entry to be written to the CSV file."""

    board_page: int
    thread_id: str
    thread_title: str
    thread_url: str
    thread_page: int
    floor: str
    author_id: str
    post_time: str
    ips: str


def load_config(config_path: Optional[Path]) -> dict:
    """Load configuration from JSON if it exists."""

    defaults = {"start_page": 1, "end_page": 1, "target_ids": []}
    if not config_path:
        return defaults

    if not config_path.exists():
        return defaults

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"无法解析配置文件 {config_path}: {exc}") from exc

    merged = defaults.copy()
    merged.update({k: v for k, v in data.items() if k in merged})
    return merged


def create_session() -> Session:
    """Create a pre-configured HTTP session."""

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
            "Referer": BASE_URL + "/",
        }
    )
    return session


def fetch_text(session: Session, url: str, retries: int = 3, timeout: float = 20.0) -> str:
    """Download a page with retries and GBK decoding."""

    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            response: Response = session.get(url, timeout=timeout)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or "gb18030"
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt != retries:
                time.sleep(1.0)
    raise RuntimeError(f"无法获取 {url}: {last_error}") from last_error


def parse_board(html: str, board_page: int) -> Iterator[ThreadLink]:
    """Yield every thread link that appears on a board page."""

    soup = BeautifulSoup(html, "html.parser")
    for cell in soup.select("td.title_9"):
        anchor = cell.find("a", href=True)
        if not anchor:
            continue
        href = anchor["href"]
        if not href.startswith("/nForum/article/FamilyLife/"):
            continue
        yield ThreadLink(
            title=anchor.get_text(strip=True),
            url=urljoin(BASE_URL, href),
            board_page=board_page,
        )


def extract_max_thread_page(soup: BeautifulSoup) -> int:
    """Return the last available page number for the currently loaded thread."""

    max_page = 1
    for anchor in soup.select("div.t-pre ol.page-main li a"):
        text = anchor.get_text(strip=True)
        if text.isdigit():
            max_page = max(max_page, int(text))
    return max_page


def pick_author_id(raw_text: str) -> Optional[str]:
    """Extract the author ID from the metadata paragraph."""

    match = AUTHOR_RE.search(raw_text)
    if not match:
        return None
    value = match.group(1).strip()
    # Many posts use "发信人: foo (bar)", so cut away the nickname.
    return value.split("(")[0].strip()


def pick_post_time(raw_text: str) -> str:
    """Extract the '发信站' timestamp portion."""

    match = POST_TIME_RE.search(raw_text)
    return match.group(1).strip() if match else ""


def collect_ips(content_cell: BeautifulSoup) -> str:
    """Collect all IP fragments from font.f005 elements."""

    ips: List[str] = []
    for font in content_cell.select("font.f005"):
        matches = FROM_RE.findall(font.get_text())
        for ip in matches:
            ip_clean = ip.strip()
            if ip_clean and ip_clean not in ips:
                ips.append(ip_clean)
    return "|".join(ips)


def sleep_with_delay(delay_range: DelayRange) -> None:
    """Sleep for a random duration inside the provided range."""

    delay_min, delay_max = delay_range
    if delay_max <= 0:
        return
    if delay_min < 0:
        delay_min = 0
    if delay_min > delay_max:
        delay_min, delay_max = delay_max, delay_min
    wait = delay_max if abs(delay_max - delay_min) < 1e-6 else random.uniform(delay_min, delay_max)
    if wait > 0:
        time.sleep(wait)


def parse_thread_page(
    soup: BeautifulSoup,
    link: ThreadLink,
    thread_page: int,
    target_ids: Set[str],
) -> List[PostRecord]:
    """Parse every visible post on one page of a thread."""

    records: List[PostRecord] = []
    for wrap in soup.select("div.a-wrap.corner"):
        content_cell = wrap.select_one("td.a-content")
        if content_cell is None:
            continue
        raw_text = content_cell.get_text("\n", strip=True)
        author = pick_author_id(raw_text)
        if not author:
            continue
        if target_ids and author not in target_ids:
            continue
        ips = collect_ips(content_cell)
        floor_node = wrap.select_one("span.a-pos")
        floor = floor_node.get_text(strip=True) if floor_node else ""
        records.append(
            PostRecord(
                board_page=link.board_page,
                thread_id=link.thread_id or "",
                thread_title=link.title,
                thread_url=link.url,
                thread_page=thread_page,
                floor=floor,
                author_id=author,
                post_time=pick_post_time(raw_text),
                ips=ips,
            )
        )
    return records


def crawl_thread(
    session: Session,
    link: ThreadLink,
    target_ids: Set[str],
    delay_range: DelayRange,
) -> Iterator[PostRecord]:
    """Fetch an entire thread (all available pages)."""

    first_page_html = fetch_text(session, link.url)
    sleep_with_delay(delay_range)
    soup = BeautifulSoup(first_page_html, "html.parser")
    max_page = extract_max_thread_page(soup)
    for record in parse_thread_page(soup, link, 1, target_ids):
        yield record

    for page in range(2, max_page + 1):
        paged_url = f"{link.url}?p={page}"
        html = fetch_text(session, paged_url)
        sleep_with_delay(delay_range)
        soup = BeautifulSoup(html, "html.parser")
        for record in parse_thread_page(soup, link, page, target_ids):
            yield record


def ensure_directory(path: Path) -> None:
    """Create the parent directory for a file if needed."""

    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)


def sanitize_ids(values: Sequence[str]) -> Set[str]:
    """Normalize ID filters."""

    sanitized = {item.strip() for item in values if item and item.strip()}
    return sanitized


def resolve_delay_range(cli_delay: Optional[float], cfg_delay: object) -> DelayRange:
    """Determine the effective delay range from CLI/config/defaults."""

    def clamp_pair(low: float, high: float) -> DelayRange:
        low = max(0.0, low)
        high = max(low, high)
        return (low, high)

    if cli_delay is not None:
        value = max(0.0, float(cli_delay))
        return (value, value)

    if isinstance(cfg_delay, (int, float)):
        value = max(0.0, float(cfg_delay))
        return (value, value)

    if isinstance(cfg_delay, (list, tuple)) and len(cfg_delay) == 2:
        low, high = cfg_delay
        try:
            return clamp_pair(float(low), float(high))
        except (TypeError, ValueError):
            pass

    if isinstance(cfg_delay, dict):
        low = cfg_delay.get("min")
        high = cfg_delay.get("max")
        try:
            if low is not None and high is not None:
                return clamp_pair(float(low), float(high))
        except (TypeError, ValueError):
            pass

    return DEFAULT_DELAY_RANGE


def crawl_board(
    session: Session,
    start_page: int,
    end_page: int,
    target_ids: Set[str],
    delay_range: DelayRange,
    max_threads: int,
) -> Iterator[PostRecord]:
    """Walk every board page inside the requested range."""

    visited_threads: Set[str] = set()
    processed_threads = 0
    for page in range(start_page, end_page + 1):
        url = BOARD_URL_PATTERN.format(page=page)
        try:
            html = fetch_text(session, url)
        except RuntimeError as exc:
            print(f"[Board {page}] 下载失败: {exc}", file=sys.stderr)
            continue
        sleep_with_delay(delay_range)
        links = list(parse_board(html, page))
        print(f"[Board {page}] 收到 {len(links)} 个主题")
        for link in links:
            thread_id = link.thread_id
            if not thread_id:
                continue
            if thread_id in visited_threads:
                continue
            visited_threads.add(thread_id)
            if max_threads and processed_threads >= max_threads:
                print("主题数量达到 --max-threads 限制，提前结束。")
                return
            processed_threads += 1
            print(f"  └─ 抓取主题 {thread_id} 《{link.title}》")
            try:
                for record in crawl_thread(session, link, target_ids, delay_range):
                    yield record
            except RuntimeError as exc:
                print(f"    [线程 {thread_id}] 下载失败: {exc}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    """Define CLI arguments."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("scraper.config.json"),
        help="包含 start_page/end_page/target_ids 的 JSON 配置文件",
    )
    parser.add_argument("--start-page", type=int, help="起始版面页码 (默认为配置文件或1)")
    parser.add_argument("--end-page", type=int, help="结束版面页码 (默认为配置文件或1)")
    parser.add_argument(
        "--id",
        dest="ids",
        action="append",
        help="只记录指定的ID，可重复使用多个 --id",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/familylife_posts.csv"),
        help="保存结果的 CSV 文件路径",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        help="相邻请求之间的固定等待秒数 (留空则按配置/默认随机等待)",
    )
    parser.add_argument(
        "--max-threads",
        type=int,
        default=None,
        help="限制最多抓取的主题数量 (0 表示不限，用于调试/抽样)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    start_page = args.start_page or int(cfg.get("start_page", 1))
    end_page = args.end_page or int(cfg.get("end_page", start_page))
    start_page = max(1, start_page)
    end_page = max(start_page, end_page)

    target_ids_cfg = cfg.get("target_ids") or []
    if isinstance(target_ids_cfg, str):
        target_ids_cfg = [target_ids_cfg]
    target_ids_raw: List[str] = []
    if target_ids_cfg:
        target_ids_raw.extend(target_ids_cfg)
    if args.ids:
        target_ids_raw.extend(args.ids)
    target_ids = sanitize_ids(target_ids_raw)
    delay_range = resolve_delay_range(args.delay, cfg.get("delay"))
    cfg_max_threads = 0
    try:
        cfg_max_threads = int(cfg.get("max_threads", 0))
    except (TypeError, ValueError):
        cfg_max_threads = 0
    if args.max_threads is None:
        max_threads = cfg_max_threads
    else:
        max_threads = args.max_threads
    max_threads = max(0, max_threads)

    ensure_directory(args.output)
    session = create_session()
    fields = [
        "board_page",
        "thread_id",
        "thread_title",
        "thread_url",
        "thread_page",
        "floor",
        "author_id",
        "post_time",
        "ips",
    ]
    total_written = 0
    with args.output.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields)
        writer.writeheader()
        for record in crawl_board(
            session,
            start_page,
            end_page,
            target_ids,
            delay_range,
            max_threads=max_threads,
        ):
            if not record.thread_id:
                continue
            writer.writerow(record.__dict__)
            total_written += 1
    session.close()
    print(f"完成，共记录 {total_written} 条帖子。输出文件: {args.output}")


if __name__ == "__main__":
    main()
