"""BaoStock 能力探测（本地跑，无需注册/无额度）。

用法: pip install baostock; python probe_baostock.py

它要回答的问题, 按重要性排序:
  B1 ★ 季频财务接口是否返回 pubDate(公告日)  —— go/no-go, 决定财务能否用于回测
  B2   query_stock_basic 是否含退市股票(outDate/status) —— 存活偏差
  B3   行业分类是否有历史(有无日期参数)      —— 历史板块
  B4   query_all_stock(day=) 能否给出"当日在市列表" —— PIT 证券列表的替代路径
  B5   日线与 tdx2db 是否对得上              —— 交叉校验主源
  B6   复权因子/除权除息覆盖情况

设计原则同 probe_jq.py: 每个探测独立捕获, 但**失败必须打印原因**, 不静默。
"""
import sys
import traceback

RESULTS = []


def probe(name, key, fn):
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


try:
    import baostock as bs
except ImportError:
    sys.exit("请先 pip install baostock")

import pandas as pd

lg = bs.login()
print(f"登录: code={lg.error_code} msg={lg.error_msg}")


def to_df(rs):
    """把 baostock 的 ResultData 转 DataFrame, 并把 error_code 一并带出。"""
    if rs.error_code != '0':
        raise RuntimeError(f"error_code={rs.error_code} msg={rs.error_msg}")
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=rs.fields)


# ------------------------------------------------ B1 ★ 财务数据是否带公告日
def _b1():
    out = []
    apis = [('query_profit_data', bs.query_profit_data),
            ('query_balance_data', bs.query_balance_data),
            ('query_cash_flow_data', bs.query_cash_flow_data),
            ('query_growth_data', bs.query_growth_data),
            ('query_operation_data', bs.query_operation_data),
            ('query_dupont_data', bs.query_dupont_data)]
    for nm, fn in apis:
        try:
            df = to_df(fn(code='sh.600000', year=2015, quarter=1))
            has_pub = any('pub' in c.lower() for c in df.columns)
            has_stat = any('stat' in c.lower() for c in df.columns)
            mark = '★有公告日' if has_pub else '⚠无公告日'
            out.append(f"{nm}: {mark} 字段={list(df.columns)[:6]}… "
                       f"(pubDate={has_pub}, statDate={has_stat}) 行数={len(df)}")
            if has_pub and len(df):
                out.append(f"    样例: {df.iloc[0].to_dict()}")
        except Exception as e:                          # noqa: BLE001
            out.append(f"{nm}: 失败 {type(e).__name__}: {e}")
    return "\n    " + "\n    ".join(out)

probe("★季频财务接口是否返回 pubDate(公告日) —— go/no-go", "B1", _b1)


# ------------------------------------------------------ B2 退市股票与上市状态
def _b2():
    df = to_df(bs.query_stock_basic())
    cols = list(df.columns)
    res = [f"字段={cols}", f"总记录 {len(df)}"]
    if 'outDate' in df.columns:
        delisted = df[df['outDate'].astype(str).str.len() > 4]
        res.append(f"有退市日期的记录 {len(delisted)} 条 → 退市股票{'在库' if len(delisted) else '不在库'}")
        if len(delisted):
            res.append(f"样例: {delisted.head(3)[['code','code_name','ipoDate','outDate']].to_dict('records')}")
    if 'status' in df.columns:
        res.append(f"status 取值分布: {df['status'].value_counts().to_dict()}")
    if 'type' in df.columns:
        res.append(f"type 取值分布: {df['type'].value_counts().to_dict()}")
    return "\n    " + "\n    ".join(res)

probe("query_stock_basic 是否含退市股票(存活偏差)", "B2", _b2)


