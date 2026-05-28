# smth

当前仓库核心脚本：`run_parallel_ids_to_sqlite.py`。

功能：
- 多账号并行抓取（每个账号严格只允许 1 个连接）
- 直接写入 SQLite（`data/smth_stock.db`）
- 可选同时落 CSV
- 支持断点续跑（checkpoint）
- 支持“持续抓取直到命中指定标题后自动停止”
- 支持低频重连、账号禁用、审核限制自动熔断
- 持续抓取模式使用账号长连接 worker，不会按批次主动重登

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

先进入目录并激活虚拟环境。脚本会自动读取当前目录的 `.env`；手动 `source .env` 也可以。

```bash
cd /data/automation/code/personal/smth
source .venv/bin/activate
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
  --idle-timeout 0.06 \
  --sqlite-batch 50 \
  --reconnect-after-short-partial 0 \
  --min-reconnect-interval 60 \
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
  --idle-timeout 0.06 \
  --sqlite-batch 50 \
  --reconnect-after-short-partial 0 \
  --min-reconnect-interval 60 \
  --no-resume
```

说明：
- `--start-id 500000`：从帖子 ID 500000 开始。
- `--until-title "版面严禁封神,切勿轻信代客理财"`：标题中包含该文本即停止，不要求完全相等。
- `--no-csv`：只写 SQLite，不生成 CSV。
- `--sessions-per-account 1`：每个 SMTH 账号只允许 1 个连接；传其他值会拒绝运行。
- `--split-mode round_robin`：多个账号轮流分配 ID。
- `--idle-timeout 0.06`：读取到数据后，如果连续 0.06 秒没有新数据，就立刻进入下一步；网络慢或 partial 增多时可调到 `0.08`/`0.10`。
- `--sqlite-batch 50`：每 50 条提交一次，运行中也能看到数据库逐步有数据。
- `--reconnect-after-short-partial 0`：partial 返回不主动重连；建议在账号受限时使用。
- `--min-reconnect-interval 60`：同一账号两次连接尝试至少间隔 60 秒。
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
  --idle-timeout 0.06 \
  --sqlite-batch 50 \
  --reconnect-after-short-partial 0 \
  --min-reconnect-interval 60
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
- 失败日志会记录失败的 `post_id`、账号、原因和一段 `raw_preview`，便于判断卡在哪个页面。
- 启动后会打印实际启用账号，例如 `using accounts: cwjcw, ccxm, mynewlife`。
- 成功进版会打印 `account=... entered board=stock`。
- 同一账号不会同时建立多个连接；如果账号列表里重复出现同一个用户名，只保留第一次。

## partial 和账号熔断

- `partial` 表示只读到不完整页面，无法解析出正常帖子结构，例如提示页、菜单残留、分页残片或很短的返回内容。
- 默认 `--reconnect-after-short-partial 0`，遇到 partial 不主动重连，只记录失败后继续下一条。
- 如果返回 `全站审核中，暂不能查看本文内容`，脚本会认为该账号被审核限制挡住。
- 默认同一账号连续 3 次遇到审核限制提示后会自动停用该账号，本次运行中不再继续使用它。

## 主要参数

- `--start-id`：起始帖子 ID；续跑时可省略，由 checkpoint 决定
- `--end-id`：结束帖子 ID（固定区间模式）
- `--until-title`：命中该标题后停止（持续模式）
- `--accounts`：账号列表，格式 `u1:p1,u2:p2,u3:p3`
- `--disabled-accounts`：禁用账号列表，格式 `u1,u2`；也可设置 `SMTH_DISABLED_ACCOUNTS=u1,u2`
- `--sessions-per-account`：必须为 `1`，每个账号严禁多个同时连接。
- `--db`：SQLite 路径，默认 `data/smth_stock.db`
- `--no-csv`：不输出 CSV（推荐大批量时开启）
- `--csv`：CSV 路径（不加 `--no-csv` 时生效）
- `--sqlite-batch`：SQLite 批量写入大小；正式运行建议 `50`
- `--idle-timeout`：读取响应时的数据空闲判断秒数，默认 `0.06`；也可设置 `SMTH_IDLE_TIMEOUT`。
- `--reconnect-after-short-partial`：连续 partial 返回后的自动重连阈值；`0` 表示不因 partial 主动重连。
- `--min-reconnect-interval`：同一账号两次连接尝试之间的最小间隔秒数；也可设置 `SMTH_MIN_RECONNECT_INTERVAL`。
- `--max-audit-blocks`：同一账号连续遇到审核限制提示多少次后停用；默认 `3`。
- `--login-fail-sleep`：登录失败后等待多少秒再重试，默认 `600`。
- 登录成功但未进入目标版面时，脚本会关闭当前连接，并遵守 `--min-reconnect-interval` 后再尝试重连。
- `--batch-size`：持续模式 ID 队列缓冲和 `until-progress` 汇报粒度，默认 `300`；不会触发账号按批次重登
- `--fail-log-file`：失败日志路径，默认 `data/smth_stock.fail.log`
- `--lock-file`：单实例锁文件，默认 `data/smth_stock.run.lock`

## 输出说明

结束后会打印：
- `SUMMARY total=... ok=... miss=... fail=...`
- `SQLITE db=... imported=...`
- `FAIL_LOG file=...`
- 持续模式命中时：`STOP matched_post_id=... matched_title=...`
