#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核对一个聚宽增量包【到底进没进库】。

用法：
    python3 datalake/build/verify_jq_import.py            # 核对最新那个包
    python3 datalake/build/verify_jq_import.py <tar>

## 🔴 判据是「包里有、而库里没有」，不是「库里的日期看着旧」

2026-09-28 实测的三种情况，长得很像而处置完全不同 —— 这个脚本存在的
全部理由就是把它们分开：

  ① **本来就没有新数据**：`fundamentals_indicator_q` 合并后与库里
     **逐行相同**（269946 = 269946）。中报 08-31 披完、三季报 10 月
     才开始，这段是空窗期。此时库里停在 08-31 是**对的**。
  ② **合并了，但 loader 没跑**：`merge_jq_increment.py` 不加 `--and-load`
     只把 CSV 合进 `downloads/`，parquet 要 loader 才生成。实测当天
     `downloads/income_2026.csv.gz` 是 22:30 的，而
     `raw/jq/financials/income.parquet` 还是 08-26 的。
  ③ **根本没合进来**：包在手里但 merge 没跑或失败。

## 🔴 不许拿两边的【行数】比

`std/*` 是 loader 的**产物**，它会筛行、会改口径 —— 与原始 CSV 的行数
天然不可比。实测拿 155171 与 155170 比，得出"差 1 行"，而真相是
**9 行被更新**（`board_plan_pub_date` 08-25 -> 09-05、进度「董事会预案」
-> 「实施方案」）**加 1 行新增**。行数相等也可能内容全变了。

