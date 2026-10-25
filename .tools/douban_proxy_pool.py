#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
douban_proxy_pool.py — 豆瓣导入用的「香港节点池 + 一键换出口 IP」

思路：不自己实现代理协议，全部交给 verge-mihomo 内核。
  1. fetch  拉多个 Clash 订阅 -> 解析 proxies -> 筛香港节点 -> 生成候选配置
  2. test   起内核，用 mihomo 自带的 /proxies/<name>/delay 接口逐个测活
  3. up     用「只含活节点」的配置起内核，监听 127.0.0.1:7897 + API 19197
  4. use X  切到指定节点；next 切到下一个；ip 打印当前出口 IP

用法见文件末尾 __main__ 或 `python douban_proxy_pool.py -h`
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 pyyaml: .venv\\Scripts\\python.exe -m pip install pyyaml\n")
    raise

# ---------------------------------------------------------------- 常量 / 路径
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(TOOLS_DIR, "proxy_pool")
RAW_DIR = os.path.join(WORK, "raw")
CONFIG_ALL = os.path.join(WORK, "clash-all.yaml")      # 全部香港节点（测活用）
CONFIG_LIVE = os.path.join(WORK, "clash-live.yaml")    # 只含活节点（日常用）
NODES_JSON = os.path.join(WORK, "nodes.json")          # 解析+测活结果
ACTIVE_JSON = os.path.join(TOOLS_DIR, "active_nodes.json")  # 验收要的那份
STATE_JSON = os.path.join(WORK, "state.json")
PID_FILE = os.path.join(WORK, "mihomo.pid")
SUBS_JSON = os.path.join(WORK, "subs.json")

MIHOMO = r"C:\Program Files\Clash Verge\verge-mihomo.exe"
VERGE_DATA = os.path.join(
    os.environ.get("APPDATA", ""),
    "io.github.clash-verge-rev.clash-verge-rev",
)

# 默认占 7899，刻意不抢 7897（那个口子归 Clash Verge GUI 的服务管，
# 杀了会被服务重拉，还会影响用户正常上网）。导入器只需把
# DOUBAN_PROXY 指到 http://127.0.0.1:7899。
MIXED_PORT = int(os.environ.get("DOUBAN_POOL_PORT", "7899"))
API_PORT = int(os.environ.get("DOUBAN_POOL_API_PORT", "19197"))
API = f"http://127.0.0.1:{API_PORT}"
GROUP = "POOL"

# 豆瓣详情接口（验收脚本用的那一个）
DOUBAN_URL = "https://m.douban.com/rexxar/api/v2/movie/1291818"
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
# 想留几个不同出口 IP
TARGET_IPS = int(os.environ.get("DOUBAN_POOL_TARGET", "10"))

# 订阅链接是隐私（URL 里的 token 拿到就能白嫖/盗用代理流量），所以绝不再硬编码：
# 一律从环境变量读，没有就是空字符串。
#   DOUBAN_POOL_SUB_A / DOUBAN_POOL_SUB_B / DOUBAN_POOL_SUB_C
# 也可以用 WORK/subs.json 覆盖（同样不进版本库）：{"A": "https://...", "B": "..."}
# 一个都没配时 fetch 只打印一句提示就退出（返回码 1），不会抛异常，
# 因为豆瓣代理池只是可选功能，缺了不影响站点其它部分。
DEFAULT_SUBS = {
    "A": os.environ.get("DOUBAN_POOL_SUB_A", "").strip(),
    "B": os.environ.get("DOUBAN_POOL_SUB_B", "").strip(),
    "C": os.environ.get("DOUBAN_POOL_SUB_C", "").strip(),
}
UA = "clash-verge/v1.7.7"
FETCH_PROXY = "http://127.0.0.1:7897"

HK_RE = re.compile(r"hk|香港|hong|🇭🇰", re.IGNORECASE)

