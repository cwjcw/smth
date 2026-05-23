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
  --no-csv
```

### 2) 从某个 ID 持续抓，直到命中标题自动停

```bash
python run_parallel_ids_to_sqlite.py \
  --start-id 1191100 \
  --until-title "【公告】版面严禁封神,切勿轻信代客理财" \
  --db data/smth_stock.db \
  --no-csv
```

### 3) 强制从指定 ID 重跑（忽略断点）

```bash
python run_parallel_ids_to_sqlite.py \
  --start-id 1191100 \
  --until-title "【公告】版面严禁封神,切勿轻信代客理财" \
  --db data/smth_stock.db \
  --no-csv \
  --no-resume
```

## 断点续跑

- 默认 checkpoint 文件：`data/smth_stock.last_id`
- 默认行为：启动时自动读取 checkpoint，从 `last_id + 1` 继续
- 可用 `--checkpoint-file` 指定路径
- 可用 `--no-resume` 关闭续跑

## 主要参数

- `--start-id`：起始帖子 ID（必填）
- `--end-id`：结束帖子 ID（固定区间模式）
- `--until-title`：命中该标题后停止（持续模式）
- `--accounts`：账号列表，格式 `u1:p1,u2:p2,u3:p3`
- `--sessions-per-account`：每账号会话数，默认 `1`
- `--db`：SQLite 路径，默认 `data/smth_stock.db`
- `--no-csv`：不输出 CSV（推荐大批量时开启）
- `--csv`：CSV 路径（不加 `--no-csv` 时生效）
- `--sqlite-batch`：SQLite 批量写入大小，默认 `2000`
- `--batch-size`：持续模式每轮分配 ID 数，默认 `300`

## 输出说明

结束后会打印：
- `SUMMARY total=... ok=... miss=... fail=...`
- `SQLITE db=... imported=...`
- 持续模式命中时：`STOP matched_post_id=... matched_title=...`
