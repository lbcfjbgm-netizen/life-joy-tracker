# 部署教程 · life joy（Steam 游戏记录站）

> 这套脚本是**分模块**的，一格一格的，哪个卡住只影响它自己，不会让整套全废。
> 每个脚本**默认只检查、不动手**（dry-run）。真要它动手，必须自己加 `-Apply`（或 `-Execute`）参数。

---

## 一、这是什么

把本地 Steam 游戏库做成一个能看的网站。数据存在一个 SQLite 文件里，图片缓存在本地磁盘上。

三个东西必须同时活着：
- Django 网站（端口 8001）
- Celery worker（后台导入任务）
- Redis（Celery 的消息队列）

外加一个「看门狗」，每 5 分钟探一下 8001，挂了就自动拉起来。

---

## 二、前置要求

| 项 | 要求 | 怎么查 |
|---|---|---|
| Python | **3.12**（3.11 可能行，未测） | `python --version` |
| Node.js | 18+（只给 Tailwind 编译用） | `node --version` |
| Chrome | 装了就行（令牌刷新 + 截图验证要用） | 路径 `C:\Program Files\Google\Chrome\Application\chrome.exe` |
| 磁盘 | **3 GB 以上空闲**（图片缓存会长到 1.3 GB 以上） | 资源管理器 |
| 端口 8001 | 没被别的程序占用 | `netstat -ano | findstr :8001` |

**不用 Docker。** 这套东西是原生跑的。

---

## 三、五步快速部署（必须跑的五个）

每条命令都可以**直接复制粘贴**。**先 cd 到项目根目录**（有 `deploy\` 和 `src\` 的那一层），命令里的 `.\deploy\xxx.ps1` 是相对路径；脚本会自己算出项目根，不用改路径。

### 第 1 步 · 环境体检

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\01_check_env.ps1"
```

**成功长什么样**：一片 `[OK]`，最后一行是「体检通过」。
**失败怎么办**：每一条 `[FAIL]` 后面都写了原因和补救办法，照着做。

纯只读，不会改任何东西。

---

### 第 2 步 · 装依赖

```powershell
# 先看要装什么（不装）
powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\02_install_deps.ps1"

# 确认没问题，真的装
powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\02_install_deps.ps1" -Apply
```

**成功长什么样**：`pip` 最后一行是 `Successfully installed ...`，Tailwind 能打印版本号。
**卡住怎么办**：pip 慢就配国内镜像源，别去折腾代理。本项目之前实测 `registry.npmmirror.com` 3 秒装完。

---

### 第 3 步 · 数据库

```powershell
# 只看，不动
powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\03_setup_db.ps1"

# 真跑 migration（库会被先备份）
powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\03_setup_db.ps1" -Apply
```

**这条最重要**：如果你的数据库里已经有几千个游戏，`-Apply` 之前它会**先备份**到 `.tools\backups\`，备份完才动。备份文件可以还原。

**成功长什么样**：`Applying users.00xx_... OK` 若干行，最后 `migrate` 无红色报错。

---

### 第 4 步 · 配置文件

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\04_setup_env.ps1"
```

它**只读键名、绝不打印任何值**，所以可以放心跑，输出不会泄密。
它会告诉你 `.env` 里缺了哪几个键，每个键该怎么填。

`.env` 是纯文本，用记事本就能改。**改完不用重启**，因为代码里每次都现读。

---

### 第 5 步 · 静态文件

```powershell
# 看
powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\05_static.ps1"

# 做
powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\05_static.ps1" -Apply
```

它做两件事：跑 `collectstatic`，再编译 Tailwind。
**它已经处理了 `NODE_PATH` 这个坑**（不设会报 "Can't resolve 'tailwindcss'"），你不用自己设。

---

## 四、可选步骤

### 第 6 步 · 预热图片（可选，但强烈建议）

```powershell
# 只看数量
powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\06_warm_images.ps1"

# 真下载（约 20 分钟，1.3 GB）
powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\06_warm_images.ps1" -Apply -Only all
```

