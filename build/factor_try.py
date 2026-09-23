# -*- coding: utf-8 -*-
"""试一个新因子：给公式 -> 跑全历史 -> 出体检报告。

    python3 datalake/build/factor_try.py "ma(C,20)/ma(C,60)-1" --name 双均线比
    python3 datalake/build/factor_try.py "(C-ma(C,20))/stdp(C,20)" --years 3
    python3 datalake/build/factor_try.py "..." --as-spec      # 打印可直接粘的 Spec
    python3 datalake/build/factor_try.py --ops                # 有哪些列和算子

## 这个工具要解决的是「中间那一步」

    想到一个公式  ->  【这里】  ->  写进族文件  ->  全量重建进面板
                      跑全历史
                      看它长什么样

在此之前要试一个想法，只能改 .py 再整库重建 142 秒；试错一轮的成本压过了
想法本身。现在一条命令，秒级到分钟级出结果。

## 🔴 与正式落盘【同一条计算路径】

试跑复用 `factors.Ctx` 的那些算子、同一套派生列、同一种分块 ——
另写一条快路径的话，"试的时候是这个数、进面板变成另一个数"迟早发生，
**而两边都不报错**（同「两处实现必然分叉」）。所以这里没有一行自己的算法。

## 🔴 **刻意没有「只跑最近 N 年」这个选项**

第一版有 `--years`，理由是"调试时快很多"。实测全历史 1632 万行**只要 5 秒**
（贵的是 98 个因子一起算，单个表达式不贵），而截断历史会让长窗口因子在
起点附近算出**与面板不同的值** —— 试的时候看到一个分布、进面板变成另一个，
**而它不报错**。一个"快但会给出不同数值"的选项比没有更糟
（同「刻意删掉那个『这个率含不含规费』开关 —— 它是最容易填错的一处」）。

## 🔴 先问「是不是已经有了」

报告里必带**与现有 98 个因子的最大相关**。一个新想法与 `bias20` 的秩相关
是 0.98 的话，它不是新因子、是同一个东西换了个写法 ——
而光看自己的分布图是看不出来的。

## ⚠ 表达式用受限 `eval`，与 `query.py` 的三层防护【不是一回事】

`query.py` 要防的是"从看板上把 mart/ 写坏"，所以有语句白名单 + 关键字黑名单
+ 子进程超时。这里是**本地命令行开发工具**，输入来自你自己的键盘，
不接任何外部输入 —— 命名空间限制只为了**手滑时报错指得到原因**
（写错列名当场告诉你有哪些列），不是安全边界。别把两者的分工记反。
"""
import argparse
import os
import sys
import time

import duckdb
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DL = os.path.dirname(HERE)
PANEL = os.path.join(DL, 'mart', 'panel_daily', 'panel_*.parquet')
FDIR = os.path.join(DL, 'mart', 'factor_daily')

sys.path.insert(0, HERE)
from factors import Ctx, all_specs, GROUPS                  # noqa: E402
from factors import load                                    # noqa: E402

# ★ 求值器、列别名、算子表**全在 factors/expr.py** —— 这里一行都不重写。
#   两边各写一个的话，"试的时候是这个数、进面板变成另一个"迟早发生
#   （同「两处实现必然分叉」）。
from factors.expr import ALIAS, OPS, PT, evaluate, deps_of, warm_of  # noqa: E402


def _load(con, codes=None):
    """★ 转发给 `factors/load.py` —— 取数只有一份实现。

    原来这里自己拼列名，加财务族之后 `deps` 里出现了 as-of 才有的
    `b_*` 列，SELECT 当场报"没有这一列"，**而 `--selftest` 因此整条挂掉**。
    """
    return load.chunk_df(con, PANEL, DL, codes)


def _chunks(con, chunk):
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT jq_code FROM read_parquet('%s') ORDER BY 1" % PANEL
    ).fetchall()]
    for i in range(0, len(codes), chunk):
        df = _load(con, codes[i:i + chunk])
        if not df.empty:
            yield df


def run(expr, con, chunk=700):
    """按股票分块跑全历史 —— 分块口径与 `build_factor_daily` 一致。

    🔴 **按股票分块不是随便挑的**：按年分段会让每段头部静默变 NaN
      （EMA 永远记着起点、长期停牌股的窗口跨过缺口），那一轮的教训写在
      `build_factor_daily.py` 的 docstring 里。这里必须跟它一致，
      否则"试出来的"与"进面板的"不是一个东西。
    """
    outs = []
    for df in _chunks(con, chunk):
        x = Ctx(df)
        v = evaluate(x, expr)
        outs.append(pd.DataFrame({
            'jq_code': x.df['jq_code'].to_numpy(),
            'date': x.df['date'].to_numpy(),
            'v': np.asarray(v, dtype='float32')}))
    return pd.concat(outs, ignore_index=True)


