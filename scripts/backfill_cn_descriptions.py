# -*- coding: utf-8 -*-
"""用 Steam 本地缓存里的中文简介补 `Item.synopsis`（零网络，只读本地 JSON）。

数据源：`D:\\steam\\userdata\\<account id>\\config\\librarycache\\<appid>.json`
（`<account id>` 是 Steam 账号在本机的数字目录名，因人而异；用
`--cache-dir` 或环境变量 `STEAM_LIBRARYCACHE_DIR` 指定，不写死。）
每个文件是个数组，其中形如 `["descriptions", {"version": 1, "data": {...}}]`
的一项里带 `strFullDescription`（Steam 商店的中文简介，带 BBCode 标记）。

规则（按铁律）：
  * **只补当前简介为空的条目**，已有内容一律跳过，绝不覆盖（可能被手改过）；
  * 只认含中文的描述（纯英文的不补，免得把英文简介塞进中文站）；
  * 不联网、不猜、不编造：缓存里没有就留空；
  * BBCode 去掉转成纯文本，图片链接整段丢弃。

用法（在 src 目录跑）：
    ..\\.venv\\Scripts\\python.exe ..\\scripts\\backfill_cn_descriptions.py            # 默认 dry-run
    ..\\.venv\\Scripts\\python.exe ..\\scripts\\backfill_cn_descriptions.py --apply    # 真写库
    ..\\.venv\\Scripts\\python.exe ..\\scripts\\backfill_cn_descriptions.py --report 路径.md
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402

from app.models import Item, MediaTypes, Sources  # noqa: E402


def detect_cache_dir():
    r"""Return the librarycache folder found on this machine, or ``""``.

    The folder name is the Steam account id, which differs per person, so the
    only portable way to find it is to look for it under the usual install
    locations: ``<Steam>\userdata\<account id>\config\librarycache``.
    """
    steam_dirs = (
        r"C:\Program Files (x86)\Steam",
        r"C:\Program Files\Steam",
        r"D:\steam",
        r"D:\Steam",
        r"E:\steam",
        r"E:\Steam",
    )
    for steam_dir in steam_dirs:
        userdata = Path(steam_dir) / "userdata"
        if not userdata.is_dir():
            continue
        for account in sorted(userdata.iterdir()):
            cache_dir = account / "config" / "librarycache"
            if cache_dir.is_dir():
                return str(cache_dir)
    return ""


def resolve_cache_dir():
    r"""Pick the librarycache folder: env var first, then auto detection.

    Returns ``""`` when nothing is configured and nothing was found; the caller
    turns that into a plain-language hint instead of a traceback.
    """
    override = os.environ.get("STEAM_LIBRARYCACHE_DIR", "").strip()
    if override:
        return override
    return detect_cache_dir()


DEFAULT_CACHE_DIR = resolve_cache_dir()

# 少于这个字数的一律不补：Steam 有时只给一句广告词，那算不上简介
MIN_LEN = 40

CJK = re.compile(r"[\u4e00-\u9fff]")
# 整段丢弃：图片 / 视频 / 预览图，链接本身对读者没意义
DROP_BLOCK = re.compile(
    r"\[(img|video|previewyoutube|url)[^\]]*\].*?\[/\1\]",
    re.I | re.S,
)
# 换行类标记：标题另起一段
BLOCK_TAG = re.compile(r"\[/?(h1|h2|h3|p|list|olist|hr|quote|code|table|tr|td|th|noparse|spoiler)[^\]]*\]", re.I)
# 粗体 / 斜体 / 下划线 / 删除线：只去掉标记，文字保留
INLINE_TAG = re.compile(r"\[/?(b|i|u|strike|h1|h2|h3|spoiler)[^\]]*\]", re.I)
LIST_ITEM = re.compile(r"\[\*\]")
ANY_TAG = re.compile(r"\[[^\]]{1,30}\]")
HTML_TAG = re.compile(r"<[^>]+>")
URL_IN_TEXT = re.compile(r"https?://\S+", re.I)
SEPARATOR_LINE = re.compile(r"^[\s\-_=*~·—]{3,}$")
BLANK_LINES = re.compile(r"\n{3,}")
TRAILING_SPACE = re.compile(r"[ \t]+\n")


def read_cached_description(cache_dir, appid):
    """Return the raw `strFullDescription` of a game, or None when absent."""
    path = os.path.join(cache_dir, f"{appid}.json")
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None

    if not isinstance(payload, list):
        return None
    for entry in payload:
        if isinstance(entry, list) and entry and entry[0] == "descriptions":
            body = entry[1]
            data = body.get("data") if isinstance(body, dict) else None
            value = (data or {}).get("strFullDescription")
            if isinstance(value, str) and value.strip():
                return value
    return None


def clean_description(raw):
    """Turn Steam BBCode into plain text, dropping media and stray links."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = DROP_BLOCK.sub("", text)
    text = BLOCK_TAG.sub("\n", text)
    text = LIST_ITEM.sub("\n• ", text)
    text = INLINE_TAG.sub("", text)
    text = ANY_TAG.sub("", text)
    text = HTML_TAG.sub("", text)

    lines = []
    pending_bullet = False
    for line in text.split("\n"):
        line = URL_IN_TEXT.sub("", line).strip()
        # 只剩图片/链接、纯分隔线、空行都对读者没用
        if not line or SEPARATOR_LINE.match(line):
            continue
        # Steam 的 [*] 用完后常把「•」单独留一行，和标题合回一行更顺眼
        if line == "•":
            pending_bullet = True
            continue
        if pending_bullet:
            line = f"• {line}"
            pending_bullet = False
        lines.append(line)

    text = "\n".join(lines)
    text = BLANK_LINES.sub("\n\n", text)
    text = TRAILING_SPACE.sub("\n", text)
    return text.strip()


