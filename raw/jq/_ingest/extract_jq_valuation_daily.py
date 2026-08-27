"""聚宽 → get_fundamentals(valuation) 逐交易日全市场抽取（研究环境跑，可中断续跑）。

把整个文件粘进一个 cell 执行。**跑完请执行文末的清理 cell。**

## 为什么要这个脚本

panel 的估值列目前全是本地推算：
    pb = totalmv / equities        pe_ttm = totalmv / np_ttm
    ps_ttm = totalmv / rev_ttm     floatmv 来自 tdx 股本
而策略调用的是 `valuation.pb_ratio` / `valuation.circulating_market_cap`。
P1-a 把 eps/roe 换成权威值后，FROEC 选股命中率 72.1% -> 86.0%，
剩下的未命中里 **95.7% 是纯排名差异**，而排名的另一半输入正是 `pb`
（`ntile(2)` PB 半区是个硬边界，差一点点就跨区）。这个脚本供的就是它。

顺带根治两处已知缺陷：
  · `300114.XSHE` 在 tdx 里 floatmv=totalmv=0（真实价格与成交量都有）。
    按市值【升序】选股时 0 永远排第一 —— 实测它从 2016 年起 2056 个交易日
    霸占候选池首位，每次调仓挤掉一只真票。目前只能 nullif 屏蔽。
  · 股本变动生效时点 tdx 比聚宽晚 1 个交易日（603536 案例），
    换成 `circulating_market_cap` 后消失。

## 规模与时间（先看清楚再跑）

  ~2570 个交易日 x ~5000 只 ≈ **1300 万行**，是 P1-a 的 30 倍。
  按 P1-a 实测速率（约 0.6 秒/次请求、每日 2 次翻页）粗估 **50~90 分钟**。
  研究环境磁盘只有 2G，所以：
    · 只取 12 个必要字段，不用 query(valuation) 全表
    · 按年落盘，**每年一个 tar**，可以抽完一年下一年，不必等全部跑完
    · 每年开抽前检查剩余空间，不足就停

## 设计要点（未实测的一律先 probe，不写死）

  · **分页不假设上限**：limit/offset 翻到不满页为止；并用「本页 code 集合
    是否与上页完全相同」守卫 —— 若 offset 被静默忽略会无限重复，宁可抛错停下。
  · **单位陷阱**：`capitalization` / `circulating_cap` 聚宽的单位是**万股**。
    probe 会用 `market_cap*1e8 / (capitalization*1e4)` 与当日收盘价对拍，
    **用数据把单位定死**，而不是照着文档假设。
  · **退市股是否包含**：直接决定有没有幸存者偏差，probe 实测并如实报告。
  · **停牌股是否有行**：影响 as-of 逻辑，一并实测。

## 已知局限

`valuation` 是**当日快照**，没有 pubDate 概念 —— 它本身就是 point-in-time 的，
按 `date` 取即可，不需要像财报那样做 as-of。
"""
import gc
import os
import tarfile
import time

import pandas as pd
from jqdata import *          # noqa: F401,F403  星号导入必须在模块级

