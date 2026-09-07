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

## 🔴 `hsjday.zip` 里的路径是 Windows 反斜杠 —— Python 解压不出目录树

2026-09-03 实测：`hsjday.zip` 的 **12,392 个条目全部**长这样

    sh + 反斜杠 + lday + 反斜杠 + sh000001.day    零个目录条目

Python 的 `zipfile` 按 ZIP 规范把反斜杠当**普通字符**，所以 `extractall`
出来是**平坦的、文件名里带反斜杠的 12,392 个文件**，而不是
`vipdoc/sh/lday/*.day` 的目录树。tdx2db 于是一个文件都扫不到，报

    🛑 failed to import stock csv: IO Error: No files found that match
       the pattern ".../tdx2db-temp-*/stock.csv"

—— 这个报错**完全指不到"路径分隔符"这件事上**（它在抱怨自己的中间文件）。

★ 讽刺的是命令行 `unzip -q hsjday.zip -d vipdoc` 是对的（unzip 会把反斜杠
  转成 `/`），但 Windows 上没有 unzip —— 而这个文件的意义就是跨 OS。
  所以 `_extract_zip` 自己把反斜杠归一成 `/` 再建目录，顺手防路径穿越
  （`..` / 绝对路径一律拒 —— `extractall` 的老 CVE 就是这个）。

## 🔴 升级 tdx2db 会让现有 `tdx.db` 不兼容 —— 所以 `--install` 有护栏

2026-09-03 实测：本机 v2026.5 建的库是 `_meta.schema_version = 5.0`，
而 v2026.8.12 要 6.x。装上新版之后 `cron` 直接：

    🛑 数据库 schema 版本不兼容 (当前库: v5.x, 需要: v6.x)

★ 它**响亮报错、且没碰数据**（跑完四张表行数与跑前逐一相同）—— 这是好事：
  静默写坏才是灾难。但升级的代价是**整库重建**（548 MB 下载 + 全量 init），
  而**没有迁移脚本**（release notes 有内容，逐个读过，见下一节）。

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

## 🔴 结论：**不要升级**（2026-09-04 逐个 PR 读过）

问题是"切换到最新版数据能得到什么"。v2026.5 之后一共 3 个 release，
release notes 有内容（我先前写"是空的"是错的），逐个读完 diff：

| 版本 | 改了什么 | 对本项目 |
|---|---|---|
| v2026.6.27 | 无 notes（只有 Full Changelog） | ? |
| v2026.7.1 | PR #118「按品种缩放日线价格，修复 ETF 价格缩小」 | **零收益** |
| v2026.8.12 | PR #124「修复 md1 ETF 成交量溢出并导入指数涨跌家数」 | **近零** |

- **PR #118** 的 diff 明写 `scale 由 model.PriceScale(symbol) 决定:
  股票=100, ETF/LOF/B股=1000` —— **股票口径没变**，改的只是 ETF/LOF/B股，
  而本项目 panel 只做股票（`m.class='stock'` 且排除 B 股）。
- **PR #124** 把 md1 的 volume 从 `uint32 [56:60]` 改成 `uint64 [56:64]`
  （测试用例 8,285,468,730），并从 md1 的 `[152:156]/[232:236]` 读
  指数涨跌家数存进 `raw_kline_daily`。涨跌家数本项目自己算
  （`market.TRADEABLE` 一处判据），只有交叉校验价值。

🔴 **而它修不了本项目那两个真问题**（两个都在 2026-09-04 定案，
判据与证据见 `build/build_panel_daily.py` 文件头「成交量单位」那一节）：

1. **那 17 天 volume 少乘 100**（74,952 行、每天几乎全市场）。成因是 md1
   合并路径写 `.day` 时 volume 没换算成"股×100"：
   `makeDayRecord` 里 `PutUint32(buf[24:28], rec.Volume)` 直接写股数
   —— **PR #118/#124 都没动那一行**。已在 mart 层加第三档判据修掉。
2. **京东方 A 单日 51.9 亿股溢出**（9 行）。`.day` 的 volume 字段就是
   uint32、上限 42.9 亿股，拿新浪真值对过精确到个位
   （`5,187,899,669 − 2^32 = 892,932,373` = 库里 `volume/100`）。
   这是**通达信 .day 的格式物理上限**，换任何版本的 tdx2db 都修不了。

而升级的代价是确定的三条（都在下面各节有实测）：
schema v5→v6 **整库重建**、`hsjday.zip` **不含已摘牌代码**（A 股宇宙
5,925 → 5,886，幸存者偏差）、全库 `turnover` **口径变成小数**。

**所以：收益接近零、代价确定 → 保持 v2026.5 + schema 5.0。**
`--install` 的护栏（一次性小库试探 schema）继续拦着就是对的。
★ 什么时候该重新评估：本项目开始用 ETF/LOF/B 股，或者 tdx2db 出了
  带迁移脚本的版本，或者官方全量包开始包含已摘牌代码。

## 🔴 `init` 只导日线 —— 复权因子与基础面要靠紧跟的 `cron`

2026-09-03 实测：`init --dayfiledir vipdoc` 跑完自报"🚀 股票数据导入成功 /
初始化完成"，但

    raw_adjust_factor    23,525,905 -> 0      整张表空
    raw_basic_daily      23,525,905 -> 0      整张表空

因为**复权因子要 gbbq（股本变迁）**，而 gbbq 不在 `hsjday.zip` 里。
`tdx2db` 自己会下它（二进制里有
`http://www.tdx.com.cn/products/data/data/dbf/gbbq.zip`），但那是在
**`cron`** 里 —— 紧跟一次 `cron` 之后：

    🐢 开始下载股本变迁数据 / 📈 股本变迁数据导入成功
    📟 计算股票基础行情 -> 🔢 基础行情导入成功
    📟 计算股票复权因子 -> 🔢 复权因子导入成功
    raw_adjust_factor / raw_basic_daily  0 -> 22,057,837

