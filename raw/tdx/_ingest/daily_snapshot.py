#!/usr/bin/env python3
"""P0: 每日 PIT 快照。

为什么必须每天跑: tdx2db 是 type-1 覆盖写 —— 名称、分类、板块成分每次更新都被
最新值覆盖，当天的"当时状态"一旦错过就**永久丢失**，事后无法从任何地方重建。
所以这个脚本的价值不对称: 成本是每天几十 KB，收益是从今天起拥有真 PIT 历史。

存储策略: **内容哈希去重**。
  名称/分类/板块极少变动，逐日全量快照 99% 是重复的。所以:
    · 内容与上一版相同 → 不写新文件，只在 manifest 里记一行指向同一版本
    · 内容变了         → 写新版本文件，并打印 diff
  于是 manifest 是完整的逐日 PIT 记录，而磁盘只存不同的版本 —— 等价于变更事件，
  但不需要自己做 diff 逻辑。

用法:
    python pitdb/snapshot/daily_snapshot.py              # 快照今天
    python pitdb/snapshot/daily_snapshot.py --date 2026-08-24
    python pitdb/snapshot/daily_snapshot.py --status     # 只看统计，不写

接进日常流程: 在 update_and_screener.sh 数据更新之后调用。
"""
import argparse
import datetime as dt
import hashlib
import os
import sys

import duckdb
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.path.join(ROOT, 'snapshots')
MANIFEST = os.path.join(SNAP, 'manifest.csv')
TDX = '/Users/guhao/finacial/tdx2db/tdx.db'

# 要快照的数据集: 名字 → SQL。全部是 tdx2db 会被覆盖写的东西。
DATASETS = {
    'symbol_name':  'SELECT * FROM raw_symbol_name ORDER BY symbol',
    'symbol_class': 'SELECT * FROM raw_symbol_class ORDER BY symbol',
    'blocks_info':  'SELECT * FROM raw_tdx_blocks_info ORDER BY block_symbol',
    'blocks_member': 'SELECT * FROM raw_tdx_blocks_member ORDER BY 1, 2',
}


def content_hash(df):
    """内容哈希。对列序与行序不敏感的部分已由 SQL 的 ORDER BY 固定。"""
    b = df.to_csv(index=False).encode('utf-8')
    return hashlib.sha256(b).hexdigest()[:16]


def load_manifest():
    if os.path.exists(MANIFEST):
        return pd.read_csv(MANIFEST, dtype=str)
    return pd.DataFrame(columns=['snap_date', 'dataset', 'rows', 'hash',
                                 'version_file', 'is_new_version'])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='快照日期, 默认今天')
    ap.add_argument('--status', action='store_true', help='只看统计')
    ap.add_argument('--db', default=TDX)
    args = ap.parse_args()

    if not os.path.exists(SNAP):
        os.makedirs(SNAP)
    man = load_manifest()

    if args.status:
        if man.empty:
            print('还没有任何快照')
            return 0
        print('快照日期数: %d  (%s ~ %s)' % (
            man['snap_date'].nunique(), man['snap_date'].min(), man['snap_date'].max()))
        for ds in sorted(man['dataset'].unique()):
            sub = man[man['dataset'] == ds]
            nv = (sub['is_new_version'] == 'True').sum()
            print('  %-16s 记录 %4d 天, 不同版本 %3d 个' % (ds, len(sub), nv))
        tot = sum(os.path.getsize(os.path.join(SNAP, f))
                  for f in os.listdir(SNAP) if f.endswith('.parquet'))
        print('磁盘占用: %.1f MB' % (tot / 1048576.0))
        return 0

    snap_date = args.date or dt.date.today().isoformat()
    if not os.path.exists(args.db):
        sys.exit('❌ 找不到数据库 %s' % args.db)

    # 幂等: 同一天重复跑直接退出，不重复记录
    if not man.empty and (man['snap_date'] == snap_date).any():
        print('✓ %s 已有快照，跳过（幂等）' % snap_date)
        return 0

    con = duckdb.connect(args.db, read_only=True)
    print('P0 每日快照  %s' % snap_date)
    print('=' * 64)
    new_rows = []
    for ds, sql in DATASETS.items():
        try:
            df = con.execute(sql).df()
        except Exception as e:                             # noqa: BLE001
            # 取数失败必须让整个脚本失败 —— 静默跳过等于当天数据永久丢失
            con.close()
            sys.exit('❌ %s 取数失败: %s: %s' % (ds, type(e).__name__, e))

        h = content_hash(df)
        prev = man[man['dataset'] == ds]
        last_hash = prev.iloc[-1]['hash'] if len(prev) else None
        vfile = '%s_%s.parquet' % (ds, h)
        is_new = (h != last_hash)

        if is_new:
            path = os.path.join(SNAP, vfile)
            if not os.path.exists(path):
                df.to_parquet(path, index=False, compression='zstd')
            size = os.path.getsize(path) / 1024.0
            if last_hash is None:
                print('  %-16s %7d 行  首个版本 %s  (%.0f KB)' % (ds, len(df), h, size))
            else:
                # 内容变了 → 打印变了什么，这才是有价值的信号
                old_file = prev.iloc[-1]['version_file']
                try:
                    old = pd.read_parquet(os.path.join(SNAP, old_file))
                    key = df.columns[0]
                    added = set(df[key]) - set(old[key])
                    removed = set(old[key]) - set(df[key])
                    print('  %-16s %7d 行  ★变更 %s→%s  新增 %d / 消失 %d  (%.0f KB)'
                          % (ds, len(df), last_hash, h, len(added), len(removed), size))
                    if added:
                        print('      新增样例: %s' % sorted(added)[:5])
                    if removed:
                        print('      消失样例: %s' % sorted(removed)[:5])
                except Exception as e:                     # noqa: BLE001
                    print('  %-16s %7d 行  ★变更 %s→%s  (diff 失败: %s)'
                          % (ds, len(df), last_hash, h, e))
        else:
            print('  %-16s %7d 行  未变 (指向 %s)' % (ds, len(df), h))

        new_rows.append({'snap_date': snap_date, 'dataset': ds, 'rows': len(df),
                         'hash': h, 'version_file': vfile, 'is_new_version': is_new})

    con.close()
    man = pd.concat([man, pd.DataFrame(new_rows)], ignore_index=True)
    man.to_csv(MANIFEST, index=False)

    n_new = sum(1 for r in new_rows if r['is_new_version'])
    tot = sum(os.path.getsize(os.path.join(SNAP, f))
              for f in os.listdir(SNAP) if f.endswith('.parquet'))
    print('=' * 64)
    print('完成: %d 个数据集, 其中 %d 个是新版本; 累计磁盘 %.1f MB, manifest %d 行'
          % (len(new_rows), n_new, tot / 1048576.0, len(man)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
