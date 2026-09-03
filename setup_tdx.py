#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tdx 数据链的一键装配：装 tdx2db → 下全量日线 → 建 tdx.db → 挂每日定时。

    python3 datalake/setup_tdx.py                 # 只体检，什么都不改（默认）
    python3 datalake/setup_tdx.py --install       # 装/升级 tdx2db（按本机 OS 选）
    python3 datalake/setup_tdx.py --bootstrap     # 从零：下 548MB 全量包 -> init
    python3 datalake/setup_tdx.py --sync          # 跑一次增量（= cron 那一步）
    python3 datalake/setup_tdx.py --install-timer # 挂每日定时（按本机 OS）
    python3 datalake/setup_tdx.py --uninstall-timer

## 为什么是一个 Python 文件，而不是 .sh + .ps1 两份

命令确实分 OS（tar/unzip vs Expand-Archive、launchd vs systemd vs schtasks），
但**判据只有一套**：装哪个包、装完怎么验、init 之后行数许不许缩。
两份脚本必然分叉，而分叉那份跑出来的结果**看着正常**（同 `_ingest` 的
"前端不另存一份"那条）。所以 OS 差异收在这一个文件的几张表里
（`ASSETS` / `_install_timer_*`），判据只写一遍。

## 🔴 四个与"网上文档"不一致的实测事实（2026-09-03 核过）

1. **`pip install tdx2db` 装的是【另一个项目】。** PyPI 上的 `tdx2db`
   是 `xbfighting/tdx2db` 0.5.0，只支持 PostgreSQL/MySQL/SQLite —— **没有
   DuckDB**，而本链全靠 DuckDB 的 `tdx.db`。装上去会"看着成功"，然后
   CLI 参数对不上、DuckDB 用不了。
   我们用的是 **Go 写的 `github.com/jing2uo/tdx2db`**（395 star，
   从二进制里的符号 `github.com/jing2uo/tdx2db/database/clickhouse` 确认）。
2. **CLI 实参是 `--dburi` 与 `--min`（布尔）**，不是文档里常见的
   `--dbpath` 与 `--minline 1,5`。以 `tdx2db init --help` 为准 ——
   本机 v2026.5 实测。
3. **没有 `Darwin_x86_64` 预编译包**（只有 arm64）。Intel Mac 要自己
   `go build`，本脚本会响亮报错而不是装一个跑不了的包。
4. **全量包是 `hsjday.zip`（548 MB，沪深京日线合一）**，不是老 README 里
   那三个分市场的 `shlday/szlday/bjlday.zip`（后者仍可用，沪市 230 MB）。

## 🔴 升级 tdx2db 会让现有 `tdx.db` 不兼容 —— 所以 `--install` 有护栏

2026-09-03 实测：本机 v2026.5 建的库是 `_meta.schema_version = 5.0`，
而 v2026.8.12 要 6.x。装上新版之后 `cron` 直接：

    🛑 数据库 schema 版本不兼容 (当前库: v5.x, 需要: v6.x)

★ 它**响亮报错、且没碰数据**（跑完四张表行数与跑前逐一相同）—— 这是好事：
  静默写坏才是灾难。但升级的代价是**整库重建**（548 MB 下载 + 全量 init），
  而 release notes 是空的、没有迁移脚本。

`--install` 的做法（实测可行）：
  ① 新二进制先下到**临时目录**，不覆盖在用的那个
  ② 造一个**只含 `_meta` 一张表**的一次性小库，`schema_version` 抄生产库的
  ③ 拿新二进制对着这个小库跑 `cron`，看它认不认
  ④ 报"不兼容"就**拒绝安装**，并把代价说清楚
🔴 **不拿生产库去试探** —— v2026.8.12 那次确实报错就退了、没碰数据
  （跑完四张表行数逐一相同），但"这次没写坏"是这一版的行为、不是契约。
  一次性小库让这件事跟运气无关。
★ tag（`v2026.8.12`）里看不出它要哪个 schema，所以只能问它 ——
  硬编码一张"版本 -> schema"的表只会在下一个 release 过期。

## 🔴 `init` 是全量覆盖 —— 所以有缩表保护

现有 `tdx.db` 里有 **2,191 个 2026 年后再无数据的代码**（已摘牌）。
如果全量包不含它们，`init` 会让历史**静默缩水** —— 而回测拿缩水后的宇宙
跑出来的结果看着完全正常（幸存者偏差，且不报错）。

