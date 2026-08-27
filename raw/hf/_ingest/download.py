#!/usr/bin/env python3
"""下载 yifishbossman/financial-analyst-data-full 数据集。

为什么不直接用 snapshot_download
--------------------------------
hf-mirror.com 不重写目录树分页的 `Link: rel="next"` 头，返回的下一页 URL 指向
huggingface.co（国内被墙）。而 snapshot_download 的 allow_patterns 过滤必须先列全
80,347 条目录树，走到第 2 页就断。本脚本自己分页并把 next URL 的 host 改写回镜像，
再逐文件调 hf_hub_download —— 文件下载通道（/resolve/）在镜像上是正常的。

数据集实测（sha d16be98，2026-05-24）：
    cn_data_5min/  16,453 个   4.948 GB   qlib 5 分钟线
    parquet/           24 个   0.697 GB   打包好的 parquet（含两个 340MB 大文件）
    cn_data/       56,388 个   0.680 GB   qlib 日线（文件极多但极小，均 12KB）
    news_data/      7,363 个   0.273 GB   F10 原始文本
    tdx_finance/      117 个   0.239 GB   通达信财报 zip
    合计           80,347 个   6.838 GB
注：README 写的 "~14.1 GB" 与实测不符，真实总量 6.838 GB。

瓶颈是每文件约 1 秒的请求往返，不是带宽，所以并发要开大、能少下文件就少下。

用法：
    python download.py --mirror --dry-run              # 看清单和体积（首次要走 87 页树）
    python download.py --mirror --subset no-bars       # 不要日线/5min：7,506 个 / 1.2GB
    python download.py --mirror --subset parquet       # 只要 parquet：24 个 / 0.7GB
    python download.py --mirror                        # 全量：80,347 个 / 6.8GB
目录树会缓存到 .tree_cache.json，重跑不再走 87 页；用 --refresh-tree 强制刷新。
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import fnmatch
import json
import os
import shutil
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import httpx

REPO_ID = "yifishbossman/financial-analyst-data-full"
REPO_TYPE = "dataset"
REVISION = "main"
HERE = Path(__file__).resolve().parent
TARGET_DIR = HERE / "a_stock_data"
TREE_CACHE = HERE / ".tree_cache.json"

MIRROR_ENDPOINT = "https://hf-mirror.com"
DEFAULT_WORKERS = 8
# 实测 hf-mirror：/resolve/ 到 12 rps 仍 100% 通过，/api/ 在 5 rps 就 429。
# 本脚本只走 /resolve/，8 rps 留了安全余量。
DEFAULT_RPS = 8.0
PAGE_LIMIT = 1000

SUBSETS = {
    "parquet": ["parquet/*"],
    "daily": ["cn_data/*"],
    "5min": ["cn_data_5min/*"],
    "news": ["news_data/*"],
    "finance": ["tdx_finance/*"],
    "no-bars": ["parquet/*", "news_data/*", "tdx_finance/*", "*.md"],
    "all": None,
}

NET_HINT = (
    "\n排查顺序：1) 直连 huggingface.co 是否通（国内通常不通）"
    "\n         2) 加 --mirror 走 hf-mirror.com"
    "\n         3) 若仓库需鉴权，设 HF_TOKEN 或先 huggingface-cli login"
)

_print_lock = threading.Lock()


class RateLimiter:
    """限制每秒发起的请求数。镜像限流一次要睡 280s，宁可主动放慢。"""

    def __init__(self, rps: float):
        self.interval = 1.0 / rps if rps > 0 else 0.0
        self.lock = threading.Lock()
        self.next_slot = 0.0

    def acquire(self) -> None:
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            wait = max(0.0, self.next_slot - now)
            self.next_slot = max(now, self.next_slot) + self.interval
        if wait:
            time.sleep(wait)


def say(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def gb(n: int) -> float:
    return n / 1024**3


def api_host(endpoint: str) -> str:
    return urllib.parse.urlparse(endpoint).netloc


def fetch_tree(endpoint: str, revision: str) -> list[tuple[str, int]]:
    """分页拉全量文件树。关键：把镜像返回的 next URL host 改写回镜像。"""
    host = api_host(endpoint)
    url = (
        f"{endpoint}/api/{REPO_TYPE}s/{REPO_ID}/tree/{revision}"
        f"?recursive=true&expand=false&limit={PAGE_LIMIT}"
    )
    rows: list[tuple[str, int]] = []
    page = 0
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        while url:
            resp = client.get(url)
            resp.raise_for_status()
            for item in resp.json():
                if item.get("type") == "file":
                    rows.append((item["path"], item.get("size") or 0))
            page += 1
            if page % 10 == 0:
                say(f"  ...第 {page} 页，累计 {len(rows)} 个文件")
            nxt = resp.links.get("next", {}).get("url")
            if not nxt:
                break
            parsed = urllib.parse.urlparse(nxt)
            url = urllib.parse.urlunparse(parsed._replace(scheme="https", netloc=host))
    say(f"目录树完成：{page} 页 / {len(rows)} 个文件")
    return rows


def load_tree(endpoint: str, revision: str, refresh: bool) -> list[tuple[str, int]]:
    if TREE_CACHE.exists() and not refresh:
        cached = json.loads(TREE_CACHE.read_text())
        if cached.get("revision") == revision:
            rows = [(p, s) for p, s in cached["files"]]
            say(f"复用目录树缓存 {TREE_CACHE.name}（{len(rows)} 个文件，--refresh-tree 可刷新）")
            return rows
    say("拉取目录树（80k 文件约 87 页，需几分钟）...")
    rows = fetch_tree(endpoint, revision)
    TREE_CACHE.write_text(json.dumps({"revision": revision, "files": rows}))
    return rows


def select(rows, include: list[str] | None, exclude: list[str] | None):
    out = rows
    if include:
        out = [(p, s) for p, s in out if any(fnmatch.fnmatch(p, pat) for pat in include)]
    if exclude:
        out = [(p, s) for p, s in out if not any(fnmatch.fnmatch(p, pat) for pat in exclude)]
    return out


def pending(rows):
    """按本地文件大小判断是否已下完 —— 避免为 8 万个文件各发一次 HEAD 校验 etag。"""
    todo = []
    for path, size in rows:
        local = TARGET_DIR / path
        if local.exists() and local.stat().st_size == size:
            continue
        todo.append((path, size))
    return todo


def check_disk(need_bytes: int) -> None:
    probe = TARGET_DIR if TARGET_DIR.exists() else TARGET_DIR.parent
    free = shutil.disk_usage(probe).free
    need = need_bytes * 1.1 + 1 * 1024**3  # 10% 余量 + 1GB
    if free < need:
        sys.exit(f"✗ 磁盘剩余 {gb(free):.1f}GB，需要约 {gb(need):.1f}GB")
    say(f"磁盘剩余 {gb(free):.1f}GB，需要约 {gb(need):.1f}GB，够用")


RETRY_WAITS = (5, 15, 45, 90)
RETRYABLE = {429, 500, 502, 503, 504}
CHUNK = 1 << 20


def file_url(endpoint: str, revision: str, path: str) -> str:
    return f"{endpoint}/{REPO_TYPE}s/{REPO_ID}/resolve/{revision}/{urllib.parse.quote(path)}"


def fetch_one(client, url: str, dest: Path, size: int, limiter: RateLimiter) -> str | None:
    """下一个文件。返回 None 表示成功，否则返回错误描述。

    直连 /resolve/ 而不用 hf_hub_download：后者每个文件要先 HEAD /api/resolve-cache/，
    而实测 /api/ 在 5 rps 就开始 429，/resolve/ 到 12 rps 仍 100% 通过。
    文件大小已从目录树拿到，不需要 etag 协商。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    last = "未知错误"
    for wait in (0,) + RETRY_WAITS:
        if wait:
            time.sleep(wait)
        limiter.acquire()
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code in RETRYABLE:
                    last = f"HTTP {resp.status_code}"
                    continue
                resp.raise_for_status()
                append = have > 0 and resp.status_code == 206
                if not append:
                    have = 0
                with open(part, "ab" if append else "wb") as fh:
                    for chunk in resp.iter_bytes(CHUNK):
                        fh.write(chunk)
        except (httpx.HTTPError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            continue
        got = part.stat().st_size
        if size and got != size:
            last = f"大小不符 {got} != {size}"
            part.unlink(missing_ok=True)
            continue
        part.replace(dest)
        return None
    return last


def download_all(todo, *, endpoint: str, revision: str, workers: int, rps: float) -> int:
    limiter = RateLimiter(rps)
    total = len(todo)
    total_bytes = sum(s for _, s in todo)
    done = done_bytes = 0
    failed: list[tuple[str, str]] = []
    throttled = 0
    lock = threading.Lock()
    t0 = time.monotonic()

    client = httpx.Client(
        timeout=httpx.Timeout(30.0, read=120.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=workers, max_keepalive_connections=workers),
    )

    def one(item) -> None:
        nonlocal done, done_bytes, throttled
        path, size = item
        err = fetch_one(client, file_url(endpoint, revision, path), TARGET_DIR / path, size, limiter)
        with lock:
            if err:
                failed.append((path, err))
                if "429" in err:
                    throttled += 1
                return
            done += 1
            done_bytes += size
            if done % 200 == 0 or done == total:
                el = time.monotonic() - t0
                rate = done / el if el else 0
                eta = (total - done) / rate / 60 if rate else 0
                say(
                    f"  进度 {done}/{total} ({done / total * 100:.1f}%) "
                    f"{gb(done_bytes):.2f}/{gb(total_bytes):.2f}GB "
                    f"{rate:.1f} 文件/s 剩约 {eta:.1f} 分钟 失败 {len(failed)}"
                )

    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, todo))
    client.close()

    if throttled:
        say(f"\n⚠ {throttled} 个文件重试耗尽仍被限流(429) —— 调小 --rps 后重跑")
    if failed:
        say(f"\n✗ {len(failed)} 个文件失败（重跑本脚本会自动补，已下好的会跳过）：")
        for path, err in failed[:10]:
            say(f"   {path}  {err}")
        if len(failed) > 10:
            say(f"   ...另有 {len(failed) - 10} 个")
    return len(failed)


