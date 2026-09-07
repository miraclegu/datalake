#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""⓿ 总调度：把九个 `pull_0X_*.py` 串起来跑，带任务清单与总进度。

    python pull_all.py                      # 全量，断点续跑
    python pull_all.py --dry-run            # 只列任务清单和分片数，不登录
    python pull_all.py --groups 3,4,5       # 只跑第 ③④⑤ 组
    python pull_all.py --only kline_day     # 只跑某几张表（跨组按表名找）
    python pull_all.py --retry-failed       # 只重跑失败的分片
    python pull_all.py --sleep 0.2          # 怕限流就整体放慢

## 为什么是 import 而不是起九个子进程

**登录一次就够了**（`ad.login` 建的是长连接的网关会话），九个进程各登一次
既慢又可能被服务端按并发拒掉。所以这里 import 九个模块的 `specs()`，
在**同一个进程、同一次登录**下跑完。
★ 九个脚本仍然能各自单独跑（`python pull_03_financial.py`）——
它们的 `specs(args, cfg)` 签名一致，`amazing_common.run_with_args` 两边共用。

## 任务清单是先算出来的，不是边跑边说

开跑前先把**所有**表的分片数、已完成数列成一张表打出来（`--dry-run` 只做
这一步）。这样"总共要多久""断在哪"一眼能看到 —— 边跑边打的话，
中间断了根本不知道还剩多少。

## 🔴 落盘布局

    <out>/<表名>/part-<分片id>.parquet     数据（一个分片一个文件）
    <out>/<表名>/_字段说明.md              这张表的逐字段说明（每次跑都重写）
                                       🔴 下划线开头是必须的：pyarrow 读整个目录时
                                       会跳过 `_`/`.` 开头的文件，不跳的话
                                       `pd.read_parquet('<表名>/')` 直接炸
    <out>/字段说明总览.md                  34 张表的索引（本脚本写）
    _ingest/state/<表名>.json              manifest：哪片好了、哪片失败了