所以 `--bootstrap` 在覆盖之前：
  ① 把旧 `tdx.db` 的关键计数记下来（代码数、行数、最早/最晚日）
  ② init 到**另一个文件**，不碰旧的
  ③ 逐项对账，**任一项缩了就拒绝替换**并打印差异
  ④ 只有全部不缩才 `os.replace`（原子），旧的留 `.before_init`
显式要缩就得写 `--allow-shrink`（同 `rebuild_lake_db` 那个开关的理由：
硬拒而不给出路，最后会变成绕过整个入口）。
"""
import argparse
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))            # datalake/
REPO = os.path.dirname(ROOT)                                 # finacial/
TDX = os.path.join(ROOT, 'raw', 'tdx', '_ingest')            # tdx2db 与 tdx.db 的家
BIN = os.path.join(TDX, 'tdx2db' + ('.exe' if os.name == 'nt' else ''))
DB = os.path.join(TDX, 'tdx.db')
VIPDOC = os.path.join(TDX, 'vipdoc')
STAMP = os.path.join(TDX, 'tdx2db.version.json')             # 装的是哪个版本

GH = 'https://api.github.com/repos/jing2uo/tdx2db/releases/latest'
DL = 'https://github.com/jing2uo/tdx2db/releases/download/%s/%s'
# 全量日线包。★ 沪深京合一 548 MB；老的分市场包（shlday/szlday/bjlday）也还能用。
VIPDOC_URL = 'https://data.tdx.com.cn/vipdoc/hsjday.zip'

# OS/架构 -> release 资产名。🔴 **没有 Darwin_x86_64** —— Intel Mac 走 go build。
ASSETS = {
    ('Darwin', 'arm64'): 'tdx2db_Darwin_arm64.tar.gz',
    ('Linux', 'arm64'): 'tdx2db_Linux_arm64.tar.gz',
    ('Linux', 'x86_64'): 'tdx2db_Linux_x86_64.tar.gz',
    ('Windows', 'x86_64'): 'tdx2db_Windows_x86_64.zip',
}
# datalake 只用 tdx.db 的这四张表（load_tdx_kline.py 里查得到）——
# 对账只对它们，别的表变了不影响本链。
TABLES = ('raw_kline_daily', 'raw_adjust_factor', 'raw_basic_daily',
          'raw_symbol_class')


def _arch():
    """归一化成 release 资产名里用的那套写法。"""
    m = platform.machine().lower()
    if m in ('arm64', 'aarch64'):
        return 'arm64'
    if m in ('x86_64', 'amd64'):
        return 'x86_64'
    return m


def _plat():
    return platform.system(), _arch()


def _say(*a):
    print(*a, flush=True)


def _run(cmd, cwd=None, check=True):
    _say('  $ %s' % ' '.join(cmd))
    r = subprocess.run(cmd, cwd=cwd)
    if check and r.returncode != 0:
        raise SystemExit('  ✗ 退出码 %d' % r.returncode)
    return r.returncode


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'curl/8', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=timeout) as f:
        return f.read()


def _download(url, dst, expect=None):
    """带进度的下载。★ 先写 .part 再 rename —— 中断的半个文件不许占正名，
    否则下次跑会把半个 zip 当成"已经下好了"。"""
    tmp = dst + '.part'
    req = urllib.request.Request(url, headers={'User-Agent': 'curl/8'})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers.get('Content-Length') or 0)
        got = 0
        with open(tmp, 'wb') as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    sys.stdout.write('\r  %.0f/%.0f MB  %.0f%%  %.1f MB/s' % (
                        got / 1e6, total / 1e6, got * 100.0 / total,
                        got / 1e6 / max(time.time() - t0, .1)))
                    sys.stdout.flush()
    sys.stdout.write('\n')
    if expect and os.path.getsize(tmp) != expect:
        os.remove(tmp)
        raise SystemExit('  ✗ 大小不对：期望 %d，实得 %d' % (expect, got))
    os.replace(tmp, dst)
    return dst


# ------------------------------------------------------------------ 体检
def _bin_version():
    if not os.path.isfile(BIN):
        return None
    try:
        out = subprocess.run([BIN, 'version'], capture_output=True, text=True,
                             timeout=30).stdout
        for ln in out.splitlines():
            if ln.strip().startswith('tdx2db'):
                return ln.split()[-1]
    except Exception:                                       # noqa: BLE001
        return '?'
    return '?'


def _db_schema_version(path=None):
    """库里 `_meta.schema_version`。tdx2db 用它判兼容 —— 直接读它，
    不要拿生产库去试探新二进制（"这次没写坏"是行为不是契约）。"""
    path = path or DB
    if not os.path.isfile(path):
        return None
    try:
        import duckdb
        con = duckdb.connect(path, read_only=True)
    except Exception:                                       # noqa: BLE001
        return None
    try:
        if not con.execute("SELECT count(*) FROM duckdb_tables()"
                           " WHERE table_name = '_meta'").fetchone()[0]:
            return None
        r = con.execute("SELECT value FROM _meta"
                        " WHERE key = 'schema_version'").fetchone()
        return r[0] if r else None
    except Exception:                                       # noqa: BLE001
        return None
    finally:
        con.close()


def _major(v):
    """'5.0' / 'v2026.8.12' -> 主版本号里的第一个整数（取不到给 None）。"""
    import re
    m = re.search(r'(\d+)', str(v or ''))
    return int(m.group(1)) if m else None


def _db_stats(path=None):
    """tdx.db 的关键计数 —— init 前后对账用的就是它。"""
    path = path or DB
    if not os.path.isfile(path):
        return None
    try:
        import duckdb
    except ImportError:
        return {'error': '没装 duckdb'}
    out = {}
    try:
        con = duckdb.connect(path, read_only=True)
    except Exception as e:                                  # noqa: BLE001
        return {'error': str(e)[:80]}
    try:
        have = {r[0] for r in con.execute('SHOW TABLES').fetchall()}
        for t in TABLES:
            if t not in have:
                out[t] = {'missing': True}
                continue
            n = con.execute('SELECT count(*) FROM %s' % t).fetchone()[0]
            row = {'rows': n}
            cols = {c[0] for c in con.execute('DESCRIBE %s' % t).fetchall()}
            if 'symbol' in cols:
                row['codes'] = con.execute(
                    'SELECT count(DISTINCT symbol) FROM %s' % t).fetchone()[0]
            if 'date' in cols:
                a, b = con.execute(
                    'SELECT min(date), max(date) FROM %s' % t).fetchone()
                row['first'], row['last'] = str(a)[:10], str(b)[:10]
            out[t] = row
    finally:
        con.close()
    return out


def check():
    osname, arch = _plat()
    _say('本机          %s / %s  (python %s)' % (
        osname, arch, platform.python_version()))
    a = ASSETS.get((osname, arch))
    _say('对应的包      %s' % (a or '🔴 无预编译包（见文末说明）'))
    v = _bin_version()
    _say('tdx2db        %s  %s' % (v or '未安装', BIN if v else ''))
    if os.path.isfile(STAMP):
        try:
            _say('              装自 %s' % json.load(
                io.open(STAMP, encoding='utf-8')).get('tag'))
        except Exception:                                   # noqa: BLE001
            pass
    _say('vipdoc        %s' % ('%d 个文件' % sum(
        len(f) for _, _, f in os.walk(VIPDOC)) if os.path.isdir(VIPDOC)
        else '不存在（增量同步不需要它，只有全量 init 要）'))
    st = _db_stats()
    if st is None:
        _say('tdx.db        不存在 —— 跑 --bootstrap 从零建')
    elif 'error' in st:
        _say('tdx.db        读不出来：%s' % st['error'])
    else:
        _say('tdx.db        %.2f GB   schema %s' % (
            os.path.getsize(DB) / 2 ** 30, _db_schema_version() or '?'))
        for t, r in st.items():
            if r.get('missing'):
                _say('   🔴 %-20s 缺这张表' % t)
                continue
            _say('   %-20s %10s 行  %6s 代码  %s ~ %s' % (
                t, '{:,}'.format(r['rows']), '{:,}'.format(r['codes'])
                if 'codes' in r else '—', r.get('first', '—'),
                r.get('last', '—')))
    _say('每日定时      %s' % _timer_status())
    if not a:
        _say('\n🔴 这个平台没有预编译包（release 只有 Darwin_arm64 / '
             'Linux_arm64 / Linux_x86_64 / Windows_x86_64）。')
        _say('   自己编：git clone https://github.com/jing2uo/tdx2db '
             '&& cd tdx2db && go build')
        _say('   ⚠️ 不要 `pip install tdx2db` —— PyPI 上那个是同名的另一个'
             '项目，不支持 DuckDB。')
    return 0


# ------------------------------------------------------- 装 / 升级 tdx2db
def _rel(p):
    return os.path.relpath(p, REPO)


def _unpack(pkg, dst, asset):
    """从 tar.gz / zip 里取出 tdx2db 可执行文件。"""
    if asset.endswith('.zip'):
        with zipfile.ZipFile(pkg) as z:
            for n in z.namelist():
                if os.path.basename(n).startswith('tdx2db'):
                    with z.open(n) as src, open(dst, 'wb') as out:
                        shutil.copyfileobj(src, out)
                    return n
        return None
    with tarfile.open(pkg) as t:
        for m in t.getmembers():
            if m.isfile() and os.path.basename(m.name).startswith('tdx2db'):
                with t.extractfile(m) as f, open(dst, 'wb') as out:
                    shutil.copyfileobj(f, out)
                return m.name
    return None


def _probe_schema(newbin, have):
    """新二进制认不认 `schema_version = have` 的库？

    True 认 / False 不认 / None 探不出来。

    ★ 造一个**只含 `_meta` 一张表**的一次性小库来问 —— 不碰生产库。
      v2026.8.12 实测：对着这个小库跑 `cron` 会打
      "数据库 schema 版本不兼容 (当前库: v5.x, 需要: v6.x)"，够判断了。
    """
    try:
        import duckdb
    except ImportError:
        return None
    import tempfile
    d = tempfile.mkdtemp(prefix='tdxprobe')
    try:
        con = duckdb.connect(os.path.join(d, 'tdx.db'))
        con.execute('CREATE TABLE _meta(key VARCHAR, value VARCHAR)')
        con.execute("INSERT INTO _meta VALUES ('schema_version', ?)", [have])
        con.close()
        r = subprocess.run([newbin, 'cron', '--dburi', 'duckdb://./tdx.db'],
                           cwd=d, capture_output=True, text=True, timeout=300)
        blob = (r.stdout or '') + (r.stderr or '')
        if '不兼容' in blob or 'incompatible' in blob.lower():
            return False
        # ★ 没说不兼容不等于成功（小库里没有数据表，它会因为别的原因失败）——
        #   但"没有版本抱怨"就是我们要的那个信号。
        return True
    except Exception:                                       # noqa: BLE001
        return None
    finally:
        shutil.rmtree(d, ignore_errors=True)


def install(force=False):
    osname, arch = _plat()
    asset = ASSETS.get((osname, arch))
    if not asset:
        raise SystemExit(
            '🔴 %s/%s 没有预编译包。自己编：\n'
            '   git clone https://github.com/jing2uo/tdx2db && cd tdx2db '
            '&& go build\n'
            '   然后把产物放到 %s\n'
            '   ⚠️ 不要 pip install tdx2db —— 那是同名的另一个项目'
            '（不支持 DuckDB）。' % (osname, arch, BIN))
    _say('查最新 release…')
    rel = json.loads(_get(GH))
    tag = rel['tag_name']
    hit = [x for x in rel['assets'] if x['name'] == asset]
    if not hit:
        raise SystemExit('🔴 release %s 里没有 %s（资产名变了？现有：%s）'
                         % (tag, asset, [x['name'] for x in rel['assets']]))
    size = hit[0]['size']
    cur = _bin_version()
    if cur and cur == tag and not force:
        _say('已是 %s，无需重装（要强制请加 --force）' % tag)
        return 0
    _say('下载 %s  %s  (%.1f MB)' % (tag, asset, size / 1e6))
    os.makedirs(TDX, exist_ok=True)
    # ★ 先落到临时目录：兼容性没确认之前，不覆盖在用的那个二进制。
    stage = os.path.join(TDX, '.stage')
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    pkg = os.path.join(stage, asset)
    _download(DL % (tag, asset), pkg, expect=size)
    _say('解包…')
    newbin = os.path.join(stage, os.path.basename(BIN))
    got = _unpack(pkg, newbin, asset)
    os.remove(pkg)
    if not got:
        shutil.rmtree(stage, ignore_errors=True)
        raise SystemExit('🔴 包里找不到 tdx2db 可执行文件')
    if os.name != 'nt':
        os.chmod(newbin, 0o755)

    # ---- 兼容性护栏：新版认不认现有这个库 ----
    have = _db_schema_version()
    if have:
        need = _probe_schema(newbin, have)
        if need is False:
            shutil.rmtree(stage, ignore_errors=True)
            _say('')
            _say('🔴 拒绝安装 %s —— 它不认现有的 tdx.db（schema %s）。'
                 % (tag, have))
            _say('   代价：升级 = **整库重建**（下 548 MB 全量包 + 全量'
                 ' init），而 release notes 是空的、没有迁移脚本。')
            _say('   🔴 装了不重建的话，每日同步会从明天开始一直失败 ——'
                 '而失败是在 launchd 的日志里，你不会立刻看到。')
            _say('   要升就两步一起做：')
            _say('     python3 %s --install --force' % _rel(__file__))
            _say('     python3 %s --bootstrap' % _rel(__file__))
            _say('   不升级也完全可以：现在这个 %s 每天照跑（判据是'
                 ' `--sync` 说"已是最新"）。' % (cur or '?'))
            return 3
        if need is None:
            _say('  ⚠️ 探不出新版认不认这个库（探针本身失败了）——'
                 '按不确定处理，仍然安装，但装完请立刻 `--sync` 验一次。')
        else:
            _say('  ✓ 新版认现有的库（schema %s）' % have)

    # 确认过了才换上去
    if os.path.isfile(BIN):
        shutil.copyfile(BIN, BIN + '.prev')      # 换不回来最麻烦，留一手
    shutil.move(newbin, BIN)
    shutil.rmtree(stage, ignore_errors=True)
    if os.name != 'nt':
        os.chmod(BIN, 0o755)
    # 🔴 装完必须**跑一次**核对版本 —— "文件落地了"不等于"能跑"
    #   （下错架构、被 Gatekeeper 拦、包坏了，都表现为文件在但跑不起来）。
    v = _bin_version()
    if not v or v == '?':
        raise SystemExit('🔴 装好了但 `tdx2db version` 跑不出来 —— '
                         '架构不对？macOS 上可能要 '
                         '`xattr -d com.apple.quarantine %s`' % BIN)
    io.open(STAMP, 'w', encoding='utf-8').write(json.dumps(
        {'tag': tag, 'asset': asset, 'version': v, 'from': got,
         'at': time.strftime('%Y-%m-%dT%H:%M:%S')},
        ensure_ascii=False, indent=2))
    _say('✅ tdx2db %s（release %s）' % (v, tag))
    return 0


# ------------------------------------------------------- 全量：下载 + init
def bootstrap(allow_shrink=False, keep_zip=False, reuse_vipdoc=False):
    if not _bin_version():
        raise SystemExit('🔴 先装 tdx2db：python3 %s --install'
                         % os.path.relpath(__file__, REPO))
    os.makedirs(TDX, exist_ok=True)
    zp = os.path.join(TDX, 'hsjday.zip')
    if reuse_vipdoc and os.path.isdir(VIPDOC):
        _say('沿用已有 vipdoc（%d 个文件）' % sum(
            len(f) for _, _, f in os.walk(VIPDOC)))
    else:
        if not os.path.isfile(zp):
            _say('下载全量日线包（约 548 MB）…')
            _download(VIPDOC_URL, zp)
        else:
            _say('已有 %s（%.0f MB），跳过下载' % (
                os.path.basename(zp), os.path.getsize(zp) / 1e6))
        # 坏包早失败：解之前先验一次（同上传聚宽包那条）
        _say('校验 zip…')
        with zipfile.ZipFile(zp) as z:
            bad = z.testzip()
            if bad:
                raise SystemExit('🔴 zip 坏在 %s —— 删掉重下' % bad)
            n = len(z.namelist())
        _say('  %d 个文件，解到 %s' % (n, VIPDOC))
        shutil.rmtree(VIPDOC, ignore_errors=True)
        os.makedirs(VIPDOC, exist_ok=True)
        with zipfile.ZipFile(zp) as z:
            z.extractall(VIPDOC)
        if not keep_zip:
            os.remove(zp)

    before = _db_stats()
    # 🔴 init 到【另一个文件】，对账通过才替换 —— 直接覆盖的话，
    #   万一全量包不含已摘牌代码，历史就静默缩水了。
    new = DB + '.new'
    if os.path.exists(new):
        os.remove(new)
    _say('\n全量导入（init）—— 这一步比较久')
    _run([BIN, 'init', '--dburi', 'duckdb://./%s' % os.path.basename(new),
          '--dayfiledir', VIPDOC], cwd=TDX)
    after = _db_stats(new)
    if not after or 'error' in after:
        raise SystemExit('🔴 新库读不出来：%s' % after)

    if before and 'error' not in before:
        _say('\n对账（旧 -> 新）：')
        shrink = []
        for t in TABLES:
            b, a = before.get(t) or {}, after.get(t) or {}
            if b.get('missing') or a.get('missing'):
                _say('   %-20s %s' % (t, '新库缺这张表' if a.get('missing')
                                      else '旧库没有，跳过'))
                if a.get('missing'):
                    shrink.append((t, 'table', 1, 0))
                continue
            for k, label in (('rows', '行数'), ('codes', '代码数')):
                if k not in b or k not in a:
                    continue
                _say('   %-20s %-6s %12s -> %12s  %s' % (
                    t, label, '{:,}'.format(b[k]), '{:,}'.format(a[k]),
                    '' if a[k] >= b[k] else '🔴 缩了 %s' % '{:,}'.format(
                        b[k] - a[k])))
                if a[k] < b[k]:
                    shrink.append((t, label, b[k], a[k]))
            if b.get('first') and a.get('first') and a['first'] > b['first']:
                _say('   %-20s 最早日 %s -> %s  🔴 起点变晚' % (
                    t, b['first'], a['first']))
                shrink.append((t, '最早日', b['first'], a['first']))
        if shrink and not allow_shrink:
            _say('\n🔴 拒绝替换 —— 有 %d 项缩了。' % len(shrink))
            _say('   最可能的原因：全量包**不含已摘牌代码**，而旧库里有'
                 '（本机实测 2,191 个 2026 年后再无数据的代码）。')
            _say('   缩水后的宇宙跑回测会有幸存者偏差，**而它不报错**。')
            _say('   新库留在 %s，自己比对完确认要用就加 --allow-shrink。'
                 % os.path.relpath(new, REPO))
            return 2
        if shrink:
            _say('\n⚠️ 有 %d 项缩了，但你显式给了 --allow-shrink，继续。'
                 % len(shrink))
    old = DB + '.before_init'
    if os.path.isfile(DB):
        shutil.move(DB, old)
        _say('\n旧库备份到 %s' % os.path.relpath(old, REPO))
    os.replace(new, DB)
    _say('✅ tdx.db 就绪（%.2f GB）' % (os.path.getsize(DB) / 2 ** 30))
    _say('\n接下来把 tdx.db 灌进 raw/std/mart：')
    _say('   bash %s' % os.path.relpath(
        os.path.join(ROOT, 'sync_daily.sh'), REPO))
    return 0


def sync(minute=False):
    """跑一次增量。★ 与 sync_daily.sh 第 1/6 步是**同一条命令** ——
    这里只是让它能单独跑，判据（--dburi 怎么写）仍然只有一处。"""
    if not _bin_version():
        raise SystemExit('🔴 先装 tdx2db：--install')
    cmd = [BIN, 'cron', '--dburi', 'duckdb://./tdx.db']
    if minute:
        cmd.append('--min')          # ★ 布尔，不是 `--minline 1,5`
    return _run(cmd, cwd=TDX, check=False)


# ------------------------------------------------------------ 每日定时任务
LABEL = 'com.miraclegu.finacial.sync'
SH = os.path.join(ROOT, 'sync_daily.sh')


def _launchd_path():
    return os.path.expanduser('~/Library/LaunchAgents/%s.plist' % LABEL)


def _timer_status():
    osname = platform.system()
    try:
        if osname == 'Darwin':
            p = _launchd_path()
            if not os.path.isfile(p):
                return '未安装（launchd）'
            out = subprocess.run(['launchctl', 'list'], capture_output=True,
                                 text=True).stdout
            live = LABEL in out
            # 🔴 已安装的 plist 与仓库正本不一致要报出来 —— 改了仓库里的
            #   但没重装，跑的还是旧的（比如时间还停在旧值），而这不报错。
            same = (os.path.isfile(os.path.join(ROOT, '_manifest',
                                                LABEL + '.plist'))
                    and io.open(p, encoding='utf-8').read()
                    == io.open(os.path.join(ROOT, '_manifest',
                                            LABEL + '.plist'),
                               encoding='utf-8').read())
            return 'launchd %s%s' % ('已加载' if live else '已装但未加载',
                                     '' if same else '　⚠️ 与仓库正本不一致')
        if osname == 'Linux':
            u = os.path.expanduser(
                '~/.config/systemd/user/finacial-sync.timer')
            if os.path.isfile(u):
                out = subprocess.run(
                    ['systemctl', '--user', 'is-enabled', 'finacial-sync.timer'],
                    capture_output=True, text=True).stdout.strip()
                return 'systemd timer（%s）' % (out or '?')
            out = subprocess.run(['crontab', '-l'], capture_output=True,
                                 text=True).stdout
            return 'cron 已装' if SH in out else '未安装（systemd/cron）'
        if osname == 'Windows':
            out = subprocess.run(
                ['schtasks', '/query', '/tn', 'finacial-sync'],
                capture_output=True, text=True)
            return '计划任务已装' if out.returncode == 0 else '未安装（schtasks）'
    except FileNotFoundError:
        return '查不了（调度器命令不在 PATH）'
    except Exception as e:                                  # noqa: BLE001
        return '查不了：%s' % str(e)[:40]
    return '这个平台没做'


def _sched_path():
    """定时任务要用的 PATH。

    🔴 **launchd / systemd 的默认环境很窄，`python3` 会解析到系统那个**，
      而系统 python3 没装 duckdb —— 表现是 `sync_daily.sh` 的
      2/6 PIT 快照 `ModuleNotFoundError: No module named 'duckdb'`。
      那一步是"漏一天永久丢失"的，而失败只写在 launchd 的日志里，
      **第二天你不会看到**。

    ★ 判据用**当前正在跑本脚本的那个解释器**（`sys.executable` 的目录）
      放在最前 —— 硬编码 `/opt/homebrew/bin` 只在我这台机器上对，
      而这个文件的全部意义就是换机器也能用。
      （2026-09-03 实测踩过：我重新生成 plist 时漏抄了原正本里的
      EnvironmentVariables，当晚手动触发就炸在这一步。）
    """
    here = os.path.dirname(os.path.abspath(sys.executable))
    base = ['/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin',
            '/usr/sbin', '/sbin']
    return ':'.join([here] + [x for x in base if x != here])


def _plist(hour, minute):
    """从**当前仓库路径**生成 —— 仓库里那份的绝对路径是我这台机器的，
    换台机器直接 cp 过去就指错了（而 launchd 不会因此报错，只是不跑）。"""
    log = os.path.join(ROOT, '_manifest', 'launchd.out')
    err = os.path.join(ROOT, '_manifest', 'launchd.err')
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<!-- 由 datalake/setup_tdx.py --install-timer 生成（路径取自当前仓库）。
     手改这个文件的话，下次 --install-timer 会覆盖它。 -->
<dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>%s</string></array>
  <key>WorkingDirectory</key><string>%s</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>%d</integer>
        <key>Minute</key><integer>%d</integer></dict>
  <key>StandardOutPath</key><string>%s</string>
  <key>StandardErrorPath</key><string>%s</string>
  <!-- 机器睡过了预定时刻，醒来补跑一次 —— 这正是不用 cron 的理由 -->
  <key>RunAtLoad</key><false/>
  <!-- 🔴 PATH 里必须有装了 duckdb 的那个 python3；launchd 默认环境很窄，
       缺了它 2/6 PIT 快照会 ModuleNotFoundError，而那一步漏一天不可逆 -->
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>%s</string></dict>
</dict>
</plist>
""" % (LABEL, SH, ROOT, hour, minute, log, err, _sched_path())