# 豆瓣是境内站，海外 IP 会被风控得更狠，但只要出口 IP 能拿到 200 就能用。
# 所以不再只筛香港，按「离得近的先试」排队，全部节点都留下交给测活环节。
REGION_PATTERNS = [
    ("HK", re.compile(r"hk|香港|hong|🇭🇰", re.IGNORECASE)),
    ("TW", re.compile(r"tw|台湾|台灣|taiwan|🇹🇼", re.IGNORECASE)),
    ("JP", re.compile(r"jp|日本|japan|东京|東京|大阪|🇯🇵", re.IGNORECASE)),
    ("SG", re.compile(r"sg|新加坡|singapore|狮城|🇸🇬", re.IGNORECASE)),
    ("KR", re.compile(r"kr|韩国|韓國|korea|首尔|首爾|🇰🇷", re.IGNORECASE)),
    ("US", re.compile(r"us|美国|美國|united states|洛杉矶|圣何塞|西雅图|🇺🇸", re.IGNORECASE)),
]
# 这些不是真节点，测了也白测
SKIP_RE = re.compile(
    r"剩余流量|套餐|到期|官网|地址|订阅|重置|过期|群组|频道|"
    r"DIRECT|REJECT|REJECT-DROP|PASS|COMPATIBLE|GLOBAL",
    re.IGNORECASE,
)


def region_of(name: str) -> str:
    """按节点名猜地区；猜不出就是 OT（其他）。"""
    for code, pat in REGION_PATTERNS:
        if pat.search(name):
            return code
    return "OT"


# 地区优先级：先香港，再近的，最后远的
REGION_ORDER = {"HK": 0, "TW": 1, "JP": 2, "SG": 3, "KR": 4, "US": 5, "OT": 6}

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------- 小工具
def log(msg: str) -> None:
    print(msg, flush=True)


def ensure_dirs() -> None:
    for d in (WORK, RAW_DIR):
        os.makedirs(d, exist_ok=True)


