#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""把整个工作区迁到另一台机器。

    python3 datalake/migrate.py                    # 迁移方案：拷什么、排除什么、怎么验
    python3 datalake/migrate.py --fingerprint      # 迁移【前】在老机器上跑，生成指纹
    python3 datalake/migrate.py --verify FP.json   # 迁移【后】在新机器上跑，逐项比对

## 🔴 结论先说：主方案是【拷数据】，不是【从零重建】

`setup_tdx.py --bootstrap` 能从公开的 `hsjday.zip` 重建 tdx 链，但那**得不到
等价的库**（三条都是实测定案的，见 setup_tdx.py 与数据字典索引 4）：

| 差异 | 后果 |
|---|---|
| `hsjday.zip` **不含已摘牌代码** | A 股宇宙 5,925 → 5,886（−39 只），`raw_kline_daily` −670 万行。少的全是 1997~2021 摘牌的 —— 不影响"今天能买什么"，但**影响回测的历史宇宙（幸存者偏差）** |
| 新包的 `volume` 是**真实股数**（老库是股数×100） | `turnover` 从百分数变小数，全程差 100 倍。**行数一行不少，是值变了** —— 只查行数的护栏拦不住 |
| tdx2db 升级要 schema v6，v5 库不兼容 | 整库重建，且没有迁移脚本 |

而且**聚宽那份也不能"重跑一次"**：`extract_jq_increment.py` 的 `SINCE` 是按
**当前本地状态**算的（最落后那张表往前留 5 天重叠），在新机器上跑会得到
不同的窗口；`raw/jq/_ingest/downloads/` 里那些 `.csv.gz` 是**原始凭据**。

排除掉三类不该带的之后总量只有 **约 6 GB**，一次 rsync 的事 ——
用重建去省这几个 G，换来的是"数据不等价而且不报错"的风险。不值。

## 三类不该带的（本机实测省下 9 GB）

| 排除 | 大小 | 为什么 |
|---|---|---|
| `datalake/.tmp/` | 6.1 G | DuckDB 溢写临时文件。正常退出会自删，残留说明当时进程被 kill。下次构建自己会建 |
| `datalake/raw/hf/` | 1.5 G | 同花顺概念，**零引用**：`last_fetched` 停在 2026-04-28、没接进 `sync_daily.sh`，selftest 里有断言钉住"代码里没引用它" |
| `.git/`（两个仓库） | 1.8 G + 18 M | 用 `git clone` 拿代码，不要拷 `.git` 目录。datalake 的 `.git` 里有 7,376 个松散对象、**其中最大两个 blob（558M/524M）不在任何提交里**（曾 `git add` 过大文件没提交）—— `clone` 不会传它们 |

## 分四类，判据是「能不能重建」

★ 清单只在本文件的 `PARTS` 里定义一处，`--plan` / `--fingerprint` /
  `--verify` 都读它 —— 分三处写迟早分叉，而"漏拷了一项"的表现是
  **新机器上某个功能静默用空数据**。
