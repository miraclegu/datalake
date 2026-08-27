"""聚宽研究环境能力探测（免费账号）。

用法: 把整个文件粘进聚宽研究环境的一个 notebook cell 里跑。

输出: 每个需要的数据接口 —— 能不能调 / 返回什么字段 / 有没有 PIT 语义 / 估算全量抽取体量。

关键判据（决定方案能不能走）:
  P1  ★ get_all_securities(date=...) 是否真按日期返回当时在市列表（含已退市）→ 存活偏差
  P2    get_security_info 是否给 start_date/end_date/display_name
  P3    get_industry(date=...) 是否支持历史日期 → 历史板块
  P4    get_index_stocks(date=...) 是否支持历史日期
  P5    get_extras('is_st') 是否可用
  P6  ★ 财务数据是否带**公告日** → 无公告日则财务模块作废(回测必然用到未来信息)

设计原则: 每个探测独立捕获异常, 但**失败必须打印原因并进汇总** —— 探测的目的就是
把失败暴露出来, 这里不允许静默。
"""
# ⚠️ 星号导入必须在模块级: 写在函数里是 SyntaxError(import * only allowed at module level),
#    而 ast.parse 检查不出来, 必须用 compile() 校验。
from jqdata import *          # noqa: F401,F403
try:
    from jqdata import finance
except Exception as _e:        # noqa: BLE001
    finance = None
    print(f"⚠️ finance 模块不可用: {type(_e).__name__}: {_e}")

import traceback

RESULTS = []


def probe(key, name, fn):
    print(f"\n{'='*72}\n[{key}] {name}\n{'='*72}")
    try:
        d = fn()
        RESULTS.append((key, name, 'OK', d))
        print(f"✅ OK  {d}")
    except Exception as e:                              # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        RESULTS.append((key, name, 'FAIL', msg))
        print(f"❌ FAIL  {msg}")
        traceback.print_exc(limit=2)


def quota():
    """额度查询。研究环境通常没有 get_query_count(那是本地 SDK 的函数),
    取不到就算了 —— 不要因为这个阻塞整个探测。"""
    for mod, fname in (('jqdata', 'get_query_count'),
                       ('jqdatasdk', 'get_query_count')):
        try:
            m = __import__(mod, fromlist=[fname])
            return getattr(m, fname)()
        except Exception:                               # noqa: BLE001
            continue
    try:
        return get_query_count()        # noqa: F405  研究环境可能已注入全局
    except Exception as e:              # noqa: BLE001
        return f"不可用({type(e).__name__}) —— 研究环境请在界面查看剩余额度"


print("聚宽研究环境能力探测")
print("探测前额度:", quota())


# --------------------------------------------------- P1 ★ 存活偏差(最关键判据)
def _p1():
    hist = get_all_securities(types=['stock'], date='2015-06-30')   # noqa: F405
    now = get_all_securities(types=['stock'], date='2026-08-20')    # noqa: F405
    gone = sorted(set(hist.index) - set(now.index))
    verdict = ('✅ 含已退市, 存活偏差可解决' if len(gone) > 50
               else '❌ 疑似只有当前快照, 存活偏差无法解决')
    return (f"{verdict} | 2015-06-30 在市 {len(hist)} 只, 2026-08-20 在市 {len(now)} 只, "
            f"其后退市/改代码 {len(gone)} 只 | 字段={list(hist.columns)} | 样例={gone[:3]}")


probe("P1", "★get_all_securities 按日期返回当时在市列表(存活偏差)", _p1)


# ----------------------------------------------------------- P2 单只证券信息
def _p2():
    info = get_security_info('000001.XSHE')                          # noqa: F405
    keys = [a for a in dir(info) if not a.startswith('_')]
    return (f"字段={keys} | display_name={getattr(info,'display_name',None)}, "
            f"name={getattr(info,'name',None)}, "
            f"start={getattr(info,'start_date',None)}, end={getattr(info,'end_date',None)}")


probe("P2", "get_security_info 是否给上市/退市日期与名称", _p2)


# ------------------------------------------------------------- P3 历史行业
def _p3():
    a = get_industry('000001.XSHE', date='2012-01-04')               # noqa: F405
    b = get_industry('000001.XSHE', date='2026-08-20')               # noqa: F405
    return (f"{'✅ 历史可用(两期不同)' if a != b else '⚠ 两期相同, 可能只有快照(也可能该股确实未变过行业)'}"
            f" | 2012={a} | 2026={b}")


probe("P3", "get_industry 是否支持历史日期(历史板块)", _p3)


def _p3b():
    """换一只 2012 年后换过行业的股票交叉验证, 避免被"该股本来没变过"误导。"""
    codes = ['600000.XSHG', '000002.XSHE', '600519.XSHG', '000063.XSHE']
    changed = []
    for c in codes:
        try:
            a = get_industry(c, date='2012-01-04')                   # noqa: F405
            b = get_industry(c, date='2026-08-20')                   # noqa: F405
            if a != b:
                changed.append(c)
        except Exception:                                            # noqa: BLE001
            pass
    return (f"抽查 {len(codes)} 只, 其中 {len(changed)} 只两期行业不同 → "
            f"{'✅ 确认有历史' if changed else '⚠ 全部相同, 高度怀疑只有快照'} | 变化的={changed}")


