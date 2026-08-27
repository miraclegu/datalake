"""聚宽 → PIT 维度数据一次性抽取（在研究环境跑）。

抽取范围（都是"便宜且高价值"的部分，合计约 60~80 次查询）:
  1. dim_security      证券全集(按年末 union, 含所有曾上市的股票) —— 解决存活偏差
  2. dim_name_history  名称变更史(STK_NAME_HISTORY) —— 解决"退市股票没名字/名称被覆盖"
  3. dim_status_change 状态变更史(STK_STATUS_CHANGE) —— ST/暂停/退市, 含双日期
  4. dim_industry      行业归属(按月快照, 后续在本地压成变更区间)
  5. idx_weight_month  指数月度权重(先探覆盖范围再决定抽多少)

设计约束（都是实测出来的，不是猜的）:
  · finance.run_query **单次上限 5000 行** → 一律按 id 分页
  · 研究环境 pandas 版本很老 → 不用命名聚合 / to_parquet, 统一写纯 CSV(utf-8-sig)
  · get_all_securities(date=) 已验证返回当时在市列表(含已退市)
  · STK_NAME_HISTORY / STK_STATUS_CHANGE 自带 start_date|change_date + pub_date
    → 天然 bitemporal(valid_from + known_from), 原样落盘即可, 不要在这里加工

不做的事:
  · 不做任何清洗/合并/推导 —— 这是 L0 落地层, 原样保存, 加工在本地做
  · 不抽财务(760 次查询, 等额度确认后单独跑 extract_jq_financials.py)
"""
import os
import time

import pandas as pd
from jqdata import *          # noqa: F401,F403  星号导入必须在模块级
from jqdata import finance

OUT = 'pitdb_out'
PAGE = 5000                   # 实测上限
YEARS = list(range(2003, 2027))
MANIFEST = []


def save(name, df):
    """落盘 + 记录清单。

    写**纯 CSV + utf-8-sig**, 不用 gzip:
      · 研究环境的文件预览器把 .gz 当文本渲染会报 "not UTF-8 encoded"(文件其实没问题,
        只是二进制不能预览), 纯 CSV 可以直接在浏览器里看
      · BOM 让 Excel 打开中文不乱码
      · 这几张维度表本来就只有几 MB, 压缩省不了多少
    老 pandas 没有 to_parquet, 所以不用 parquet(转 parquet 在本地做)。
    """
    if not os.path.exists(OUT):
        os.makedirs(OUT)
    path = os.path.join(OUT, name + '.csv')
    df.to_csv(path, index=False, encoding='utf-8-sig')
    size = os.path.getsize(path)
    MANIFEST.append((name, len(df), list(df.columns), size))
    print("  → 保存 %s: %d 行, %d 列, %.1f KB" % (name, len(df), len(df.columns), size / 1024.0))


def page_all(table, id_col, label, hard_cap=400):
    """按 id 分页抽全表。hard_cap 是安全阀, 防止意外无限循环。"""
    frames, last_id, n_page = [], -1, 0
    while n_page < hard_cap:
        df = finance.run_query(
            query(table).filter(id_col > last_id).order_by(id_col).limit(PAGE))
        if len(df) == 0:
            break
        frames.append(df)
        last_id = int(df['id'].max())   # 这几张表都有 id 主键
        n_page += 1
        if n_page % 5 == 0:
            print("    %s: 已抽 %d 页 / %d 行 (last_id=%d)"
                  % (label, n_page, sum(len(f) for f in frames), last_id))
        if len(df) < PAGE:
            break
    if n_page >= hard_cap:
        raise RuntimeError("%s 达到安全阀 %d 页, 请调大 hard_cap 或检查分页逻辑" % (label, hard_cap))
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    print("    %s: 共 %d 页, %d 行" % (label, n_page, len(out)))
    return out


t0 = time.time()
print("=" * 72)
print("聚宽 PIT 维度数据抽取")
print("=" * 72)

# ---------------------------------------------------------------- 1 证券全集
print("\n[1/5] dim_security: 按年末 union 取证券全集(解决存活偏差)")
rows = []
for y in YEARS:
    d = '%d-12-31' % y
    try:
        df = get_all_securities(types=['stock'], date=d)              # noqa: F405
        df = df.reset_index()
        df.columns = ['code'] + list(df.columns[1:])
        df['as_of'] = d
        rows.append(df)
        print("  %s: 在市 %d 只" % (d, len(df)))
    except Exception as e:                                            # noqa: BLE001
        # 探测式失败必须可见: 打印后继续, 但会体现在最终清单的缺失年份里
        print("  %s: ❌ %s: %s" % (d, type(e).__name__, e))
sec = pd.concat(rows, ignore_index=True)
# 同一 code 在多个年末重复出现是正常的; 去重保留每只的首末与静态字段
uniq = sec.drop_duplicates(subset=['code'], keep='last')
print("  年末快照合计 %d 行, 去重后 %d 只标的" % (len(sec), len(uniq)))
save('dim_security_asof', sec)      # 保留全部年末快照, 便于校验"当时在市数"
save('dim_security', uniq)