所以这里比的是**日期水位**（包声明的 `max_date` vs 两层各自的 max），
拿不到日期列的表退回逐键比对。
"""
import argparse
import glob
import io
import json
import os
import sys
import tarfile

import duckdb

DL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWNLOADS = os.path.join(DL, 'raw', 'jq', '_ingest', 'downloads')

# 包里的表 -> sync_status.CHECKS 里的 key（"库里在读的那一层"）。
# ★ 路径不在这里写第二遍 —— 从 sync_status 取，免得两份分叉。
TO_CHECK = {
    'fundamentals_indicator_q': 'fin_indicator',
    'income_2026': 'fin_income', 'income_2025': 'fin_income',
    'indicator_2026': 'fin_indicator_y', 'indicator_2025': 'fin_indicator_y',
    'balance_2026': 'fin_balance', 'balance_2025': 'fin_balance',
    'cashflow_2026': 'fin_cashflow', 'cashflow_2025': 'fin_cashflow',
    'stk_xr_xd': 'dividend',
    'stk_fin_forcast': 'fin_forcast',
    'share_change': 'share_change',
}
# 有意不监控的（连同理由）—— 空着不写的话，下面那条完整性断言会把它们
# 当成"漏了"而响亮报出来，那正是我们要的：包里多一张表必须有人过问。
NOT_WATCHED = {
    'dim_name_history': '维度表，只喂 l0_dim_*，没有单独的新鲜度判据',
    'dim_status_change': '同上',
}


# 三大报表：loader 有【口径过滤】（只要定期报告 + 标准 A 股代码）。
# 所以"库里 < 包里"有两种，处置完全不同 —— 必须分开：
#   · 差的那些**本就不该进库** -> ✅（2026-09-28 实测的 19 行就是这种）
#   · 差的那些**够格却没进去** -> ⚠ 真的没到位
# ★ indicator 走的是**同一个** loader（load_jq_financials）与同一套口径，
#   漏了它就会一直报「loader 没跑」而其实是被口径筛掉的（实测）。
FIN_STEMS = ('income', 'balance', 'cashflow', 'indicator')


def _fin_gap(stem, store_max):
    """三大报表：库里之后的那批行，有几行【够格进库】（`stem` 带年份）。

    🔴 判据调 loader 的 `loadable_mask()`，不在这里写第二份 ——
      口径改了而核对器没跟着改的话，它会开始说反话，而且看着一直是绿的。
    返回 (够格却没进去的行数, 说明文字)；读不了则 (None, 原因)。
    """
    import pandas as pd
    sys.path.insert(0, os.path.join(DL, 'build'))
    import load_jq_financials as lf
    p = _mid_path(stem)
    if not p or not os.path.exists(p):
        return None, '找不到 %s 的合并目标' % stem
    try:
        df = pd.read_csv(p, compression='gzip', low_memory=False,
                         dtype={c: str for c in lf.ID_COLS})
    except Exception as e:                                  # noqa: BLE001
        return None, '读不了 %s：%s' % (p, str(e)[:60])
    df = df[pd.to_datetime(df['pub_date'], errors='coerce')
            > pd.Timestamp(str(store_max)[:10])]
    if df.empty:
        return 0, '库里之后没有新行'
    ok, n_src, n_code = lf.loadable_mask(df)
    n_ok = int(ok.sum())
    return n_ok, ('库里之后共 %d 行：非定期报告 %d、非标准代码 %d、够格进库 %d'
                  % (len(df), n_src, n_code, n_ok))


def _store_max(key):
    """库里那一层的 max(日期) —— SQL 从 sync_status.CHECKS 取，不复述。"""
    sys.path.insert(0, os.path.join(DL, 'build'))
    import sync_status as st
    sql = next((s for k, _n, _l, s in st.CHECKS if k == key), None)
    if sql is None:
        return None, 'sync_status.CHECKS 里没有 %r' % key
    try:
        v = duckdb.connect(':memory:').execute(sql.format(R=DL)).fetchone()[0]
        return v, None
    except Exception as e:                                  # noqa: BLE001
        return None, str(e)[:80]


def _mid_path(stem):
    """合并那一层的目标文件 —— **从 merge 的 TARGETS 取，不猜文件名**。

    🔴 第一版按 `<表名>.csv.gz` 猜，于是 `share_change` 找不到
      （merge 把它落成 `stk_capital_change.csv.gz`），报成"🔴 没合进来"
      —— 一个指错方向的报错比不报更糟。
    ★ 三大报表按年分片（`income_2026.csv.gz`），TARGETS 里没有它们
      （包文件直接替换同名文件），所以那一路才走 downloads 下的同名文件。
    """
    sys.path.insert(0, os.path.join(DL, 'build'))
    import merge_jq_increment as mg
    t = mg.TARGETS.get(stem)
    if t:
        return os.path.join(DL, t[0])
    for ext in ('.csv.gz', '.csv', '.parquet'):
        p = os.path.join(DOWNLOADS, stem + ext)
        if os.path.exists(p):
            return p
    return None


def _read_max(path, col):
    rd = 'read_parquet' if path.endswith('.parquet') else 'read_csv_auto'
    try:
        return duckdb.connect(':memory:').execute(
            "SELECT max(%s) FROM %s('%s')" % (col, rd, path)).fetchone()[0], None
    except Exception as e:                                  # noqa: BLE001
        return None, str(e)[:70]


def _read_rows(path):
    rd = 'read_parquet' if path.endswith('.parquet') else 'read_csv_auto'
    try:
        return duckdb.connect(':memory:').execute(
            "SELECT count(*) FROM %s('%s')" % (rd, path)).fetchone()[0]
    except Exception:                                       # noqa: BLE001
        return None


def _mid_max(stem, date_col):
    """合并那一层的 max(日期)。"""
    p = _mid_path(stem)
    if not p or not os.path.exists(p):
        return None, '找不到合并目标'
    v, err = _read_max(p, date_col)
    return (None, '%s（读不了：%s）' % (p, err)) if err else (v, p)


def _d(x):
    return str(x)[:10] if x is not None else '—'


def main(argv=None):
    ap = argparse.ArgumentParser(description='核对聚宽增量包进没进库')
    ap.add_argument('tar', nargs='?', help='不给就取 downloads 下最新的那个')
    a = ap.parse_args(argv)
    tar = a.tar
    if not tar:
        cands = sorted(glob.glob(os.path.join(DOWNLOADS, '*jq_increment*.tar')))
        if not cands:
            print('🔴 downloads 下没有 jq_increment 包')
            return 2
        tar = cands[-1]
    with tarfile.open(tar) as tf:
        man = json.load(io.TextIOWrapper(tf.extractfile('_manifest.json'), 'utf-8'))

    print('=' * 78)
    print(' 核对 %s' % os.path.basename(tar))
    print(' 抽取于 %s   SINCE=%s   季度 %s'
          % (man.get('extracted_at'), man.get('since'), man.get('quarters')))
    print('=' * 78)

    # 🔴 完整性：包里每张表都必须被"监控"或"显式豁免"。照清单拼会漏掉
    #   新加的表，而漏了不报错、只是保护范围悄悄缩小。
    unknown = [t for t in man['tables']
               if t not in TO_CHECK and t not in NOT_WATCHED]
    rc = 0
    rows = []
    for stem, meta in sorted(man['tables'].items()):
        if stem in NOT_WATCHED:
            rows.append((stem, '—', '—', '—', '不监控（%s）' % NOT_WATCHED[stem]))
            continue
        col = meta.get('date_col')
        pkg = meta.get('max_date')
        key = TO_CHECK[stem]
        sm, err = _store_max(key)
        if not col or not pkg:
            # 🔴 没有日期列（`fundamentals_indicator_q`）：退回**逐层比行数**。
            #   这一张的 loader 是直筒 COPY（raw -> std），所以行数可比；
            #   其它表的 std 是筛过的产物，行数天然不可比（见文件头）。
            mp = _mid_path(stem)
            mr = _read_rows(mp) if mp else None
            sr = _read_rows(os.path.join(DL, 'std', 'fin_indicator_q.parquet'))
            if mr is None or sr is None:
                verdict, mark = '读不到行数，判不了（不猜）', 1
            elif sr >= mr:
                verdict = ('✅ 已到位（合并后 %d 行 = 库里 %d 行 —— 这一批'
                           '**没有新行**，空窗期库里停着是对的）' % (mr, sr))
                mark = 0
            else:
                verdict = ('⚠ 合并后 %d 行 > 库里 %d 行，**loader 没跑**'
                           % (mr, sr))
                mark = 1
            rc = max(rc, mark)
            rows.append((stem, '无日期列', str(mr), str(sr), verdict))
            continue
        mid, where = _mid_max(stem, col)
        if err:
            verdict, mark = '库里读不到（%s）' % err, 1
        elif sm is not None and str(sm)[:10] >= str(pkg)[:10]:
            verdict, mark = '✅ 已到位', 0
        elif mid is None or str(mid)[:10] < str(pkg)[:10]:
            verdict, mark = '🔴 没合进来（merge 没跑或失败）', 1
        elif stem.split('_')[0] in FIN_STEMS:
            # 🔴 三大报表：差的那些可能**按口径本就不该进库**。
            #   把这一档单列出来，否则"筛掉了"与"丢了"在屏幕上一模一样
            #   （2026-09-28 我自己就先判成了"loader 没跑"）。
            n_ok, why = _fin_gap(stem, sm)   # ★ 带年份：合并目标是 income_2026.csv.gz
            if n_ok is None:
                verdict, mark = '判不了：%s（不猜）' % why, 1
            elif n_ok == 0:
                verdict, mark = '✅ 已到位（%s）' % why, 0
            else:
                verdict, mark = ('⚠ 有 %d 行**够格却没进库** —— %s' % (n_ok, why)), 1
        else:
            verdict, mark = ('⚠ 合并到位了、库里没有 —— loader 没跑，'
                             '或被该表 loader 的口径筛掉了（去看它的剔除计数）'), 1
        rc = max(rc, mark)
        rows.append((stem, _d(pkg), _d(mid), _d(sm), verdict))

    w = max(len(r[0]) for r in rows)
    print(' %-*s  %-10s %-10s %-10s  %s'
          % (w, '表', '包里', '合并后', '库里', '结论'))
    for r in rows:
        print(' %-*s  %-10s %-10s %-10s  %s' % (w, r[0], r[1], r[2], r[3], r[4]))
    print()
    if unknown:
        print('🔴 包里这些表【既没监控也没豁免】：%s' % '、'.join(unknown))
        print('   —— 加一张表就必须有人过问它进没进库，否则保护范围会悄悄缩小。')
        rc = 1
    if rc == 0:
        print('✅ 包里的内容都已经到库里那一层了。')
    else:
        print('还没到位的，按 merge_jq_increment.py 打印的顺序补跑 loader'
              '（或重跑 merge 时加 --and-load）。')
    return rc


if __name__ == '__main__':
    sys.exit(main())
