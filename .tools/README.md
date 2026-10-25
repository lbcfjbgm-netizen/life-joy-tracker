# yamtrack/.tools 脚本清单

> 逐个读源码写成（2026-10-25）。**只描述现状：没有移动 / 重命名 / 删除 / 修改任何脚本，也没有执行任何脚本。**
> 目录里**当前**有 **64 个 `.py` + 16 个 `.js`**。旁边的 `yamtrack\scripts\` 还有 21 个 `.py`，不算本文件主体，但第四节说明了分工。

> ⚠️ **这个目录是活的**。我写这份清单的 20 分钟里，文件数从 58 → 70 → 64（有别的任务在同一个目录里新建又删掉临时脚本）。**如果下面某个文件名找不到了，别以为是清单写错，先 `ls` 一下。**

---

## 一、怎么用（先看这三件事）

### 1. 从哪个目录跑

```powershell
cd "C:\Users\USERNAME\Downloads\life joy\yamtrack"
```

**绝大多数脚本自己写死了绝对路径，所以在 `yamtrack\` 下跑最稳。**
只有 4 个必须先在 `src` 里跑（代码里用相对路径 `src\db\...` / `db\db.sqlite3`）：`_h2_probe.py`、`ai_copy_counts.py`、`ai_copy_schema.py`、`weread_report.py`。
另外 `import_ai_copy.py` / `import_ai_note.py` / `fix_book_progress.py` / `verify_book_progress.py` / `mark_reviewed_007.py` / `verify_ai_copy.py` 会**自己 chdir 到 src**，随便在哪跑都行。

### 2. 用哪个解释器

```powershell
.venv\Scripts\python.exe          # 有黑窗口，能看输出 —— 干活用这个
.venv\Scripts\pythonw.exe         # 无窗口，只给计划任务用
```

实测 **Python 3.12.10**。别用系统全局的 `python`，依赖不全。

### 3. 跑之前先备份

真正要备份的只有一个文件：`src\db\db.sqlite3`。

```powershell
# 站点不用停（在线热备，走 SQLite 的 backup API）
.venv\Scripts\python.exe scripts\hot_backup.py
.venv\Scripts\python.exe .tools\_backup_before_snapshot.py   # 同上，文件名带时间戳
```

备份都落在 `.tools\backups\`（现有 49 个 `.sqlite3`）。**动库前先看那里有没有当天的文件。**

---

## 二、危险清单（会写数据库的脚本）

> **会写数据库的 20 个**（A 段 9 个 + B 段 11 个），另有 4 个不写库但会改 `.env` 或生成备份（C 段）。跑之前必须：① 先备份 ② 优先用 dry-run ③ 看清输出再决定。
> 标「⚠️ 无 dry-run」的，**一跑就写，没有后悔药**。

### A. 无 dry-run，直接写（9 个，最危险）

| 脚本 | 写什么 | 警告 |
|---|---|---|
| ⚠️ `apply_blocklist_status.py` | `app_game.status` → Dropped / 还原 | 一跑就写。读 `raw\steam_blocklist.json` + 备份库做还原 |
| ⚠️ `fix_headers_940.py` | `app_item.image`（940 条） | 默认 `--stage all` **就写库**；只有 `--stage probe` 只探测。会先自动热备 |
| ⚠️ `weread_backfill_progress.py` | 书的 `progress` + 阅读时长 | 只改 progress，不新建不删除 |
| ⚠️ `import_ai_copy.py` | `app_aicopy` | `update_or_create`，可重复跑 |
| ⚠️ `import_ai_note.py` | `app_aicopy.note` | 只动 note 字段 |
| ⚠️ `mark_reviewed_007.py` | `app_aicopy.reviewed=True`（仅 batch-007） | 走 ORM，范围极小 |
| ⚠️ `make_superuser.py` | `users_user` 建/改管理员 | 见坑 8，密码硬编码 |
| ⚠️ `_mem_probe.py` | `users_user` 的偏好字段（统计页日期范围 / 列表密度） | 用 test client 写偏好，**不碰活站不重启**，但库是真被改了（脚本末尾会自己重置） |
| ⚠️ `rollback_abandon.py` | `app_game.status` 还原 | **dry-run 是反的**：什么都不加就是真回滚，加 `--dry-run` 才只看不改 |

### B. 有 dry-run / 默认只读（11 个，相对安全，但仍要看清输出）

| 脚本 | 写什么 | dry-run 怎么用 |
|---|---|---|
| `mark_abandoned.py` | `app_game.status` → Dropped | ✅ **默认只读**，`--execute` 才写；写前自动热备 + 生成 `abandon_rollback.json` |
| `fix_images_p0.py` | `app_item.image`（41 条） | ✅ **默认 `--stage test` 只探测**，`fix` 才写 |
| `fix_posters.py` | `app_item.image` | ✅ **默认只出报告**，`--write` 才写 |
| `apply_store_meta.py` | `app_item` 的 title/synopsis/image | ✅ `--dry-run`；要 `--apply` 且不带 `--dry-run` 才写 |
| `apply_title_en.py` | `app_item.title_en` | ✅ `--dry-run`；只填空不覆盖 |
| `import_family_games.py` | 新增 `app_item`/`app_game` 行 | ✅ **默认就是 dry-run**，`--apply` 才写；只 `get_or_create` |
| `backfill_douban_watch_date.py` | `app_game.end_date` | ✅ **默认就是 dry-run**，`--apply` 才写；**不联网** |
| `fix_book_progress.py` | `app_book.progress`/`status` | ✅ **默认就是 dry-run**，`--apply` 才写 |
| `fix_completion_dates.py` | `app_game.end_date`/`completion_estimated`/`status` | ✅ `--dry-run`；走外网成就接口，慢 |
| `fetch_steam_reviews.py` | `app_game.steam_review_pct`/`status` | ✅ `--dry-run`；只填原本 NULL 的 |
| `snapshot_playtime.py` | 新建 `app_playtime_snapshot` 月度行 | ✅ **默认就是干跑**，`--apply` 才写；同一个月跑多少次都不重复 |

### C. 不写数据库，但会改 `.env` 或生成备份（4 个）

| 脚本 | 干什么 | 风险 |
|---|---|---|
| `backup_before_migration.py` | 热备到 `db-before-memory-*.sqlite3` 并校验 | 只写备份，安全 |
| `_backup_before_snapshot.py` | 同上，文件名带时间戳 `db-before-snapshot-*.sqlite3` | 只写备份，安全 |
| `steam-login.py` | 开 Chrome 登录 Steam，把令牌写 `.env` | 交互式，需人工 |
| `refresh-steam-token.py` | 静默刷新令牌写 `.env` | 拿不到令牌时**绝不覆盖**旧值 |

**完全不写库、可以放心跑的（38 个）**：`chk_status.py`、`final_check.py`、`audit_completed_images.py`、`probe_achievements.py`、`scan_headers_batch.py`、`warm_images.py`、`warm_images_media.py`、`watchdog.py`、`verify_login.py`、`verify_ai_copy.py`、`verify_book_progress.py`、`rebuild_blocklist.py`、`fetch_family_reviews.py`、`ai_copy_counts.py`、`ai_copy_schema.py`、`weread_report.py`、`weread_final.py`、`douban_proxy_pool.py`、`proxy_pool_boot.py`、`_h2_probe.py`、`_check_extra_charts.py`、`profile_home.py`、`profile_home2.py`、`profile_home3.py`、`profile_home4.py`、`profile_stats.py`、`_accept940.py`、`_accept940b.py`、`_accept940c.py`、`_accept940d.py`、`_accept940e.py`、`_ctype940.py`、`_diag940.py`、`_dist940.py`、`_final940.py`、`_report940.py`、`_suffix940.py`、`_verify940.py`。

其中 `audit_completed_images.py`、`probe_achievements.py`、`scan_headers_batch.py`、`warm_images.py`、`warm_images_media.py` 是真用 `mode=ro` / `PRAGMA query_only=1` 打开的，纯读；`verify_book_progress.py` 的写入全在事务里最后 rollback。

---

## 三、按功能分类的清单

格式：**文件 · 一行说明 · dry-run · 写库 · 外网**

### 【日常运维】站点活着相关的（9 个）

| 文件 | 说明 | dry-run | 写库 | 外网 |
|---|---|---|---|---|
| `watchdog.py` | 每 5 分钟探 8001，连挂 3 次才通过 `schtasks` 拉起站点。探的是 HTTP 而不是端口——Django 会「端口还在、每页 500」 | 无需 | ❌ | ✅ 本地 HTTP |
| `warm_images.py` | 预热**游戏**封面缓存（Redis 30 天 + 磁盘 `raw\covers\`），用和视图完全相同的 `fetch_image`，缓存键 = 上游 URL 的 sha256 | ✅ `--dry-run` | ❌ | ✅ 下载图 |
| `warm_images_media.py` | 同上，但管**书 / 电影 / 剧集**的封面。参数 `--only` / `--limit` / `--dry-run` / `--apply` | ✅ `--dry-run` | ❌ | ✅ 下载图 |
| `snapshot_playtime.py` | 每月给每个游戏存一条「累计游玩时长」快照，下月减这月就是当月时长（历史每日时长原本补不出来） | ✅ 默认干跑 | `--apply` | ❌ |
| `refresh-steam-token.py` | 计划任务每天 03:50 跑。用 `steam-profile\` 专用 Chrome 目录静默读令牌写 `.env`；拿不到就**不覆盖** | 退出码 1 = 失败 | ❌（改 `.env`） | ✅ 起 Chrome |
| `steam-login.py` | 一次性登录助手，只需跑一次。之后 cookie 长期有效（实测到 2027 年） | 交互式 | ❌（改 `.env`） | ✅ 起 Chrome |
| `proxy_pool_boot.py` | 登录时启动豆瓣代理池内核（只起 `verge-mihomo.exe`，**绝不碰 Clash Verge GUI**），日志写 `logs\proxy_pool.log` | 无 | ❌ | ✅ |
| `douban_proxy_pool.py` | 豆瓣导入用的「香港节点池 + 一键换出口 IP」。子命令 `fetch`/`test`/`up`/`use X`/`next`/`ip`，`-h` 看全部 | 无 | ❌ | ✅ 拉订阅测节点 |
| `verify_login.py` | 打登录页 + 试登录，确认账号密码还能用 | 无 | ❌ | ✅ 本地 HTTP |

> 站点启停**不在 `.tools`**，在 `yamtrack\` 根目录：`start_silent.py`（静默起 Redis+Celery+Django，带 `CREATE_BREAKAWAY_FROM_JOB`）、`stop_silent.py`（停全部）、`set_steam_token.py`（手工粘令牌的兜底）。
> 日常请双击 `start-win.vbs` / `stop-win.vbs`，**不要**从 DSH 终端手工起。

### 【数据修复】改库里的内容（14 个）

| 文件 | 说明 | dry-run | 写库 | 外网 |
|---|---|---|---|---|
| `mark_abandoned.py` | 把「买了没玩、没成就、2020 年前」的老游戏标成 Dropped。默认只出名单，`--execute` 才落库，写前自动热备 + 写 `abandon_rollback.json` | ✅ 默认只读 | ✅ | ❌ |
| `rollback_abandon.py` | 上面那个的反悔脚本，按 `abandon_rollback.json` 逐条还原 status | ✅ `--dry-run`（**反过来！**） | ✅ | ❌ |
| `apply_blocklist_status.py` | 按 `raw\steam_blocklist.json` 把被拉黑的游戏批量改成 Dropped；新规则放行的从备份库读回原 status | ❌ ⚠️ | ✅ | ❌ |
| `fix_posters.py` | 查 `image like '%header%'` 的横版图，推导 `library_600x900.jpg` 竖版并 HEAD 验证。**默认只出报告** | ✅ 默认只读 | `--write` | ✅ HEAD |
| `fix_headers_940.py` | P1 竖版封面写库（940 条），只改 `app_item.image`，复用 p0 的 HEAD/指纹逻辑 | ⚠️ `--stage probe` | ✅ 默认写 | ✅ HEAD |
| `fix_images_p0.py` | 封面修复 P0（41 条），只改 `app_item.image`，HEAD 200 才写 | ✅ 默认 `stage=test` | `--stage fix` | ✅ HEAD |
| `fix_completion_dates.py` | 用 Steam 成就**最后解锁时间**当通关日期。只写 `end_date`/`completion_estimated`/`status` 三个字段 | ✅ `--dry-run` | ✅ | ✅ Steam 成就接口（走 7897） |
| `fix_book_progress.py` | 修微信读书书的进度天花板：`progress>100` 和「已完成但进度≠100」两类脏数据。只动 `app_book` | ✅ 默认 dry-run | `--apply` | ❌ |
| `apply_store_meta.py` | 把 `scripts\fetch_steam_store.py` 抓到的商店元数据回填（**只填空，绝不覆盖已有值**） | ✅ `--dry-run` | ✅ | ❌ 读本地 JSON |
| `apply_title_en.py` | 用 Steam 英文商店 JSON 填 `Item.title_en`，只填空、名字还含中文的跳过 | ✅ `--dry-run` | ✅ | ❌ 读本地 JSON |
| `backfill_douban_watch_date.py` | 给已导入的豆瓣电影补 `end_date`，**完全不发请求**（读 `.tools\_douban_all.json` 里已存的 646 条） | ✅ 默认 dry-run | `--apply` | ❌ |
| `weread_backfill_progress.py` | 微信读书第一批导入把 `progress` 写成 0 了，这里回填百分比（278 行） | ❌ ⚠️ | ✅ | ❌ |
| `rebuild_blocklist.py` | 按规则重算拉黑名单（差评 + 不到 10 小时 + 最近没玩，三条全中才拉黑）。**只写 JSON，不写库** | 无 | ❌（写 JSON） | ❌ 读本地 |
| `fetch_steam_reviews.py` | 回填 `app_game.steam_review_pct` 好评率，用 `appreviewhistogram`（全语言口径），只填原本 NULL 的 | ✅ `--dry-run` | ✅ | ✅ Steam |

### 【导入】（6 个）

| 文件 | 说明 | dry-run | 写库 | 外网 |
|---|---|---|---|---|
| `import_family_games.py` | 把「家庭库有、本库没有、过了质量门槛」的游戏导进来。复用 `steam.py` 的 `primary_cover_url()`，只用 `get_or_create`，**已存在的 appid 一律跳过** | ✅ 默认 dry-run | `--apply` | ❌ 读本地 JSON |
| `import_ai_copy.py` | 把 AI 金句 JSON 回填进 `app_aicopy`。可重复跑（以 item_id 为唯一键） | ❌ ⚠️ | ✅ | ❌ |
| `import_ai_note.py` | 把手札 JSON 回填进 `app_aicopy.note`，不碰 summary/quote/已有 batch。可重复跑 | ❌ ⚠️ | ✅ | ❌ |
| `mark_reviewed_007.py` | 审核收尾：batch-007 的条目 `reviewed=True`，走 ORM | ❌ ⚠️ | ✅ | ❌ |
| `make_superuser.py` | 建/改管理员账号，读 `SU_USERNAME`/`SU_PASSWORD`/`SU_EMAIL` | ❌ ⚠️ | ✅ | ❌ |
| `fetch_family_reviews.py` | 抓家庭库里「本库没有的」游戏好评率，按规则筛出可导入的，产出 `steam_family_reviews.json` + `steam_blocklist.json` | 无 | ❌（写 JSON） | ✅ Steam |

### 【探测 / 一次性诊断】只读，可归档（12 个）

一轮任务结束后的残留，**留着有考古价值，但别当日常工具**。

| 文件 | 说明 | 写库 | 外网 |
|---|---|---|---|
| `chk_status.py` | 打状态分布 + 抽 8 个 completed 看成就数 | ❌ | ❌ |
| `final_check.py` | Steam 导入指标总览（条目数 / 有封面 URL 数…） | ❌ | ❌ |
| `audit_completed_images.py` | 只读核查已通关游戏的竖/横版图可用性，输出 `AC-report.txt` + `AC-completed-games.csv` | ❌（`mode=ro`） | ✅ HEAD |
| `probe_achievements.py` | 抽样测 Steam 成就能补回多少。`mode=ro` + `PRAGMA query_only=1`，启动还做「写必须失败」自检 | ❌ | ✅ Steam |
| `scan_headers_batch.py` | 全库 header 扫描，42 个一批调 `IStoreBrowseService/GetItems`，产出 `P1-header-scan.csv`，支持断点续跑 | ❌（`mode=ro`） | ✅ Steam |
| `profile_home.py` | 首页/统计页 SQL 条数、最慢 SQL、返回行数 | ❌ | ❌ |
| `profile_home2.py` | cProfile 找 Python 侧热点 + 抓正确性基线 | ❌ | ❌ |
| `profile_home3.py` | 把首页一个分区拆成几阶段计时，定位 500ms 去哪了 | ❌ | ❌ |
| `profile_home4.py` | 实验：实例化 2086 行 vs `values()` 轻量行，哪个快 | ❌ | ❌ |
| `profile_stats.py` | 统计页各函数分别计时，定位 235ms | ❌ | ❌ |
| `ai_copy_counts.py` | 打印「绝不能被改」的表行数（`app_game`/`app_item`/`app_highlight`/`app_note`） | ❌ | ❌ |
| `ai_copy_schema.py` | 打印 `app_aicopy` 表结构 + 行数 | ❌ | ❌ |

### 【940 封面那一轮的一次性残留】建议归档（12 个）

`.tools` 里 12 个 `_*940*.py` 全是同一轮任务的产物，彼此高度重复（`_accept940.py` 到 `_accept940e.py` 五个同族），**全部只读或只出报告**：

| 文件 | 说明 |
|---|---|
| `_accept940.py` | 首页状态 + 抽 15 个走 relay 抓状态码/Content-Type + 确认 header 剩几行 |
| `_accept940b.py` | 首页登录表单 + 详情页 relay 图 |
| `_accept940c.py` | 首页 img 统计（host 分布 / 关键词出现次数） |
| `_accept940d.py` | 首页 relay 图数量 + curl 前 6 张 |
| `_accept940e.py` | 详情页 3 个 portrait + 列表页 + **940 条落库核对** + 22 条真空缺未动 |
| `_ctype940.py` | 两个 Steam 图片域名的 HEAD Content-Type 对比 |
| `_diag940.py` | 库内计数快照（app_game / app_item / header / 竖版） |
| `_dist940.py` | 图片 URL 形态分布（header / 600x900 / capsule / portrait） |
| `_suffix940.py` | CSV 里新 URL 的后缀/host 分布 + 22 条 verdict |
| `_report940.py` | **生成 P1 写库报告**（`ui-redesign\shots\impl\P1-940-writeback.txt`），只读库、只写 txt |
| `_verify940.py` | 写库前后快照对比（库 vs `db-before-940-*.sqlite3`） |
| `_final940.py` | 终检：integrity、库大小、关键计数、全列指纹、备份目录、脚本存在性 |

### 【外部粘进来的代码块，不是能直接跑的脚本】（3 个）

这三个都是往 `src\` 拼代码时留下的**碎片**，**没有 `__main__` 入口，直接跑什么都不会发生**（既不报错也不干活）。别当脚本用：

| 文件 | 说明 |
|---|---|
| `_statistics_backup.py` | `src\app\statistics.py` 的**旧版快照**（35518 字节，当前 42884）。有 15 条 import、26 个函数定义，但**没有 `__main__`**，所以仍是碎片不是脚本。改之前的备份，留着考古 |
| `_extra_charts_block.py` | 「四张新图表」的 8 个函数定义（**连 import 都没有**），准备插进 statistics.py 用的 |
| `_extra_charts_js_block.js` | 上面那四张表的 JS 片段 |

### 【前端 / 截图 / 审计，`.js` 16 个】

全部是 **Chrome 9222 端口 CDP** 小工具，**只读，不写库、不改站点**。需要站点在跑且 Chrome 开了 `--remote-debugging-port=9222`。

| 文件 | 说明 |
|---|---|
| `audit-cdp.js` | CDP 导航 + 全页截图 + 页面内测量（主工具） |
| `audit-cdp2.js` / `audit-cdp3.js` | 上面那两个变体 |
| `audit-eval-a11y.js` | 页面可访问性检查：抓所有可聚焦元素，列 tabindex / aria |
| `audit-eval-contrast.js` | 用 canvas 取色，算文字与背景对比度 |
| `audit-eval-drawer.js` | 点汉堡按钮，测移动端抽屉能否打开 |
| `audit-eval-filter.js` | 点「已完成 / 全部 / 已暂停」+ 排序，测筛选是否生效 |
| `audit-eval-heat.js` | 抓热力图的 `rounded-sm` + `w-4` 方块，按 class 归类统计 |
| `audit-eval-journal.js` | 日志页：查碎图（`naturalWidth===0`）+ 热力图填充 |
| `audit-eval-modal.js` / `audit-eval-modal2.js` | 点状态按钮，测弹窗能否打开（2 是修正版） |
| `audit-eval-scroll.js` | 测无限滚动：卡片数 + 加载指示器 + 总数文案 |
| `final-shots.js` | 出 `final-*.png` 定妆照 |
| `fix-card-shot.js` | 截图 + DOM 测量，出 `fix-card-*.png` |
| `p3a-probe.js` | 量可用高度 |
| `_extra_charts_js_block.js` | 见上一节，是代码片段，不是工具 |

### 【已归档】两个目录，合计 135 个 `.py`

| 目录 | 内容 | 说明 |
|---|---|---|
| `.tools\archive_scripts\` | **77 个 `.py`** | 已挪走的旧脚本，绝大多数无 docstring、命名如 `_probe_xxx.py`。可随时还原（= 搬回来，注意别和现有同名冲突） |
| `.tools\.tools-archive\scratch_20261025\` | **88 个文件（其中 58 个 `.py`）** | 2026-10-25 那一轮的一次性草稿，按日期打包 |
| `.tools\logs\` | 6 个 `.py` | ⚠️ **别搬走**：`sync_rest.py` 是活的日常脚本 |

> ⚠️ `logs\sync_rest.py` 会写库（全量成就同步，约 90 分钟，断点续传）。它是**生产脚本**，不要跟 archive 里的东西混为一谈。

---

## 四、`scripts\` 和 `.tools\` 的分工

- **`yamtrack\scripts\`（21 个 `.py`）**：早期规范的脚本目录，路径按 `src` 写。`refresh_status.py`、`fix_covers.py`、`fetch_cn_names.py`、`hot_backup.py`、`prune_backups.py` 这些「半正式」的都在这儿。
- **`yamtrack\.tools\`**：后来所有任务脚本落地的地方，混了生产脚本 + 一次性探测。

**共同点**：绝大多数自己写死绝对路径，从 `yamtrack\` 跑最稳。
**必须 `cd src`**：`_h2_probe.py`、`ai_copy_counts.py`、`ai_copy_schema.py`、`weread_report.py`。

---

## 五、已验证的坑（读代码 + 只读查库核对过，不是猜的）

### 坑 1（最危险）：`scripts\refresh_status.py` 会把 1603 个 Dropped 全改成 Paused

**代码依据**：`decide()` 只返回三种值 —— `Completed`（全成就）/ `Planning`（`progress==0`）/ `Paused`（其余）。`Status` 枚举里明明有 `DROPPED`，但这个函数**永远不返回它**。

文件顶部有个 `AUTO_STATUSES` 集合（只含 Planning/InProgress/Paused/Completed，注释写着「用户手动设过的状态不覆盖」）—— 但 **`grep` 全文件只有定义那 1 处，从来没被引用**，是死代码。所以那句注释是假的，Dropped 照样被冲掉。

**只读复算结果**（我按它的 `decide()` 逻辑在只读连接上跑了一遍，**没执行脚本本体**）：

```
steam 游戏 4935 个，需要改 1774 条
  Dropped -> Paused      1587
  Completed -> Paused     167      ← 部分已通关的还会被降级
  Dropped -> Planning      16
  In progress -> Paused     3
  Completed -> Planning     1