probe("P3b", "多只交叉验证行业是否真有历史", _p3b)


# -------------------------------------------------------- P4 指数成分历史
def _p4():
    a = set(get_index_stocks('000300.XSHG', date='2012-01-04'))      # noqa: F405
    b = set(get_index_stocks('000300.XSHG', date='2026-08-20'))      # noqa: F405
    return (f"{'✅ 历史可用' if a != b else '⚠ 疑似只有当前快照'} | "
            f"沪深300: 2012 有 {len(a)} 只, 2026 有 {len(b)} 只, 交集 {len(a & b)} 只")


probe("P4", "get_index_stocks 是否支持历史日期", _p4)


# ------------------------------------------------------------- P5 ST 历史
def _p5():
    df = get_extras('is_st', ['000001.XSHE', '600000.XSHG'],         # noqa: F405
                    start_date='2010-01-01', end_date='2010-03-01')
    return f"✅ 可用 | 形状={df.shape}, 列={list(df.columns)}, True 占比={float(df.mean().mean()):.3f}"


probe("P5", "get_extras('is_st') ST 历史", _p5)


# ------------------------------------------ P6 ★ 财务数据(go/no-go: 有无公告日)
def _p6a():
    q = query(valuation.code, valuation.market_cap).filter(          # noqa: F405
        valuation.code.in_(['000001.XSHE', '600519.XSHG']))          # noqa: F405
    df = get_fundamentals(q, date='2015-06-30')                      # noqa: F405
    return f"✅ get_fundamentals 可用 | 形状={df.shape}, 列={list(df.columns)}"


probe("P6a", "get_fundamentals 基础可用性", _p6a)


def _p6b():
    """★ go/no-go: 财务数据有没有公告日。没有 → 财务模块暂缓。"""
    out = []
    # 路线1: income 表的 statDate / pubDate
    try:
        q = query(income.code, income.statDate, income.pubDate,       # noqa: F405
                  income.total_operating_revenue).filter(             # noqa: F405
            income.code == '000001.XSHE')                             # noqa: F405
        df = get_fundamentals(q, statDate='2015q1')                    # noqa: F405
        out.append(f"★income.pubDate 可查 → {df.to_dict('records')}")
    except Exception as e:                                            # noqa: BLE001
        out.append(f"income.pubDate 不可用: {type(e).__name__}: {e}")
    # 路线2: finance.STK_INCOME_STATEMENT (通常带 pub_date/report_date/report_type, 含重述)
    if finance is not None:
        try:
            df2 = finance.run_query(
                query(finance.STK_INCOME_STATEMENT).filter(           # noqa: F405
                    finance.STK_INCOME_STATEMENT.code == '000001.XSHE').limit(3))
            cols = list(df2.columns)
            dated = [c for c in cols if any(k in c.lower() for k in ('pub', 'report', 'date'))]
            out.append(f"★finance.STK_INCOME_STATEMENT 可用, {len(cols)} 列, 日期相关列={dated}")
            if dated:
                out.append(f"    样例={df2[dated].head(2).to_dict('records')}")
        except Exception as e:                                        # noqa: BLE001
            out.append(f"finance.STK_INCOME_STATEMENT 不可用: {type(e).__name__}: {e}")
    else:
        out.append("finance 模块未导入, 跳过路线2")
    return "\n    " + "\n    ".join(out)


probe("P6b", "★财务数据是否带公告日(go/no-go)", _p6b)


# ------------------------------------------------------------ P7 体量估算
def _p7():
    n = len(get_all_securities(types=['stock'], date='2026-08-20'))   # noqa: F405
    quarters = (2026 - 2005) * 4
    return (f"当前股票 {n} 只; 2005 起约 {quarters} 个报告期 → "
            f"财务宽表约 {n*quarters/1e4:.0f} 万行; "
            f"行业/ST 若按变更区间存, 预计各 <5 万行")


probe("P7", "全量抽取体量估算", _p7)


# ------------------------------------------------------------------ 汇总
print(f"\n\n{'='*72}\n汇总\n{'='*72}")
for key, name, st, d in RESULTS:
    print(f"{'✅' if st == 'OK' else '❌'} [{key:<4}] {name}\n        {d}")
print("\n探测后额度:", quota())
print("""
判读:
  · P1 若"其后退市 ≤50 只"        → 只有当前快照, 存活偏差无法解决, 必须换源
  · P3/P3b 若全部两期相同          → 只有快照, 历史板块要另找源(申万官网)+每日快照积累
  · P4 若两期相同                  → 指数成分只有快照
  · P6b 若两条路线都拿不到公告日   → **财务模块暂缓**, 先做日线+实体PIT+行业PIT
  · 对比前后额度 → 推算财务全量抽取要分几天
""")
