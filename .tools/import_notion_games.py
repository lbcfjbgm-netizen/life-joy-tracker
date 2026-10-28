# -*- coding: utf-8 -*-
"""Import the Notion Switch game table into Yamtrack as manual game entries.

Reads ``.tools/_notion_games.json`` (produced by ``.tools/_notion_fetch.py``),
keeps only the rows that are not already in the local library, and creates the
rest as ``source="manual"`` games.

Writing goes through ``integrations.imports.helpers.bulk_create_media`` on
purpose: ``Game.save()`` would run ``process_status`` / ``fetch_releases`` and
hit the network, which is not acceptable inside a standalone script.

Usage (run from the repo root):
    .venv\\Scripts\\python.exe .tools\\import_notion_games.py --dry-run
    .venv\\Scripts\\python.exe .tools\\import_notion_games.py --dry-run --limit 10
    .venv\\Scripts\\python.exe .tools\\import_notion_games.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
import uuid
from datetime import datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "src"
sys.path.insert(0, str(SRC_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from app.models import Game, Item, MediaTypes, Sources, Status  # noqa: E402
from integrations.imports import helpers  # noqa: E402

NOTION_JSON = ROOT / ".tools" / "_notion_games.json"
PLAN_JSON = ROOT / ".tools" / "_notion_import_plan.json"
SHOT_REPORT_JSON = ROOT / ".tools" / "_notion_shot_download.json"
NOTES = "Notion 导入"

PUNCT = set(
    "·•®™©★☆～~`'\"“”‘’《》〈〉「」『』【】（）()[]{}<>《》、，,。.！!？?:：;；-–—_/\\|&＆+＋*＊#@%^$=  \t　"
)
SPLIT_RE = re.compile(r"\s*[/／|｜]\s*|\s+-\s+|\s+–\s+|\s+—\s+")
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# The library is Steam-only and half of it is filed under the English name, so a
# Chinese Notion title never meets its own row. These pairs were read off the
# actual library rows (see .tools/_crosslang_check.py); each left side is a
# Notion title and each right side a substring that must exist in the library.
CROSSLANG = {
    "纪念碑谷1. 2": "Monument Valley",
    "行尸走肉合集": "The Walking Dead: The Telltale Definitive Series",
    "掘地求升": "Getting Over It",
    "atri我挚爱的时光": "ATRI",
    "武士0": "武士 零",
    "细胞奇点：进化无止境": "Cell to Singularity",
    "女神异闻录5无双": "Persona 5 Strikers",
    "lOOhidden cats 2": "100 hidden cats 2",
    "gta5": "Grand Theft Auto V",
    "slay the princess再见公主": "Slay the Princess",
    "妖之乡dlc：守乡之战": "妖之乡",
    "反物质维度": "Antimatter Dimensions",
    "VA-11：赛博酒保": "VA-11 Hall-A",
    "苏打地牢2": "Soda Dungeon 2",
    "气球塔防6": "Bloons TD 6",
    "巫师3": "巫师3：狂猎",
}


def norm(text: str | None) -> str:
    """Comparison key: width folded, lower case, punctuation and spaces dropped."""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", str(text)).lower()
    kept = []
    for ch in folded:
        if ch in PUNCT:
            continue
        if unicodedata.category(ch).startswith(("P", "Z", "S")):
            continue
        kept.append(ch)
    return "".join(kept).strip()


def variants(title: str) -> set[str]:
    """Every sensible alias a local title carries (brackets, slashes, CJK/Latin)."""
    out: set[str] = set()
    title = (title or "").strip()
    if not title:
        return out
    out.add(title)
    for part in SPLIT_RE.split(title):
        if part.strip():
            out.add(part.strip())
    for inner in re.findall(r"[\(（\[【《]([^\)）\]】》]+)[\)）\]】》]", title):
        if inner.strip():
            out.add(inner.strip())
    stripped = re.sub(r"[\(（\[【《][^\)）\]】》]*[\)）\]】》]", " ", title)
    stripped = SPLIT_RE.sub(" ", stripped).strip()
    if stripped:
        out.add(stripped)
    if CJK.search(title) and re.search(r"[A-Za-z]", title):
        cjk = "".join(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", title))
        latin = " ".join(re.findall(r"[A-Za-z][A-Za-z0-9'’®™\.\!]*", title))
        for part in (cjk, latin):
            if part and part.strip():
                out.add(part.strip())
    return {i for i in out if i and (len(norm(i)) >= 3 or CJK.search(i))}


def map_status(raw: str) -> str:
    """Map the Notion status wording onto the site's Status enum."""
    text = (raw or "").strip()
    if not text:
        return Status.PAUSED.value
    if "放弃" in text or text in {"弃坑", "Dropped"}:
        return Status.DROPPED.value
    if "正在玩" in text or "进行中" in text or "在玩" in text:
        return Status.IN_PROGRESS.value
    if "全成就" in text or "通关" in text or "完成" in text or "收集" in text:
        return Status.COMPLETED.value
    return Status.PAUSED.value


