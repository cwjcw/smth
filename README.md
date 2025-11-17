# smth
用来抓取水木社区某些版面的帖子（默认 FamilyLife）。`smth_scraper.py` 会遍历版面页、进入每个主题、解析 `发信人` 与 `[FROM: …]` 中的 IP，并把结果写入 CSV。

## 环境准备
1. 安装 Python 3.11+。
2. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```

## 参数文件（scraper.config.json）

`scraper.config.json` 用来保存“页数参数”和“ID 参数”，默认内容如下：

```json
{
  "boards": [
    "FamilyLife"
  ],
  "start_page": 1,
  "end_page": 1,
  "target_ids": [],
  "delay": {
    "min": 2,
    "max": 5
  },
  "max_threads": 0
}
```

- `boards`：需要抓取的版面列表，填写的是 `/nForum/board/XXXX` 里的 `XXXX`（例如 `FamilyLife`、`PieLove` 等）。
- `start_page`：起始的版面页码，默认 1。
- `end_page`：结束的版面页码，默认 1，即仅抓取第一页，按需修改。
- `target_ids`：需要过滤的发信人 ID 列表；留空代表记录所有帖子。
- `delay`：请求间的等待时间。可以是单个数字（固定秒数），也可以像示例一样写成 `min/max`，脚本会在 2~5 秒之间随机等待。
- `max_threads`：单次运行最多抓取多少个主题，0 表示不限。

需要调整抓取范围或只记录某个 ID 时，只需要改这个文件，无需修改代码。

## 运行示例

```bash
# 使用配置文件中的页码/ID 设置
python smth_scraper.py

# 指定多个版面、页码范围，只记录某个 ID，并把延迟固定成 1 秒
python smth_scraper.py --board FamilyLife --board PieLove --start-page 3 --end-page 5 --id wpn --id anotherID --delay 1

# 调试或抽样抓取可用 --max-threads 限制主题数量
python smth_scraper.py --start-page 1 --end-page 1 --max-threads 3
```

脚本默认把数据写入 `data/familylife_posts.csv`，字段包括：

- `board_name`：版面名称（即 `boards` 中的值）；
- `board_page`：主题出现的版面页码；
- `thread_id / thread_title / thread_url`：主题信息；
- `thread_page`：所在主题页码；
- `floor`：楼层（如“楼主”“1”“2”）；
- `author_id`：从 `发信人:` 后到 `(` 之前截取的 ID；
- `post_time`：整行 `发信站:` 字符串；
- `ips`：在该楼层内所有 `f0XX`（除了 `f000`、`f006`）字体里出现的 `[FROM:…]` 信息，每条记录格式为 `f0XX:IP`，多条使用 `|` 连接。

可以通过 `--board` 重写要抓取的版面列表；`--output` 更改结果文件；`--delay`（仅数字）会把等待时间固定为指定秒数，留空则采用配置文件或默认的 2~5 秒随机间隔；`--max-threads` 可覆盖配置中的主题数量限制，用于调试或采样。