def corr_with_existing(res, con, sample_year=None):
    """与现有 98 个因子的**秩相关**（Spearman），取最近一年做样本。

    ★ 用秩相关不是皮尔逊：因子拿去做横截面排序，重要的是序不是标度；
      而且量纲差几个数量级时皮尔逊会被极值主导。
    """
    ys = sorted(int(os.path.basename(p)[7:11])
                for p in __import__('glob').glob(os.path.join(FDIR, 'factor_*.parquet')))
    if not ys:
        return None, '盘上还没有因子面板，跳过相关性'
    y = sample_year or ys[-1]
    old = pd.read_parquet(os.path.join(FDIR, 'factor_%d.parquet' % y))
    cur = res[res['date'].dt.year == y]
    if cur.empty:
        return None, '试跑没覆盖 %d 年，跳过相关性' % y
    m = old.merge(cur, on=['jq_code', 'date'], how='inner')
    if len(m) < 1000:
        return None, '与 %d 年只对上 %d 行，样本太小' % (y, len(m))
    base = m['v']
    out = []
    for c in old.columns:
        if c in ('jq_code', 'date'):
            continue
        s = m[c]
        ok = base.notna() & s.notna()
        if ok.sum() < 1000:
            continue
        r = base[ok].rank().corr(s[ok].rank())
        if pd.notna(r):
            out.append((abs(r), r, c))
    out.sort(reverse=True)
    return out, '%d 年 %d 行' % (y, len(m))


def report(expr, name, res, corr, note, secs):
    v = res['v'].to_numpy('float64')
    fin = np.isfinite(v)
    n = len(v)
    print('\n表达式  %s' % expr)
    if name:
        print('名称    %s' % name)
    print('规模    %s 行 / %d 只 / %s ~ %s    %.0f 秒'
          % (format(n, ','), res['jq_code'].nunique(),
             res['date'].min().date(), res['date'].max().date(), secs))
    # 🔴 inf 单独数：它是合法 float、不报错，却会毁掉之后任何均值与排序
    ninf = int(np.isinf(v).sum())
    print('覆盖    非空 %.1f%%   inf %d %s   全空的票 %d'
          % (100.0 * fin.mean(), ninf, '🔴' if ninf else '',
             int((res.groupby('jq_code')['v']
                  .apply(lambda s: s.notna().sum() == 0)).sum())))
    if fin.sum():
        q = np.percentile(v[fin], [1, 25, 50, 75, 99])
        print('分布    p1 %.4g | p25 %.4g | 中位 %.4g | p75 %.4g | p99 %.4g'
              % tuple(q))
        print('        min %.4g   max %.4g   均值 %.4g   标准差 %.4g'
              % (v[fin].min(), v[fin].max(), v[fin].mean(), v[fin].std()))
    # 逐年覆盖最低的三年 —— 长窗口因子在早年会大片是空的，那是事实不是 bug，
    # 但要看得见（不然拿它做 2005 年的回测时会静默少掉一半票）
    by = res.assign(ok=res['v'].notna()).groupby(res['date'].dt.year)['ok'].mean()
    lo = by.nsmallest(3)
    print('逐年覆盖 最低三年: %s'
          % '  '.join('%d %.0f%%' % (y, 100 * r) for y, r in lo.items()))
    print()
    if corr is None:
        print('相关性  %s' % note)
    else:
        print('🔴 与现有因子的秩相关（%s）—— 先问"是不是已经有了"：' % note)
        for a, r, c in corr[:5]:
            flag = '  ← 几乎是同一个东西' if a >= 0.95 else (
                '  ← 高度重合' if a >= 0.85 else '')
            print('        %-16s %+.3f%s' % (c, r, flag))
        if corr and corr[0][0] >= 0.95:
            print('        ⚠ 最高 |r| = %.3f：它多半不是一个新因子，'
                  '而是同一个东西换了个写法' % corr[0][0])


