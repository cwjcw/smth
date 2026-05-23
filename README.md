# smth

当前仓库核心脚本：`run_parallel_ids_to_sqlite.py`。

功能：
- 多账号并行抓取（默认每账号 1 个会话窗口）
- 直接写入 SQLite（`data/smth_stock.db`）
- 可选同时落 CSV
- 支持断点续跑（checkpoint）
- 支持“持续抓取直到命中指定标题后自动停止”

## 环境准备

1. 安装 Python 3.11+
2. 安装依赖

```bash
pip install -r requirements.txt
```

3. 配置环境变量（参考 `.env.example`）

```bash
cp .env.example .env
```

## 常用运行方式

先进入目录并加载环境变量：

```bash
cd /data/automation/code/personal/smth
source .venv/bin/activate
set -a && source .env && set +a
```

### 1) 抓固定 ID 区间（直接写库）

```bash
python run_parallel_ids_to_sqlite.py \
  --start-id 500000 \
  --end-id 500099 \
  --db data/smth_stock.db \
  --no-csv \
  --sessions-per-account 1 \
  --split-mode round_robin \
  --retries 1 \
  --short-wait 0.15 \
  --long-wait 0.45 \
  --sqlite-batch 50 \
  --reconnect-after-short-partial 3 \
  --no-resume
```

### 2) 正式抓取：从 500000 开始，直到公告标题自动停

```bash
python run_parallel_ids_to_sqlite.py \
  --start-id 500000 \
  --until-title "版面严禁封神,切勿轻信代客理财" \
  --db data/smth_stock.db \
  --no-csv \
  --sessions-per-account 1 \
  --split-mode round_robin \
  --retries 1 \
  --short-wait 0.15 \
  --long-wait 0.45 \
  --sqlite-batch 50 \
  --reconnect-after-short-partial 3 \
  --no-resume
```

说明：
- `--start-id 500000`：从帖子 ID 500000 开始。
- `--until-title "版面严禁封神,切勿轻信代客理财"`：标题中包含该文本即停止，不要求完全相等。
- `--no-csv`：只写 SQLite，不生成 CSV。
- `--sessions-per-account 1`：每个 SMTH 账号只登录 1 个窗口。
- `--split-mode round_robin`：多个账号轮流分配 ID。
- `--sqlite-batch 50`：每 50 条提交一次，运行中也能看到数据库逐步有数据。
- `--reconnect-after-short-partial 3`：连续 3 次拿到过短 partial 返回时自动重连该账号。
- `--no-resume`：本次强制从 500000 开始，不使用历史 checkpoint。

### 3) 续跑模式（从上次 checkpoint 继续）

```bash
python run_parallel_ids_to_sqlite.py \
  --until-title "版面严禁封神,切勿轻信代客理财" \
  --db data/smth_stock.db \
  --no-csv \
  --sessions-per-account 1 \
  --split-mode round_robin \
  --retries 1 \
  --short-wait 0.15 \
  --long-wait 0.45 \
  --sqlite-batch 50 \
  --reconnect-after-short-partial 3
```

续跑时不用写 `--start-id`。脚本会读取 `data/smth_stock.last_id`，自动从 `last_id + 1` 开始。如果没有 checkpoint，首次运行需要使用上面的正式抓取命令提供 `--start-id 500000`。

## 断点续跑

- 默认 checkpoint 文件：`data/smth_stock.last_id`
- 默认行为：启动时自动读取 checkpoint，从 `last_id + 1` 继续
- 可用 `--checkpoint-file` 指定路径
- 可用 `--no-resume` 关闭续跑

## 运行保护和失败日志

- 默认锁文件：`data/smth_stock.run.lock`
- 作用：防止同时启动多个脚本，避免同一个 SMTH 账号重复登录。
- 可用 `--no-lock` 绕过锁（一般不建议）。
- 默认失败日志：`data/smth_stock.fail.log`
- 失败日志会记录失败的 `post_id`、worker 和原因。

## 主要参数

- `--start-id`：起始帖子 ID；续跑时可省略，由 checkpoint 决定
- `--end-id`：结束帖子 ID（固定区间模式）
- `--until-title`：命中该标题后停止（持续模式）
- `--accounts`：账号列表，格式 `u1:p1,u2:p2,u3:p3`
- `--sessions-per-account`：每账号会话数，默认 `1`
- `--db`：SQLite 路径，默认 `data/smth_stock.db`
- `--no-csv`：不输出 CSV（推荐大批量时开启）
- `--csv`：CSV 路径（不加 `--no-csv` 时生效）
- `--sqlite-batch`：SQLite 批量写入大小；正式运行建议 `50`
- `--reconnect-after-short-partial`：连续短 partial 返回后的自动重连阈值，建议 `3`
- `--batch-size`：持续模式每轮分配 ID 数，默认 `300`
- `--fail-log-file`：失败日志路径，默认 `data/smth_stock.fail.log`
- `--lock-file`：单实例锁文件，默认 `data/smth_stock.run.lock`

## 输出说明

结束后会打印：
- `SUMMARY total=... ok=... miss=... fail=...`
- `SQLITE db=... imported=...`
- `FAIL_LOG file=...`
- 持续模式命中时：`STOP matched_post_id=... matched_title=...`
