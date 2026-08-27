"""聚宽 → 财务报表抽取（在研究环境跑，可中断续跑）。

把整个文件粘进一个 cell。**跑完请执行清理 cell（见文末），否则 Out[n] 会一直占内存。**

抽 4 张表 × 2003~2026，按年落盘：
    STK_INCOME_STATEMENT      利润表
    STK_BALANCE_SHEET         资产负债表
    STK_CASHFLOW_STATEMENT    现金流量表
    STK_FINANCIAL_INDICATOR   财务指标

全部约束都是实测出来的，不是猜的：
  · **单次 run_query 上限 5000 行** → 按 id 分页
  · **必须 report_type = 0（合并报表）**。实测同一 (code, report_date) 有 0/1 两条、
    pub_date 相同：平安银行 2015 年报合并营收 961.6 亿 / 母公司 734.1 亿，净利差 10%。
    不加过滤会静默取错。运行时会先探测该表有无此列。
  · **起点 2003**：实测 pub_date 占位率（pub_date <= report_date）1989-2002 为
    0.38~1.00，2003 起为 0.00。早期的公告日是假的，用了会有未来函数。
  · **内存 800M**：每年抽完立即落盘 + del + gc.collect()，峰值只有单年 ~2 万行。
  · **磁盘 2G**：按年 gzip（约 200 MB 总量）。每年前检查剩余空间，不足就停。
  · **可续跑**：每年一个文件，存在即跳过。额度耗尽/中断后重跑即续。
  · Python 3.6 + 老 pandas：不用命名聚合、不用 to_parquet。

.csv.gz 在文件预览器里会报 "not UTF-8 encoded" —— 那只是不能**预览**，**下载正常**。
财务数据太大必须压缩。下载后在本地解压转 parquet。
"""
import gc
import os
import time

import pandas as pd
from jqdata import *          # noqa: F401,F403  星号导入必须在模块级
from jqdata import finance

OUT = 'pitdb_fin'
PAGE = 5000
YEARS = list(range(2003, 2027))
MIN_FREE_MB = 200             # 剩余空间低于此值就停, 不把磁盘撑爆

# 打包: 研究环境一次只能下载一个文件, 96 个年度文件逐个下载太折磨人。
# 所以抽完打成一个 tar —— 用 tar 不用 zip, 因为里面的 .csv.gz 已经压过了,
# 再压一遍纯浪费 CPU 和磁盘(而且磁盘只有 2G)。
# PACK_ONLY 为空元组 = 打包全部; 指定前缀 = 只打包这些表(已经下载过的就别再打包了)。
PACK_ONLY = ('indicator',)    # ← 已有 income/balance/cashflow 的话就只打这个

# (标签, 表, 报告期列名, 是否启用)
# 报告期列名各表不同 —— 实测 STK_FINANCIAL_INDICATOR 没有 report_date, 用 end_date。
# 硬编码 report_date 会撞 AttributeError, 所以这里显式声明, 并在探测阶段校验存在。
#
# indicator 已启用: ROE/EPS/BPS 这些是**会计惯例**而非策略选择, 不存在"参数被冻结"
# 的问题, 用现成的合理。唯一遗留风险是它**没有 report_type**, 合并/母公司口径不明 ——
# 所以 load 阶段会拿 indicator.net_profit_this_year 与 income.net_profit(已确认
# report_type=0 合并) 逐条对账, 用数据把口径定死, 而不是靠假设。
TABLES = [
    ('income',    finance.STK_INCOME_STATEMENT,    'report_date', True),
    ('balance',   finance.STK_BALANCE_SHEET,       'report_date', True),
    ('cashflow',  finance.STK_CASHFLOW_STATEMENT,  'report_date', True),
    ('indicator', finance.STK_FINANCIAL_INDICATOR, 'end_date',    True),
]

# 额度/权限类错误的特征词。命中就立即停止(继续跑只是白耗)，其余错误只跳过当年。
# 只有命中这些明确信号才判定为额度问题并停止。其余错误一律"记录+继续下一年",
# **绝不谎称是额度问题** —— 把确定性错误报成"明天重跑即续"是误导性错误信息,
# 明天重跑只会一模一样地失败。
QUOTA_WORDS = ('额度不足', '额度已用', '配额', 'quota exceeded', 'quota limit',
               '超过限制', '调用次数', 'rate limit', 'too many requests')


