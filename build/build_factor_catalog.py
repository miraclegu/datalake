# -*- coding: utf-8 -*-
"""因子目录表 —— 「有哪些因子、编号是什么、怎么算的、什么意思」一张表答完。

    python3 datalake/build/build_factor_catalog.py            # 建表
    python3 datalake/build/build_factor_catalog.py --print    # 只打印，不写盘

产出 `mart/factor_catalog.parquet`，用只读查询直接查：

    python3 datalake/build/query.py \\
      "SELECT factor_id, name_cn, formula FROM factor_catalog WHERE group_key='ma'"

## 🔴 目录表【从注册表生成】，不手工维护

手工维护一份的话它迟早与代码分叉，而**分叉的文档比没有文档更危险**
（同「新结论先写进代码注释，再回到索引加一行」那条的另一半）。所以这里
只做三件事：读注册表 -> 对回 `factors.xlsx` 的行号 -> 落盘。

## 🔴 `src_row`：能对回原清单的那把钥匙

`factor_id` 是**我们自己的稳定编号**（落盘列名、接口 key 都用它，发布后不改）。
但人手里那份 `factors.xlsx` 是按行看的，所以另给一列 `src_row` = 中文名在
原表里的行号。对不上就留空并计数 —— **不猜**。

⚠ **7 个中文名在原表里出现过不止一次**（`动量因子`×2 / `流动性因子`×3 …），
  光看名字分不出是哪一个。那几条 `src_row` 给**第一次出现**的行号并把
  `src_dup` 置真 —— 页面与查询要能看见这件事，否则"对上了"是假的。

## ⚠ 刻意【不带】IC / IR

`factors.xlsx` 里那两列的股票池、持有期、超额口径**全未知**，与本模块算出来的
不是同一个量。把它放进目录表等于摆一个看着权威的数在那里请人误用
（同「别取现成的那个股息率」「摆一个输入框在那里就是在邀请人去猜」）。
要对照就去看原文件，那时"口径不同"这件事还在眼前。
"""
import argparse
import io
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DL = os.path.dirname(HERE)                    # datalake 根
REPO = os.path.dirname(DL)                    # 工作区根（factors.xlsx 在这儿）
OUT = os.path.join(DL, 'mart', 'factor_catalog.parquet')

sys.path.insert(0, HERE)
from factors import all_specs, GROUPS, TIERS   # noqa: E402


def _src_rows():
    """`factors.xlsx` 的「因子名称」-> 行号（1-based，表头算第 1 行）。

    读不到就返回空 dict —— 那个文件不是构建依赖，缺了只是对不回原清单，
    **不该让建表失败**（同「外部挂了退回本地那份，不让整页打不开」）。
    """
    p = os.path.join(REPO, 'factors.xlsx')
    if not os.path.isfile(p):
        return {}, '没找到 %s' % p
    try:
        df = pd.read_excel(p)
    except Exception as e:                                  # noqa: BLE001
        return {}, '读不了 factors.xlsx: %s' % e
    col = df.columns[0]
    m = {}
    for i, v in enumerate(df[col].astype(str), start=2):    # 表头占第 1 行
        m.setdefault(v.strip(), i)
    return m, None


def build():
    specs = all_specs()
    src, err = _src_rows()
    rows = []
    for s in specs:
        r = s.row()
        r['src_row'] = src.get(s.name_cn)
        rows.append(r)
    df = pd.DataFrame(rows, columns=[
        'factor_id', 'name_cn', 'group_key', 'group_cn',
        'formula', 'desc', 'unit', 'tier', 'tier_cn',
        'deps', 'warm', 'warm_eff', 'note', 'src_dup', 'src_row'])
    # src_row 可能有缺 -> 可空整数，不要变成 float（1.0 那种显示很丑且会被
    # 误当成"这是个计算出来的数"）
    df['src_row'] = df['src_row'].astype('Int64')
    return df, err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--print', dest='pr', action='store_true', help='只打印不写盘')
    a = ap.parse_args()

    df, err = build()
    if err:
        print('⚠ %s —— src_row 留空' % err)

    n_miss = int(df['src_row'].isna().sum())
    n_dup = int(df['src_dup'].sum())
    print('因子 %d 个，%d 个族：' % (len(df), df['group_key'].nunique()))
    for g, sub in df.groupby('group_key', sort=False):
        print('  %-6s %-16s %2d 条   %s'
              % (g, GROUPS[g], len(sub), '/'.join(sub['factor_id'].head(4))))
    print('可信度：%s' % '  '.join(
        '%s=%d' % (k, int((df['tier'] == k).sum())) for k in TIERS))
    print('对回 factors.xlsx：命中 %d / 未命中 %d；重名待定 %d'
          % (len(df) - n_miss, n_miss, n_dup))

    if a.pr:
        with pd.option_context('display.max_colwidth', 46, 'display.width', 200):
            print(df[['factor_id', 'name_cn', 'group_key', 'tier',
                      'unit', 'src_row']].to_string(index=False))
        return 0

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + '.tmp'
    df.to_parquet(tmp, index=False)
    os.replace(tmp, OUT)                     # 原子替换，同项目里其它落盘
    print('-> %s  (%.1f KB)' % (os.path.relpath(OUT, REPO),
                                os.path.getsize(OUT) / 1024.0))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