def as_spec(e, name, res):
    """打印一段**可以直接跑**的 Spec。

    🔴 第一版打印的是 `lambda x: <表达式>` —— 那是跑不了的：裸的 `rsum`/`R`/`V`
      在族文件的模块作用域里根本不存在。而修法不是"把表达式翻译成
      `x.rsum(...)`"，是让 Spec 用**同一个求值器**（`expr('...')`）——
      于是 `formula` 字段与 `calc` 是**同一个字符串**，公式与实现分叉不了。
    """
    print('\n# 粘进 datalake/build/factors/<族>.py，并在文件头 from . import expr：')
    print("""        Spec('改个编号', %r, '改个族名',
             %r,
             '改成：这个因子干什么用的（与公式分开写）',
             '改成：单位（比率一律写 小数(0.05=5%%)）',
             %r, %d,
             expr(%r)),"""
          % (name or '改成 factors.xlsx 里的原名', e, deps_of(e), warm_of(e), e))
    print('# ★ 族名从这些里挑: %s' % ' / '.join(GROUPS))
    print('# ★ deps 与 warm 是**自动推**的：deps 可靠（漏一列的表现是那个因子')
    print('#   整列为空且不报错）；warm 只是建议 —— 递推类(ema/wilder)真正要的')
    print('#   远大于 n，本族约定 n*4，而 Spec.warm_eff 还有 120 根的下限。')
    print('# 🔴 写完跑 build_factor_daily.py --force（142s）它才会进面板；')
    print('#    公式指纹会变，那正是"盘上的值按旧公式算"的唯一告警。')