def free_mb(path='.'):
    """剩余磁盘 MB。研究环境的 statvfs 曾返回 1e9 MB(1PB) 这种明显不可信的值,
    所以超过 100 TB 一律视为不可用 —— 返回 None 让调用方跳过检查并说明,
    而不是假装检查通过。假的校验比没有校验更危险。"""
    try:
        st = os.statvfs(path)
        mb = st.f_bavail * st.f_frsize / 1048576.0
    except Exception:                                       # noqa: BLE001
        return None
    return None if mb > 100 * 1024 * 1024 else mb


def is_quota_error(e):
    m = ('%s %s' % (type(e).__name__, e)).lower()
    return any(w.lower() in m for w in QUOTA_WORDS)


def probe_columns(tbl, label, date_col):
    """探一行, 拿到列名, 顺便确认有没有 report_type。"""
    df = finance.run_query(query(tbl).limit(1))
    cols = list(df.columns)
    has_rt = 'report_type' in cols
    ok = date_col in cols
    print('  %s: %d 列, 报告期列 %s=%s, report_type=%s'
          % (label, len(cols), date_col, '有' if ok else '❌无',
             '有' if has_rt else '无(不过滤)'))
    if not ok:
        # schema 错误在这里就拦住, 不要等到查询时撞 AttributeError
        print('    ❌ 缺报告期列 %s, 跳过该表。实际的日期类列: %s'
              % (date_col, [c for c in cols if c.endswith('_date')]))
        return None, False
    if not has_rt:
        print('    ⚠ 该表无 report_type, 无法区分合并/母公司报表 —— 落盘后需人工确认口径')
    return cols, has_rt


def fetch_year(tbl, has_rt, year, date_col):
    """抽一年, 按 id 分页。返回 DataFrame。"""
    lo, hi = '%d-01-01' % year, '%d-12-31' % year
    frames, last_id, n_page = [], -1, 0
    while True:
        dcol = getattr(tbl, date_col)          # 报告期列名各表不同, 见 TABLES
        conds = [dcol >= lo, dcol <= hi, tbl.id > last_id]
        if has_rt:
            conds.append(tbl.report_type == 0)      # ★ 合并报表, 不加就取错
        df = finance.run_query(query(tbl).filter(*conds).order_by(tbl.id).limit(PAGE))
        if len(df) == 0:
            break
        frames.append(df)
        last_id = int(df['id'].max())
        n_page += 1
        if len(df) < PAGE:
            break
        if n_page > 200:
            raise RuntimeError('%d 年分页超过 200 页, 分页逻辑可能有问题' % year)
    if not frames:
        return pd.DataFrame(), 0
    out = pd.concat(frames, ignore_index=True)
    return out, n_page