def _verify_sched_env():
    """用定时任务那份 PATH 真跑一次 `python3 -c "import duckdb"`。

    🔴 装完必须验 —— "plist 写出去了"不等于"到点跑得起来"。
      不验的话，坏了也要等到明天、而且只写在日志里。
    """
    env = dict(os.environ, PATH=_sched_path())
    r = subprocess.run(['python3', '-c', 'import duckdb, sys;'
                        ' print(sys.executable)'],
                       capture_output=True, text=True, env=env, timeout=60)
    if r.returncode != 0:
        _say('  🔴 定时任务的 PATH 里那个 python3 跑不了 import duckdb：')
        _say('     %s' % (r.stderr or '').strip()[:200])
        _say('     PATH=%s' % _sched_path())
        _say('     -> 到点会炸在 2/6 PIT 快照（漏一天不可逆），先修这个')
        return False
    _say('  ✓ 定时环境自证：%s 能 import duckdb'
         % (r.stdout or '').strip())
    return True


def install_timer(at='18:10'):
    hh, mm = (int(x) for x in at.split(':'))
    osname = platform.system()
    if not os.path.isfile(SH):
        raise SystemExit('🔴 找不到 %s' % SH)
    ok = _verify_sched_env()
    if osname == 'Darwin':
        p = _launchd_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        io.open(p, 'w', encoding='utf-8').write(_plist(hh, mm))
        # ★ 先 unload 再 load：已加载时 load 会报错退出，而"报错了"
        #   与"装没装上"是两件事 —— 判据永远是 launchctl list。
        subprocess.run(['launchctl', 'unload', '-w', p],
                       capture_output=True)
        _run(['launchctl', 'load', '-w', p], check=False)
        shutil.copyfile(p, os.path.join(ROOT, '_manifest', LABEL + '.plist'))
        _say('✅ launchd 每日 %s；正本也更新到 _manifest/%s'
             % (at, '' if ok else '　⚠️ 但环境自证没过，见上'))
    elif osname == 'Linux':
        d = os.path.expanduser('~/.config/systemd/user')
        os.makedirs(d, exist_ok=True)
        io.open(os.path.join(d, 'finacial-sync.service'), 'w',
                encoding='utf-8').write(
            '[Unit]\nDescription=finacial 数据同步\n\n[Service]\n'
            'Type=oneshot\nWorkingDirectory=%s\n'
            # 🔴 同 launchd 那条：systemd 的环境也很窄，python3 会解析到
            #   系统那个（没装 duckdb），2/6 PIT 快照就炸，而它漏一天不可逆
            'Environment=PATH=%s\n'
            'ExecStart=/bin/bash %s\n' % (ROOT, _sched_path(), SH))
        io.open(os.path.join(d, 'finacial-sync.timer'), 'w',
                encoding='utf-8').write(
            '[Unit]\nDescription=每日 %s 跑数据同步\n\n[Timer]\n'
            'OnCalendar=*-*-* %02d:%02d:00\n'
            # ★ 机器在预定时刻睡着 -> 醒来补跑（launchd 天生如此，
            #   systemd 要显式 Persistent=true，cron 则做不到）
            'Persistent=true\n\n[Install]\nWantedBy=timers.target\n'
            % (at, hh, mm))
        _run(['systemctl', '--user', 'daemon-reload'], check=False)
        _run(['systemctl', '--user', 'enable', '--now',
              'finacial-sync.timer'], check=False)
        _say('✅ systemd user timer 每日 %s（Persistent=true，睡过会补跑）'
             % at)
        _say('   ⚠️ 要在没登录时也跑：sudo loginctl enable-linger $USER')
    elif osname == 'Windows':
        _run(['schtasks', '/create', '/tn', 'finacial-sync', '/tr',
              'bash "%s"' % SH, '/sc', 'daily', '/st', at, '/f'],
             check=False)
        _say('✅ 计划任务 finacial-sync 每日 %s' % at)
        _say('   ⚠️ sync_daily.sh 是 bash 脚本 —— Windows 上要有 '
             'Git Bash / WSL，且 tdx2db 要用 Windows 版')
    else:
        raise SystemExit('🔴 %s 上没做定时安装' % osname)
    return 0