# ---------------------------------------------------------- B3 行业分类有无历史
def _b3():
    df_now = to_df(bs.query_stock_industry())
    res = [f"字段={list(df_now.columns)}, 记录 {len(df_now)}"]
    if 'updateDate' in df_now.columns:
        res.append(f"updateDate 分布(前5): {df_now['updateDate'].value_counts().head().to_dict()}")
        res.append("→ 若 updateDate 只有少数几个值, 说明是快照而非逐日历史")
    # 试试带日期参数(文档未必支持, 试出来才知道)
    try:
        df_old = to_df(bs.query_stock_industry(date='2015-06-30'))
        same = set(zip(df_old.get('code', []), df_old.get('industry', []))) == \
            set(zip(df_now.get('code', []), df_now.get('industry', [])))
        res.append(f"date='2015-06-30' 可传, 返回 {len(df_old)} 条, 与当前完全相同={same}")
        res.append("→ 若相同, 则日期参数无效, 仍是快照")
    except Exception as e:                              # noqa: BLE001
        res.append(f"不支持 date 参数: {type(e).__name__}: {e} → 只有当前快照")
    return "\n    " + "\n    ".join(res)

probe("行业分类是否有历史(历史板块的关键)", "B3", _b3)


# -------------------------------------------------- B4 当日在市列表(PIT 替代路径)
def _b4():
    a = to_df(bs.query_all_stock(day='2015-06-30'))
    b = to_df(bs.query_all_stock(day='2026-08-20'))
    only_old = set(a['code']) - set(b['code'])
    return (f"2015-06-30 返回 {len(a)} 条, 2026-08-20 返回 {len(b)} 条; "
            f"仅 2015 有的 {len(only_old)} 条 → "
            f"{'✅ 可作 PIT 在市列表' if len(only_old) > 50 else '⚠ 疑似只有当前快照'}; "
            f"字段={list(b.columns)}")

probe("query_all_stock(day=) 能否给出当日在市列表", "B4", _b4)


# ------------------------------------------------------- B5 日线与 tdx 交叉校验
def _b5():
    df = to_df(bs.query_history_k_data_plus(
        "sh.600000", "date,open,high,low,close,volume,amount,adjustflag,turn,pctChg",
        start_date='2026-08-01', end_date='2026-08-21', frequency="d", adjustflag="3"))
    res = [f"不复权日线字段={list(df.columns)}, {len(df)} 行"]
    try:
        import duckdb
        con = duckdb.connect('/Users/guhao/finacial/tdx2db/tdx.db', read_only=True)
        t = con.execute("""SELECT date, close FROM v_stock_bfq
                           WHERE symbol='sh600000' AND date BETWEEN '2026-08-01' AND '2026-08-21'
                           ORDER BY date""").df()
        con.close()
        m = df.assign(date=pd.to_datetime(df['date']), close=df['close'].astype(float)) \
              .merge(t.assign(date=pd.to_datetime(t['date'])), on='date', suffixes=('_bs', '_tdx'))
        if len(m):
            d = (m['close_bs'] - m['close_tdx']).abs().max()
            res.append(f"与 tdx v_stock_bfq 比对 {len(m)} 天, 最大收盘价差 {d:.4f} "
                       f"→ {'✅ 一致' if d < 0.011 else '⚠ 不一致, 需查复权/刻度口径'}")
        else:
            res.append("⚠ 无重叠日期, 无法比对")
    except Exception as e:                              # noqa: BLE001
        res.append(f"tdx 比对跳过: {type(e).__name__}: {e}")
    return "\n    " + "\n    ".join(res)

probe("日线与 tdx2db 交叉校验", "B5", _b5)


# ------------------------------------------------------------- B6 除权除息覆盖
def _b6():
    df = to_df(bs.query_dividend_data(code="sh.600000", year="2015", yearType="report"))
    return f"字段={list(df.columns)}, {len(df)} 行; 样例={df.head(1).to_dict('records')}"

probe("除权除息数据", "B6", _b6)


bs.logout()
print(f"\n\n{'='*72}\n汇总\n{'='*72}")
for key, name, st, d in RESULTS:
    print(f"{'✅' if st == 'OK' else '❌'} [{key:<3}] {name}")
    print(f"      {d}")
print("""
判读:
  · B1 若全部"无公告日"       → BaoStock 不能作财务主源, 转 AkShare 东财系(stock_yjbb_em
                                等接口带公告日)或巨潮; **绝不用无公告日的财务数据回测**
  · B2 若退市股票在库          → 存活偏差问题解决(这是 BaoStock 最大价值)
  · B3 若只有快照              → 历史板块只能靠申万官网 + 每日快照积累
  · B5 若不一致                → 先查是不是 2026-05-25 那次刻度变更(ETF/债券 ×10)
""")