def main():
    if not os.path.exists(OUT):
        os.makedirs(OUT)
    print('=' * 72)
    print('聚宽财务报表抽取  (可中断续跑; 已存在的年份文件会跳过)')
    _f = free_mb()
    print('剩余磁盘: %s' % ('%.0f MB' % _f if _f is not None else
                            '检测不可用(statvfs 返回值不可信), 磁盘检查已禁用'))
    print('=' * 72)

    print('\n探测各表列结构:')
    meta = {}
    for label, tbl, date_col, enabled in TABLES:
        if not enabled:
            print('  %s: 已禁用(见 TABLES 注释), 跳过' % label)
            meta[label] = (None, False)
            continue
        try:
            meta[label] = probe_columns(tbl, label, date_col)
        except Exception as e:                                  # noqa: BLE001
            print('  %s: ❌ %s: %s' % (label, type(e).__name__, e))
            if is_quota_error(e):
                print('\n🛑 额度类错误, 停止。明天重跑即可续。')
                return
            meta[label] = (None, False)

    t0 = time.time()
    n_q, done, failed, stopped = 0, [], [], False

    for label, tbl, date_col, enabled in TABLES:
        cols, has_rt = meta.get(label, (None, False))
        if cols is None:
            print('\n[%s] 跳过' % label)
            continue
        print('\n[%s] %s' % (label, '=' * 60))
        for y in YEARS:
            path = os.path.join(OUT, '%s_%d.csv.gz' % (label, y))
            if os.path.exists(path):
                print('  %d: 已存在, 跳过 (%.1f MB)' % (y, os.path.getsize(path) / 1048576.0))
                continue
            _f = free_mb()
            if _f is not None and _f < MIN_FREE_MB:
                print('  🛑 剩余磁盘仅 %.0f MB (<%d), 停止。'
                      '请下载并删除已抽文件后重跑。' % (_f, MIN_FREE_MB))
                stopped = True
                break
            try:
                df, pages = fetch_year(tbl, has_rt, y, date_col)
                n_q += pages
            except Exception as e:                              # noqa: BLE001
                print('  %d: ❌ %s: %s' % (y, type(e).__name__, e))
                if is_quota_error(e):
                    print('\n🛑 额度耗尽, 停止。已完成的年份不会重抽, 明天重跑即续。')
                    stopped = True
                    break
                failed.append((label, y, str(e)[:80]))
                continue
            if len(df) == 0:
                print('  %d: 无数据' % y)
                continue
            # 抽完立即落盘, 立即释放 —— 这是 800M 内存下的关键
            df.to_csv(path, index=False, compression='gzip', encoding='utf-8')
            sz = os.path.getsize(path) / 1048576.0
            # 顺手做一条 PIT 自检: pub_date 应晚于 report_date
            try:
                bad = (pd.to_datetime(df['pub_date'], errors='coerce') <=
                       pd.to_datetime(df[date_col], errors='coerce')).sum()
            except Exception:                                   # noqa: BLE001
                bad = -1
            print('  %d: %6d 行 / %d 页 → %.1f MB   pub_date 占位 %s'
                  % (y, len(df), pages, sz,
                     ('%d 条 ⚠' % bad) if bad > 0 else ('0 ✓' if bad == 0 else '未检')))
            done.append((label, y, len(df), sz))
            del df
            gc.collect()
        if stopped:
            break

    tot = sum(os.path.getsize(os.path.join(OUT, f)) for f in os.listdir(OUT))
    print('\n' + '=' * 72)
    print('本次: 新抽 %d 个年度文件, 约 %d 次查询, 耗时 %.0f 秒'
          % (len(done), n_q, time.time() - t0))
    _f = free_mb()
    print('目录合计 %.1f MB, 剩余磁盘 %s' % (tot / 1048576.0,
          ('%.0f MB' % _f) if _f is not None else '未知'))
    if failed:
        print('\n失败的年份(非额度原因, 需人工看):')
        for label, y, msg in failed:
            print('  %s %d: %s' % (label, y, msg))
    # 进度总览
    print('\n进度:')
    for label, _t, _d, _e in TABLES:
        have = sorted(int(f.split('_')[1].split('.')[0])
                      for f in os.listdir(OUT) if f.startswith(label + '_'))
        miss = [y for y in YEARS if y not in have]
        print('  %-10s 已有 %2d/%d 年%s' % (label, len(have), len(YEARS),
              ('  缺: %s' % miss) if miss else '  ✓ 完整'))
    # ------------------------------------------------------------------ 打包
    import tarfile
    want = [f for f in sorted(os.listdir(OUT))
            if f.endswith('.csv.gz') and (not PACK_ONLY or f.startswith(PACK_ONLY))]
    if not want:
        print('\n打包: 没有匹配 PACK_ONLY=%s 的文件, 跳过' % (PACK_ONLY,))
    else:
        tar_name = 'pitdb_fin_%s.tar' % ('_'.join(PACK_ONLY) if PACK_ONLY else 'all')
        src_mb = sum(os.path.getsize(os.path.join(OUT, f)) for f in want) / 1048576.0
        _f = free_mb()
        if _f is not None and _f < src_mb + 100:
            print('\n打包: 剩余磁盘 %.0f MB 不足(需 ~%.0f MB), 跳过打包。'
                  '请先下载删除部分文件。' % (_f, src_mb + 100))
        else:
            if os.path.exists(tar_name):
                os.remove(tar_name)
            with tarfile.open(tar_name, 'w') as tf:      # 'w' = 不再压缩
                for f in want:
                    tf.add(os.path.join(OUT, f), arcname=f)
            tmb = os.path.getsize(tar_name) / 1048576.0
            print('\n' + '=' * 72)
            print('打包完成: %s' % tar_name)
            print('  %d 个文件 → 1 个 tar, %.1f MB' % (len(want), tmb))
            print('  ★ 只下载这一个文件即可。本地解压:')
            print('      tar -xf %s -C /Users/guhao/finacial/pitdb/jqdata/' % tar_name)
            print('  解压后直接跑: python pitdb/load/load_jq_financials.py')

    print('=' * 72)
    print("""
跑完请务必执行清理(否则 Out[n] 一直占内存):
    import gc
    get_ipython().magic('reset -f out')
    gc.collect()
或直接重启内核。

下载: pitdb_fin/ 下的 .csv.gz 直接下载(不要点预览, 二进制预览会报 not UTF-8)。
下载完可删掉已下载的年份文件腾空间, 重跑会跳过已存在的、继续抽没抽的。
""")


main()