def http_json(method: str, path: str, body=None, timeout: float = 8.0):
    """访问本机 mihomo API；强制绕过系统/环境代理。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    with opener.open(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


def api_alive() -> bool:
    try:
        http_json("GET", "/version", timeout=1.5)
        return True
    except Exception:
        return False


def wait_api(seconds: float = 15.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if api_alive():
            return True
        time.sleep(0.4)
    return False


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def dump_json(path, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 1. fetch
def decode_payload(raw: bytes):
    """订阅可能是 YAML 原文，也可能是 base64 后的 YAML。返回 dict。"""
    text = raw.decode("utf-8", "replace").lstrip("\ufeff")
    for candidate in (text, None):
        if candidate is None:
            compact = "".join(text.split())
            try:
                candidate = base64.b64decode(
                    compact + "=" * (-len(compact) % 4)
                ).decode("utf-8", "replace")
            except Exception:
                return None
        try:
            cfg = yaml.safe_load(candidate)
        except Exception:
            continue
        if isinstance(cfg, dict) and isinstance(cfg.get("proxies"), list):
            return cfg
    return None


def fetch_one(code: str, url: str, use_proxy: str | None) -> dict:
    """用 requests（可选走代理）拉订阅。失败时返回 {"error": ...}"""
    import requests

    path = os.path.join(RAW_DIR, f"{code}.txt")
    proxies = {"http": use_proxy, "https": use_proxy} if use_proxy else None
    try:
        r = requests.get(
            url,
            headers={"User-Agent": UA, "Accept": "*/*"},
            proxies=proxies,
            timeout=45,
            verify=True,
        )
        if r.status_code != 200:
            return {"code": code, "error": f"HTTP {r.status_code}", "bytes": 0, "proxies": 0}
        with open(path, "wb") as f:
            f.write(r.content)
        cfg = decode_payload(r.content)
        if cfg is None:
            return {"code": code, "error": "无法解析（既不是 YAML 也不是 base64-YAML）",
                    "bytes": len(r.content), "proxies": 0}
        return {
            "code": code,
            "http": 200,
            "bytes": len(r.content),
            "proxies": len(cfg["proxies"]),
            "list": cfg["proxies"],
        }
    except Exception as e:
        return {"code": code, "error": f"{type(e).__name__}: {e}", "bytes": 0, "proxies": 0}


def dedup_name(name: str, seen: dict) -> str:
    if name not in seen:
        seen[name] = 1
        return name
    seen[name] += 1
    return f"{name} #{seen[name]}"


def cmd_fetch(args) -> int:
    ensure_dirs()
    subs = dict(DEFAULT_SUBS)
    override = load_json(SUBS_JSON)
    if isinstance(override, dict):
        subs.update({k: v for k, v in override.items() if isinstance(v, str)})
    if args.only:
        keep = {c.strip().upper() for c in args.only.split(",") if c.strip()}
        subs = {k: v for k, v in subs.items() if k.upper() in keep}

    # 优雅降级：订阅链接只从环境变量 / subs.json 来，没配就当作这个功能不存在。
    # 不抛异常、不写文件，只提示一句然后返回 1（调用方据此判断「池子不可用」）。
    subs = {k: v.strip() for k, v in subs.items() if isinstance(v, str) and v.strip()}
    if not subs:
        log(
            "[fetch] 没有可用的订阅链接，跳过。\n"
            "        订阅是隐私，需自行配置环境变量后重试：\n"
            "          DOUBAN_POOL_SUB_A=<你的 Clash 订阅链接>\n"
            "          DOUBAN_POOL_SUB_B=<可留空>\n"
            "          DOUBAN_POOL_SUB_C=<可留空>\n"
            "        或者写 .tools/proxy_pool/subs.json：{\"A\": \"https://...\"}\n"
            "        豆瓣代理池是可选功能，不配不影响站点其它部分。"
        )
        return 1

    proxy = None if args.no_proxy else FETCH_PROXY
    if proxy and not port_open(MIXED_PORT):
        log(f"[!] {proxy} 没在监听，先试着直连…")
        proxy = None

    log(f"[fetch] 拉 {len(subs)} 个订阅（代理={proxy or '直连'}）…")
    results = []
    for code, url in subs.items():
        r = fetch_one(code, url, proxy)
        results.append(r)
        if "error" in r:
            log(f"   {code}: ✗ {r['error']}")
        else:
            log(f"   {code}: ✓ {r['bytes']} 字节, {r['proxies']} 个节点")

    # 汇总：不再只筛香港。豆瓣按 IP 限额，能多一个出口 IP 就多一份额度，
    # 所以台湾/日本/新加坡/美国这些也留下，交给测活环节用实测结果说话。
    seen: dict = {}
    all_proxies, hk_proxies = [], []
    per_sub_hk = {}
    per_sub_all = {}
    for r in results:
        if "error" in r:
            continue
        n_hk = 0
        n_all = 0
        for p in r.get("list") or []:
            if not isinstance(p, dict) or "server" not in p:
                continue
            p = dict(p)  # 浅拷贝，字段原样保留
            raw_name = str(p.get("name") or f"node-{len(all_proxies)}")
            if SKIP_RE.search(raw_name):
                # 广告位、流量信息、内置策略，不是真节点
                continue
            p["name"] = dedup_name(raw_name, seen)
            p["_src"] = r["code"]
            p["region"] = region_of(raw_name)
            all_proxies.append(p)
            n_all += 1
            if p["region"] == "HK":
                hk_proxies.append(p)
                n_hk += 1
        per_sub_hk[r["code"]] = n_hk
        per_sub_all[r["code"]] = n_all

    # 香港排前面，同地区内保持订阅原顺序
    all_proxies.sort(key=lambda p: REGION_ORDER.get(p["region"], 9))
    hk_proxies.sort(key=lambda p: REGION_ORDER.get(p["region"], 9))

    # 先只保留香港，再补其他地区，总量封顶以免测活太久（每个节点要起代理握手）
    cap = int(os.environ.get("DOUBAN_POOL_MAX", "90"))
    if len(all_proxies) > cap:
        log(f"[fetch] 节点太多（{len(all_proxies)}），按地区优先级截到 {cap} 个")
        all_proxies = all_proxies[:cap]
        hk_proxies = [p for p in all_proxies if p["region"] == "HK"]

    nodes = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "subs": [
            {k: v for k, v in r.items() if k != "list"} for r in results
        ],
        "total_nodes": len(all_proxies),
        "hk_nodes": len(hk_proxies),
        "hk_per_sub": per_sub_hk,
        "all_per_sub": per_sub_all,
        "region_count": {
            code: sum(1 for p in all_proxies if p["region"] == code)
            for code, _ in REGION_PATTERNS
        },
        "nodes": all_proxies,
    }
    dump_json(NODES_JSON, nodes)

    log("")
    log(f"[fetch] 合计可用 {len(all_proxies)} 个节点；其中香港 {len(hk_proxies)} 个")
    rc = nodes["region_count"]
    log(f"        地区分布：{rc}")
    log(f"        按订阅：{per_sub_all}")
    log(f"        明细 -> {NODES_JSON}")
    if not all_proxies:
        log("[x] 一个节点都没筛到，检查订阅是否可用")
        return 1
    return 0


# ---------------------------------------------------------------- 配置生成
def copy_geo_files() -> list:
    """mihomo 需要的 geo 数据。geoip.metadb 常被占用，刻意不复制。"""
    copied = []
    for fn in ("Country.mmdb", "geosite.dat", "geoip.dat", "GeoSite.dat", "GeoIP.dat"):
        src = os.path.join(VERGE_DATA, fn)
        if os.path.isfile(src):
            dst = os.path.join(WORK, fn)
            try:
                if not os.path.isfile(dst) or os.path.getsize(dst) != os.path.getsize(src):
                    shutil.copy2(src, dst)
                copied.append(fn)
            except Exception as e:
                log(f"   [!] 复制 {fn} 失败: {e}")
    return copied


def build_config(proxies: list, path: str, startup: str | None = None) -> str:
    names = [p["name"] for p in proxies]
    start = startup if startup in names else names[0]
    cfg = {
        "mixed-port": MIXED_PORT,
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "unified-delay": True,
        "tcp-concurrent": True,
        "external-controller": f"127.0.0.1:{API_PORT}",
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": GROUP,
                "type": "select",
                "proxies": names,
            }
        ],
        "rules": [f"MATCH,{GROUP}"],
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, width=4096)
    return start


# ---------------------------------------------------------------- 2. test
def start_kernel(config_path: str, force: bool = False) -> int | None:
    """起 verge-mihomo 内核，返回 pid。"""
    os.makedirs(WORK, exist_ok=True)
    if api_alive() or port_open(MIXED_PORT) or port_open(API_PORT):
        if not force:
            log(f"[!] 端口 {MIXED_PORT}/{API_PORT} 已被占用。用 --force 杀掉占用者后重启。")
            return None
        kill_port_owners([MIXED_PORT, API_PORT])

    existing = load_json(PID_FILE)
    if isinstance(existing, dict) and existing.get("pid"):
        try:
            os.kill(int(existing["pid"]), 9)
            time.sleep(0.5)
        except Exception:
            pass

    copy_geo_files()
    args = [MIHOMO, "-d", WORK, "-f", config_path]
    log(f"[up] {' '.join(args)}")
    creationflags = 0
    if os.name == "nt":
        creationflags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        args,
        stdout=open(os.path.join(WORK, "mihomo.log"), "ab"),
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
        cwd=WORK,
    )
    dump_json(PID_FILE, {"pid": proc.pid, "config": config_path, "started": time.time()})
    if not wait_api(20):
        log("[x] 内核没起来，看 " + os.path.join(WORK, "mihomo.log"))
        return None
    log(f"[up] 内核 PID={proc.pid}，API={API}，混合端口={MIXED_PORT}")
    return proc.pid


def kill_port_owners(ports) -> None:
    """杀掉监听指定端口的进程（只杀本机、PID 明确的那几个）。"""
    if os.name != "nt":
        return
    for port in ports:
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue)"
                 f".OwningProcess | Sort-Object -Unique"],
                capture_output=True, text=True, timeout=20,
            ).stdout
        except Exception:
            continue
        for line in out.split():
            line = line.strip()
            if not line.isdigit():
                continue
            pid = int(line)
            if pid in (0, 4, os.getpid()):
                continue
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=15)
                log(f"   [kill] 端口 {port} 的 PID {pid} 已结束")
            except Exception:
                pass
    time.sleep(1.0)


def test_delay(name: str, url: str, timeout_ms: int) -> int | None:
    q = urllib.parse.urlencode({"url": url, "timeout": timeout_ms})
    path = f"/proxies/{urllib.parse.quote(name, safe='')}/delay?{q}"
    try:
        res = http_json("GET", path, timeout=(timeout_ms / 1000.0) + 6)
        delay = (res or {}).get("delay")
        return int(delay) if isinstance(delay, (int, float)) and delay > 0 else None
    except Exception:
        return None


def cmd_test(args) -> int:
    nodes = load_json(NODES_JSON)
    if not nodes or not nodes.get("nodes"):
        log("[x] 没有节点数据，先跑 fetch")
        return 1

    hk = nodes["nodes"]
    log(f"[test] 香港节点 {len(hk)} 个，准备起内核测活…")
    build_config(hk, CONFIG_ALL)
    if start_kernel(CONFIG_ALL, force=args.force) is None:
        return 1

    url = args.url
    log(f"[test] 并发 {args.workers} 线程，测活地址 {url}，超时 {args.timeout}ms")
    t0 = time.time()
    results = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(test_delay, p["name"], url, args.timeout): p["name"] for p in hk}
        for fut in futs:
            pass
        done = 0
        for fut, name in futs.items():
            try:
                results[name] = fut.result()
            except Exception:
                results[name] = None
            done += 1
            if done % 20 == 0:
                log(f"   … {done}/{len(hk)}")

    alive = [p for p in hk if results.get(p["name"])]
    for p in alive:
        p["_delay"] = results[p["name"]]
    alive.sort(key=lambda p: p["_delay"])

    nodes["tested_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    nodes["test_url"] = url
    nodes["alive"] = alive
    nodes["alive_count"] = len(alive)
    dump_json(NODES_JSON, nodes)

    summary = {
        "generated_at": nodes["tested_at"],
        "source_subs": nodes.get("hk_per_sub"),
        "total_nodes": nodes.get("total_nodes"),
        "hk_nodes": nodes.get("hk_nodes"),
        "alive_count": len(alive),
        "dead_count": len(hk) - len(alive),
        "test_url": url,
        "mixed_port": MIXED_PORT,
        "api": API,
        "group": GROUP,
        "nodes": [
            {
                "name": p["name"],
                "src": p.get("_src"),
                "type": p.get("type"),
                "server": p.get("server"),
                "port": p.get("port"),
                "delay_ms": p.get("_delay"),
            }
            for p in alive
        ],
    }
    dump_json(ACTIVE_JSON, summary)

    log("")
    log(f"[test] 活 {len(alive)} / {len(hk)}（死 {len(hk)-len(alive)}），耗时 {time.time()-t0:.1f}s")
    for p in alive[:15]:
        log(f"   {p['_delay']:>5}ms  {p['name']}  [{p.get('_src')}/{p.get('type')}]")
    if len(alive) > 15:
        log(f"   … 其余 {len(alive)-15} 个见 {ACTIVE_JSON}")
    log(f"[test] 结果 -> {ACTIVE_JSON}")
    return 0 if alive else 1


# ---------------------------------------------------------------- 3. up / use / next
def live_nodes() -> list:
    data = load_json(ACTIVE_JSON)
    if not data:
        return []
    nodes = load_json(NODES_JSON) or {}
    alive_names = {n["name"] for n in data.get("nodes", [])}
    return [p for p in nodes.get("nodes", []) if p["name"] in alive_names]


def cmd_up(args) -> int:
    nodes = live_nodes()
    if not nodes:
        log("[x] 没有活节点数据，先跑 fetch 再 test")
        return 1
    state = load_json(STATE_JSON) or {}
    start = state.get("node") if state.get("node") in {n["name"] for n in nodes} else None
    start = build_config(nodes, CONFIG_LIVE, startup=start)
    pid = start_kernel(CONFIG_LIVE, force=args.force)
    if pid is None:
        return 1
    set_group(start)
    save_state(start)
    log(f"[up] 当前出口节点：{start}")
    log(f"[up] 豆瓣导入器用 DOUBAN_PROXY=http://127.0.0.1:{MIXED_PORT} 即可")
    show_ip()
    return 0


def set_group(name: str) -> bool:
    try:
        http_json("PUT", f"/proxies/{urllib.parse.quote(GROUP, safe='')}", {"name": name})
        return True
    except Exception as e:
        log(f"[!] 切组失败: {e}")
        return False


def current_of_group() -> str | None:
    try:
        data = http_json("GET", f"/proxies/{urllib.parse.quote(GROUP, safe='')}")
        return (data or {}).get("now")
    except Exception:
        return None


def save_state(name: str) -> None:
    st = load_json(STATE_JSON) or {}
    st["node"] = name
    st["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    dump_json(STATE_JSON, st)


def show_ip(rounds: int = 1) -> str | None:
    """用混合端口查一次出口 IP。"""
    per_round = []
    for _ in range(max(1, rounds)):
        per_round.append(_ip_once())
        time.sleep(0.3)
    ip = per_round[0]
    log(f"    出口 IP: {ip or '（取不到，可能是节点挂了）'}")
    return ip


def _ip_once() -> str | None:
    import requests

    for url in ("http://api.ipify.org?format=json", "https://api.ipify.org?format=json",
                "http://ip-api.com/json/?fields=query"):
        try:
            r = requests.get(
                url,
                proxies={"http": f"http://127.0.0.1:{MIXED_PORT}",
                         "https": f"http://127.0.0.1:{MIXED_PORT}"},
                timeout=12,
            )
            if r.status_code != 200:
                continue
            txt = r.text.strip()
            if txt.startswith("{"):
                obj = json.loads(txt)
                return obj.get("ip") or obj.get("query")
            if txt:
                return txt
        except Exception:
            continue
    return None


def cmd_use(args) -> int:
    nodes = live_nodes()
    names = [n["name"] for n in nodes]
    if not names:
        log("[x] 没有活节点数据，先跑 fetch + test")
        return 1
    want = args.name
    if want not in names:
        # 允许模糊/序号
        if want.isdigit() and 0 <= int(want) < len(names):
            want = names[int(want)]
        else:
            hits = [n for n in names if want.lower() in n.lower()]
            if len(hits) == 1:
                want = hits[0]
            else:
                log(f"[x] 找不到节点「{want}」。可用（{len(names)} 个）：")
                for i, n in enumerate(names):
                    log(f"   [{i}] {n}")
                return 1
    if not api_alive():
        log("[x] 内核没在跑，先 up")
        return 1
    if not set_group(want):
        return 1
    save_state(want)
    log(f"[use] -> {want}")
    show_ip()
    return 0


def cmd_next(args) -> int:
    nodes = live_nodes()
    names = [n["name"] for n in nodes]
    if not names:
        log("[x] 没有活节点数据")
        return 1
    cur = current_of_group()
    idx = names.index(cur) if cur in names else -1
    for step in range(1, len(names) + 1):
        cand = names[(idx + step) % len(names)]
        if set_group(cand):
            ip = _ip_once()
            if ip:  # 只接受能出 IP 的
                save_state(cand)
                log(f"[next] {cur or '?'} -> {cand}  出口 IP: {ip}")
                return 0
            log(f"   {cand} 没出 IP，跳过")
    log("[x] 所有节点都出不来 IP")
    return 1


def cmd_list(args) -> int:
    data = load_json(ACTIVE_JSON) or {}
    nodes = data.get("nodes", [])
    if not nodes:
        log("[x] 还没测活，先 fetch + test")
        return 1
    cur = current_of_group()
    for i, n in enumerate(nodes):
        mark = "*" if n["name"] == cur else " "
        log(f"{mark}[{i:>3}] {n.get('delay_ms'):>5}ms  {n['name']}  [{n.get('src')}/{n.get('type')}]")
    return 0


def cmd_ip(args) -> int:
    show_ip(rounds=args.rounds)
    return 0


def cmd_status(args) -> int:
    data = load_json(ACTIVE_JSON) or {}
    log(f"活节点数 : {data.get('alive_count', '?')} / 香港 {data.get('hk_nodes', '?')}")
    log(f"内核进程 : {'在跑' if api_alive() else '没跑'}")
    log(f"当前分组 : {GROUP} = {current_of_group()}")
    log(f"混合端口 : {MIXED_PORT}")
    log(f"API      : {API}")
    show_ip()
    return 0


def cmd_stop(args) -> int:
    st = load_json(PID_FILE) or {}
    pid = st.get("pid")
    if pid:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
            log(f"[stop] 已结束内核 PID {pid}")
        except Exception as e:
            log(f"[stop] {e}")
    else:
        kill_port_owners([MIXED_PORT, API_PORT])
        log("[stop] 按端口清理完成")
    return 0


def cmd_douban_check(args) -> int:
    """对当前节点打一次豆瓣，确认 200。"""
    import requests

    probe = "https://movie.douban.com/j/subject_suggest?q=test"
    try:
        r = requests.get(
            probe,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            proxies={"http": f"http://127.0.0.1:{MIXED_PORT}",
                     "https": f"http://127.0.0.1:{MIXED_PORT}"},
            timeout=20,
        )
        code = r.status_code
        body = r.text[:120].replace("\n", " ")
    except Exception as e:
        code, body = 0, f"{type(e).__name__}: {e}"
    log(f"[douban] HTTP {code}  {body}")
    return 0 if code == 200 else 1


# ---------------------------------------------------------------- 按 IP 去重
def probe_douban_once() -> tuple[int, str]:
    """用当前节点打一次豆瓣详情，返回 (状态码, 说明)。

    状态码的读法：
      200            -> 这个出口 IP 现在能抓
      400 + 1309     -> 这个 IP 被限流了（会退，留着有用）
      0              -> 连不上/TLS 被切断（这个 IP 对豆瓣不可用，排除）
    """
    import requests

    try:
        r = requests.get(
            DOUBAN_URL,
            headers={"User-Agent": IPHONE_UA, "Referer": "https://m.douban.com/"},
            proxies={"http": f"http://127.0.0.1:{MIXED_PORT}",
                     "https": f"http://127.0.0.1:{MIXED_PORT}"},
            timeout=25,
        )
    except Exception as e:
        return 0, f"{type(e).__name__}: {str(e)[:60]}"

    code = r.status_code
    if code == 200:
        try:
            title = r.json().get("title") or ""
        except Exception:
            title = ""
        return 200, title

    body = (r.text or "")[:200]
    if "1309" in body or "rate_limit" in body:
        return 400, "rate_limit"
    return code, body[:80].replace("\n", " ")


def cmd_dedupe(args) -> int:
    """逐个节点取出口 IP，同一个 IP 只留延迟最低的那个，再验豆瓣。

    这是整个工具最关键的一步：豆瓣按 IP 限额，所以真正有用的是「多少个不同的
    出口 IP」，而不是「多少个节点名」。六个节点挤在一台机器上只能算一个 IP。
    """
    nodes = load_json(NODES_JSON) or {}
    alive = nodes.get("alive") or []
    if not alive:
        log("[x] 没有测活结果，先跑 fetch 再 test")
        return 1

    log(f"[dedupe] 对 {len(alive)} 个活节点逐个取出口 IP，同 IP 只留最快的一个…")
    by_ip: dict[str, dict] = {}
    no_ip: list[dict] = []

    for i, p in enumerate(alive, 1):
        name = p["name"]
        if not set_group(name):
            no_ip.append({**p, "exit_ip": None, "note": "切组失败"})
            continue
        time.sleep(0.6)
        ip = _ip_once()
        if not ip:
            no_ip.append({**p, "exit_ip": None, "note": "取不到出口 IP"})
            log(f"   [{i}/{len(alive)}] {name[:26]:26} -> IP 取不到")
            continue

        delay = p.get("_delay") or 99999
        cur = by_ip.get(ip)
        if cur is None:
            by_ip[ip] = {**p, "exit_ip": ip, "note": ""}
            log(f"   [{i}/{len(alive)}] {name[:26]:26} -> {ip}  (新 IP)")
        elif delay < (cur.get("_delay") or 99999):
            by_ip[ip] = {**p, "exit_ip": ip, "note": ""}
            log(f"   [{i}/{len(alive)}] {name[:26]:26} -> {ip}  (换掉更慢的同 IP 节点)")
        else:
            log(f"   [{i}/{len(alive)}] {name[:26]:26} -> {ip}  (重复 IP，丢弃)")

    log("")
    log(f"[dedupe] 得到 {len(by_ip)} 个不同出口 IP，开始验豆瓣…")

    usable, limited, dead = [], [], []
    for ip, p in by_ip.items():
        if not set_group(p["name"]):
            dead.append({**p, "douban_status": "切组失败"})
            continue
        time.sleep(0.6)
        code, note = probe_douban_once()
        p["douban_status"] = f"{code}"
        p["douban_note"] = note
        p["douban_ok"] = code == 200
        if code == 200:
            usable.append(p)
            log(f"   {ip:16} {p['region']:2} {p['name'][:24]:24} 豆瓣 200 ✓ {note}")
        elif code == 400 and note == "rate_limit":
            limited.append(p)
            log(f"   {ip:16} {p['region']:2} {p['name'][:24]:24} 豆瓣被限流（可等）")
        else:
            dead.append(p)
            log(f"   {ip:16} {p['region']:2} {p['name'][:24]:24} 豆瓣不可用: {code} {note}")
        time.sleep(0.4)

    # 可用的排前面，限流的其次，死的最后
    order = usable + limited + dead
    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_nodes": nodes.get("total_nodes"),
        "alive_count": len(alive),
        "unique_ips": len(by_ip),
        "usable_count": len(usable),
        "limited_count": len(limited),
        "dead_count": len(dead),
        "mixed_port": MIXED_PORT,
        "api": API,
        "group": GROUP,
        "nodes": [
            {
                "name": p["name"],
                "region": p.get("region", "OT"),
                "exit_ip": p.get("exit_ip"),
                "delay_ms": p.get("_delay"),
                "douban_ok": bool(p.get("douban_ok")),
                "douban_status": p.get("douban_status", ""),
                "douban_note": p.get("douban_note", ""),
            }
            for p in order
        ],
    }
    dump_json(ACTIVE_JSON, summary)
    # 只把能用的节点写进一份新列表，供 up 用
    nodes["usable"] = usable
    nodes["limited"] = limited
    dump_json(NODES_JSON, nodes)

    log("")
    log(f"[dedupe] 汇总：{len(by_ip)} 个不同 IP ｜ 可用 {len(usable)} ｜ 限流 {len(limited)} ｜ 不可用 {len(dead)}")
    log(f"         明细 -> {ACTIVE_JSON}")
    if len(usable) < TARGET_IPS:
        log(f"[!] 目标是 {TARGET_IPS} 个可用 IP，实际只有 {len(usable)} 个，跑一段就得歇")
    # 切回第一个可用节点，方便直接开跑
    if usable:
        set_group(usable[0]["name"])
        save_state(usable[0]["name"])
        log(f"[dedupe] 已切到 {usable[0]['name']}（{usable[0]['exit_ip']}）")
    return 0


def cmd_rotate(args) -> int:
    """在可用节点之间轮换，供导入被限流时自动调用。"""
    nodes = load_json(NODES_JSON) or {}
    usable = nodes.get("usable") or []
    if not usable:
        log("[x] 没有可用节点，先跑 dedupe")
        return 1
    st = load_json(STATE_JSON) or {}
    idx = int(st.get("rotate_idx") or 0)
    idx = (idx + 1) % len(usable)
    pick = usable[idx]
    if not set_group(pick["name"]):
        return 1
    time.sleep(0.6)
    ip = _ip_once()
    save_state(pick["name"])
    st = load_json(STATE_JSON) or {}
    st["rotate_idx"] = idx
    dump_json(STATE_JSON, st)
    log(f"[rotate] 换到 {pick['name']}（{pick['region']}）出口 IP={ip}  [{idx + 1}/{len(usable)}]")
    return 0


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="豆瓣导入用：香港节点池 + 出口 IP 轮换")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="拉订阅、解析、筛香港节点")
    f.add_argument("--only", default=None, help="只拉指定订阅，如 A,C")
    f.add_argument("--no-proxy", action="store_true", help="不走本机代理直连")
    f.set_defaults(func=cmd_fetch)

    t = sub.add_parser("test", help="起内核逐个测活")
    t.add_argument("--url", default="http://www.gstatic.com/generate_204")
    t.add_argument("--timeout", type=int, default=4000, help="单个节点超时(ms)")
    t.add_argument("--workers", type=int, default=16)
    t.add_argument("--force", action="store_true", help="杀掉占用 7897/19197 的进程")
    t.set_defaults(func=cmd_test)

    u = sub.add_parser("up", help="用活节点起内核")
    u.add_argument("--force", action="store_true", help="杀掉占用 7897/19197 的进程")
    u.set_defaults(func=cmd_up)

    us = sub.add_parser("use", help="切到指定节点（名字或序号）")
    us.add_argument("name")
    us.set_defaults(func=cmd_use)

    nx = sub.add_parser("next", help="切到下一个能出 IP 的节点")
    nx.set_defaults(func=cmd_next)

    ls = sub.add_parser("list", help="列出活节点")
    ls.set_defaults(func=cmd_list)

    ip = sub.add_parser("ip", help="打印当前出口 IP")
    ip.add_argument("--rounds", type=int, default=1)
    ip.set_defaults(func=cmd_ip)

    st = sub.add_parser("status", help="总览")
    st.set_defaults(func=cmd_status)

    sp = sub.add_parser("stop", help="停掉内核")
    sp.set_defaults(func=cmd_stop)

    dc = sub.add_parser("douban-check", help="用当前节点打一次豆瓣")
    dc.set_defaults(func=cmd_douban_check)

    dd = sub.add_parser("dedupe", help="按出口 IP 去重 + 逐个验豆瓣，输出可用 IP 清单")
    dd.set_defaults(func=cmd_dedupe)

    rt = sub.add_parser("rotate", help="在可用节点之间切到下一个")
    rt.set_defaults(func=cmd_rotate)

    a = p.parse_args(argv)
    ensure_dirs()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