def build_index() -> tuple[dict[str, list], dict[str, list]]:
    """Return (normalized-title index, alias index) over every game in the DB."""
    by_norm: dict[str, list] = {}
    by_alias: dict[str, list] = {}
    for row in Item.objects.filter(media_type=MediaTypes.GAME.value).values(
        "id", "media_id", "source", "title", "title_zh", "title_en"
    ):
        keys = {norm(row["title"])}
        for field in ("title_zh", "title_en"):
            if row.get(field):
                keys.add(norm(row[field]))
        for key in filter(None, keys):
            by_norm.setdefault(key, []).append(row)
        for name in (row["title"], row.get("title_zh"), row.get("title_en")):
            for alias in variants(name or ""):
                akey = norm(alias)
                if akey:
                    by_alias.setdefault(akey, []).append(row)
    return by_norm, by_alias


def take_unique(hits: list, key_field: str = "media_id"):
    """Return (row, 'one'|'multi'|'none') so ambiguity is never silently resolved."""
    if not hits:
        return None, "none"
    uniq = {h[key_field]: h for h in hits}
    if len(uniq) == 1:
        return hits[0], "one"
    return None, "multi"


def containment_hits(key: str, by_norm: dict[str, list]) -> list:
    """Rows whose normalized title is a substring of ``key`` (or vice versa).

    ``Gorogoa画中世界`` has to hit the existing ``Gorogoa``, and
    ``大侠立志传`` has to hit ``大侠立志传：碧血丹心``. Very short keys are
    skipped because two- and three-letter fragments match half the library.
    """
    if len(key) < 4:
        return []
    out: list = []
    for lib_key, rows in by_norm.items():
        if len(lib_key) < 4 or lib_key == key:
            continue
        if lib_key in key or key in lib_key:
            out.extend(rows)
    return out


def near_miss_hits(key: str, by_norm: dict[str, list]) -> list:
    """Rows whose normalized title differs from ``key`` by exactly one character.

    Catches the alternating-character spelling ``功徳`` / ``功德`` without
    pretending to be a full fuzzy matcher.
    """
    if len(key) < 4 or not CJK.search(key):
        return []
    out: list = []
    for lib_key, rows in by_norm.items():
        if len(lib_key) != len(key) or lib_key == key:
            continue
        diff = sum(1 for a, b in zip(key, lib_key) if a != b)
        if diff == 1:
            out.extend(rows)
    return out


def parse_finished(value: str | None):
    """Turn '2024-07-01' into an aware datetime, or None."""
    if not value:
        return None
    try:
        day = datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return timezone.make_aware(
        datetime.combine(day, time(12, 0, 0)), timezone.get_current_timezone()
    )