def scan_dups(thr=0.999, n=200000, year=None):
    """扫现有面板里**秩相关 >= thr** 的因子对。

    🔴 「这个想法是不是已经有了」对**已经在面板里的**同样要问一遍。
      实测本地 98 个因子里有 33 对 |秩相关| >= 0.999，其中好几对是
      **代数恒等**的（`(C−M)/M` 与 `C/M−1`、`Σ₂₀TO` 与 `20×MA(TO,20)`、
      `var` 与 `std`）—— 它们在 `factors.xlsx` 里是不同的行，
      所以那 285 个名字背后的**独立因子数远没有那么多**。

    ★ **不因此删因子**：秩相同 ≠ 值相同（var 与 std 的值差一个平方），
      而且原清单里它们本来就是两行。这里只负责让重复**看得见**。
    """
    import glob as _g
    ys = sorted(int(os.path.basename(q)[7:11])
                for q in _g.glob(os.path.join(FDIR, 'factor_*.parquet')))
    if not ys:
        print('盘上还没有因子面板')
        return 1
    y = year or ys[-1]
    df = pd.read_parquet(os.path.join(FDIR, 'factor_%d.parquet' % y))
    cols = [c for c in df.columns if c not in ('jq_code', 'date')]
    rk = df[cols].sample(n=min(n, len(df)), random_state=0).rank()
    C = rk.corr()
    out = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            r = C.loc[a, b]
            if pd.notna(r) and abs(r) >= thr:
                out.append((abs(r), r, a, b))
    out.sort(reverse=True)
    print('%d 年 %d 行抽样 %d 行，%d 个因子两两 %d 对：'
          % (y, len(df), min(n, len(df)), len(cols),
             len(cols) * (len(cols) - 1) // 2))
    print('|秩相关| >= %.3f 的有 %d 对\n' % (thr, len(out)))
    for a, r, c1, c2 in out:
        print('  %-16s %-16s %+.4f' % (c1, c2, r))
    print('\n★ 秩相同 != 值相同 —— var 与 std 的序一样而值差一个平方。')
    print('  不因此删因子（原清单里它们本来就是两行），只让重复看得见。')
    return 0


def selftest(con, n_codes=300):
    """三条自证 —— 「记得手工核对一遍」不是判据，所以写成命令。

    跑在 `n_codes` 只票的**全历史**上（不是最近几年）：分块已被证明不改变
    任何值，所以取一个票子集是合法的，而截断历史**不是**。
    """
    from factors import Spec, register, all_specs
    from factors.expr import expr as mk, deps_of, warm_of
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT jq_code FROM read_parquet('%s') ORDER BY 1 LIMIT %d"
        % (PANEL, n_codes)).fetchall()]
    df = _load(con, codes)
    ok = True

    # ① 表达式能复现面板里已有的因子 —— 证明求值器与族文件里手写的 calc 同口径
    x = Ctx(df)
    got = np.asarray(evaluate(x, '(C-ma(C,20))/ma(C,20)'), dtype='float32')
    want = np.asarray([s for s in all_specs() if s.id == 'bias20'][0].calc(x),
                      dtype='float32')
    ne = int((~((got == want) | (np.isnan(got) & np.isnan(want)))).sum())
    ok &= ne == 0
    print('① 表达式复现 bias20        %8d 行  不等 %d  %s'
          % (len(got), ne, '✓' if ne == 0 else '🔴'))

    # ② 用 expr() 注册成 Spec 之后，与直接试跑【逐位相同】
    E = 'rsum(where(R>0,V,0),20)/rsum(V,20)'
    fid = '_selftest_tmp'
    if not any(s.id == fid for s in all_specs()):
        register([Spec(fid, '自检临时因子', 'vol', E, '自检用', '小数(0~1)',
                       deps_of(E), warm_of(E), mk(E))])
    sp = [s for s in all_specs() if s.id == fid][0]
    a = np.asarray(sp.calc(x), dtype='float32')             # 走 Spec
    b = np.asarray(evaluate(x, E), dtype='float32')          # 走试跑
    ne = int((~((a == b) | (np.isnan(a) & np.isnan(b)))).sum())
    ok &= ne == 0
    print('② Spec 与试跑同一条路      %8d 行  不等 %d  %s'
          % (len(a), ne, '✓' if ne == 0 else '🔴'))

    # ③ 🔴 反向自证：判据抓不抓得住。没有这一条的话，前两条"全绿"可能只是
    #    因为比较写错了（比如两边都取到 NaN）。
    bad = a * np.float32(1.000001)
    ne3 = int((~((bad == b) | (np.isnan(bad) & np.isnan(b)))).sum())
    ok &= ne3 > len(a) * 0.5
    print('③ 反向自证（×1.000001）     %8d 行  不等 %d  %s'
          % (len(a), ne3, '✓ 抓得住' if ne3 > len(a) * 0.5 else '🔴 判据空转'))

    print('\n总判定: %s' % ('✓ 试跑 == 落盘 == 族文件手写，三者同一条路'
                           if ok else '🔴 有一条不成立，结论不能用'))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('expr', nargs='?', help='因子表达式')
    ap.add_argument('--name', help='中文名（对回 factors.xlsx 用）')
    ap.add_argument('--chunk', type=int, default=700)
    ap.add_argument('--save', help='把结果落到这个 parquet')
    ap.add_argument('--as-spec', action='store_true', help='打印可直接粘的 Spec')
    ap.add_argument('--ops', action='store_true', help='列出可用的列与算子')
    ap.add_argument('--dups', nargs='?', const=0.999, type=float, metavar='阈值',
                    help='扫现有面板里秩相关 >= 阈值 的因子对')
    ap.add_argument('--selftest', action='store_true',
                    help='三条自证：试跑 == 落盘 == 族文件手写')
    a = ap.parse_args()

    if a.selftest:
        return selftest(duckdb.connect(':memory:'))

    if a.dups:
        return scan_dups(a.dups)

    if a.ops or not a.expr:
        print('列（短名 = 因子实现规格.md 的记号）:')
        for k, v in ALIAS.items():
            print('  %-4s = %s' % (k, v))
        print('\n窗口算子  op(列, n):')
        print('  ' + '  '.join(sorted(OPS)))
        print('\n逐点函数:')
        print('  ' + '  '.join(sorted(PT)))
        print('\n例子:')
        print('  "ma(C,20)/ma(C,60)-1"          双均线比')
        print('  "(C-ma(C,20))/stdp(C,20)"      价格的 z 分数')
        print('  "rsum(where(R>0,V,0),20)/rsum(V,20)"   20 日涨日成交量占比')
        print('  "ema(ma(C,5),9)"               嵌套（中间量会自动物化）')
        return 0

    con = duckdb.connect(':memory:')
    t0 = time.time()
    try:
        res = run(a.expr, con, chunk=a.chunk)
    except NameError as e:
        raise SystemExit('表达式里有不认识的名字：%s\n用 --ops 看有哪些列和算子' % e)
    secs = time.time() - t0
    corr, note = corr_with_existing(res, con)
    report(a.expr, a.name, res, corr, note, secs)
    if a.save:
        res.to_parquet(a.save, index=False)
        print('\n-> %s (%.0f MB)' % (a.save, os.path.getsize(a.save) / 1e6))
    if a.as_spec:
        as_spec(a.expr, a.name, res)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
