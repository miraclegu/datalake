"""聚宽抽取第三轮：分红送配 STK_XR_XD（+ 业绩预告 / 陆股通，可选）。

把整个文件粘进一个 cell。跑完自动打成一个 tar，只需下载一个文件。
**跑完请执行文末的清理，否则 Out[n] 会一直占内存。**

━━━ 为什么必须抽这张表（不能用通达信 gbbq 顶替）━━━
本地 tdx 的 raw_gbbq category=1 有 68,312 条分红记录（已验证 c1=每10股派现税前、
date=除权日）。但红利策略的可见性锚点是 **board_plan_pub_date（董事会预案公告日）**：

    df = _query_dividends(stock_list, finance.STK_XR_XD.board_plan_pub_date, time0, time1, ...)

预案公告 → 除权通常隔 1~2 个月。拿除权日当可见日，等于「该买入的时候还看不到分红」，
是反向未来函数，选股时点整体后移一个季度。此外 gbbq 还缺三样东西：
  · plan_progress          —— 方案进度，策略用它去重（同一方案预案/股东大会/实施三条记录）
  · bonus_cancel_pub_date  —— 已取消/终止的分红，必须剔除，gbbq 里根本不记
  · report_date            —— 分红归属会计年度，_dividend_by_fiscal_year 靠它汇总
所以 gbbq 只能算「已实施的历史现金流」，做不了 PIT 股息率。

━━━ 约束（前两轮实测出来的，别再踩）━━━
  · finance.run_query 单次 5000 行 → 必须按 id 分页
  · from jqdata import * 必须在模块级（函数内是 SyntaxError）
  · 研究环境 Python 3.6 / 老 pandas → 不用命名聚合、不用 to_parquet
  · 内存 800M → 每段落盘后 del + gc.collect()
  · 文件一次只能下一个 → 结尾打 tar
  · 列名必须在抽取前探测，**不要把 AttributeError 当成额度耗尽**（第一轮踩过）
"""
import gc
import os
import time

import pandas as pd
from jqdata import *          # noqa: F401,F403  星号导入必须在模块级
from jqdata import finance

OUT = 'pitdb_r3'
PAGE = 5000

SEC_XRXD = True        # ★ 核心：分红送配，153 处引用
SEC_FORCAST = True     # 业绩预告，1 处引用，很轻
SEC_HKHOLD = False     # 陆股通持股，日频 × 3000 只，很大；默认关

# 红利策略实际用到的字段（按引用次数排序）+ 少数补齐字段。
# 不用 select 挑列而是整表取，因为 finance.run_query 的列筛选写法在老版本上不稳；
# 取回来再裁列，代价只是网络流量。
XRXD_MUST_HAVE = [
    'code', 'report_date',
    'board_plan_pub_date',      # ★ 预案公告日 —— 可见性锚点，缺了整个策略没法复现
    'a_registration_date',      # 股权登记日 —— rolling365 口径用
    'bonus_amount_rmb',         # 派现总额(万元)
    'plan_progress',            # 方案进度 —— 去重用
    'bonus_type',
]
# 2026-08-25 实测: STK_XR_XD 共 47 列, 下面这些是核对过真实存在的名字。
# 上一版把除权日写成 exdividend_date(聚宽无此列), 被 keep 逻辑静默丢弃 —— 真名是 a_xr_date。
XRXD_NICE_TO_HAVE = [
    'bonus_cancel_pub_date', 'shareholders_plan_pub_date', 'implementation_pub_date',
    'bonus_ratio_rmb', 'at_bonus_ratio_rmb',        # 税前 / 税后 每股派现
    'dividend_ratio', 'transfer_ratio',             # 送股比例 / 转增比例
    'dividend_number', 'transfer_number',
    'a_xr_date',              # ★ A股除权日 —— 与 tdx raw_gbbq 交叉验证的连接键
    'a_bonus_date',           # 派息日
    'plan_progress_code',     # ★ 数字进度码。策略用文本 plan_progress 做 map(STAGE_ORDER),
                              #   文本有变体就静默变 NaN->0, 去重失效且不报错; 数字码稳
    'company_id', 'id',
]