# The export step has changed its column names twice; accept either spelling so a
# re-export never silently turns every row into an empty title.
COLUMN_ALIASES = {
    "no": ("no", "seq"),
    "title": ("title", "name"),
    "type": ("type", "genre"),
    "sub_type": ("sub_type", "sub_genre"),
    "score_story": ("score_story", "story"),
    "score_mechanic": ("score_mechanic", "mechanic"),
    "score_visual": ("score_visual", "visual"),
    "finished_on": ("finished_on", "finish"),
    "screenshot_urls": ("screenshot_urls", "screenshots"),
}


def to_float(value, cast=float):
    """Parse a Notion cell that may be '', None, '12.5' or 12."""
    if value is None or value == "":
        return None
    try:
        return cast(str(value).strip().rstrip("hH时").strip())
    except (TypeError, ValueError):
        return None


def finish_to_iso(value) -> tuple[str | None, bool]:
    """Turn '2024年' / '2024-05' / '2024-05-01' into (ISO day, is_only_a_year).

    Only a year is ever stored, so the returned day is a placeholder and the
    caller marks it as an estimate instead of pretending it is the real date.
    """
    text = str(value or "").strip()
    if not text:
        return None, False
    match = re.search(r"(\d{4})", text)
    if not match:
        return None, False
    year = int(match.group(1))
    month = re.search(r"(\d{4})\s*[-/年.]\s*(\d{1,2})", text)
    day = re.search(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})", text)
    if day:
        return f"{year:04d}-{int(day.group(2)):02d}-{int(day.group(3)):02d}", False
    if month:
        return f"{year:04d}-{int(month.group(2)):02d}-01", True
    return f"{year:04d}-07-01", True  # only the year is known: July 1st, like the old rows


def normalize_row(raw: dict) -> dict:
    """Map whatever column names the exporter used onto the importer's schema."""
    row = dict(raw)
    for target, sources in COLUMN_ALIASES.items():
        if row.get(target) in (None, "", []):
            for source in sources:
                value = raw.get(source)
                if value not in (None, "", []):
                    row[target] = value
                    break
    row["no"] = to_float(row.get("no"), int)
    row["hours"] = to_float(row.get("hours"))
    for field in ("score", "score_story", "score_mechanic", "score_visual"):
        row[field] = to_float(row.get(field))
    row["finished_on"], row["finished_estimated"] = finish_to_iso(row.get("finished_on"))
    row["title"] = str(row.get("title") or "").strip()
    row["screenshot_urls"] = list(row.get("screenshot_urls") or [])
    if not row.get("page_id"):
        row["page_id"] = f"notion:{row['no']}:{row['title']}"
    return row


def load_rows(limit: int | None) -> list[dict]:
    data = [normalize_row(r) for r in json.loads(NOTION_JSON.read_text(encoding="utf-8"))]
    data.sort(key=lambda r: (r.get("no") is None, r.get("no") or 0))
    if limit:
        data = data[:limit]
    return data