def collect_candidates(cache_dir):
    """Return the games whose cached description can fill an empty synopsis."""
    if not cache_dir:
        message = (
            "没找到 Steam 的本地缓存目录 (librarycache)。\n"
            "\n"
            "这个目录藏在 Steam 安装目录下的:\n"
            "    <Steam 安装目录>\\userdata\\<你的账号数字>\\config\\librarycache\n"
            "账号数字每个人都不同, 所以程序没法替你猜死。请二选一:\n"
            "  1) 命令行指路:\n"
            '     --cache-dir "D:\\steam\\<账号数字>\\config\\librarycache"\n'
            "  2) 设个环境变量:\n"
            '     $env:STEAM_LIBRARYCACHE_DIR = "<同上那个路径>"\n'
            "\n"
            "不确定是哪个? 在 Steam 安装目录里搜一下 librarycache 这个文件夹名就有了。"
        )
        raise SystemExit(message)

    if not Path(cache_dir).is_dir():
        message = (
            "这个缓存目录打不开, 可能路径写错了或盘符没挂上:\n"
            f"    {cache_dir}\n"
            "确认一下目录里有没有一堆 <appid>.json 文件, 再重跑。"
        )
        raise SystemExit(message)

    stats = {
        "cache_files": 0,
        "with_description": 0,
        "with_chinese": 0,
        "in_library": 0,
        "already_filled": 0,
        "too_short": 0,
        "not_ours": 0,
    }
    candidates = []

    items = {
        str(item.media_id): item
        for item in Item.objects.filter(
            source=Sources.STEAM.value,
            media_type=MediaTypes.GAME.value,
            season_number__isnull=True,
            episode_number__isnull=True,
        )
    }

    for name in sorted(os.listdir(cache_dir)):
        if not name.endswith(".json"):
            continue
        appid = name[:-5]
        stats["cache_files"] += 1

        raw = read_cached_description(cache_dir, appid)
        if raw is None:
            continue
        stats["with_description"] += 1
        if not CJK.search(raw):
            continue
        stats["with_chinese"] += 1

        item = items.get(appid)
        if item is None:
            stats["not_ours"] += 1
            continue
        stats["in_library"] += 1

        if (item.synopsis or "").strip():
            stats["already_filled"] += 1
            continue

        cleaned = clean_description(raw)
        if len(cleaned) < MIN_LEN or not CJK.search(cleaned):
            stats["too_short"] += 1
            continue

        candidates.append((item, cleaned, len(raw)))

    return stats, candidates