所以 `--bootstrap` = **init 然后 cron**，对账放在 cron 之后做。
★ `init` "成功"了但两张表是空的 —— 这就是为什么对账不能只看"命令退出码 0"。

## 🔴 已摘牌的股票补不回来（2026-09-03 实测定案）

`hsjday.zip` 只有 12,392 个 `.day`，而旧库有 43,401 个代码。对账（init+cron）：

| | 旧 | 新 | 差 |
|---|---|---|---|
| `raw_kline_daily` | 36,320,978 | 29,622,428 | −670 万行 |
| `raw_symbol_class` | 43,401 | 12,389 | −31,012 |
| **A 股代码（60/00/30/68）** | **5,925** | **5,886** | **−39 只** |
| `raw_adjust_factor` | 23,525,905 | 22,057,837 | −147 万 |
| `raw_gbbq` | 205,328 | 205,372 | **+44**（更新了） |

拆开看：
- 少的 31,012 里 **22,016 个债券 + 6,811 个基金**，本项目用不到（panel 只做股票）
- **指数/板块 2,431 个是齐的** —— `hsjday.zip` 含指数，不需要另下 `tdxzs_day.zip`
- 🔴 少的股票**全部是已摘牌的**：最后交易于 1997~2021 年，
  2026-06 之后还在交易的 **0 只** —— 所以缩水不影响"今天能买什么"，
  但**影响回测的历史宇宙**（幸存者偏差）

**结论：全量包不含已摘牌代码，这一点现在是实测定案，不再是"没验证过"。**
所以 `--bootstrap` 只适合**从零装机**；已有库的机器不要跑它
（会用 5,886 只的宇宙换掉 5,925 只的）。

## 🔴🔴 `--bootstrap` 会让 `turnover` 全程差 100 倍 —— 缩表护栏拦不住

2026-09-03 实测（v2026.5 vs v2026.8.12、生产库 vs hsjday.zip 新建，三方对比）：

**通达信改过 `.day` 里 `volume` 的单位。** 同一 (symbol, date)：

    生产库（老 vipdoc 多年累积）  volume = 真实股数 × 100
    新库（hsjday.zip 现在下的）    volume = 真实股数        ← 差 100 倍

而 `turnover` 是 tdx2db 用 `volume / 流通股数` **算**出来的，所以：

    生产库  turnover = 百分数（2026 年中位 1.95，即 1.95%）
    新库    turnover = 小数  （同期约 0.0195）

`load_tdx_kline.py` 把 `raw_basic_daily` **原样**拷进 `raw/tdx/basic`，
`build_panel_daily.py` 直接取 `b.turnover` —— 所以**重建一次，面板的
turnover 就从百分数变成小数，全程差 100 倍**。换手榜、`sgmspeg` 的
`turnover_volatility` 全都会静默错 100 倍。

🔴 **缩表护栏拦不住它**：行数一行不少，是**值**变了。所以另加一条
**口径护栏**（`_check_scale`）：拿两边重叠的最近一段比 `turnover` 中位数，
比值偏离 1 超过 5 倍就拒绝替换。要绕过同样得显式 `--allow-shrink`。

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