def plan(rows: list[dict], by_norm, by_alias) -> dict:
    """Split the Notion rows into skip / create buckets with a reason each."""
    skip: list[dict] = []
    todo: list[dict] = []
    for row in rows:
        title = (row.get("title") or "").strip()
        if not title:
            skip.append({"row": row, "reason": "空标题"})
            continue
        key = norm(title)
        hit, state = take_unique(by_norm.get(key) or [])
        if hit is not None:
            skip.append({
                "row": row,
                "reason": "库里已有（归一化同名）",
                "matched": {"title": hit["title"], "source": hit["source"], "media_id": hit["media_id"]},
            })
            continue
        if state == "multi":
            skip.append({"row": row, "reason": "库里多条目同名，需人工判断"})
            continue
        hit, state = take_unique(by_alias.get(key) or [])
        if hit is not None:
            skip.append({
                "row": row,
                "reason": "库里已有（别名/中英文拆名撞上）",
                "matched": {"title": hit["title"], "source": hit["source"], "media_id": hit["media_id"]},
            })
            continue
        if state == "multi":
            skip.append({"row": row, "reason": "别名撞上多个条目，需人工判断"})
            continue
        near, near_state = take_unique(near_miss_hits(key, by_norm))
        if near is not None:
            skip.append({
                "row": row,
                "reason": "库里已有（差一个字/Near-miss）",
                "matched": {"title": near["title"], "source": near["source"], "media_id": near["media_id"]},
            })
            continue
        if near_state == "multi":
            skip.append({"row": row, "reason": "差一个字的候选有多个，需人工判断"})
            continue
        cont, cont_state = take_unique(containment_hits(key, by_norm))
        if cont is not None:
            skip.append({
                "row": row,
                "reason": "库里已有（包含关系命中）",
                "matched": {"title": cont["title"], "source": cont["source"], "media_id": cont["media_id"]},
            })
            continue
        if cont_state == "multi":
            skip.append({"row": row, "reason": "包含关系命中多个条目，需人工判断"})
            continue
        probe = CROSSLANG.get(title)
        if probe:
            hits = list(Item.objects.filter(
                media_type=MediaTypes.GAME.value, title__icontains=probe
            ).values("title", "source", "media_id")[:2])
            if hits:
                skip.append({
                    "row": row,
                    "reason": "库里已有（英文名对应，人工核对表）",
                    "matched": {"title": hits[0]["title"], "source": hits[0]["source"],
                                "media_id": hits[0]["media_id"]},
                })
                continue
        if Item.objects.filter(
            media_type=MediaTypes.GAME.value, title__iexact=title
        ).exists():
            skip.append({"row": row, "reason": "库里已有（title__iexact 精确同名）"})
            continue
        todo.append(row)
    return {"skip": skip, "todo": todo}


def load_prior_ids(path: Path) -> dict[str, str]:
    """Return {page_id: media_id} from an earlier plan so a re-run keeps its UUIDs.

    The screenshot folder is named after the media ID, so the dry-run and the
    later --apply must agree on the same UUID or the downloads land in a folder
    the site never looks at. The screenshot report is consulted too, because a
    later scan can legitimately overwrite the plan file.
    """
    out: dict[str, str] = {}
    for candidate in (path, SHOT_REPORT_JSON):
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        entries = data.get("create") or data.get("results") or []
        for entry in entries:
            page_id, media_id = entry.get("page_id"), entry.get("media_id")
            if page_id and media_id:
                out.setdefault(page_id, media_id)
            elif entry.get("no") is not None and media_id:
                out.setdefault(f"notion:{entry.get('no')}:{entry.get('title')}", media_id)
    return out