"""
import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))            # datalake/
REPO = os.path.dirname(ROOT)                                 # finacial/
ASSAY = os.path.join(REPO, 'assay')

# (相对 REPO 的路径, 类别, 一句话为什么)
#   must     不可重建 —— 上游拿不回来了
#   derived  可重建，但**主方案照样拷**（逐字节一致，零风险；重建有顺序风险）
#   skip     不该带
PARTS = [
    ('datalake/raw/tdx/_ingest/tdx.db', 'must',
     '🔴 含 39 只已摘牌 A 股 + 670 万行历史，hsjday.zip 补不回来'),
    ('datalake/raw/tdx/snapshots', 'must',
     '🔴 PIT 快照：tdx 的名称/分类/板块成分是 type-1 覆盖写，漏一天永久丢失'),
    ('datalake/raw/tdx/_snapshots', 'must', '同上（早期快照）'),
    ('datalake/raw/jq/_ingest/downloads', 'must',
     '🔴 聚宽导出的原始 csv.gz —— 重跑会得到不同窗口（SINCE 按当前状态算）'),
    ('datalake/docs/evidence', 'must',
     '对数用的真值快照。不是管道的一部分，是证据（已入 git，clone 就有）'),
    ('assay/live', 'must',
     '🔴 实盘账本 append-only —— 决策证据，丢了没法复盘（已入 git）'),
    ('assay/runs', 'must',
     '回测归档：账户绑的版本"回测过没有"靠它。gitignore，只能拷'),
    ('datalake/std', 'derived', '各 load_*.py 从 raw 重建'),
    ('datalake/mart', 'derived', 'build_panel_daily.py 从 std 重建（全量约 5 分钟）'),
    ('datalake/raw/tdx/kline', 'derived', 'load_tdx_kline.py 从 tdx.db 重建'),
    ('datalake/raw/tdx/basic', 'derived', '同上'),
    ('datalake/raw/tdx/adjust_factor.parquet', 'derived', '同上'),
    ('datalake/raw/jq/financials', 'derived', 'load_jq_financials.py 从 downloads 重建'),
    ('datalake/lake.db', 'derived', 'rebuild_lake_db.py（只是一堆视图）'),
    ('datalake/rt', 'derived',
     '盘中 1 分钟线。★ 漏了不要紧 —— trends2 每次给全天，快照下一分钟就补上'),
    ('datalake/raw/amazing', 'derived',
     'AmazingData 的 wheel 与手册。要用再从网盘下（macOS 跑不了，见 README）'),
    ('datalake/.tmp', 'skip', 'DuckDB 溢写临时文件，下次构建自己会建'),
    ('datalake/raw/hf', 'skip', '同花顺概念，零引用（selftest 有断言钉住）'),
    ('datalake/.git', 'skip', '用 git clone 拿代码；里面还有 1 G 悬空对象'),
    ('assay/.git', 'skip', '同上'),
]
EXCLUDES = ['.tmp/', 'raw/hf/', '.git/', '__pycache__/', '*.pyc',
            '.DS_Store', 'raw/amazing/_ingest/venv/']

# 数值指纹：证明【内容等价】而不是【字节相同】。
#   🔴 parquet 的字节含压缩参数与元数据 —— 内容相同也可能字节不同
#     （实测踩过：同一份 equity 前后两次归档，0 行数值差、字节 hash 不同）。
#     所以「重建」路线**只能**用数值指纹验；「拷贝」路线两种都能用。
DATA_FP = [
    ('panel', "read_parquet('%s/mart/panel_daily/*.parquet')" % ROOT,
     "md5(string_agg(CAST(date AS VARCHAR) || jq_code || "
     "COALESCE(CAST(round(close_hfq,4) AS VARCHAR),'') || "
     "COALESCE(CAST(round(volume_shares,2) AS VARCHAR),'') || "
     "COALESCE(CAST(round(turnover,6) AS VARCHAR),''), '|' "
     "ORDER BY date, jq_code))"),
]


def _say(*a, **kw):
    """默认走 stdout。

    🔴 `--fingerprint` 的 stdout 是**纯 JSON**（给 `> fp.json` 用），
      所以那条路径上的提示一律 `err=True` 走 stderr ——
      混进 stdout 会把 JSON 污染成解析不了，而调用方
      （`--verify`）读到的是 JSONDecodeError，指不到真正的原因。
    """
    print(*a, flush=True, file=sys.stderr if kw.get('err') else sys.stdout)


def _du(p):
    """目录/文件大小（字节）。不存在返回 None —— 与"空"区分开。"""
    if not os.path.exists(p):
        return None
    if os.path.isfile(p):
        return os.path.getsize(p)
    n = 0
    for r, _d, fs in os.walk(p):
        for f in fs:
            try:
                n += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return n


def _h(n):
    if n is None:
        return '（不存在）'
    for u in ('B', 'K', 'M', 'G'):
        if n < 1024 or u == 'G':
            return '%.1f%s' % (n, u)
        n /= 1024.0


def _md5(path, chunk=1 << 22):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def plan():
    _say('=' * 72)
    _say('迁移方案（本机 %s / %s）' % (platform.system(), platform.machine()))
    _say('=' * 72)
    tot = {'must': 0, 'derived': 0, 'skip': 0}
    for kind, title in (('must', '① 必须拷 —— 不可重建，上游拿不回来'),
                        ('derived', '② 派生数据 —— 能重建，但主方案照样拷'),
                        ('skip', '③ 不要拷')):
        _say('\n%s' % title)
        for rel, k, why in PARTS:
            if k != kind:
                continue
            n = _du(os.path.join(REPO, rel))
            tot[k] += n or 0
            _say('   %-42s %8s  %s' % (rel, _h(n), why))
        _say('   %-42s %8s' % ('小计', _h(tot[kind])))
    _say('\n要传的总量：约 %s（① + ②）；不排除的话是 %s'
         % (_h(tot['must'] + tot['derived']),
            _h(tot['must'] + tot['derived'] + tot['skip'])))

    _say('\n' + '-' * 72)
    _say('步骤')
    _say('-' * 72)
    _say('【老机器】')
    _say('  1) 生成指纹（迁完要靠它比对，别跳过）：')
    _say('       python3 datalake/migrate.py --fingerprint > /tmp/fp.json')
    _say('  2) 把两个仓库推上远端（代码走 git，不要拷 .git 目录）：')
    _say('       cd assay && git push  &&  cd ../datalake && git push')
    _say('  3) 拷数据。macOS/Linux：')
    _say('       rsync -a --info=progress2 \\')
    for e in EXCLUDES:
        _say('         --exclude=%r \\' % e)
    _say('         %s/ /Volumes/移动盘/finacial/' % REPO)
    _say('     Windows：robocopy 加 /XD .tmp .git __pycache__ /XF *.pyc')
    _say('\n【新机器】')
    _say('  4) 装依赖（本项目只要这两个；playwright 仅 selftest 的 web 用例要）：')
    _say('       pip install duckdb pandas pyarrow')
    _say('       pip install playwright && playwright install chromium')
    _say('  5) clone 代码，再把数据放回同样的相对位置：')
    _say('       git clone <assay>  &&  git clone <datalake>')
    _say('       rsync -a /Volumes/移动盘/finacial/ ~/finacial/')
    _say('       ln -sf assay/CLAUDE.md ~/finacial/CLAUDE.md   # 符号链接不在任何仓库里')
    _say('  6) 🔴 tdx2db 的二进制**不要拷**（平台相关）—— 按本机 OS 重装：')
    _say('       python3 datalake/setup_tdx.py --install')
    _say('     ★ 装的必须还是 **v2026.5**：新版要 schema v6，而库是 v5。')
    _say('       护栏会拦（一次性小库探 schema），别加 --force。')
    _say('  7) 比对指纹：')
    _say('       python3 datalake/migrate.py --verify /tmp/fp.json')
    _say('  8) 跑验证：')
    _say('       python3 datalake/build/build_panel_daily.py --verify')
    _say('       python3 assay/selftest.py --all')
    _say('  9) 重装定时（plist 里有绝对路径，必须在新机器上重新生成）：')
    _say('       python3 datalake/setup_tdx.py --install-timer')
    _say('       # 实盘/看板：python3 assay/serve.py')
    _say('\n🔴 别忘了：`--readonly` 与不带参数**两种模式各起一次** ——')
    _say('   --readonly 不走起实盘/行情线程那段，只验它会漏掉整条路径。')
    return 0


def _busy():
    """有没有别的东西正在改数据。

    🔴 **指纹是时点相关的**。实测：生成指纹时每日同步（launchd 的
      com.miraclegu.finacial.sync）正在跑，几分钟内 panel 就从
      16,253,495 涨到 16,258,702 行、tdx.db 也涨了 1 万行 ——
      于是"老机器的指纹"和"实际拷走的数据"是**两个时点**的，
      迁移后比对必然不一致，而你会以为是拷坏了。
      所以拷贝与指纹必须在同一个静止时点。
    """
    # ★ 模式带扩展名（`sync_daily\.sh` 而不是 `sync_daily`）——
    #   `pgrep -f` 匹配**整条命令行**，光写词根会命中任何提到它的进程
    #   （编辑器打开那个文件、或者一条 shell 命令里含这个字面量）。
    # ★ 而且**把命中的命令行打出来**：精确模式也总有边缘情况，
    #   显示出来人一眼能判断是不是误报 —— 比悄悄放宽或悄悄拦住都好。
    # ★ 排除自己与父进程，否则 migrate.py 自己就可能命中。
    mine = {os.getpid(), os.getppid()}
    out = []
    for pat in (r'sync_daily\.sh', r'tdx2db cron', r'build_panel_daily\.py',
                r'daily_snapshot\.py', r'merge_jq_increment\.py',
                r'load_tdx_kline\.py'):
        try:
            r = subprocess.run(['pgrep', '-f', pat], capture_output=True,
                               text=True)
            for pid in (r.stdout or '').split():
                if int(pid) in mine:
                    continue
                cmd = subprocess.run(['ps', '-o', 'command=', '-p', pid],
                                     capture_output=True, text=True).stdout
                cmd = ' '.join(cmd.split())[:96]
                if cmd and (pid, cmd) not in out:
                    out.append((pid, cmd))
        except Exception:                                       # noqa: BLE001
            pass
    return out


def fingerprint():
    """两层指纹：字节层证明【拷贝无损】，数值层证明【内容等价】。"""
    busy = _busy()
    if busy:
        _say('🔴 有东西正在改数据：', err=True)
        for pid, cmd in busy:
            _say('     PID %-7s %s' % (pid, cmd), err=True)
        _say('   指纹是【时点相关】的 —— 等它跑完再来，'
             '否则指纹与实际拷走的数据是两个时点。', err=True)
        _say('   （每日同步实测约 100 秒；等它结束：'
             'until ! pgrep -f "tdx2db cron"; do sleep 3; done）', err=True)
        return 2
    out = {'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
           'host': platform.node(), 'files': {}, 'data': {}, 'counts': {}}
    for rel, k, _w in PARTS:
        if k == 'skip':
            continue
        p = os.path.join(REPO, rel)
        if not os.path.exists(p):
            out['files'][rel] = None
            continue
        if os.path.isfile(p):
            out['files'][rel] = {'size': os.path.getsize(p), 'md5': _md5(p)}
        else:
            n = f = 0
            for r, _d, fs in os.walk(p):
                for x in fs:
                    if x.endswith(('.pyc',)) or x == '.DS_Store':
                        continue
                    f += 1
                    n += os.path.getsize(os.path.join(r, x))
            out['files'][rel] = {'files': f, 'size': n}
    # tdx.db 的四张表：行数 + 区间（比文件 md5 更有意义 —— 它证明内容）
    try:
        import duckdb
        c = duckdb.connect(os.path.join(ROOT, 'raw/tdx/_ingest/tdx.db'),
                           read_only=True)
        for t in ('raw_kline_daily', 'raw_adjust_factor', 'raw_basic_daily',
                  'raw_symbol_class', 'raw_gbbq'):
            try:
                r = c.execute('SELECT count(*), count(DISTINCT symbol) FROM %s'
                              % t).fetchone()
                out['counts'][t] = list(r)
            except Exception as e:                              # noqa: BLE001
                out['counts'][t] = 'ERR %s' % e
        out['counts']['_meta'] = c.execute('SELECT * FROM _meta').fetchall()
        c.close()
    except Exception as e:                                      # noqa: BLE001
        out['counts']['_error'] = str(e)
        # 🔴 指纹**不完整就必须响亮失败**。踩过：`tdx2db cron` 在跑（每日
        #   18:10 的 launchd 同步）时 tdx.db 被锁，这里只记了个 _error，
        #   而各表显示成 None —— 迁移后比对时**两边都是 None 就"通过"了**，
        #   等于 tdx.db 那 1.3 G 根本没验。
        _say('🔴 tdx.db 读不到，指纹【不完整】：%s' % e, err=True)
        if 'lock' in str(e).lower():
            _say('   看着是被别的进程锁着。查一眼是谁：', err=True)
            _say('     ps -o pid=,command= -p <报错里那个 PID>', err=True)
            _say('   如果是每日同步（sync_daily.sh / launchd 的'
                 ' com.miraclegu.finacial.sync，实测约 100 秒），'
                 '等它跑完再来。', err=True)
        out['incomplete'] = True
    # 数值指纹
    try:
        import duckdb
        c = duckdb.connect()
        for name, src, expr in DATA_FP:
            try:
                out['data'][name] = c.execute(
                    'SELECT count(*), %s FROM %s' % (expr, src)).fetchone()
                out['data'][name] = list(out['data'][name])
            except Exception as e:                              # noqa: BLE001
                out['data'][name] = 'ERR %s' % e
                out['incomplete'] = True
        c.close()
    except Exception as e:                                      # noqa: BLE001
        out['data']['_error'] = str(e)
        out['incomplete'] = True
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    if out.get('incomplete'):
        _say('\n❌ 指纹不完整 —— **不要**拿它去比对（会静默"通过"）。'
             '修好上面的问题再重跑。', err=True)
        return 1
    return 0


def verify(fp):
    old = json.load(open(fp, encoding='utf-8'))
    if old.get('incomplete'):
        _say('🔴 这份指纹是**不完整**的（生成时有项目失败，见里面的 _error）。')
        _say('   拿它比对会静默"通过" —— 回老机器重新生成。')
        return 1
    _say('比对老机器 %s（%s）的指纹\n' % (old.get('host'), old.get('at')))
    import io as _io
    buf = _io.StringIO()
    _stdout = sys.stdout
    sys.stdout = buf
    fingerprint()
    sys.stdout = _stdout
    new = json.loads(buf.getvalue())

    bad = 0
    _say('① 文件/目录')
    for rel in sorted(old['files']):
        a, b = old['files'][rel], new['files'].get(rel)
        if a == b:
            _say('   ✓ %-42s %s' % (rel, _h((a or {}).get('size'))))
        else:
            bad += 1
            _say('   🔴 %-42s\n        老 %s\n        新 %s' % (rel, a, b))
    _say('\n② tdx.db 的表（行数 + 代码数）')
    for t in sorted(old['counts']):
        a, b = old['counts'][t], new['counts'].get(t)
        _say('   %s %-22s %s' % ('✓' if a == b else '🔴', t, a))
        if a != b:
            bad += 1
            _say('        新机器：%s' % (b,))
    _say('\n③ 数值指纹（证明【内容】等价 —— parquet 字节不同不代表内容不同）')
    for k in sorted(old['data']):
        a, b = old['data'][k], new['data'].get(k)
        _say('   %s %-10s %s' % ('✓' if a == b else '🔴', k, a))
        if a != b:
            bad += 1
            _say('        新机器：%s' % (b,))
    _say('\n%s' % ('✅ 全部一致' if not bad
                   else '❌ %d 项不一致 —— 逐项查上面标 🔴 的' % bad))
    if not bad:
        _say('\n还要跑（指纹只证明数据，不证明代码跑得起来）：')
        _say('   python3 datalake/build/build_panel_daily.py --verify')
        _say('   python3 assay/selftest.py --all')
        _say('   python3 assay/serve.py           # 全功能，看 5 行启动横幅')
        _say('   python3 assay/serve.py --readonly')
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser(description='工作区迁移到另一台机器')
    ap.add_argument('--fingerprint', action='store_true',
                    help='生成指纹（迁移前在老机器上跑）')
    ap.add_argument('--verify', metavar='FP.json',
                    help='与老机器的指纹比对（迁移后在新机器上跑）')
    a = ap.parse_args()
    if a.fingerprint:
        return fingerprint()
    if a.verify:
        return verify(a.verify)
    return plan()


if __name__ == '__main__':
    sys.exit(main() or 0)