def _extract_zip(zp, dest):
    """解 zip，**把 Windows 反斜杠归一成目录分隔符**。

    🔴 `hsjday.zip` 里 12,392 个条目的分隔符全是**反斜杠**、
      零个目录条目。Python 的 `zipfile` 按规范把反斜杠当普通字符，
      `extractall` 会给出一堆平坦的怪文件名，而 tdx2db 要的是目录树 ——
      它扫不到文件，报的却是"我的中间文件 stock.csv 不存在"，
      **那个报错指不到真正的原因**。

    ★ 顺手防路径穿越：`..` 与绝对路径一律拒（`extractall` 的老问题）。
    返回落盘的文件数。
    """
    n = 0
    with zipfile.ZipFile(zp) as z:
        for i in z.infolist():
            name = i.filename.replace('\\', '/')
            if i.is_dir() or name.endswith('/'):
                continue
            parts = [x for x in name.split('/') if x not in ('', '.')]
            if any(x == '..' for x in parts) or name.startswith('/'):
                raise SystemExit('🔴 zip 里有可疑路径，拒绝解压：%r'
                                 % i.filename)
            out = os.path.join(dest, *parts)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with z.open(i) as src, open(out, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            n += 1
    return n


def _check_scale(old_db, new_db):
    """口径护栏：两边重叠的最近一段，`turnover` 中位数的比值。

    🔴 缩表护栏只看行数，而这个坑是**值**变了（volume 的单位变了 ->
      turnover 从百分数变小数，全程 100 倍），行数一行不少。
    返回 (比值, 说明) 或 None（比不了）。
    """
    try:
        import duckdb
    except ImportError:
        return None
    con = duckdb.connect(':memory:')
    try:
        con.execute("ATTACH '%s' AS o (READ_ONLY)" % old_db)
        con.execute("ATTACH '%s' AS n (READ_ONLY)" % new_db)
        r = con.execute("""
            SELECT count(*), median(a.turnover), median(b.turnover)
            FROM o.raw_basic_daily a JOIN n.raw_basic_daily b
              USING (symbol, date)
            WHERE a.date >= (SELECT max(date) FROM n.raw_basic_daily)
                              - INTERVAL 250 DAY
              AND a.turnover > 0 AND b.turnover > 0
              AND substr(a.symbol, 3, 2) IN ('60', '00', '30', '68')
        """).fetchone()
        if not r or not r[0] or not r[2]:
            return None
        return (r[1] / r[2], '最近 250 天 %s 行，turnover 中位 旧 %.6g / 新 %.6g'
                % (format(r[0], ','), r[1], r[2]))
    except Exception:                                       # noqa: BLE001
        return None
    finally:
        con.close()


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
                 ' init），而且没有迁移脚本。')
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
        _say('  %d 个条目，解到 %s' % (n, VIPDOC))
        shutil.rmtree(VIPDOC, ignore_errors=True)
        os.makedirs(VIPDOC, exist_ok=True)
        got = _extract_zip(zp, VIPDOC)
        # 🔴 自证：解出来必须是【目录树】而不是一堆带反斜杠的平坦文件。
        #   不验的话，下一步 init 报的是"我的中间文件不存在"，
        #   而那个报错指不到路径分隔符这件事上（已踩，见模块 docstring）。
        days = sum(len([f for f in fs if f.endswith('.day')])
                   for _, _, fs in os.walk(VIPDOC))
        subs = [d for d in os.listdir(VIPDOC)
                if os.path.isdir(os.path.join(VIPDOC, d))]
        _say('  落盘 %d 个文件，其中 .day %d 个，子目录 %s'
             % (got, days, sorted(subs) or '（没有！）'))
        if not days or not subs:
            raise SystemExit(
                '🔴 解压后没有目录树（.day %d 个 / 子目录 %d 个）——'
                ' tdx2db 会扫不到任何文件。'
                '\n   zip 里的条目名可能又换写法了，查一眼：'
                '\n   python3 -c "import zipfile;'
                'print(zipfile.ZipFile(%r).namelist()[:3])"' % (
                    days, len(subs), zp))
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
    # 🔴 init 只导日线！复权因子与基础面要靠紧跟的 cron（它自己去下 gbbq）——
    #   不跑这一步的话 raw_adjust_factor / raw_basic_daily 是**空表**，
    #   而 init 自己会报"导入成功"。实测踩过，见模块 docstring。
    _say('\n补 gbbq / 复权因子 / 基础行情（cron）')
    _run([BIN, 'cron', '--dburi', 'duckdb://./%s' % os.path.basename(new)],
         cwd=TDX)
    after = _db_stats(new)
    # 自证：这两张表不许是空的（空表下游算不出后复权，而回测吃的是它）
    for t in ('raw_adjust_factor', 'raw_basic_daily'):
        if not (after.get(t) or {}).get('rows'):
            raise SystemExit(
                '🔴 %s 是空表 —— cron 那一步没生效（gbbq 没下下来？）。'
                '\n   新库留在 %s，别拿它替换。' % (t, _rel(new)))
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
        # ---- 口径护栏（行数不缩也要查值）----
        sc = _check_scale(DB, new)
        if sc:
            ratio, note = sc
            _say('\n口径核对：%s' % note)
            if not 0.2 < ratio < 5:
                _say('   🔴 turnover 量级变了 **%.0f 倍** —— 通达信改过'
                     ' .day 里 volume 的单位（实测：老 vipdoc 是'
                     '"股×100"、现在下的是"股"）。' % ratio)
                _say('   面板的 turnover 会从百分数变小数（或反过来），'
                     '换手榜与 sgmspeg 的 turnover_volatility 全都'
                     '**静默错 100 倍**。')
                shrink.append(('raw_basic_daily', 'turnover 量级', 1, ratio))
            else:
                _say('   ✓ turnover 量级一致（比值 %.3f）' % ratio)

        if shrink and not allow_shrink:
            _say('\n🔴 拒绝替换 —— 有 %d 项缩了。' % len(shrink))
            _say('   原因已实测定案（2026-09-03）：`hsjday.zip` **不含已摘牌'
                 '代码**。少掉的绝大部分是债券/基金（本项目用不到），')
            _say('   但 A 股也少 39 只 —— 全是 1997~2021 年摘牌的，'
                 '2026-06 后还在交易的 0 只。')
            _say('   不影响"今天能买什么"，但**影响回测的历史宇宙**'
                 '（幸存者偏差），而它不报错。')
            _say('   -> 已有库的机器**不要**拿它替换；'
                 '`--bootstrap` 是给从零装机用的。')
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
# 早上重算调仓信号的那个 timer。★ 与 sync 分开是因为它们回答不同的问题：
#   sync   18:10  把当天的行情/复权抓全（数据）
#   tick   次日 07:00/08:00/09:00  出当天的调仓清单（决策）
#   —— A 股公告集中在 16:00~22:00，其中 ST/停牌是次日生效的，
#      18:10 那份算出来的清单可能与早上不一样（见 assay/tick_daily.py）。
TICK_LABEL = 'com.miraclegu.finacial.tick'
TICK_PY = os.path.join(os.path.dirname(ROOT), 'assay', 'tick_daily.py')
# 定时窗口的**默认值**。真正生效的是 _manifest/schedule.json（看板可改）——
# 见 load_schedule()。默认值只在配置文件不存在/读不了时用。
DEFAULT_SCHED = {
    'sync': {'from': '16:00', 'to': '20:00', 'every': 10},
    'tick': {'from': '07:00', 'to': '09:20', 'every': 60},
}
SCHED_FILE = os.path.join(ROOT, '_manifest', 'schedule.json')
# 合法区间。🔴 `every` 有下限是因为每个点位都是一次 launchd 唤醒 + 一次
#   判据查询：写 1 分钟就是 240 个点位，而 plist 里点位越多、
#   "改一个点位要改哪儿"就越不明显。上限则防手滑（写 0 会除零/死循环）。
SCHED_LIMITS = {'every_min': 5, 'every_max': 240, 'max_slots': 100}
# 数据同步改成**轮询**：16:00 起每 10 分钟问一次「齐没齐」，齐了就早退。
#   判据在 build/is_stale.py（那里写了为什么不写死时间）。
# ★ 这个序列恰好**包含 18:10**（16:00 + 10×13），所以原来那个独立的
#   18:10 timer 是冗余的 —— 删掉它，少一处要对齐的时间常量。
# 🔴 而且第一个点位（16:00）必然判成"该跑"（那时今天的 PIT 快照与行情都
#   还没抓），所以**完整链每个交易日至少跑一次** —— `daily_snapshot.py`
#   漏一天永久丢失，这条保证不能靠"数据恰好齐了"。

