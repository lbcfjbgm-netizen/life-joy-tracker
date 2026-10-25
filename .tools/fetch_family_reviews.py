# -*- coding: utf-8 -*-
"""抓家庭库里「本库没有的」游戏的 Steam 好评率，并按规则筛出可导入的。

数据源与 .tools/fetch_steam_reviews.py 完全一致（appreviewhistogram，全语言汇总），
直接复用那边的 fetch_one / compute，避免两套算法算出两种口径。

输入：.tools/raw/steam_family_candidates.json
输出：
    .tools/raw/steam_family_reviews.json   每个 appid 的好评率 / 评论数 / 判定
    .tools/raw/steam_blocklist.json        拉黑清单（评论数<100 或 好评率<60）

规则（用户 2026-10-24 拍板）：
    * rt_playtime > 0（玩过）      -> 一律可导入
    * 没玩过：评论数>=100 且 好评率>=60 -> 可导入
    * 没玩过且（评论数<100 或 好评率<60）-> 不导入，进 blocklist
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANDS = ROOT / ".tools" / "raw" / "steam_family_candidates.json"
OUT = ROOT / ".tools" / "raw" / "steam_family_reviews.json"
BLOCK = ROOT / ".tools" / "raw" / "steam_blocklist.json"

MIN_REVIEWS = 100
MIN_PCT = 60

spec = importlib.util.spec_from_file_location(
    "fsr", str(ROOT / ".tools" / "fetch_steam_reviews.py")
)
fsr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fsr)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    cands = json.loads(CANDS.read_text(encoding="utf-8"))
    fsr.log("=" * 70)
    fsr.log(f"家庭库评分抓取：候选 {len(cands)} 个，门槛 评论>={MIN_REVIEWS} 且 好评率>={MIN_PCT}")

    results: dict[str, dict] = {}
    if OUT.exists():
        try:
            results = json.loads(OUT.read_text(encoding="utf-8"))
            fsr.log(f"已有缓存 {len(results)} 条，跳过已抓的")
        except (OSError, ValueError):
            results = {}

    todo = [c for c in cands if str(c["appid"]) not in results]
    fsr.log(f"本次要抓 {len(todo)} 个")

    started = time.time()
    done = 0
    for start in range(0, len(todo), 250):
        chunk = todo[start:start + 250]
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = {ex.submit(fsr.fetch_one, str(c["appid"]), fsr.PROXY): c for c in chunk}
            for f in as_completed(futs):
                c = futs[f]
                _a, payload, err = f.result()
                done += 1
                if payload is None:
                    results[str(c["appid"])] = {
                        "appid": c["appid"], "name": c.get("name"),
                        "rt_playtime": c.get("rt_playtime", 0),
                        "pct": None, "count": 0, "error": err, "verdict": "fetch_failed",
                    }
                else:
                    pct, up, dn, status = fsr.compute(payload)
                    results[str(c["appid"])] = {
                        "appid": c["appid"], "name": c.get("name"),
                        "rt_playtime": c.get("rt_playtime", 0),
                        "pct": pct, "up": up, "down": dn, "count": up + dn,
                        "status": status,
                        "verdict": "pending",
                    }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        per = (time.time() - started) / done
        fsr.log(f"已完成 {done}/{len(todo)}，{per:.2f}s/条，剩余约 "
                f"{per * (len(todo) - done) / 60:.1f} 分钟")
        time.sleep(5)

    # 判定
    keep, blocked = [], []
    for key, rec in results.items():
        played = int(rec.get("rt_playtime") or 0) > 0
        pct = rec.get("pct")
        cnt = int(rec.get("count") or 0)
        if played:
            rec["verdict"] = "import"          # 玩过的一律导入
            keep.append(rec)
        elif pct is None:
            rec["verdict"] = "block"
            rec["block_reason"] = rec.get("error") or rec.get("status") or "无数据"
            blocked.append(rec)
        elif cnt < MIN_REVIEWS:
            rec["verdict"] = "block"
            rec["block_reason"] = f"评论数{cnt}<{MIN_REVIEWS}"
            blocked.append(rec)
        elif pct < MIN_PCT:
            rec["verdict"] = "block"
            rec["block_reason"] = f"好评率{pct}<{MIN_PCT}"
            blocked.append(rec)
        else:
            rec["verdict"] = "import"
            keep.append(rec)

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    from datetime import datetime

    BLOCK.write_text(json.dumps({
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rule": f"未游玩 且（评论数<{MIN_REVIEWS} 或 好评率<{MIN_PCT}）",
        "note": "导入前先查本清单；若该 appid 的 rt_playtime>0 则自动解除拉黑并正常导入。",
        "count": len(blocked),
        "appids": [
            {"appid": b["appid"], "name": b.get("name"),
             "review_pct": b.get("pct"), "review_count": b.get("count"),
             "reason": b.get("block_reason")}
            for b in sorted(blocked, key=lambda x: x["appid"])
        ],
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    fsr.log(f"判定完成：可导入 {len(keep)}，拉黑 {len(blocked)}")
    print(f"\n可导入 {len(keep)} / 拉黑 {len(blocked)} / 总计 {len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