**为什么值得跑**：不跑的第一次访问，每张图都要等 1.8 秒从 Steam 拉。跑完是 40 毫秒从本地读。差距是 45 倍。

**不跑也行**，就是头几天慢一点，点开过的页面会自己缓存下来。

### 第 7 步 · 注册计划任务（可选）

```powershell
# 看要注册哪几个
powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\07_tasks.ps1"

# 真注册
powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\07_tasks.ps1" -Apply
```

四个任务：

| 名称 | 干什么 | 什么时候 |
|---|---|---|
| `SteamTracker Start` | 启动整个站点 | 手动 / 被看门狗调 |
| `SteamTracker Watchdog` | 每 5 分钟探一次，挂了自动拉起 | 每 5 分钟 |
| `SteamTracker TokenRefresh` | 刷新 Steam 令牌 | 每天 03:50 |
| `SteamTracker RecentPlay` | 最近玩过的游戏标成「进行中」 | 每天 04:00 |

> 第五个「自动导入 Steam」是**站内**的 Celery beat 配的，不用在这里注册。

### 第 8 步 · 验收（推荐每次都跑）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\08_verify.ps1"
```

它抓 8 个页面、读 4 个数据基线数字、量 3 个页面的响应耗时，最后出一张表。
**纯只读**，随便跑。

---

## 五、坑表（这些全是这个项目真实踩过的）

| # | 坑 | 现象 | 正确做法 |
|---|---|---|---|
| 1 | `DEBUG` 改成 `False` | 静态文件全 404，页面变成纯文字 | **`DEBUG` 必须保持 `True`**。要藏 debug_toolbar 就设 `INTERNAL_IPS = []`，别动 `DEBUG` |
| 2 | 设了 `DB_HOST` | 直接报 Postgres 连接错误 | **绝对不能设**。本项目用 SQLite |
| 3 | Tailwind 没设 `NODE_PATH` | 报 "Can't resolve 'tailwindcss'" | `$env:NODE_PATH="$env:APPDATA\npm\node_modules"`，脚本 05 已自动处理 |
| 4 | 编译完忘了复制 | 浏览器用的是旧 CSS，看不到改动 | 必须把 `src\static\css\main.css` 再复制一份到 `src\staticfiles\css\` |
| 5 | 改了 `.html` 没重启 | 磁盘上是新的，网页还是旧的 | Django 5.2 默认缓存模板 + 本站 `--noreload`，**改模板必须重启** |
| 6 | `manage.py` 在错目录跑 | 报找不到 manage.py | 必须 `cd src` 再跑 |
| 7 | Celery 用默认池 | Windows 上跑不起来 | 必须 `--pool solo`，而且 **beat 要单独起**（`--beat` 会直接报错退出） |
| 8 | 用 `Start-Process` 起站点 | 日志全干净，站点莫名消失 | 会被 DSH 的作业对象连坐杀掉。**只能用 `schtasks /run /tn "SteamTracker Start"`** 或双击 `start-win.vbs` |
| 9 | 计划任务没设优先级 | 页面慢 24~35% | 必须写 `<Priority>4</Priority>`（Normal） |
| 10 | 用了 cloudflare/akamai 的封面域名 | 图片全超时 | 只用 `media.steampowered.com` 和 `shared.fastly.steamstatic.com` |
| 11 | 新机器上没有图片缓存 | 图片全慢 | 跑 `06_warm_images.ps1 -Apply`，缓存不进版本库 |
| 12 | 搜索一直转圈 / 报错 | `store.steampowered.com` 连不通 | 这是本机网络环境问题，**不是部署失败**。等网络恢复或走代理 |
| 13 | 验证时用了 `?status=` / `?sort=` / `?layout=table` | 用户的界面偏好被悄悄改掉 | **只能用 `?layout=grid&density=4/8/12`** |

---

## 六、日常运维

以下路径都相对于项目根目录。

```powershell
# 启动
.\start-win.vbs

# 停止
.\stop-win.vbs

