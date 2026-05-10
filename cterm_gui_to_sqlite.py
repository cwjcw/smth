#!/usr/bin/env python3
import re
import sqlite3
import time
from pathlib import Path

import win32clipboard
from pywinauto import Desktop
from pywinauto.keyboard import send_keys

DB = Path('data/smth_stock.db')
DB.parent.mkdir(parents=True, exist_ok=True)

SENDER_RE = re.compile(r"发信人[:：]\s*([^\s(]+)")
BOARD_RE = re.compile(r"信区[:：]\s*(\S+)")
TITLE_RE = re.compile(r"标\s*题[:：]\s*(.+)")
TIME_RE = re.compile(r"发信站[:：]\s*(.+)")


def get_clipboard_text() -> str:
    txt = ''
    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
            data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            txt = data if isinstance(data, str) else ''
            win32clipboard.CloseClipboard()
            return txt
        except Exception:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass
            time.sleep(0.1)
    return txt


def copy_screen() -> str:
    # Re-focus CTerm and retry multiple copy shortcuts.
    w = Desktop(backend='uia').window(title_re='CTerm.*')
    for keys in ('%ec', '^c', '^a^c'):
        try:
            w.set_focus()
            time.sleep(0.08)
            send_keys(keys, pause=0.02)
            time.sleep(0.25)
            t = get_clipboard_text()
            if t:
                return t
        except Exception:
            time.sleep(0.12)
    return ""


def clean_body_lines(lines: list[str]) -> str:
    cleaned = []
    in_quote = False
    for line in lines:
        s = line.strip()
        if not s:
            cleaned.append('')
            continue
        if s.startswith('【 在') and '大作中提到' in s:
            in_quote = True
            continue
        if in_quote and (s.startswith(':') or s.startswith('>')):
            continue
        if in_quote:
            in_quote = False
        if s.startswith(':') or s.startswith('>') or s.startswith('※ 来源') or s.startswith('--'):
            continue
        cleaned.append(line.rstrip())
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(cleaned)).strip()


def parse_post(post_id: int, text: str):
    lines = [x.rstrip('\n') for x in text.split('\n')]
    sender = board = title = post_time = ''
    meta_end = -1
    for i, line in enumerate(lines):
        if not sender:
            m = SENDER_RE.search(line)
            if m: sender = m.group(1).strip()
        if not board:
            m = BOARD_RE.search(line)
            if m: board = m.group(1).strip()
        if not title:
            m = TITLE_RE.search(line)
            if m: title = m.group(1).strip()
        if not post_time:
            m = TIME_RE.search(line)
            if m:
                post_time = m.group(1).strip()
                meta_end = i
    if not (sender and board and title and post_time):
        return None
    body = clean_body_lines(lines[meta_end + 1:])
    return (post_id, sender, board, title, post_time, body, text)


def ensure_db(conn):
    conn.execute('''
    CREATE TABLE IF NOT EXISTS posts (
      post_id INTEGER PRIMARY KEY,
      author TEXT NOT NULL,
      board TEXT NOT NULL,
      title TEXT NOT NULL,
      post_time TEXT NOT NULL,
      body TEXT NOT NULL,
      raw_text TEXT
    )
    ''')
    conn.commit()


def upsert(conn, rec):
    conn.execute('''
    INSERT INTO posts(post_id,author,board,title,post_time,body,raw_text)
    VALUES(?,?,?,?,?,?,?)
    ON CONFLICT(post_id) DO UPDATE SET
      author=excluded.author,board=excluded.board,title=excluded.title,
      post_time=excluded.post_time,body=excluded.body,raw_text=excluded.raw_text
    ''', rec)


def extract_ids(board_text: str):
    ids = []
    for ln in board_text.splitlines():
        m = re.match(r'^\s*(\d{5,8})\s+', ln)
        if m:
            i = int(m.group(1))
            if i not in ids:
                ids.append(i)
    return ids[:10]


def gather_post_text(post_id: int) -> str:
    send_keys(str(post_id) + '{ENTER}', pause=0.02)
    time.sleep(0.6)
    chunks = []
    seen = set()
    for _ in range(60):
        t = copy_screen()
        if t and t not in seen:
            seen.add(t)
            chunks.append(t)
        merged = '\n'.join(chunks)
        if '发信人' in merged and '发信站' in merged and '标  题' in merged and ('下面还有喔' not in t):
            break
        if '下面还有喔' in t or '(SPACE)' in t or '按空格' in t:
            send_keys('{SPACE}', pause=0.01)
            time.sleep(0.25)
            continue
        if '发信人' in merged and '发信站' in merged and '标  题' in merged:
            break
        send_keys('{SPACE}', pause=0.01)
        time.sleep(0.2)
    send_keys('q', pause=0.02)
    time.sleep(0.25)
    return '\n'.join(chunks)


def main():
    w = Desktop(backend='uia').window(title_re='CTerm.*')
    w.set_focus()
    time.sleep(0.3)

    # try to return to stock list by canceling transient prompts
    for _ in range(3):
        send_keys('q', pause=0.02)
        time.sleep(0.2)
    board_screen = copy_screen()
    if '请输入要转载的讨论区名称' in board_screen:
        send_keys('{ESC}', pause=0.02)
        time.sleep(0.2)
        board_screen = copy_screen()
    if '请输入要转载的讨论区名称' in board_screen:
        send_keys('q', pause=0.02)
        time.sleep(0.2)
        board_screen = copy_screen()

    def is_board_list(txt: str) -> bool:
        return ("离开[←,e]" in txt and "阅读[→,r]" in txt) or ("讨论区 [Stock]" in txt and "备忘录[Ctrl-W]" in txt)

    # If not in stock post list, go by user-confirmed path.
    if not is_board_list(board_screen):
        send_keys('s{ENTER}', pause=0.03)
        time.sleep(0.4)
        send_keys('stock{ENTER}', pause=0.03)
        time.sleep(1.0)
        board_screen = copy_screen()

    ids = extract_ids(board_screen)
    if not ids:
        print('ERR: cannot parse latest ids from current CTerm screen')
        print(board_screen[:1000])
        return

    conn = sqlite3.connect(DB)
    ensure_db(conn)
    ok = 0
    for pid in ids:
        raw = gather_post_text(pid)
        rec = parse_post(pid, raw)
        if rec:
            upsert(conn, rec)
            ok += 1
            conn.commit()
        time.sleep(0.15)

    conn.commit()
    conn.close()
    print('ids=', ids)
    print('ok=', ok)
    print('db=', DB)


if __name__ == '__main__':
    main()