SH = os.path.join(ROOT, 'sync_daily.sh')


def _launchd_path(label=LABEL):
    return os.path.expanduser('~/Library/LaunchAgents/%s.plist' % label)


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


def _plist(label, args, times, tag):
    """从**当前仓库路径**生成 —— 仓库里那份的绝对路径是我这台机器的，
    换台机器直接 cp 过去就指错了（而 launchd 不会因此报错，只是不跑）。

    ★ `times` 是 [(时,分), ...]。多个时间点时 StartCalendarInterval 用
      **数组**（launchd 支持）—— 装三个 plist 会让"改一个点位"变成改三处。
    """
    log = os.path.join(ROOT, '_manifest', 'launchd-%s.out' % tag)
    err = os.path.join(ROOT, '_manifest', 'launchd-%s.err' % tag)
    if len(times) == 1:
        sched = ('  <dict><key>Hour</key><integer>%d</integer>\n'
                 '        <key>Minute</key><integer>%d</integer></dict>'
                 % times[0])
    else:
        sched = '  <array>\n' + '\n'.join(
            '    <dict><key>Hour</key><integer>%d</integer>'
            '<key>Minute</key><integer>%d</integer></dict>' % t
            for t in times) + '\n  </array>'
    prog = ''.join('<string>%s</string>' % x for x in args)
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<!-- 由 datalake/setup_tdx.py 的 install-timer 生成（路径取自当前仓库）。
     手改这个文件的话，下次 install-timer 会覆盖它。
     🔴 注释里不能出现两个连字符 —— XML 规范禁止，而 plutil -lint 会说 OK
     （Apple 的解析器宽容、launchd 也照跑），Python 的 expat 却直接拒绝。
     原来这里写的是带前缀的参数名，于是 plist 一直是非法 XML 而没人发现。 -->
<dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key>
  <array>%s</array>
  <key>WorkingDirectory</key><string>%s</string>
  <key>StartCalendarInterval</key>
%s
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
""" % (label, prog, ROOT, sched, log, err, _sched_path())


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



def _hhmm(t):
    """'16:00' -> 分钟数。格式不对就抛，别猜。"""
    try:
        hh, mm = (int(x) for x in str(t).split(':'))
    except Exception:
        raise SystemExit('🔴 时间格式应是 HH:MM，实得 %r' % t)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise SystemExit('🔴 时间超范围：%r' % t)
    return hh * 60 + mm


def check_schedule(sc):
    """校验一份配置 -> (ok, 说明)。**写入前必须过这一关。**

    🔴 不校验的后果不是"报错"，而是**装出一个不跑的 timer**：
      `every=0` 会让点位生成死循环、`from > to` 会生成空数组，
      而 launchd 对空的 StartCalendarInterval **不报错，只是永远不触发** ——
      表现是"配好了但数据再也不同步了"，几天后才发现。
    """
    for k in ('sync', 'tick'):
        d = (sc or {}).get(k)
        if not isinstance(d, dict):
            return False, '缺 %s 段' % k
        try:
            a, b = _hhmm(d.get('from')), _hhmm(d.get('to'))
        except SystemExit as e:
            return False, '%s：%s' % (k, e)
        ev = d.get('every')
        if not isinstance(ev, int) or isinstance(ev, bool):
            return False, '%s.every 要是整数（分钟），实得 %r' % (k, ev)
        lo, hi = SCHED_LIMITS['every_min'], SCHED_LIMITS['every_max']
        if not (lo <= ev <= hi):
            return False, '%s.every 要在 %d~%d 分钟之间，实得 %d' % (k, lo, hi, ev)
        if a > b:
            return False, '%s 的起点 %s 晚于终点 %s' % (k, d['from'], d['to'])
        n = (b - a) // ev + 1
        if n > SCHED_LIMITS['max_slots']:
            return False, ('%s 会生成 %d 个点位，超过上限 %d —— '
                           '把间隔调大或窗口缩短'
                           % (k, n, SCHED_LIMITS['max_slots']))
    return True, 'ok'


def load_schedule():
    """当前配置。读不了/不合法就退回默认值，并**说出来**。

    ★ 退回默认而不是报错退出：定时任务的装配不该因为一个配置文件手滑而
      整条停摆（那比"按默认跑"糟得多）。但必须打印原因 ——
      静默退回等于"我改了配置却没生效"。
    """
    if not os.path.isfile(SCHED_FILE):
        return dict(DEFAULT_SCHED), None
    try:
        sc = json.load(io.open(SCHED_FILE, encoding='utf-8'))
    except Exception as e:                                      # noqa: BLE001
        return dict(DEFAULT_SCHED), '读不了 %s（%s）—— 用默认值' % (
            os.path.basename(SCHED_FILE), str(e)[:60])
    ok, why = check_schedule(sc)
    if not ok:
        return dict(DEFAULT_SCHED), '配置不合法（%s）—— 用默认值' % why
    return sc, None


def save_schedule(sc):
    """写配置。先校验、原子写 —— 半截文件会让下次装 timer 退回默认值。"""
    ok, why = check_schedule(sc)
    if not ok:
        raise SystemExit('🔴 配置不合法：%s' % why)
    os.makedirs(os.path.dirname(SCHED_FILE), exist_ok=True)
    tmp = SCHED_FILE + '.tmp'
    io.open(tmp, 'w', encoding='utf-8').write(
        json.dumps(sc, ensure_ascii=False, indent=1))
    os.replace(tmp, SCHED_FILE)
    return sc


def _parse_win(spec):
    """'16:00-20:00/10' -> {'from':'16:00','to':'20:00','every':10}。

    ★ 也接受单个时间 '18:10'（当成 from==to，一个点位）——
      老命令 `--at 18:10` 因此仍然可用（"参数不认"是最糟的失败方式）。
    """
    spec = str(spec).strip()
    every = None
    if '/' in spec:
        spec, e = spec.rsplit('/', 1)
        every = int(e)
    if '-' in spec:
        a, b = spec.split('-', 1)
    else:
        a = b = spec
    out = {'from': a.strip(), 'to': b.strip()}
    if every is not None:
        out['every'] = every
    elif a.strip() == b.strip():
        out['every'] = SCHED_LIMITS['every_min']   # 单点位，间隔无意义
    return out


def _range_times(start, end, step_min):
    """'16:00'~'20:00' 每 10 分钟 -> 25 个点位。

    ★ 用 StartCalendarInterval 的**数组**而不是 StartInterval（每 N 秒）：
      后者全天每 10 分钟唤醒一次（144 次），日志里全是"不在窗口内"。
    """
    t, last = _hhmm(start), _hhmm(end)
    out = []
    while t <= last:
        out.append((t // 60, t % 60))
        t += step_min
    # ★ 间隔除不尽时**补上终点**：07:00~09:20 每 60 分钟本来只到 09:00，
    #   那 09:00~09:20 之间导入的数据要等到明天才会被算进去 ——
    #   而"窗口写到 09:20"的意思就是"管到 09:20"。
    if out and out[-1] != (last // 60, last % 60):
        out.append((last // 60, last % 60))
    return out


def _times(spec):
    """'07:00,08:00' -> [(7,0),(8,0)]。"""
    out = []
    for t in str(spec).split(','):
        t = t.strip()
        if not t:
            continue
        hh, mm = (int(x) for x in t.split(':'))
        out.append((hh, mm))
    if not out:
        raise SystemExit('🔴 时间点解析不出来：%r' % spec)
    return out


def show_schedule():
    """当前配置 vs **实际装上的点位** —— 两者必须一致。

    🔴 只回显配置是不够的：配置改了而 timer 没重装时，
      "页面上写着每小时一次、实际还是旧的"**不报错**。
      所以判据是**已装的 plist 里到底有几个点位**
      （同 CLAUDE.md：已安装的 plist 与仓库正本不一致要报出来）。
    """
    sc, warn = load_schedule()
    out = {'schedule': sc, 'file': SCHED_FILE,
           'exists': os.path.isfile(SCHED_FILE), 'warn': warn,
           'limits': SCHED_LIMITS, 'installed': {}}
    for key, label in (('sync', LABEL), ('tick', TICK_LABEL)):
        d = sc[key]
        want = _range_times(d['from'], d['to'], d['every'])
        got, loaded, err = None, None, None
        p = _launchd_path(label)
        if platform.system() == 'Darwin' and os.path.isfile(p):
            try:
                import plistlib
                pl = plistlib.load(io.open(p, 'rb'))
                cal = pl.get('StartCalendarInterval')
                cal = [cal] if isinstance(cal, dict) else (cal or [])
                got = [(int(x.get('Hour', 0)), int(x.get('Minute', 0)))
                       for x in cal]
            except Exception as e:                              # noqa: BLE001
                err = str(e)[:120]
            try:
                lst = subprocess.run(['launchctl', 'list'],
                                     capture_output=True, text=True).stdout
                loaded = label in lst
            except Exception:                                   # noqa: BLE001
                pass
        out['installed'][key] = {
            'label': label, 'want_slots': len(want),
            'got_slots': (len(got) if got is not None else None),
            'loaded': loaded, 'err': err,
            'match': (got == want) if got is not None else None,
            'first': ('%02d:%02d' % want[0]) if want else None,
            'last': ('%02d:%02d' % want[-1]) if want else None,
        }
    return out


def install_timer(at=None, tick_at=None):
    """装两个定时：数据同步（sync）与信号重算（tick）。

    ★ 一起装是因为它们配套 —— 只装 sync 的话早上不会重算，
      而"漏装了"的表现是**信号永远是昨晚 18:10 那份**，不报错。
    """
    osname = platform.system()
    if not os.path.isfile(SH):
        raise SystemExit('🔴 找不到 %s' % SH)
    if not os.path.isfile(TICK_PY):
        raise SystemExit('🔴 找不到 %s' % TICK_PY)
    ok = _verify_sched_env()
    # ★ 窗口从 _manifest/schedule.json 读（看板可改）；命令行的
    #   --at / --tick-at 只是**临时覆盖**，不写回配置文件 ——
    #   否则"命令行跑了一次"就悄悄改了长期配置。
    sc, warn = load_schedule()
    if warn:
        _say('⚠️ %s' % warn)
    if at:
        sc = dict(sc, sync=dict(sc['sync'], **_parse_win(at)))
    if tick_at:
        sc = dict(sc, tick=dict(sc['tick'], **_parse_win(tick_at)))
    ok, why = check_schedule(sc)
    if not ok:
        raise SystemExit('🔴 配置不合法：%s' % why)
    JOBS = [
        # 🔴 带 --if-stale：判据是"数据齐没齐"，不是"到点没到点"。
        #   齐了就秒退（连日志文件都不建），所以密集轮询的成本很低。
        (LABEL, ['/bin/bash', SH, '--if-stale'],
         _range_times(sc['sync']['from'], sc['sync']['to'],
                      sc['sync']['every']), 'sync',
         '数据同步（%s~%s 每 %d 分钟）'
         % (sc['sync']['from'], sc['sync']['to'], sc['sync']['every'])),
        # ★ tick 也是轮询：它的判据是"数据指纹变了吗"（tick_daily.py），
        #   没变就跳过 —— 所以每小时问一次几乎没成本。
        (TICK_LABEL, [sys.executable, TICK_PY],
         _range_times(sc['tick']['from'], sc['tick']['to'],
                      sc['tick']['every']), 'tick',
         '信号重算（%s~%s 每 %d 分钟）'
         % (sc['tick']['from'], sc['tick']['to'], sc['tick']['every'])),
    ]
    if osname == 'Darwin':
        for label, args, times, tag, what in JOBS:
            p = _launchd_path(label)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            io.open(p, 'w', encoding='utf-8').write(
                _plist(label, args, times, tag))
            # ★ 先 unload 再 load：已加载时 load 会报错退出，而"报错了"
            #   与"装没装上"是两件事 —— 判据永远是 launchctl list。
            subprocess.run(['launchctl', 'unload', '-w', p],
                           capture_output=True)
            _run(['launchctl', 'load', '-w', p], check=False)
            shutil.copyfile(p, os.path.join(ROOT, '_manifest',
                                            label + '.plist'))
            shown = (' / '.join('%02d:%02d' % t for t in times)
                     if len(times) <= 6 else
                     '%02d:%02d ~ %02d:%02d 共 %d 个点位'
                     % (times[0][0], times[0][1], times[-1][0],
                        times[-1][1], len(times)))
            _say('✅ %s：每日 %s' % (what, shown))
        # 🔴 判据是 launchctl 里到底有没有，不是上面几条命令的返回码
        out = subprocess.run(['launchctl', 'list'], capture_output=True,
                             text=True).stdout
        for label, _a, _t, _g, what in JOBS:
            _say('   %s %s' % ('✓' if label in out else '🔴 没装上',
                               label))
        if not ok:
            _say('   ⚠️ 环境自证没过，见上')
        _say('   正本也更新到 _manifest/')
    elif osname == 'Linux':
        d = os.path.expanduser('~/.config/systemd/user')
        os.makedirs(d, exist_ok=True)
        for label, args, times, tag, what in JOBS:
            unit = 'finacial-%s' % tag
            io.open(os.path.join(d, unit + '.service'), 'w',
                    encoding='utf-8').write(
                '[Unit]\nDescription=finacial %s\n\n[Service]\n'
                'Type=oneshot\nWorkingDirectory=%s\n'
                # 🔴 同 launchd 那条：systemd 的环境也很窄，python3 会解析到
                #   系统那个（没装 duckdb），2/6 PIT 快照就炸，漏一天不可逆
                'Environment=PATH=%s\n'
                'ExecStart=%s\n' % (what, ROOT, _sched_path(),
                                     ' '.join(args)))
            io.open(os.path.join(d, unit + '.timer'), 'w',
                    encoding='utf-8').write(
                '[Unit]\nDescription=%s\n\n[Timer]\n%s'
                # ★ 机器在预定时刻睡着 -> 醒来补跑（launchd 天生如此，
                #   systemd 要显式 Persistent=true，cron 则做不到）
                'Persistent=true\n\n[Install]\nWantedBy=timers.target\n'
                % (what, ''.join('OnCalendar=*-*-* %02d:%02d:00\n' % t
                                 for t in times)))
            _run(['systemctl', '--user', 'daemon-reload'], check=False)
            _run(['systemctl', '--user', 'enable', '--now', unit + '.timer'],
                 check=False)
            _say('✅ %s：%s（Persistent=true，睡过会补跑）'
                 % (what, ' / '.join('%02d:%02d' % t for t in times)))
        _say('   ⚠️ 要在没登录时也跑：sudo loginctl enable-linger $USER')
    elif osname == 'Windows':
        for label, args, times, tag, what in JOBS:
            for hh, mm in times:
                _run(['schtasks', '/create', '/tn',
                      'finacial-%s-%02d%02d' % (tag, hh, mm), '/tr',
                      ' '.join('"%s"' % x for x in args), '/sc', 'daily',
                      '/st', '%02d:%02d' % (hh, mm), '/f'], check=False)
            _say('✅ %s：%s' % (what, ' / '.join(
                '%02d:%02d' % t for t in times)))
        _say('   ⚠️ sync_daily.sh 是 bash 脚本 —— Windows 上要有 '
             'Git Bash / WSL，且 tdx2db 要用 Windows 版')
    else:
        raise SystemExit('🔴 %s 上没做定时安装' % osname)
    return 0


def uninstall_timer():
    osname = platform.system()
    if osname == 'Darwin':
        for label in (LABEL, TICK_LABEL):
            p = _launchd_path(label)
            if os.path.isfile(p):
                _run(['launchctl', 'unload', '-w', p], check=False)
                os.remove(p)
    elif osname == 'Linux':
        for tag in ('sync', 'tick'):
            _run(['systemctl', '--user', 'disable', '--now',
                  'finacial-%s.timer' % tag], check=False)
    elif osname == 'Windows':
        _run(['schtasks', '/delete', '/tn', 'finacial-sync', '/f'],
             check=False)
        _run(['schtasks', '/query', '/tn', 'finacial-tick-0700'],
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
    ap.add_argument('--install-timer', action='store_true',
                    help='挂每日定时（数据同步 + 信号重算，两个一起）')
    ap.add_argument('--tick-at', default=None,
                    help='临时覆盖信号重算窗口，如 07:00-09:20/60')
    ap.add_argument('--schedule', action='store_true',
                    help='打印当前窗口配置 + 实际装上的点位')
    ap.add_argument('--set-schedule', metavar='JSON',
                    help='写窗口配置（JSON），之后要 --install-timer 才生效')
    ap.add_argument('--at', default=None, help='定时时刻，默认 18:10')
    ap.add_argument('--uninstall-timer', action='store_true')
    a = ap.parse_args()
    if a.install:
        return install(force=a.force)
    if a.bootstrap:
        return bootstrap(allow_shrink=a.allow_shrink, keep_zip=a.keep_zip,
                         reuse_vipdoc=a.reuse_vipdoc)
    if a.sync:
        return sync(minute=a.min)
    if a.schedule:
        print(json.dumps(show_schedule(), ensure_ascii=False, indent=1,
                         default=str))
        return 0
    if a.set_schedule:
        save_schedule(json.loads(a.set_schedule))
        _say('✅ 已写 %s —— 还要重装才生效：--install-timer' % SCHED_FILE)
        return 0
    if a.install_timer:
        return install_timer(at=a.at, tick_at=a.tick_at)
    if a.uninstall_timer:
        return uninstall_timer()
    return check()


if __name__ == '__main__':
    sys.exit(main() or 0)