def log(m):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), m), flush=True)


def save(name, df):
    if not os.path.exists(OUT):
        os.makedirs(OUT)
    path = os.path.join(OUT, name + '.csv')
    df.to_csv(path, index=False, encoding='utf-8-sig')
    log('  → %s: %d 行, %.1f KB' % (name, len(df), os.path.getsize(path) / 1024.0))


def probe(table, name, must_have):
    """先取 1 行看列名。缺必需列就**立刻停**，不要抽完几十页才发现字段名不对。"""
    df = finance.run_query(query(table).limit(1))        # noqa: F405
    cols = list(df.columns)
    log('  %s 实际列(%d): %s' % (name, len(cols), cols))
    missing = [c for c in must_have if c not in cols]
    if missing:
        log('  ❌ 缺必需列: %s' % missing)
        log('     这是**字段名对不上**, 不是额度问题, 重跑不会好。')
        log('     请把上面那行实际列贴回来, 改 XRXD_MUST_HAVE 后再跑。')
        return None
    log('  ✓ 必需列齐全')
    return cols


def page_by_id(table, name, keep_cols):
    """按 id 升序全表分页。id 是主键单调, 不会漏也不会重。

    不按年份切片: board_plan_pub_date 早期记录大量为空, 按它切会静默漏掉这批。
    """
    frames, last, page_no, total = [], -1, 0, 0
    while True:
        try:
            df = finance.run_query(
                query(table).filter(table.id > last)     # noqa: F405
                .order_by(table.id).limit(PAGE))
        except Exception as e:                            # noqa: BLE001
            log('  ✗ 第 %d 页失败: %s: %s' % (page_no + 1, type(e).__name__, str(e)[:90]))
            log('    已累计 %d 行, 先保存已取到的部分。' % total)
            break
        if len(df) == 0:
            break
        page_no += 1
        total += len(df)
        frames.append(df[[c for c in keep_cols if c in df.columns]])
        last = int(df['id'].max())
        if page_no % 10 == 0:
            log('    第 %d 页, 累计 %d 行 (id 到 %d)' % (page_no, total, last))
        if len(df) < PAGE:
            break
    if not frames:
        log('  ⚠ %s 一行都没抽到' % name)
        return None
    out = pd.concat(frames, ignore_index=True)
    del frames
    gc.collect()
    return out


# ================================================== ① 分红送配 STK_XR_XD
if SEC_XRXD:
    log('=' * 64)
    log('① 分红送配 finance.STK_XR_XD  (红利策略命脉, 153 处引用)')
    log('=' * 64)
    path = os.path.join(OUT, 'stk_xr_xd.csv')
    if os.path.exists(path):
        log('  已存在, 跳过')
    else:
        cols = probe(finance.STK_XR_XD, 'STK_XR_XD', XRXD_MUST_HAVE)
        if cols:
            keep = [c for c in XRXD_MUST_HAVE + XRXD_NICE_TO_HAVE if c in cols]
            log('  保留 %d 列, 开始分页(每页 %d)' % (len(keep), PAGE))
            df = page_by_id(finance.STK_XR_XD, 'STK_XR_XD', keep)
            if df is not None:
                save('stk_xr_xd', df)
                # 抽完立即自检: 这三条不过就说明数据不能用于 PIT 回测
                n = len(df)
                n_pub = int(df['board_plan_pub_date'].notna().sum())
                n_amt = int((df['bonus_amount_rmb'].fillna(0) > 0).sum())
                log('  自检:')
                log('    总行数              %d' % n)
                log('    有预案公告日        %d (%.1f%%)' % (n_pub, 100.0 * n_pub / n))
                log('    有派现金额>0        %d (%.1f%%)' % (n_amt, 100.0 * n_amt / n))
                log('    覆盖股票            %d 只' % df['code'].nunique())
                if 'report_date' in df.columns:
                    log('    report_date 区间    %s ~ %s'
                        % (df['report_date'].min(), df['report_date'].max()))
                if 'plan_progress' in df.columns:
                    vc = df['plan_progress'].value_counts()
                    log('    plan_progress 取值  %s' % dict(vc.head(8)))
                if 'a_xr_date' in df.columns:
                    n_xr = int(df['a_xr_date'].notna().sum())
                    log('    有A股除权日        %d (%.1f%%)  <- 与 tdx raw_gbbq 对账用'
                        % (n_xr, 100.0 * n_xr / n))
                if n_pub < 0.5 * n:
                    log('    ⚠ 预案公告日缺失过半 —— 早期记录可能没有, '
                        '本地要用 a_registration_date 回退, 并在文档里写清哪段年份不可用')
                del df
                gc.collect()

