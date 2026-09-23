# -*- coding: utf-8 -*-
"""因子值面板 —— 按年分区落 `mart/factor_daily/factor_<年>.parquet`。

    python3 datalake/build/build_factor_daily.py            # 建（指纹没变就秒退）
    python3 datalake/build/build_factor_daily.py --force    # 强制重建
    python3 datalake/build/build_factor_daily.py --check    # 自证：换块大小重算，逐位比

## 🔴🔴 按【股票】分块，不按年 —— 这一条试错过一轮，代价是重建一遍

第一版按自然年分段、每段往前多读 3 年当预热。`--check`（重算一年与盘上逐位比）
当场把它打红，两个根因**都无法靠加预热解决**：

    ema120   757,753 / 1,233,202 行不等，max 相对差 4.8e-3（0.5%，不是噪声）
             -> EMA 递推**永远记着起点**，480 根预热只把种子权重压到 3.7e-4
    ma5/ma10 2014 年 6 / 11 个不等，恰好是 n+1 的形状
             -> **长期停牌股**复牌时窗口跨过几年缺口，预热给多少都不够
    skew20   ~1e-7 相对差 -> pandas 的 rolling 是**在线算法**，
             结果本身就依赖序列起点

也就是说「同一年的值取决于它是在哪一段里被算的」—— **不可复现**，
而它不报错（落盘、行数、非空率全都正常）。

★ 而本模块 98 个因子**全是逐股算的**（没有一个用到当日横截面），所以
  按股票分块、每块取该股**全历史**，块怎么切结果都一模一样 ——
  预热这个概念从根上就没有了。
🔴 所以加横截面因子（比如「收益率排名 / 总数」那条）时**不能直接塞进来**：
  它会被分块切碎且不报错。启动时有断言钉住这件事。

## 每天重建一遍，不做增量

全量 ~190 秒。做"只算最后一天"的增量要为递推类因子存中间状态，
而那是**第二份实现**（同「两处实现必然分叉」）—— 省三分钟不值得拿
逐位可复现去换。

## `_meta.json` 记两个指纹，少一个都会让"过期"变得看不见

    panel  面板被修正过（本项目修过 volume 7.5 万行 / 复权因子 / ETF 价格刻度）
    specs  **公式改了** —— 盘上的值是按旧公式算的，而它长得和新的一模一样

第二个尤其重要：改一行 `calc` 不会让任何东西报错，只会让 mart/ 里那 7 GB
悄悄变成过期数据。
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import sys
import time

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

HERE = os.path.dirname(os.path.abspath(__file__))
DL = os.path.dirname(HERE)
REPO = os.path.dirname(DL)
OUT = os.path.join(DL, 'mart', 'factor_daily')
META = os.path.join(OUT, '_meta.json')
PANEL = os.path.join(DL, 'mart', 'panel_daily', 'panel_*.parquet')

#: 一块多少只票。纯粹是内存旋钮 —— **改它不许改变任何一个值**，
#: 那正是 `--check` 要证的事。
CHUNK = 700


def _cols():
    """要读哪几列 —— 取各 Spec 的 `deps` 并集，不写死。

    ★ 写死的话加一个用到新列的因子会静默读不到（更糟的是拿到全 NaN）。
    """
    need = {'jq_code', 'date'}
    for s in all_specs():
        need |= set(s.deps)
    return sorted(need)


def _spec_sig():
    h = hashlib.sha256()
    for s in sorted(all_specs(), key=lambda z: z.id):
        h.update(('%s|%s|%d|%s\n' % (s.id, s.formula, s.warm, ','.join(s.deps)))
                 .encode('utf-8'))
    return h.hexdigest()[:12]


def _panel_fp():
    """面板指纹。★ 口径抄 `assay/feed.PanelFeed.fingerprint`
    （rel|size|mtime_ns 的哈希，不读内容）—— assay 不能被 datalake import
    （依赖必须单向），所以只能抄口径不能复用。"""
    sig = []
    for f in sorted(glob.glob(PANEL)):
        st = os.stat(f)
        sig.append('%s|%d|%d' % (os.path.relpath(f, DL), st.st_size, st.st_mtime_ns))
    return hashlib.sha256('\n'.join(sig).encode()).hexdigest()[:12]


def _codes(con):
    return [r[0] for r in con.execute(
        "SELECT DISTINCT jq_code FROM read_parquet('%s') ORDER BY 1" % PANEL
    ).fetchall()]


def _chunk_frame(con, codes):
    """一块股票的【全历史】+ 98 个因子 -> DataFrame。"""
    q = ', '.join("'%s'" % c for c in codes)
    df = con.execute("SELECT %s FROM read_parquet('%s') WHERE jq_code IN (%s)"
                     % (', '.join(_cols()), PANEL, q)).df()
    if df.empty:
        return None
    x = Ctx(df)
    out = x.df[['jq_code', 'date']].copy()
    for s in all_specs():
        out[s.id] = np.asarray(s.calc(x), dtype='float32')
    return out


def build(con, chunk=CHUNK, out_dir=OUT, quiet=False):
    """逐块算、按年追加写。返回 {年: {rows, bytes}}。"""
    codes = _codes(con)
    tmp_dir = out_dir + '.tmp'
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)
    writers, rows = {}, {}
    t0 = time.time()
    try:
        for i in range(0, len(codes), chunk):
            part = codes[i:i + chunk]
            sub = _chunk_frame(con, part)
            if sub is None:
                continue
            for y, g in sub.groupby(sub['date'].dt.year):
                y = int(y)
                t = pa.Table.from_pandas(g.reset_index(drop=True), preserve_index=False)
                if y not in writers:
                    writers[y] = pq.ParquetWriter(
                        os.path.join(tmp_dir, 'factor_%d.parquet' % y), t.schema)
                writers[y].write_table(t)
                rows[y] = rows.get(y, 0) + len(g)
            if not quiet:
                print('  第 %2d 块  %4d 只  %8d 行  累计 %5.0fs'
                      % (i // chunk + 1, len(part), len(sub), time.time() - t0))
    finally:
        for w in writers.values():
            w.close()
    # 🔴 整目录原子替换 —— 逐个文件替换的话中途失败会留下【半新半旧】的
    #   一批年份，而那不报错，只是有些年是新公式、有些是旧的。
    old = out_dir + '.old'
    shutil.rmtree(old, ignore_errors=True)
    if os.path.isdir(out_dir):
        os.rename(out_dir, old)
    os.rename(tmp_dir, out_dir)
    shutil.rmtree(old, ignore_errors=True)
    return {str(y): {'rows': n,
                     'bytes': os.path.getsize(
                         os.path.join(out_dir, 'factor_%d.parquet' % y))}
            for y, n in sorted(rows.items())}


def _save_meta(rows):
    m = {'built_at': time.strftime('%Y-%m-%d %H:%M:%S'),
         'panel_fp': _panel_fp(), 'spec_sig': _spec_sig(),
         'n_factors': len(all_specs()), 'chunk': CHUNK, 'years': rows}
    tmp = META + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(m, f, ensure_ascii=False, indent=1)
    os.replace(tmp, META)
    return m


def _load_meta():
    try:
        with open(META) as f:
            return json.load(f)
    except Exception:                                       # noqa: BLE001
        return {}


def check(con, year=None):
    """🔴 自证：**换一个块大小**重算，与盘上逐位比。

    要证的是「分块不改变任何一个值」。CHUNK 只是内存旋钮，它一旦影响数值，
    就说明某个因子偷偷依赖了块内的其它票（横截面），
    而那**不报错** —— 只是每块各自算出一套数。
    """
    ys = sorted(int(os.path.basename(p)[7:11])
                for p in glob.glob(os.path.join(OUT, 'factor_*.parquet')))
    if not ys:
        print('🔴 盘上没有因子面板')
        return 1
    year = year or ys[-1]
    on = pd.read_parquet(os.path.join(OUT, 'factor_%d.parquet' % year))
    alt = CHUNK // 3 or 1
    codes = _codes(con)
    parts = []
    for i in range(0, len(codes), alt):
        sub = _chunk_frame(con, codes[i:i + alt])
        if sub is not None:
            parts.append(sub[sub['date'].dt.year == year])
    re_ = pd.concat(parts, ignore_index=True)
    key = ['jq_code', 'date']
    on = on.sort_values(key).reset_index(drop=True)
    re_ = re_.sort_values(key).reset_index(drop=True)
    if len(on) != len(re_):
        print('🔴 行数不同: 盘上 %d / 重算 %d' % (len(on), len(re_)))
        return 1
    bad = []
    for c in on.columns:
        if c in key:
            if not on[c].equals(re_[c]):
                bad.append((c, 'key 不同'))
            continue
        a, b = on[c].to_numpy(), re_[c].to_numpy()
        if not ((a == b) | (np.isnan(a) & np.isnan(b))).all():
            bad.append((c, int((~((a == b) | (np.isnan(a) & np.isnan(b)))).sum())))
    print('%d 年 %d 行 × %d 列（块 %d -> %d）：%s'
          % (year, len(on), len(on.columns) - 2, CHUNK, alt,
             '✓ 逐位一致 —— 分块不改变任何值'
             if not bad else '🔴 %s' % bad[:6]))
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true', help='指纹没变也重建')
    ap.add_argument('--check', nargs='?', const=-1, type=int, metavar='YEAR',
                    help='自证：换块大小重算并逐位比')
    a = ap.parse_args()

    # 🔴 分块的前提：没有横截面因子。加了而不改这里的话，那个因子会被
    #   按块切碎（每块只看得到块内的票），**而它不报错**。
    cs = [s.id for s in all_specs() if getattr(s, 'cross_sectional', False)]
    assert not cs, ('这些是横截面因子，不能按股票分块算: %s —— '
                    '要么单独一条路径，要么别放进这个面板' % cs)

    con = duckdb.connect(':memory:')
    if a.check is not None:
        return check(con, None if a.check == -1 else a.check)

    m = _load_meta()
    if (not a.force and m.get('panel_fp') == _panel_fp()
            and m.get('spec_sig') == _spec_sig()):
        print('面板与公式都没变（panel %s / specs %s）—— 无事可做'
              % (m['panel_fp'], m['spec_sig']))
        return 0
    if m and m.get('spec_sig') != _spec_sig():
        print('⚠ 公式指纹变了（%s -> %s）：盘上的值是按旧公式算的'
              % (m.get('spec_sig'), _spec_sig()))

    t0 = time.time()
    print('按股票分块重建：%d 个因子 / 每块 %d 只' % (len(all_specs()), CHUNK))
    rows = build(con)
    mm = _save_meta(rows)
    print('\n%d 年 / %d 行 / %.2f GB   panel=%s specs=%s   %.0fs'
          % (len(rows), sum(v['rows'] for v in rows.values()),
             sum(v['bytes'] for v in rows.values()) / 1e9,
             mm['panel_fp'], mm['spec_sig'], time.time() - t0))
    return 0


sys.path.insert(0, HERE)
from factors import Ctx, all_specs                          # noqa: E402,E731

if __name__ == '__main__':
    raise SystemExit(main())