```

库现状：`Completed 285 / Dropped 1603 / In progress 3 / Paused 958 / Planning 2086`。跑完那 1603 个「已放弃」全变「已暂停」。

**结论**：目的只是「全成就 → 已完成」的话，**别跑它**。非要跑就先 `--dry-run` 逐行看完，确认你能接受这 1587+16 条 Dropped 消失。

### 坑 2：`fix_images_p0.py` 和 `fix_headers_940.py` 的默认行为**相反**

- `fix_images_p0.py`：`--stage` 默认 `test` → **默认只探测，安全**
- `fix_headers_940.py`：`--stage` 默认 `all` → **默认直接写库**（会先自动热备）

两个名字很像，跑之前先看默认值。

### 坑 3：`rollback_abandon.py` 的 dry-run 是反的

它是 `--dry-run` 才只读，**什么都不加就是真回滚**。和 `mark_abandoned.py`（默认只读、`--execute` 才写）正好相反，容易记混。

### 坑 4：`_report940.py` 的「写」是写 txt，不是写库

`w()` 那行里打印的 `UPDATE app_item SET image=? ...` 是**报告正文里的文字**，不是真执行的 SQL。脚本用 `q()` 只读库，只 `open(REPORT,"w")` 写报告文件。别被 grep 吓到。

### 坑 5：`_final940.py` 里的 `hashlib` 不是写库

`h.update(...)` 是算指纹（`hashlib`），不是 SQL 的 UPDATE。同理 `_verify940.py` 全程只读。

### 坑 6：`.tools\logs\sync_rest.py` 是活的，别当垃圾清

它在 `logs\` 目录、挨着 `archive_scripts\`，很容易被当残留。实际是每天要跑的全量成就同步（约 90 分钟，断点续传）。

### 坑 7：`AGENTS.md` 里那句 `scripts\refresh_status.py` **是对的**，别去「修」

`life joy\AGENTS.md` 第 201-202 行的写法是：先 `cd "C:\Users\USERNAME\Downloads\life joy\yamtrack"`，再 `.venv\Scripts\python.exe scripts\refresh_status.py`。这个相对路径从 `yamtrack\` 出发正好指向 `yamtrack\scripts\refresh_status.py`，**文件确实存在**。

我在 `AGENTS.md` / `_交接简报.md` / `_项目卡.md` 里 grep 过 `..\scripts`，**一个都没搜到** —— 所以「原来写成 `..\scripts\` 是错的」这个说法在现有文件里查无实据，不用改。
真正会踩的是：在 `yamtrack\` 根目录下把 `scripts\` 敲成 `.tools\scripts\`（那个目录不存在）。

### 坑 8：`make_superuser.py` 的默认密码是硬编码的

`SU_PASSWORD` 默认 `admin123456`。**部署到服务器必须显式传环境变量**，否则等于开了个公开后门。

---

## 六、部署到服务器时要注意的

我扫了全部 64 个 `.py` 的路径写法，分三类：

### 1. 写死 `C:\Users\USERNAME\...` 绝对路径 —— **换机器必坏**，共 27 个

```
_accept940.py  _accept940d.py  _accept940e.py  _backup_before_snapshot.py
_diag940.py  _dist940.py  _final940.py  _mem_probe.py  _report940.py
_suffix940.py  _verify940.py  apply_blocklist_status.py
audit_completed_images.py  backup_before_migration.py  chk_status.py
final_check.py  fix_headers_940.py  fix_images_p0.py  fix_posters.py
import_ai_copy.py  import_ai_note.py  mark_abandoned.py
mark_reviewed_007.py  probe_achievements.py  rollback_abandon.py
scan_headers_batch.py  verify_ai_copy.py
```

这 27 个里 12 个是 940 残留、4 个是纯探测，**根本不用搬服务器**。真要上服务器的只有 `mark_abandoned.py` / `rollback_abandon.py` / `apply_blocklist_status.py` / `fix_posters.py` / `audit_completed_images.py` / `chk_status.py` / `final_check.py`，上之前把那几行路径改掉。

### 2. 用 `__file__` 相对定位 —— **换机器能活**，共 26 个

```
apply_store_meta.py  apply_title_en.py  backfill_douban_watch_date.py
douban_proxy_pool.py  fetch_family_reviews.py  fetch_steam_reviews.py
fix_book_progress.py  fix_completion_dates.py  import_family_games.py
profile_home.py  profile_home2.py  profile_home3.py  profile_home4.py
profile_stats.py  proxy_pool_boot.py  rebuild_blocklist.py
refresh-steam-token.py  snapshot_playtime.py  steam-login.py
verify_book_progress.py  warm_images.py  warm_images_media.py
watchdog.py  weread_backfill_progress.py  weread_final.py
_check_extra_charts.py
```

### 3. 用相对路径，**必须 `cd src` 才能跑**，共 4 个

`_h2_probe.py`、`ai_copy_counts.py`、`ai_copy_schema.py`、`weread_report.py`

### 4. 剩下 7 个没有路径依赖（无 import / 全靠 cwd）

`_accept940b.py`、`_accept940c.py`、`_ctype940.py`、`make_superuser.py`、`verify_login.py`、`_extra_charts_block.py`、`_statistics_backup.py`

### 5. 其他上服务器要注意的

- **Windows 专属（上 Linux 要重写）**：`watchdog.py`（走 `schtasks`）、`proxy_pool_boot.py`（找 `verge-mihomo.exe`）、`start_silent.py`（`CREATE_BREAKAWAY_FROM_JOB`）、`steam-login.py`（找本机 Chrome）、`_accept940.py`/`_accept940d.py`（调 `curl.exe`）。
- **代理**：`fetch_steam_reviews.py`、`fix_completion_dates.py` 写死 `http://127.0.0.1:7897`。`fetch_steam_reviews.py` 有 `--proxy auto/on/off` 可以关。
- **部署顺序建议**：先搬「数据修复 + 备份」那几个；探测类和 940 残留**一个都不用搬**。

