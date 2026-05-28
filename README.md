# smth

当前仓库核心脚本：`run_parallel_ids_to_sqlite.py`。

功能：
- 多账号并行抓取（每个账号严格只允许 1 个连接）
- 直接写入 SQLite（`data/smth_stock.db`）
- 可选同时落 CSV
- 支持断点续跑（checkpoint）
- 支持“持续抓取直到命中指定标题后自动停止”
- 支持低频重连、账号禁用、审核提示账号级冷却后续抓
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
  --per-account-interval 3 \
  --request-jitter 1 \
  --audit-slowdown-multiplier 2 \
  --max-per-account-interval 15 \
  --recovery-successes 500 \
  --account-rest-every 250 \
  --account-rest-seconds 300 \
  --sqlite-batch 50 \
  --audit-block-cooldown 300 \
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
  --per-account-interval 3 \
  --request-jitter 1 \
  --audit-slowdown-multiplier 2 \
  --max-per-account-interval 15 \
  --recovery-successes 500 \
  --account-rest-every 250 \
  --account-rest-seconds 300 \
  --sqlite-batch 50 \
  --audit-block-cooldown 300 \
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
- `--per-account-interval 3`：同一账号两次读帖之间至少间隔 3 秒，优先降低触发审核提示的概率。
- `--request-jitter 1`：每次读帖间隔额外增加 0 到 1 秒随机抖动，避免多个账号节奏过于整齐。
- `--audit-slowdown-multiplier 2`：账号遇到审核提示后，后续读帖间隔自动翻倍。
- `--max-per-account-interval 15`：账号自适应降速最多降到每 15 秒一帖。
- `--recovery-successes 500`：账号连续成功读取 500 条后尝试恢复一档速度。
- `--account-rest-every 250`：每个账号处理 250 条后主动休息一次。
- `--account-rest-seconds 300`：主动休息 300 秒，休息期间不主动断开登录连接。
- `--sqlite-batch 50`：每 50 条提交一次，运行中也能看到数据库逐步有数据。
- `--audit-block-cooldown 300`：任一账号遇到审核提示时，只暂停该账号 300 秒，然后从遇到审核的帖子继续；其他账号继续抓取。
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
  --per-account-interval 3 \
  --request-jitter 1 \
  --audit-slowdown-multiplier 2 \
  --max-per-account-interval 15 \
  --recovery-successes 500 \
  --account-rest-every 250 \
  --account-rest-seconds 300 \
  --sqlite-batch 50 \
  --audit-block-cooldown 300 \
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

## partial 和审核提示冷却

- `partial` 表示只读到不完整页面，无法解析出正常帖子结构，例如提示页、菜单残留、分页残片或很短的返回内容。
- 默认 `--reconnect-after-short-partial 0`，遇到 partial 不主动重连，只记录失败后继续下一条。
- 如果返回 `全站审核中，暂不能查看本文内容`，脚本会把它当作当前帖子的临时读取异常处理。
- 默认只暂停触发审核提示的账号 300 秒，该账号会关闭当前连接，冷却结束后重新登录并继续读取同一个帖子；其他账号继续抓取。

## 频率控制

- 推荐先用保守频率：每个账号两次读帖之间至少间隔 `3` 秒，并额外加入 `0` 到 `1` 秒随机抖动。
- 多账号并行时总速率约等于账号数除以单账号间隔；例如 3 个账号保守参数约每秒 1 条左右。
- 如果某个账号遇到审核提示，该账号的读帖间隔会自动翻倍，最高到 `15` 秒；连续成功读取 `500` 条后再逐步恢复速度。
- 根据当前日志，首次审核大约出现在全局 900-1000 条附近；3 个账号并行折算约每账号 300 条。推荐先按每账号 `250` 条主动休息 `300` 秒，给站点侧留出缓冲。
- 主动休息只暂停对应账号 worker，不主动关闭 telnet 连接；如果连接在休息期间被服务端断开，下一次读取会按原有重连逻辑恢复。
- 如果连续运行稳定、没有审核提示，再逐步把 `--per-account-interval` 降到 `2` 或 `1.5`；如果仍频繁遇到审核提示，则调到 `5`。

## checkpoint 安全

- 持续抓取时，checkpoint 只写入从起始 ID 开始已经连续完成的最大 ID。
- 如果某个账号在某个帖子上审核冷却，其他账号可以继续抓后面的帖子，但 checkpoint 不会越过这个冷却中的帖子；进程中断后会从未连续完成的位置继续，避免漏帖。

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
- `--per-account-interval`：同一账号两次读帖之间的最小间隔秒数；保守运行建议 `3`，也可设置 `SMTH_PER_ACCOUNT_INTERVAL`。
- `--request-jitter`：每次读帖间隔额外随机抖动秒数；保守运行建议 `1`，也可设置 `SMTH_REQUEST_JITTER`。
- `--audit-slowdown-multiplier`：账号遇到审核提示后读帖间隔放大倍数，默认 `2`；也可设置 `SMTH_AUDIT_SLOWDOWN_MULTIPLIER`。
- `--max-per-account-interval`：账号自适应降速后的最大读帖间隔秒数，默认 `15`；也可设置 `SMTH_MAX_PER_ACCOUNT_INTERVAL`。
- `--recovery-successes`：账号连续成功读取多少条后尝试恢复一档速度，默认 `500`；也可设置 `SMTH_RECOVERY_SUCCESSES`。
- `--account-rest-every`：单个账号每处理多少条后主动休息，默认 `250`；`0` 表示关闭，也可设置 `SMTH_ACCOUNT_REST_EVERY`。
- `--account-rest-seconds`：单个账号主动休息秒数，默认 `300`；也可设置 `SMTH_ACCOUNT_REST_SECONDS`。
- `--reconnect-after-short-partial`：连续 partial 返回后的自动重连阈值；`0` 表示不因 partial 主动重连。
- `--min-reconnect-interval`：同一账号两次连接尝试之间的最小间隔秒数；也可设置 `SMTH_MIN_RECONNECT_INTERVAL`。
- `--audit-block-cooldown`：单个账号遇到审核提示后的暂停秒数，默认 `300`；也可设置 `SMTH_AUDIT_BLOCK_COOLDOWN`。
- `--audit-block-retries`、`--audit-block-wait`：兼容旧参数；当前审核提示逻辑使用 `--audit-block-cooldown`。
- `--max-audit-blocks`：兼容旧参数；当前不再因审核提示停用账号。
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