def build_objects(todo: list[dict], user, prior_ids: dict[str, str] | None = None):
    """Create unsaved Item/Game pairs plus the per-row manifest used for screenshots."""
    prior_ids = prior_ids or {}
    items: list[Item] = []
    games: list[Game] = []
    manifest: list[dict] = []
    for row in todo:
        title = (row.get("title") or "").strip()
        media_id = prior_ids.get(row.get("page_id") or "") or str(uuid.uuid4())
        item = Item(
            media_id=media_id,
            source=Sources.MANUAL.value,
            media_type=MediaTypes.GAME.value,
            title=title,
            title_zh=title,
            image="",
            genres=[],
        )
        hours = row.get("hours")
        game = Game(
            item=item,
            user=user,
            status=map_status(row.get("status")),
            score=row.get("score"),
            progress=int(round((hours or 0) * 60)),
            notes=NOTES,
            start_date=None,
            end_date=parse_finished(row.get("finished_on")),
            completion_estimated=bool(row.get("finished_estimated")),
            story=row.get("score_story"),
            mechanic=row.get("score_mechanic"),
            visual=row.get("score_visual"),
        )
        items.append(item)
        games.append(game)
        manifest.append({
            "page_id": row.get("page_id"),
            "no": row.get("no"),
            "title": title,
            "media_id": media_id,
            "hours": hours,
            "status_notion": row.get("status"),
            "status_site": game.status,
            "score": row.get("score"),
            "finished_on": row.get("finished_on"),
            "finished_estimated": bool(row.get("finished_estimated")),
            "screenshot_urls": row.get("screenshot_urls") or [],
        })
    return items, games, manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="only print the plan")
    parser.add_argument("--apply", action="store_true", help="write to the database")
    parser.add_argument("--limit", type=int, default=None, help="only consider the first N rows")
    parser.add_argument("--json", default=str(PLAN_JSON), help="where to write the plan")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        parser.error("pick --dry-run or --apply")

    rows = load_rows(args.limit)
    by_norm, by_alias = build_index()
    result = plan(rows, by_norm, by_alias)
    user = get_user_model().objects.get(username="admin")

    print(f"Notion 行数           : {len(rows)}")
    print(f"库里撞名跳过          : {len(result['skip'])}")
    print(f"需要新建              : {len(result['todo'])}")

    reasons: dict[str, int] = {}
    for entry in result["skip"]:
        reasons[entry["reason"]] = reasons.get(entry["reason"], 0) + 1
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  跳过原因 {reason}: {count}")

    for row in result["todo"][:10]:
        print(f"  序号 {row.get('no')} | {row.get('title')} | {row.get('hours')}h | "
              f"{row.get('status')} -> {map_status(row.get('status'))}")

    prior_ids = load_prior_ids(ROOT / ".tools" / "_notion_import_plan.json")
    items, games, manifest = build_objects(result["todo"], user, prior_ids)

    payload = {
        "mode": "apply" if args.apply else "dry-run",
        "notion_rows": len(rows),
        "skip_count": len(result["skip"]),
        "create_count": len(manifest),
        "skip_reasons": reasons,
        "skip": [
            {"no": e["row"].get("no"), "title": e["row"].get("title"), "reason": e["reason"],
             "matched": e.get("matched")}
            for e in result["skip"]
        ],
        "create": manifest,
    }
    Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"清单已写              : {args.json}")

    if not args.apply:
        print("dry-run：未写库")
        return
    if not games:
        print("没有需要新建的条目")
        return

    # Item carries no history manager, so it has to be created before the games
    # that point at it; a plain bulk_create stays inside bulk_create_media's
    # contract for Game (which is the model whose save() would hit the network).
    with transaction.atomic():
        already = set(
            Item.objects.filter(
                media_type=MediaTypes.GAME.value,
                source=Sources.MANUAL.value,
                media_id__in=[row["media_id"] for row in manifest],
            ).values_list("media_id", flat=True)
        )
        items = [obj for obj in items if obj.media_id not in already]
        keep = [row for row in manifest if row["media_id"] not in already]
        games = [game for game, row in zip(games, manifest, strict=True) if row["media_id"] not in already]
        manifest = keep
        if not games:
            print("这 10 个已经写过了，本次不重复写库")
            return
        Item.objects.bulk_create(items, batch_size=500, ignore_conflicts=True)
        stored = {
            item.media_id: item
            for item in Item.objects.filter(
                source=Sources.MANUAL.value,
                media_type=MediaTypes.GAME.value,
                media_id__in=[row["media_id"] for row in manifest],
            )
        }
        missing = [row["title"] for row in manifest if row["media_id"] not in stored]
        if missing:
            raise SystemExit(f"Item 建库后找不到：{missing}")
        for game, row in zip(games, manifest, strict=True):
            game.item = stored[row["media_id"]]
        helpers.bulk_create_media({MediaTypes.GAME.value: games}, user)

    print(f"已写库                : {len(items)} 个 Item / {len(games)} 个 Game")
    ids = [obj.id for obj in items if obj.id]
    if ids:
        print(f"Item id 范围          : {min(ids)} .. {max(ids)}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