---

## 七、覆盖情况自查

- `.tools` 下 `.py`：**64 个，全部覆盖**（逐条读 `ast.get_docstring()` + 前 12 行 + grep 写库关键字和参数关键字）。
- `.js`：**16 个**，按族说明（12 个 CDP 审计工具 + 2 个截图（`final-shots.js` / `fix-card-shot.js`）+ 1 个探测（`p3a-probe.js`）+ 1 个代码片段（`_extra_charts_js_block.js`））。
- 另说明两个 archive 目录（`archive_scripts` 77 个、`.tools-archive\scratch_20261025` 88 个文件含 58 个 `.py`）和 `logs\` 6 个。
- 也交代了不在 `.tools` 但容易混的：`yamtrack\` 根的 3 个、`scripts\` 的 21 个。

**为什么和开工时的 58 个不一样**：开工时是 58 个 `.py`，中途涨到 70，定稿时 **64** —— 有另一个任务在同一个目录里反复新建/删除临时脚本。新增并被我读过的有 6 个：`_check_extra_charts.py`、`_statistics_backup.py`、`_extra_charts_block.py`（02:42）、`warm_images_media.py`（02:44）、`_backup_before_snapshot.py`（02:48）、`snapshot_playtime.py`（02:52）。
期间还短暂存在过 `_tmp_book_hosts.py`、`_tmp_probe.py`、`_tmp_probe2.py`、`_tmp_probe3.py`、`_tmp_verify.py`、`_tmp_verify2.py` 六个，**在我定稿前被那个任务的作者自己删掉了**，所以没进正式清单（都是读 `_tmp_samples.tsv` 的临时探测）。

**怎么核对的**：每条说明都来自模块 docstring + 前 12 行的常量/路径，再用 `grep` 确认写库关键字（`commit(`、`UPDATE`、`objects.*.update`）和参数关键字（`add_argument`、`--dry-run`、`--apply`、`--write`、`--execute`、`--stage`）。没有一条是看文件名猜的。

---

## 附：三个抽查（说明 ↔ 源码对照）

| 脚本 | 我写的说明 | 我核对的源码 |
|---|---|---|
| `fix_posters.py` | 「默认只出报告，`--write` 才写库」 | docstring 原文 `python fix_posters.py  # dry-run: report only, NEVER writes the DB` + `--write  # step 2: backup + UPDATE image only (gated)`；`grep` 到 `add_argument("--write", action="store_true", ...)`；第 39 行连接串 `file:...?mode=ro` → 默认确实只读 |
| `refresh_status.py`（`scripts\`） | 「会把 Dropped 改成 Paused，只认三种状态」 | 第 43-55 行 `decide()` 只有三个 `return`，无 `DROPPED`；`grep -c AUTO_STATUSES` = 1（只定义没使用）；只读复算得 1774 条要改，其中 `Dropped -> Paused` 1587 |
| `snapshot_playtime.py` | 「默认干跑，`--apply` 才写」 | docstring 原文 `python .tools/snapshot_playtime.py  # 预演，不写库（默认）` + `--apply  # 真正写入`；`grep` 到 `--dry-run` / `--apply` 两个参数定义 |

---

*本文件只新建，没有移动 / 重命名 / 删除 / 修改任何脚本；没有执行任何脚本；没有 `--apply` / `--write` / `--execute`；没有跑 `refresh_status.py`（连 dry-run 都没跑，只用只读连接复算了它的判定函数）；没有重启站点。*
