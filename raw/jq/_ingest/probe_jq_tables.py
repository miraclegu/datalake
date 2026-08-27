"""聚宽可用数据表【全量探测】（在研究环境跑，只读 1 行/表，很轻）。

把整个文件粘进一个 cell 执行。

## 为什么先探测而不是直接抽

当前明确要找的是**机构盈利预测 / 券商一致预期**（预期差策略用）。
我们已有的 `finance.STK_FIN_FORCAST` 是**公司自己发的业绩预告**
（profit_min/profit_max + 预增/预减类型），**不是券商一致预期** ——
两者完全不同，前者是上市公司公告，后者是分析师覆盖。

我不确定聚宽有没有后者。与其猜表名（上次猜 8 个只中 1 个），
不如把 finance 模块下**所有表**枚举一遍：每张表只取 1 行拿列名，
然后按关键词打分，把「像一致预期」的排在最前。

顺带产出一份**完整的可用数据地图** —— 以后想找什么表不必再猜。

## 输出

  1. 所有表：名字 + 列数 + 列名 + 关键词命中
  2. 按「像一致预期」打分排序的候选清单
  3. 完整清单落盘 jq_tables.csv，下载后可本地检索

## 判定「像一致预期」的关键词

  表名/列名含：forecast / consensus / expect / estimate / analyst / research /
              report / rating / target_price / eps_fy / 预测 / 预期 / 评级
  且【不是】我们已有的业绩预告（STK_FIN_FORCAST）
"""
import pandas as pd
from jqdata import *          # noqa: F401,F403  星号导入必须在模块级
from jqdata import finance

# 命中即加分。分数只用于排序，最终判断靠人看列名 —— 不自动下结论。
HINTS = {
    'consensus': 10, 'analyst': 10, 'expectation': 8, 'expect': 6,
    'estimate': 8, 'forecast': 5, 'research': 6, 'rating': 6,
    'target_price': 10, 'eps_fy': 10, 'profit_fy': 10,
    '一致': 10, '预期': 8, '预测': 5, '评级': 6, '目标价': 10, '研报': 8,
}
QUOTA_WORDS = ('额度不足', '额度已用', '配额', 'quota exceeded', 'quota limit',
               '超过限制', '调用次数', 'rate limit', 'too many requests')


def is_quota_error(e):
    m = ('%s %s' % (type(e).__name__, e)).lower()
    return any(w.lower() in m for w in QUOTA_WORDS)


def score(name, cols):
    blob = (name + ' ' + ' '.join(cols)).lower()
    s, hit = 0, []
    for k, v in HINTS.items():
        if k in blob:
            s += v
            hit.append(k)
    return s, hit


def main():
    # finance 模块下所有大写开头的属性基本都是表
    names = sorted(a for a in dir(finance) if a.isupper() or a.startswith('STK_')
                   or a.startswith('FUND_') or a.startswith('SW_')
                   or a.startswith('FINANCE_') or a.startswith('MTSS'))
    print('finance 模块下候选表 %d 个，逐个取 1 行探列名…' % len(names))
    rows, failed = [], []
    for i, nm in enumerate(names, 1):
        tbl = getattr(finance, nm, None)
        if tbl is None:
            failed.append((nm, 'getattr 返回 None'))
            continue
        # ★ 不做 hasattr 预筛 —— 直接试 run_query。
        #   上一版写了 `not hasattr(tbl,'__table__') and not hasattr(tbl,'columns')`
        #   当预筛，结果把 77 张表【全部】挡掉（聚宽的表对象两个属性都没有），
        #   一张也没探到。「猜对象长什么样」比「试一下再看错误」脆弱得多。
        try:
            df = finance.run_query(query(tbl).limit(1))
        except Exception as e:                              # noqa: BLE001
            if is_quota_error(e):
                print('  额度受限，已探 %d/%d 个，明天重跑' % (i, len(names)))
                break
            failed.append((nm, '%s: %s' % (type(e).__name__, str(e)[:60])))
            continue
        cols = list(df.columns)
        sc, hit = score(nm, cols)
        rows.append({'table': nm, 'n_cols': len(cols), 'score': sc,
                     'hits': ','.join(hit), 'columns': '|'.join(cols)})
        if i % 20 == 0:
            print('  …%d/%d' % (i, len(names)))

    if not rows:
        # 全失败时也要把原因打出来 —— 否则下一轮还是不知道为什么
        print('❌ 一张表都没探到。失败原因（前 20 条）:')
        for nm, why in failed[:20]:
            print('   %-42s %s' % (nm, why))
        print('\n   共 %d 个失败。若全是同一种错误，那是权限或调用方式问题；'
              % len(failed))
        print('   若是各不相同的 SQL 错误，那说明表能访问、只是 query 写法要调。')
        return
    out = pd.DataFrame(rows).sort_values(['score', 'table'], ascending=[False, True])
    out.to_csv('jq_tables.csv', index=False)
    print('\n共探到 %d 张表，%d 个失败 -> jq_tables.csv' % (len(out), len(failed)))

    print('\n' + '=' * 76)
    print('★ 最像【机构一致预期】的前 12 张（分数仅供排序，请人工看列名判断）')
    print('=' * 76)
    for _, r in out.head(12).iterrows():
        print('\n[%2d分] %s  (%d 列)  命中: %s' % (r['score'], r['table'],
                                                r['n_cols'], r['hits'] or '—'))
        print('   %s' % r['columns'].replace('|', ', '))

    print('\n' + '=' * 76)
    print('全部表一览（名字 + 列数）')
    print('=' * 76)
    for _, r in out.sort_values('table').iterrows():
        print('  %-42s %3d 列  %s' % (r['table'], r['n_cols'],
                                     ('★' + r['hits']) if r['score'] else ''))
    if failed:
        print('\n探测失败 %d 个（多半不是表对象）:' % len(failed))
        for nm, why in failed[:25]:
            print('  %-42s %s' % (nm, why))

    print('\n→ 下载 jq_tables.csv，并把上面「前 12 张」那段贴回来。')
    print('  已知不是我们要的：STK_FIN_FORCAST（= 公司业绩预告，本地已有）。')


main()

# ============================================================
# 清理 cell（跑完单独执行）
# ------------------------------------------------------------
# import gc
# from IPython import get_ipython
# get_ipython().user_ns['Out'].clear()
# gc.collect()
# ============================================================