# 看日志
.\.tools\logs\
#   site.err / celery-worker.err / celery-beat.err / redis.out / watchdog.log

# 备份数据库（就复制这一个文件，但要在站点停着的时候）
.\src\db\db.sqlite3
```

**备份数据库最稳的方式**：双击 `stop-win.vbs` → 等 6 秒 → 复制 `db.sqlite3` → 再双击 `start-win.vbs`。
站点正在跑的时候用文件复制可能拿到一个半截的库。

**图片缓存**：`.tools\raw\covers\`，现在约 1.3 GB / 9000 个文件。删了不会丢数据，只是变慢，可以重新预热。

---

## 七、常见故障速查

### 打不开 / 转圈

1. 先看站点在不在：`Invoke-WebRequest http://127.0.0.1:8001/ -TimeoutSec 10`
2. 不在 → 双击 `start-win.vbs`
3. 还不在 → 看 `.tools\logs\site.err` 最后 30 行

### 页面是纯文字、没样式

`DEBUG` 被改成 `False` 了。改回 `True`，重启。

### 图片全是灰块

1. 先确认缓存目录有没有东西：`Get-ChildItem .tools\raw\covers -Recurse -File | Measure-Object`
2. 空的话跑 `06_warm_images.ps1 -Apply`
3. 有东西但还是灰块 → 大概率是网络到不了 Steam，看 `site.err` 里有没有 timeout

### 数据不对 / 少了东西

先查基线，这四个数不该乱变：

```powershell
$env:PYTHONIOENCODING="utf-8"
& ".\.venv\Scripts\python.exe" -c "import sqlite3;c=sqlite3.connect(r'file:src/db/db.sqlite3?mode=ro',uri=True);q=lambda s:c.execute(s).fetchone()[0];print('game',q('select count(*) from app_game'));print('item',q('select count(*) from app_item'))"
```

对不上就去 `.tools\backups\` 找最近的备份还原。

### 搜索报 500 或转很久

`store.steampowered.com` 连不通。**这是环境问题不是代码问题**（已确认：`media.steampowered.com` 和 `api.steampowered.com` 都通，只有 store 这个域名不通）。
临时办法：直接去游戏库页面用本地搜索，不经过 Steam。

---

## 八、脚本一览

| 脚本 | 默认行为 | 要动手 |
|---|---|---|
| `01_check_env.ps1` | 只读体检 | 不需要 |
| `02_install_deps.ps1` | 只报要装什么 | `-Apply` |
| `03_setup_db.ps1` | 只查库状态 | `-Apply` |
| `04_setup_env.ps1` | 只读键名 | 不需要（自己改 `.env`） |
| `05_static.ps1` | 只查 | `-Apply` |
| `06_warm_images.ps1` | 只报数量 | `-Apply` |
| `07_tasks.ps1` | 只打印将执行的命令 | `-Apply` |
| `08_verify.ps1` | 只读验证 | 不需要 |

全部脚本都支持 `-Help`（打印说明）和 `-Root <路径>`（换项目根目录）。

---

## 九、从 GitHub clone 下来，第一步做什么

八个脚本里的 `$Root` **默认自动定位**：取脚本所在目录（`deploy`）的上一级，也就是项目根。
所以 clone 到任何位置都不用改路径，也不用给 `-Root`。先 cd 到项目根目录，然后：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\01_check_env.ps1
```

只有当 `deploy\` 被挪出项目根（非常规改法）时，才需要手工 `-Root "项目根路径"`。

顺序：**复制 `.env.example` 成 `.env` → `01_check_env.ps1` → `02_install_deps.ps1 -Apply`
→ `03_setup_db.ps1 -Apply` → `04_setup_env.ps1` 补键 → `05_static.ps1 -Apply` → 双击 `start-win.vbs`**。

两个前提：`.tools\redis\` 那份便携 Redis 是**必需**的（删了站点起不来）；
数据库 `src\db\db.sqlite3` **不在仓库里**，首次要跑 `03_setup_db.ps1` 建空库再导入自己的游戏。