def main() -> int:
    parser = argparse.ArgumentParser(description=f"下载 {REPO_ID}")
    parser.add_argument("--dry-run", action="store_true", help="只列清单和体积，不下载")
    parser.add_argument("--subset", choices=sorted(SUBSETS), default="all", help="预设子集")
    parser.add_argument("--include", action="append", help="自定义匹配模式，覆盖 --subset")
    parser.add_argument("--exclude", action="append", help="排除匹配的文件，可重复传")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"并发，默认 {DEFAULT_WORKERS}")
    parser.add_argument(
        "--rps", type=float, default=DEFAULT_RPS,
        help=f"每秒最多发起多少个文件请求，默认 {DEFAULT_RPS}；镜像限流就调小",
    )
    parser.add_argument("--revision", default=REVISION, help="分支 / tag / commit sha")
    parser.add_argument("--mirror", action="store_true", help=f"走镜像 {MIRROR_ENDPOINT}")
    parser.add_argument("--endpoint", help="自定义 endpoint，覆盖 --mirror")
    parser.add_argument("--refresh-tree", action="store_true", help="强制重新拉目录树")
    parser.add_argument("--xet", action="store_true", help="走镜像时也启用 xet（默认关，镜像代理不可靠）")
    args = parser.parse_args()

    endpoint = args.endpoint or (MIRROR_ENDPOINT if args.mirror else "https://huggingface.co")
    # 镜像对 xet CAS 端点的代理不可靠，默认关掉走普通 HTTP
    if endpoint != "https://huggingface.co" and not args.xet:
        os.environ["HF_HUB_DISABLE_XET"] = "1"

    include = args.include or SUBSETS[args.subset]
    say(f"目标目录: {TARGET_DIR}")
    say(f"endpoint: {endpoint}   子集: {args.subset}" + (f"  模式: {include}" if include else ""))

    try:
        rows = load_tree(endpoint, args.revision, args.refresh_tree)
    except httpx.HTTPStatusError as exc:
        return fail(f"目录树接口返回 {exc.response.status_code}{NET_HINT}")
    except (httpx.HTTPError, OSError) as exc:
        return fail(f"拉目录树失败: {type(exc).__name__}: {exc}{NET_HINT}")
    except KeyboardInterrupt:
        return fail("已中断")

    picked = select(rows, include, args.exclude)
    if not picked:
        return fail("没有文件匹配当前 --subset / --include / --exclude")
    todo = pending(picked)
    picked_bytes = sum(s for _, s in picked)
    todo_bytes = sum(s for _, s in todo)

    say(f"\n匹配 {len(picked)} 个文件 / {gb(picked_bytes):.3f} GB")
    say(f"待下载 {len(todo)} 个 / {gb(todo_bytes):.3f} GB（已完成 {len(picked) - len(todo)} 个）")
    if todo:
        rate = min(args.workers, args.rps)
        say(f"按 {rate:.0f} 文件/秒 估算，约需 {len(todo) / rate / 60:.1f} 分钟")

    if args.dry_run:
        todo_paths = {p for p, _ in todo}
        for path, size in sorted(picked, key=lambda x: -x[1])[:15]:
            mark = "↓" if path in todo_paths else "✓"
            say(f"  {mark} {size / 1024**2:9.2f} MB  {path}")
        if len(picked) > 15:
            say(f"  ...另有 {len(picked) - 15} 个文件")
        return 0

    if not todo:
        say("✅ 全部文件已在本地，无需下载")
        return 0

    check_disk(todo_bytes)
    try:
        n_failed = download_all(
            todo, endpoint=endpoint, revision=args.revision,
            workers=args.workers, rps=args.rps,
        )
    except KeyboardInterrupt:
        say("\n已中断。已下载的部分保留，重跑本脚本会跳过已完成的文件。")
        return 130

    if n_failed:
        return 1
    say(f"✅ 数据已下载到: {TARGET_DIR}")
    return 0


def fail(msg: str) -> int:
    print(f"✗ {msg}", file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