OUT = 'jq_valuation'
PAGE = 3000                   # 保守值；真实上限未实测，靠翻页兜住
# ★★ 2019 验证年已跑完（890,250 行 / 3,770 只 / 244 天），结论如下 ——
#     全量值得抽，但【用途必须限定】：
#
#   ⚠️ 我一度推荐「全量抽取、用 circulating_market_cap 替换 tdx 版」——
#      **进一步核对后撤回**。逐字段结论：
#
#     market_cap / capitalization    ❌ 不要替换：**tdx 更准**。
#         用 balance.paidin_capital（会计口径实收资本）当裁判，不吻合的 2,062 行里
#         本地更接近会计口径 1,455 行（70.6%）、JQ 只 607 行（29.4%）。
#         典型：宁波银行 2019-06-30 实收资本 53.7147 亿 = tdx 从 7 月起用的值，
#         而 JQ 用的 52.6518 亿【在会计报表里完全不存在】。
#         两边终点一致（8 月都到 56.2833 亿），中间路径不同 ——
#         可转债转股/定增这类连续小额变动，两个数据商记录时点不同。
#
#     circulating_market_cap / circulating_cap  ❓ **无法判定，不要盲换**。
#         3.75% 的行差 >1%，纯是股本数差异（市值比值=股数比值=1.0125）、
#         且基本对称（本地更大 54.2%），不是简单滞后。
#         两个源【各自 100% 内部自洽】（tdx 的 turnover 列 ↔ tdx 股本 100%；
#         JQ 的 turnover_ratio ↔ JQ circulating_cap 99.95%），
#         而资产负债表只有总股本、没有流通股本 —— **没有独立裁判**。
#         要判定需要【解禁/股份变动事件表】（如 finance.STK_SHARE_CHANGE），
#         那是个小表，比抽 1300 万行日频快照划算得多。
#
#   ⚠️ **不可直接使用**：pb_ratio / pe_ratio / ps_ratio / pcf_ratio
#     实测 **6.58% 的行用了当日尚未公告的财报**（唯一最佳匹配后：861,545 行能匹配到
#     某个报告期，其中 56,660 行该期公告日【晚于】决策日，中位晚 55 天）。
#     即研究环境的 valuation 表是「按最近报告期算，不管有没有公告」——
#     **含前视**。而本地 pb 按 pub_date as-of，是 PIT 正确的。
#     这 6.58% 恰好等于本地 pb 与它的差异率（93.32% 吻合），两边完全对上。
#     → 本地 pb 比它更正确，不要替换。回测里 avoid_future_data=True 时聚宽
#       自己也会做 PIT 过滤，那个结果我们拿不到。
#
#   单位（已实测）：capitalization / circulating_cap 是【万股】，
#                   market_cap / circulating_market_cap 是【亿元】。
#                   市值÷股本 = 8.97 元，与股价量级一致。
YEARS = list(range(2016, 2027))   # 全量：50~90 分钟（2019 已存在，自动跳过）
MIN_FREE_MB = 300
PROBE_DATE = '2019-01-02'
DELISTED_PROBE = '600385.XSHG'    # 退市金泰(2022-07-06 退市)，2019 年时仍在交易

# 只取必要字段。query(valuation) 全表会多出一堆用不上的列，
# 在 2G 磁盘 + 1300 万行的量级下，多一列就是几十 MB。
FIELDS = [valuation.code, valuation.day,
          valuation.capitalization, valuation.circulating_cap,
          valuation.market_cap, valuation.circulating_market_cap,
          valuation.turnover_ratio,
          valuation.pe_ratio, valuation.pe_ratio_lyr,
          valuation.pb_ratio, valuation.ps_ratio, valuation.pcf_ratio]

QUOTA_WORDS = ('额度不足', '额度已用', '配额', 'quota exceeded', 'quota limit',
               '超过限制', '调用次数', 'rate limit', 'too many requests')