# ------------------------------------------------------------- 2 名称变更史
print("\n[2/5] dim_name_history: STK_NAME_HISTORY(自带 start_date + pub_date)")
nh = page_all(finance.STK_NAME_HISTORY, finance.STK_NAME_HISTORY.id, 'name_history')
save('dim_name_history', nh)

# ------------------------------------------------------------- 3 状态变更史
print("\n[3/5] dim_status_change: STK_STATUS_CHANGE(ST/暂停/退市, 双日期)")
sc = page_all(finance.STK_STATUS_CHANGE, finance.STK_STATUS_CHANGE.id, 'status_change')
save('dim_status_change', sc)
if len(sc):
    print("  change_type 分布:", sc['change_type'].value_counts().to_dict())
    print("  change_date 范围:", sc['change_date'].min(), "~", sc['change_date'].max())

# --------------------------------------------------------------- 4 行业归属
print("\n[4/5] dim_industry: 按月快照(本地再压成变更区间)")
# get_industry 一次可传多只; 按月抽, 每月一次调用
months = []
for y in YEARS:
    for m in (3, 6, 9, 12):
        months.append('%d-%02d-%02d' % (y, m, 28))
ind_rows = []
for d in months:
    try:
        codes = list(get_all_securities(types=['stock'], date=d).index)  # noqa: F405
        if not codes:
            continue
        info = get_industry(codes, date=d)                               # noqa: F405
        for c, v in info.items():
            rec = {'code': c, 'as_of': d}
            for scheme, val in (v or {}).items():
                if isinstance(val, dict):
                    rec[scheme + '_code'] = val.get('industry_code')
                    rec[scheme + '_name'] = val.get('industry_name')
            ind_rows.append(rec)
        print("  %s: %d 只" % (d, len(codes)))
    except Exception as e:                                              # noqa: BLE001
        print("  %s: ❌ %s: %s" % (d, type(e).__name__, e))
if ind_rows:
    save('dim_industry_asof', pd.DataFrame(ind_rows))
else:
    print("  ⚠️ 行业数据一条都没抽到, 需要人工检查 get_industry 的批量调用方式")

# ----------------------------------------------------- 5 指数权重(先探覆盖)
print("\n[5/5] idx_weight_month: 先探覆盖范围, 再决定抽多少")
try:
    probe = finance.run_query(
        query(finance.IDX_WEIGHT_MONTH.index_code, finance.IDX_WEIGHT_MONTH.end_date)
        .order_by(finance.IDX_WEIGHT_MONTH.end_date).limit(PAGE))
    print("  最早 end_date:", probe['end_date'].min())
    print("  这一页出现的指数:", sorted(set(probe['index_code']))[:20])
    # 只抽常用宽基, 避免全表(可能很大)
    WANT = ['000300', '000905', '000906', '000852', '000001', '399006']
    frames = []
    for ic in WANT:
        # 逐指数分页: page_all 不适用(要同时按 index_code 过滤), 单独写
        last, fr = -1, []
        while True:
            df = finance.run_query(
                query(finance.IDX_WEIGHT_MONTH)
                .filter(finance.IDX_WEIGHT_MONTH.index_code == ic,
                        finance.IDX_WEIGHT_MONTH.id > last)
                .order_by(finance.IDX_WEIGHT_MONTH.id).limit(PAGE))
            if len(df) == 0:
                break
            fr.append(df)
            last = int(df['id'].max())
            if len(df) < PAGE:
                break
        if fr:
            one = pd.concat(fr, ignore_index=True)
            print("  %s: %d 行, %s ~ %s" % (ic, len(one),
                  one['end_date'].min(), one['end_date'].max()))
            frames.append(one)
        else:
            print("  %s: 无数据" % ic)
    if frames:
        save('idx_weight_month', pd.concat(frames, ignore_index=True))
except Exception as e:                                                  # noqa: BLE001
    print("  ❌ %s: %s" % (type(e).__name__, e))

# ------------------------------------------------------------------- 清单
print("\n" + "=" * 72)
print("抽取清单 (耗时 %.0f 秒)" % (time.time() - t0))
print("=" * 72)
total = 0
for name, n, cols, size in MANIFEST:
    total += size
    print("%-24s %9d 行  %8.1f KB" % (name, n, size / 1024.0))
    print("    列: %s" % (cols,))
print("-" * 72)
print("合计 %.1f MB, 在 %s/ 下, 从研究环境的文件管理器下载" % (total / 1048576.0, OUT))
print("""
下一步(本地):
  1. 下载 pitdb_out/ 全部 csv
  2. 转 parquet 并落进 L0: pitdb/load/load_jq_dimensions.py
  3. 把 as_of 快照压成变更区间(dim_industry), 名称/状态已是事件形态不用压
  4. 校验: "2015-06-30 在市数" 应等于 dim_security_asof 里该年末的行数量级
""")