def uninstall_timer():
    osname = platform.system()
    if osname == 'Darwin':
        p = _launchd_path()
        if os.path.isfile(p):
            _run(['launchctl', 'unload', '-w', p], check=False)
            os.remove(p)
    elif osname == 'Linux':
        _run(['systemctl', '--user', 'disable', '--now',
              'finacial-sync.timer'], check=False)
    elif osname == 'Windows':
        _run(['schtasks', '/delete', '/tn', 'finacial-sync', '/f'],
             check=False)
    _say('🔴 关掉之后 daily_snapshot 就不跑了 —— 它【漏一天永久丢失】'
         '（tdx 的名称/分类/板块成分是 type-1 覆盖写）。')
    _say('   而"关了自动同步"本身不报错，几个月后才会发现历史缺口。')
    return 0


def main():
    ap = argparse.ArgumentParser(
        description='tdx 数据链装配（跨 macOS / Linux / Windows）')
    ap.add_argument('--install', action='store_true', help='装/升级 tdx2db')
    ap.add_argument('--force', action='store_true', help='已是最新也重装')
    ap.add_argument('--bootstrap', action='store_true',
                    help='从零：下全量日线包 -> init（带缩表保护）')
    ap.add_argument('--allow-shrink', action='store_true',
                    help='init 后行数/代码数缩了也接受（要显式声明）')
    ap.add_argument('--keep-zip', action='store_true', help='解完保留 zip')
    ap.add_argument('--reuse-vipdoc', action='store_true',
                    help='已有 vipdoc 就不重下重解')
    ap.add_argument('--sync', action='store_true', help='跑一次增量')
    ap.add_argument('--min', action='store_true', help='增量也抓 1 分钟线')
    ap.add_argument('--install-timer', action='store_true', help='挂每日定时')
    ap.add_argument('--at', default='18:10', help='定时时刻，默认 18:10')
    ap.add_argument('--uninstall-timer', action='store_true')
    a = ap.parse_args()
    if a.install:
        return install(force=a.force)
    if a.bootstrap:
        return bootstrap(allow_shrink=a.allow_shrink, keep_zip=a.keep_zip,
                         reuse_vipdoc=a.reuse_vipdoc)
    if a.sync:
        return sync(minute=a.min)
    if a.install_timer:
        return install_timer(at=a.at)
    if a.uninstall_timer:
        return uninstall_timer()
    return check()


if __name__ == '__main__':
    sys.exit(main() or 0)