一个目录 = 一张表，duckdb / pandas 直接读 `<表名>/*.parquet`。
🔴 **不要把不同表的 part 混进一个目录** —— 它们 schema 不同，
整目录读会炸，而单个文件读得起来，所以问题只在合并时才暴露。
"""
import argparse
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import amazing_common as ac
import field_docs

import pull_01_basic
import pull_02_kline_day
import pull_03_financial
import pull_04_holder
import pull_05_equity
import pull_06_margin
import pull_07_etf
import pull_08_index
import pull_09_industry

# 顺序是刻意的：① 基础数据先跑 —— 代码全集、交易日历都是别的表的输入，
# 而 ⑨ 行业的后三张表要等 industry_base_info 出来才知道取哪些指数。
GROUPS = [
    ('1', '① 基础数据', pull_01_basic),
    ('2', '② 历史日K', pull_02_kline_day),
    ('3', '③ 财务数据', pull_03_financial),
    ('4', '④ 股东股本', pull_04_holder),
    ('5', '⑤ 股东权益', pull_05_equity),
    ('6', '⑥ 融资融券', pull_06_margin),
    ('7', '⑦ ETF数据', pull_07_etf),
    ('8', '⑧ 交易所指数', pull_08_index),
    ('9', '⑨ 行业指数', pull_09_industry),
]


def task_list(plan):
    """任务清单：一行一张表，带分片数 / 已完成 / 待取。"""
    ac.say('')
    ac.say('任务清单：')
    ac.say('  %s %s %s %8s %8s %8s %14s'
           % (ac.pad('组', 4), ac.pad('分组', 14), ac.pad('表', 22),
              '分片', '已完成', '待取', '已有行数'))
    tot_s = tot_d = tot_r = 0
    for gid, gname, spec in plan:
        st = ac.load_state(spec['name'])
        n = len(spec['shards'])
        done = sum(1 for sid, _ in spec['shards'] if sid in st['shards'])
        rows = sum(v.get('rows', 0) for v in st['shards'].values())
        bad = len(st.get('failed', {}))
        tot_s += n
        tot_d += done
        tot_r += rows
        ac.say('  %s %s %s %8d %8d %8d %14s%s'
               % (ac.pad(gid, 4), ac.pad(gname, 14), ac.pad(spec['name'], 22),
                  n, done, n - done, '{:,}'.format(rows),
                  ('  失败 %d 片' % bad) if bad else ''))
    ac.say('  %s %s %s %8d %8d %8d %14s'
           % (ac.pad('', 4), ac.pad('', 14), ac.pad('合计 %d 张表' % len(plan), 22),
              tot_s, tot_d, tot_s - tot_d, '{:,}'.format(tot_r)))
    return tot_s, tot_d


def overview_doc(out_root, plan):
    """写 `<out>/字段说明总览.md` —— 34 张表的索引。

    ★ 每张表自己的 `字段说明.md` 和数据放在一起（用的时候人是先进那个目录的），
    这份总览只回答"一共有哪些表、各是什么、去哪看" —— 两份内容不重复。
    """
    p = os.path.join(ac.ensure_dir(out_root), '字段说明总览.md')
    L = ['# AmazingData 落地表一览\n',
         '> 由 `_ingest/pull_all.py` 生成。每张表的**逐字段说明**在'
         ' `<表名>/_字段说明.md`；字段说明的正本是 `_ingest/field_docs.py`'
         '（由手册 PDF + wheel 的 pyc 生成，见 `_ingest/tools/parse_manual_fields.py`）。\n',
         '手册正本：`finacial/AmazingData开发手册.pdf`（V1.0.24 / 2025-12-16，148 页）。',
         '整体判断（为什么它是"第四个源"而不是替代品）见 `raw/amazing/README.md`。\n']
    L.append('| 组 | 表 | 字段数 | 分片 | 已取行数 | 接口 |')
    L.append('|---|---|---:|---:|---:|---|')
    for gid, gname, spec in plan:
        tbl = spec['name']
        st = ac.load_state(tbl)
        rows = sum(v.get('rows', 0) for v in st['shards'].values())
        nf = len([k for k in ac.merged_fields(tbl, spec.get('add_fields'))
                  if k != '__source__'])
        L.append('| %s | [`%s`](%s/_字段说明.md) | %d | %d | %s | `%s` |'
                 % (gname, tbl, tbl, nf, len(spec['shards']),
                    '{:,}'.format(rows), spec.get('api', '')))
    L.append('')
    L.append('## 口径提醒（每条都在对应表的 `字段说明.md` 里有详版）\n')
    L.append('- **行情起点 2013**（手册 §2.2）。更早**不报错、只返回空** —— '
             '那看着像"这只票还没上市"。tdx 那份 1990 起的不能扔。')
    L.append('- **财务三表同一 `(code, 报告期)` 有多行**，靠 `STATEMENT_TYPE` 区分'
             '（26+ 种）。不筛口径直接 join 会一行变多行，**而这不报错**。')
    L.append('- **PIT 用 `ACTUAL_ANN_DATE`**，不是 `ANN_DATE`。差一天就是未来函数。')
    L.append('- **`begin_date` 的含义每张表都不同**：报告期 / 交易日 / 公告日 / '
             '变动日 / 解禁日 / 到期日。拿同一个区间套所有表能跑通，'
             '**但取回来的集合不是你以为的那个**。')
    L.append('- **限售解禁的终点要伸到未来**（按解禁日过滤，而解禁日在未来）。')
    L.append('- **`margin_summary` 按交易所一天一行**（`EXCHANGE` 列手册没写）。'
             '当成全市场用会小一半。')
    L.append('- **`index_weight` 只有 5 个指数**；别的指数返回空而不报错。')
    L.append('- **成分股类表的 `is_local` 必须传 False**，且**每次全量重取**'
             '（剔除日会被最新数据改写，增量合并会留下永不剔除的僵尸成分）。')
    L.append('- **ETF 申赎清单没有历史**，只有当天；本脚本按 `ASOF_DATE` 逐日攒。')
    L.append('')
    with io.open(p, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(L))
    return p


def main():
    ap = argparse.ArgumentParser(description='AmazingData 全量采集总调度')
    ac.add_common_args(ap)
    ap.add_argument('--retry-failed', action='store_true', help='只重跑失败的分片')
    ap.add_argument('--groups', default=None,
                    help='只跑哪几组，如 1,3,9（默认全部）')
    ap.add_argument('--kinds', default=pull_02_kline_day.DEFAULT_KINDS,
                    help='② 历史日K 要哪些品种：%s'
                         % ','.join(pull_02_kline_day.KINDS))
    ap.add_argument('--list-only', action='store_true',
                    help='只打任务清单就退出（要登录，因为分片数取决于代码全集）')
    args = ap.parse_args()

    groups = GROUPS
    if args.groups:
        want = set(x.strip() for x in args.groups.split(',') if x.strip())
        bad = want - set(g[0] for g in GROUPS)
        if bad:
            # 🔴 组号拼错不能静默跳过 —— 那表现为"跑完了但什么都没取"
            raise SystemExit('--groups 里有不存在的组：%s，可选 %s'
                             % (sorted(bad), [g[0] for g in GROUPS]))
        groups = [g for g in GROUPS if g[0] in want]

    free = ac.disk_free_gb(args.out)
    ac.say('落点 %s（剩余 %.1f GB）' % (args.out, free))
    if 0 <= free < 30:
        # 全量几千万行 —— 空间不够要现在就知道，不是跑三小时之后在某片上崩掉
        ac.say('! 剩余空间不到 30 GB。全量日K + 历史证券状态就有几千万行，'
               '建议先腾空间或用 --groups 分批跑')

    cfg = None if args.dry_run else ac.load_cfg(args.account)

    # ---- 先把所有分组的 spec 都建出来（这一步会取代码全集，所以要登录）
    t0 = time.time()
    plan = []
    for gid, gname, mod in groups:
        try:
            specs = ac.pick_tables(mod.specs(args, cfg), args, strict=False)
        except SystemExit:
            raise
        except Exception as e:
            ac.say('!! %s 建任务失败：%s: %s' % (gname, type(e).__name__, e))
            continue
        for s in specs:
            plan.append((gid, gname, s))
    if args.only:
        # 跨组挑表：逐组不校验（别的组当然没有这张表），跑完拿**并集**校验一次
        got = set(s['name'] for _g, _n, s in plan)
        bad = [w.strip() for w in args.only.split(',')
               if w.strip() and w.strip() not in got]
        if bad:
            raise SystemExit('--only 里有不存在的表：%s\n可选：%s'
                             % (bad, ', '.join(sorted(got))))
    if not plan:
        raise SystemExit('没有任何任务 —— 检查 --only / --skip / --groups')

    total_shards, done_shards = task_list(plan)
    if args.dry_run or args.list_only:
        ac.say('（只列清单，没有取数）')
        return 0

    # ---- 逐表跑，外面套一条总进度
    ac.say('')
    results = []
    gbar = ac.Bar(total_shards, prefix='总进度')
    gbar.update(done_shards, '已完成 %d 片' % done_shards)
    for i, (gid, gname, spec) in enumerate(plan, 1):
        ac.say('')
        ac.say('--- [%d/%d] %s / %s ---' % (i, len(plan), gname, spec['name']))
        try:
            if args.retry_failed:
                r = ac.retry_failed_only(spec, args)
            else:
                r = ac.run_table(spec, args, cfg)
        except KeyboardInterrupt:
            ac.say('用户中断 —— 已取到的分片都留着，直接再跑一次就从这里续')
            break
        except Exception:
            import traceback
            ac.say('!! %s 整表失败：\n%s' % (spec['name'], traceback.format_exc()))
            r = {'table': spec['name'], 'shards': len(spec['shards']), 'skipped': 0,
                 'rows': 0, 'failed': len(spec['shards']), 'sec': 0.0}
        if r:
            results.append(r)
            gbar.update(max(0, len(spec['shards']) - r.get('skipped', 0)),
                        '%s 完成' % spec['name'])
    gbar.close('用时 %s' % ac.hms(time.time() - t0))

    n_bad = ac.summarize(results, '全部任务')
    p = overview_doc(args.out, plan)
    ac.say('字段说明总览：%s' % p)
    ac.say('全程用时 %s' % ac.hms(time.time() - t0))
    if n_bad:
        ac.say('有失败分片 —— 重跑：python pull_all.py --retry-failed')
    return 1 if n_bad else 0


if __name__ == '__main__':
    sys.exit(main())