# ================================================== ② 业绩预告
if SEC_FORCAST:
    log('=' * 64)
    log('② 业绩预告 finance.STK_FIN_FORCAST')
    log('=' * 64)
    path = os.path.join(OUT, 'stk_fin_forcast.csv')
    if os.path.exists(path):
        log('  已存在, 跳过')
    else:
        try:
            d0 = finance.run_query(query(finance.STK_FIN_FORCAST).limit(1))   # noqa: F405
            log('  实际列(%d): %s' % (len(d0.columns), list(d0.columns)))
            df = page_by_id(finance.STK_FIN_FORCAST, 'STK_FIN_FORCAST', list(d0.columns))
            if df is not None:
                save('stk_fin_forcast', df)
                log('  覆盖 %d 只' % df['code'].nunique())
                del df
                gc.collect()
        except Exception as e:                            # noqa: BLE001
            log('  ✗ %s: %s' % (type(e).__name__, str(e)[:90]))

# ================================================== ③ 陆股通持股(默认关)
if SEC_HKHOLD:
    log('=' * 64)
    log('③ 陆股通持股 finance.STK_HK_HOLD_INFO  (日频 × 3000 只, 很大)')
    log('=' * 64)
    path = os.path.join(OUT, 'stk_hk_hold.csv')
    if os.path.exists(path):
        log('  已存在, 跳过')
    else:
        try:
            d0 = finance.run_query(query(finance.STK_HK_HOLD_INFO).limit(1))  # noqa: F405
            log('  实际列(%d): %s' % (len(d0.columns), list(d0.columns)))
            df = page_by_id(finance.STK_HK_HOLD_INFO, 'STK_HK_HOLD_INFO', list(d0.columns))
            if df is not None:
                save('stk_hk_hold', df)
                del df
                gc.collect()
        except Exception as e:                            # noqa: BLE001
            log('  ✗ %s: %s' % (type(e).__name__, str(e)[:90]))

# ================================================================ 打包
import tarfile

if os.path.exists(OUT):
    files = sorted(f for f in os.listdir(OUT) if f.endswith('.csv'))
    if files:
        tar_name = 'pitdb_r3.tar'
        if os.path.exists(tar_name):
            os.remove(tar_name)
        with tarfile.open(tar_name, 'w') as tf:
            for f in files:
                tf.add(os.path.join(OUT, f), arcname=f)
        log('=' * 64)
        log('打包: %s (%d 个文件, %.1f MB)'
            % (tar_name, len(files), os.path.getsize(tar_name) / 1048576.0))
        log('★ 只下载这一个文件。本地解压:')
        log('    tar -xf pitdb_r3.tar -C /Users/guhao/finacial/pitdb/jqdata/')
    else:
        log('⚠ 没有任何输出文件')

print("""
跑完请执行清理(否则 Out[n] 一直占内存):
    import gc
    get_ipython().magic('reset -f out')
    gc.collect()
或直接重启内核。
""")