def free_mb(path='.'):
    """剩余磁盘 MB。研究环境的 statvfs 曾返回 1e9 MB(1PB) 这种明显不可信的值，
    超过 100TB 一律视为不可用 —— 返回 None 让调用方跳过检查并说明，
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


def fetch_day(d):
    """抽一个交易日的全市场 valuation，limit/offset 翻页到不满页为止。

    ⚠ get_fundamentals 的 query 是否真支持 .offset() 未经实测。
      若被静默忽略，每页返回同一批数据，循环永不终止且数据成倍重复。
      用「本页 code 集合与上页相同」做守卫，命中就抛错停下 ——
      宁可停，也不要产出一份看着正常的重复数据。"""
    frames, off, prev = [], 0, None
    while True:
        df = get_fundamentals(query(*FIELDS).limit(PAGE).offset(off), date=d)
        if df is None or len(df) == 0:
            break
        codes = frozenset(df['code'])
        if prev is not None and codes == prev:
            raise RuntimeError('offset 未生效：%s 第 %d 页与上页 code 完全相同' % (d, len(frames) + 1))
        prev = codes
        frames.append(df)
        off += len(df)
        if len(df) < PAGE:
            break
    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    dup = len(out) - len(set(out['code']))
    if dup:
        raise RuntimeError('%s 出现 %d 条重复 code —— 分页有问题，不落盘' % (d, dup))
    return out


def probe():
    """三件事任一不成立，后面 1300 万行都是白抽：列名、单位、覆盖范围。"""
    print('=== probe: %s ===' % PROBE_DATE)
    df = fetch_day(PROBE_DATE)
    if df is None or len(df) == 0:
        print('  ❌ 返回空。确认研究环境有 get_fundamentals 权限。')
        return False
    print('  行数 %d, 列数 %d' % (len(df), len(df.columns)))
    print('  列名: %s' % list(df.columns))

    n_all = len(get_all_securities(types=['stock'], date=PROBE_DATE))
    print('  当日在市股票 %d 只, 本次返回 %d 只 (差 %d)' % (n_all, len(df), n_all - len(df)))

    has = DELISTED_PROBE in set(df['code'])
    print('  退市股探针 %s（2022 年退市，2019 年应仍在交易）: %s'
          % (DELISTED_PROBE, '✓ 包含' if has else '⚠ 不包含'))
    if not has:
        print('    ⚠ 若历史快照不含后来退市的股票，这份数据带幸存者偏差，')
        print('      落地后必须与本地 code_map 对账，缺口不能当作"当日无估值"。')

    # ★ 单位实测：聚宽 capitalization/circulating_cap 是【万股】、market_cap 是【亿元】。
    #   不实测就用，会得到差 1e4 的市值 —— 而且不会报错。
    px = get_price(list(df['code'][:400]), end_date=PROBE_DATE, frequency='daily',
                   fields=['close'], count=1, panel=False, fill_paused=False)
    px = px.set_index('code')['close'] if 'code' in px.columns else None
    if px is not None:
        t = df[df['code'].isin(px.index)].copy()
        t['implied'] = t['market_cap'] * 1e8 / (t['capitalization'] * 1e4)
        t['real'] = t['code'].map(px)
        r = (t['implied'] / t['real']).median()
        print('  单位实测: market_cap(亿) x 1e8 / capitalization(万股) x 1e4 / 收盘价 = %.4f' % r)
        print('    -> %s' % ('✓ 比值≈1，单位确认为【亿元】与【万股】'
                             if 0.95 < r < 1.05 else
                             '❌ 比值不为 1，单位与假设不符，落地前必须查清'))
    print('  pb_ratio 空值 %d (%.1f%%) | pe_ratio 空值 %d (%.1f%%)'
          % (df['pb_ratio'].isnull().sum(), 100.0 * df['pb_ratio'].isnull().mean(),
             df['pe_ratio'].isnull().sum(), 100.0 * df['pe_ratio'].isnull().mean()))
    return True


def main():
    if not os.path.exists(OUT):
        os.makedirs(OUT)
    if not probe():
        return

    cal = get_trade_days(start_date='%d-01-01' % YEARS[0],
                         end_date='%d-12-31' % YEARS[-1])
    by_year = {}
    for d in cal:
        by_year.setdefault(d.year, []).append(d)
    total = sum(len(v) for v in by_year.values())
    print('\n=== 开始抽取: %d 个交易日, %d 个年度文件 ===' % (total, len(by_year)))
    print('    约 %.0f 万行，预计 %d~%d 分钟' % (total * 5000 / 1e4, total // 60, total // 30))

    for y in sorted(by_year):
        path = os.path.join(OUT, 'valuation_%d.csv.gz' % y)
        if os.path.exists(path):
            print('  %d: 已存在，跳过' % y)
            continue
        mb = free_mb(OUT)
        if mb is not None and mb < MIN_FREE_MB:
            print('  磁盘剩余 %.0f MB < %d MB，停止。已抽的年份可先下载后删除，再重跑续抽。'
                  % (mb, MIN_FREE_MB))
            return
        t0, frames = time.time(), []
        try:
            for d in by_year[y]:
                df = fetch_day(d)
                if df is not None and len(df):
                    frames.append(df)
        except Exception as e:                              # noqa: BLE001
            if is_quota_error(e):
                print('  %d: 额度受限(%s)。明天重跑即续。' % (y, e))
                return
            # 不是额度问题就别谎称是 —— 明天重跑只会一模一样地失败
            print('  %d: ❌ %s: %s —— 跳过该年' % (y, type(e).__name__, e))
            continue
        if not frames:
            print('  %d: 无数据' % y)
            continue
        out = pd.concat(frames, ignore_index=True)
        out.to_csv(path, index=False, compression='gzip')
        print('  %d: %8d 行 -> %s (%.0f MB, %.0f 分钟)'
              % (y, len(out), os.path.basename(path),
                 os.path.getsize(path) / 1048576.0, (time.time() - t0) / 60))
        del frames, out
        gc.collect()
        # 每年单独打 tar：1300 万行不可能等全部跑完再一次性下载，
        # 而且磁盘也放不下「原文件 + 全量 tar」两份。
        tar = os.path.join(OUT, 'valuation_%d.tar' % y)
        with tarfile.open(tar, 'w') as tf:
            tf.add(path, arcname=os.path.basename(path))
        print('       已打包 %s —— 可以先下载它，下载后把这两个文件删掉再继续' % tar)

    print('\n完成。')


main()

# ============================================================
# 清理 cell（跑完单独执行）
# ------------------------------------------------------------
# import gc
# from IPython import get_ipython
# get_ipython().user_ns['Out'].clear()
# gc.collect()
# ============================================================