def main():  # noqa: PLR0915
    parser = argparse.ArgumentParser(
        description="用 Steam librarycache 的中文简介补 Item.synopsis（默认 dry-run）",
    )
    parser.add_argument("--apply", action="store_true", help="真写库")
    parser.add_argument(
        "--cache-dir",
        default=DEFAULT_CACHE_DIR,
        help="librarycache 目录（也可用环境变量 STEAM_LIBRARYCACHE_DIR，留空自动探测）",
    )
    parser.add_argument("--report", help="把明细写成 Markdown")
    parser.add_argument("--sample", type=int, default=10, help="报告里列几条样例")
    args = parser.parse_args()

    empty_before = Item.objects.filter(synopsis="").count()
    total_items = Item.objects.count()

    stats, candidates = collect_candidates(args.cache_dir)

    print("=== 只读扫描 librarycache ===")
    print(f"缓存 json 文件          : {stats['cache_files']}")
    print(f"  其中有 strFullDescription : {stats['with_description']}")
    print(f"  其中描述含中文        : {stats['with_chinese']}")
    print(f"  其中在库里（Steam 游戏）: {stats['in_library']}")
    print(f"  已有简介、跳过          : {stats['already_filled']}")
    print(f"  清完太短、不补          : {stats['too_short']}")
    print(f"  不在库里                : {stats['not_ours']}")
    print()
    print("=== 本次可补 ===")
    print(f"待补条目                : {len(candidates)}")
    print(f"补前全库空简介          : {empty_before} / {total_items}")
    print(f"补后预期空简介          : {empty_before - len(candidates)} / {total_items}")

    if args.sample:
        print()
        print(f"=== 样例（前 {min(args.sample, len(candidates))} 条）===")
        for item, cleaned, raw_len in candidates[: args.sample]:
            head = cleaned[:70].replace("\n", " ")
            print(f"{item.media_id:>9}  {len(cleaned):>5} 字  {item.title[:18]:<20} {head}…")

    if args.report:
        lines = [
            "# Steam 中文简介回填",
            "",
            f"- 缓存目录：`{args.cache_dir}`",
            f"- 缓存文件 {stats['cache_files']} 个，其中 {stats['with_description']} 个有简介，"
            f"{stats['with_chinese']} 个含中文",
            f"- 落在库里且当前简介为空、可补：**{len(candidates)}** 条",
            f"- 已有简介跳过 {stats['already_filled']} 条，清完太短 {stats['too_short']} 条",
            f"- 全库空简介 {empty_before} → 预期 {empty_before - len(candidates)}（共 {total_items} 条 item）",
            "",
            "| appid | 游戏 | 简介字数 | 开头 |",
            "|---|---|---|---|",
        ]
        for item, cleaned, _ in candidates:
            head = cleaned[:40].replace("|", "／").replace("\n", " ")
            title = item.title.replace("|", "／")[:24]
            lines.append(f"| {item.media_id} | {title} | {len(cleaned)} | {head}… |")
        Path(args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n报告已写：{args.report}")

    if not args.apply:
        print("\n[dry-run] 没写库。确认数字后加 --apply。")
        return

    if not candidates:
        print("\n没有可补的条目，什么都不写。")
        return

    with transaction.atomic():
        for item, cleaned, _ in candidates:
            # 再确认一次：只写空的那批，别把并发写入的内容盖掉
            if (Item.objects.get(pk=item.pk).synopsis or "").strip():
                continue
            item.synopsis = cleaned
            item.save(update_fields=["synopsis"])

    empty_after = Item.objects.filter(synopsis="").count()
    print("\n=== 已写库 ===")
    print(f"空简介 {empty_before} → {empty_after}（补了 {empty_before - empty_after} 条）")


if __name__ == "__main__":
    main()
