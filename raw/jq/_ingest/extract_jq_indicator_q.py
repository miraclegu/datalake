"""聚宽 → get_fundamentals(indicator) 按季全市场抽取（在研究环境跑，可中断续跑）。

把整个文件粘进一个 cell 执行。**跑完请执行文末的清理 cell**，否则 Out[n] 会一直占内存。

## 为什么要这个脚本

我们已经有 `STK_FINANCIAL_INDICATOR`（raw/jq/financials/indicator.parquet），
但那是**报表原文**：`roe_this_year` 实测 = 100×累计归母净利/当期期末净资产，是**累计值**。
而策略调用的 `indicator.roe` 是**单季**值 —— 中间那步单季化是 JQ 做的，方法未知。

本地曾用四种推法去仿制，拿 494 笔真实成交做裁判，最好的一种只有 72.1% 命中：

    法A 差分分子/当期期末净资产   72.1%   ← 现用
    法D 直接差分 roe_this_year    69.8%
    法C 直接差分 roe_weighted     69.4%
    法B 差分 np_cum/equities      68.4%
    法E 扣非加权差分               7.1%   ← 据此排除扣非口径

**没有一种能到 90%。所以不能再猜，必须把 JQ 的成品值采下来。**
本脚本抽的就是 `get_fundamentals(query(indicator))`，与策略里
`get_history_fundamentals(fields=[indicator.roe, indicator.eps])` **同源同口径**。

## 设计要点（未经实测的一律不写死，probe 出来再说）

  · **按 statDate 抽，不按 date 抽**：`statDate='2016q1'` 一次拿全市场该报告期的值，
    94 个季度 × ~5000 只 ≈ 47 万行，很小。按 date 抽是日频，量级差 30 倍且没必要。
  · **必须带 pubDate**：本地靠 `pubDate <= 决策日` 复现 PIT。没有它，
    这份数据在回测里就是未来函数。脚本会 assert 它存在，缺了直接停。
  · **分页不假设上限**：用 limit/offset 翻页翻到不满页为止，
    不去猜 get_fundamentals 单次到底能返多少行。
  · **列名 probe 后再用**：pubDate/statDate 的大小写与下划线写法先打印出来确认，
    不硬编码。
  · **退市股是否包含 —— 脚本会实测并如实报告**，不假设。
    这直接决定这份数据有没有幸存者偏差。探针用 600385.XSHG（退市金泰，
    本地回测里出现过的死仓票之一）。
  · 可续跑：每年一个文件，存在即跳过。额度耗尽/中断后重跑即续。
  · Python 3.6 + 老 pandas：不用命名聚合、不用 to_parquet。

## 已知局限（写在这里，别让它变成以后的坑）

`get_fundamentals(statDate=)` 对同一 (code, statDate) 只返回**一条**记录。
若该报告期后来被重述，这里拿到的是 JQ 当前库里的那一版，
**多版本修订历史仍然拿不到**。要修订史得走 finance.STK_* 原始表（已有）。
本脚本解决的是「口径」问题，不解决「修订史」问题 —— 两者是独立的两笔账。
"""
import gc
import os
import tarfile
import time

import pandas as pd
from jqdata import *          # noqa: F401,F403  星号导入必须在模块级

OUT = 'jq_indicator_q'
PAGE = 3000                   # 保守值；真实上限未实测，靠翻页兜住
YEARS = list(range(2003, 2027))
END_QUARTER = '2026q2'        # 最后一个已披露完的报告期
MIN_FREE_MB = 200
DELISTED_PROBE = '600385.XSHG'    # 退市金泰：用它验这份数据含不含退市股

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


def quarters_upto(end):
    """生成 2003q1 .. end（含）。"""
    ey, eq = int(end[:4]), int(end[-1])
    out = []
    for y in YEARS:
        for q in (1, 2, 3, 4):
            if y > ey or (y == ey and q > eq):
                return out
            out.append('%dq%d' % (y, q))
    return out


def fetch_quarter(sd):
    """抽一个报告期的全市场 indicator，limit/offset 翻页到不满页为止。

    ⚠ get_fundamentals 的 query 是否真的支持 .offset() **未经实测**。
      若它被静默忽略，每页都会返回同一批数据，循环永不终止且数据成倍重复。
      所以这里用「本页 code 集合是否与上页完全相同」做守卫 —— 一旦命中，
      说明 offset 没生效，立即抛错停下，而不是产出一份看着正常的重复数据。"""
    frames, off, prev_codes = [], 0, None
    while True:
        df = get_fundamentals(query(indicator).limit(PAGE).offset(off), statDate=sd)
        if df is None or len(df) == 0:
            break
        codes = frozenset(df['code'])
        if prev_codes is not None and codes == prev_codes:
            raise RuntimeError(
                'offset 未生效：%s 第 %d 页与上页 code 完全相同。'
                '需改用别的分页方式（如按 code 区间切分）。' % (sd, len(frames) + 1))
        prev_codes = codes
        frames.append(df)
        off += len(df)
        if len(df) < PAGE:
            break
    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    dup = len(out) - len(set(out['code']))
    if dup:
        raise RuntimeError('%s 出现 %d 条重复 code —— 分页有问题，不落盘。' % (sd, dup))
    return out


def probe():
    """先探一个季度：确认列名、pubDate 是否存在、退市股是否包含。
    这三件事任何一件不成立，后面的 47 万行都是白抽。"""
    print('=== probe: 2019q1 ===')
    df = fetch_quarter('2019q1')
    if df is None or len(df) == 0:
        print('  ❌ 2019q1 返回空，无法继续。请确认研究环境有 get_fundamentals 权限。')
        return None
    cols = list(df.columns)
    print('  行数 %d, 列数 %d' % (len(df), len(cols)))
    print('  列名: %s' % cols)

    pub = [c for c in cols if c.lower().replace('_', '') == 'pubdate']
    stat = [c for c in cols if c.lower().replace('_', '') == 'statdate']
    if not pub:
        print('  ❌ 没有 pubDate 列 —— 本地无法做 PIT 过滤，抽了也不能用于回测。停止。')
        return None
    print('  ✓ 公告日列 = %s, 报告期列 = %s' % (pub[0], stat[0] if stat else '❌无'))

    # 退市股探针：这份数据含不含已退市的票，直接决定有没有幸存者偏差
    has = DELISTED_PROBE in set(df['code'])
    print('  退市股探针 %s: %s' % (DELISTED_PROBE, '✓ 包含' if has else '⚠ 不包含'))
    if not has:
        n_all = len(get_all_securities(types=['stock'], date=None))
        print('    ⚠ get_all_securities(date=None) 有 %d 只，本季只返 %d 只。'
              % (n_all, len(df)))
        print('    ⚠ 这份数据可能只覆盖存续股 —— 落地后必须与本地 code_map 对账，')
        print('      缺口部分不能当作"这些票当期没有财报"，那会引入幸存者偏差。')

    # 口径实测：roe 到底是单季还是累计。用茅台看同一年四期是否单调递增。
    print('\n=== 口径实测: 600519.XSHG roe 四期走势 ===')
    rows = []
    for q in ('2023q1', '2023q2', '2023q3', '2023q4'):
        d = get_fundamentals(query(indicator).filter(indicator.code == '600519.XSHG'),
                             statDate=q)
        if d is not None and len(d):
            rows.append((q, float(d['roe'].iloc[0]), float(d['eps'].iloc[0])))
    for q, r, e in rows:
        print('  %s  roe=%8.4f  eps=%8.4f' % (q, r, e))
    if len(rows) == 4:
        inc = all(rows[i][1] <= rows[i + 1][1] for i in range(3))
        print('  → roe %s（累计值应单调递增；单季值不会）'
              % ('单调递增 = 累计口径' if inc else '非单调 = 单季口径'))
        print('  参照：STK_FINANCIAL_INDICATOR 的 roe_this_year 同期为')
        print('        9.5269 / 17.9082 / 24.2765 / 34.6523（已实证是累计）')
    return pub[0]


def main():
    if not os.path.exists(OUT):
        os.makedirs(OUT)
    if probe() is None:
        return

    qs = quarters_upto(END_QUARTER)
    by_year = {}
    for q in qs:
        by_year.setdefault(int(q[:4]), []).append(q)

    print('\n=== 开始抽取: %d 个报告期, %d 个年度文件 ===' % (len(qs), len(by_year)))
    done = skipped = 0
    for y in sorted(by_year):
        path = os.path.join(OUT, 'indicator_q_%d.csv.gz' % y)
        if os.path.exists(path):
            skipped += 1
            continue
        mb = free_mb(OUT)
        if mb is not None and mb < MIN_FREE_MB:
            print('  磁盘剩余 %.0f MB < %d MB，停止。已抽 %d 年，重跑即续。'
                  % (mb, MIN_FREE_MB, done))
            return
        t0 = time.time()
        frames = []
        try:
            for q in by_year[y]:
                df = fetch_quarter(q)
                if df is not None and len(df):
                    frames.append(df)
        except Exception as e:                              # noqa: BLE001
            if is_quota_error(e):
                print('  %d: 额度受限(%s)。已抽 %d 年，明天重跑即续。' % (y, e, done))
                return
            # 不是额度问题就别谎称是 —— 明天重跑只会一模一样地失败
            print('  %d: ❌ %s: %s —— 跳过该年，继续下一年' % (y, type(e).__name__, e))
            continue
        if not frames:
            print('  %d: 无数据，跳过' % y)
            continue
        out = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
        out.to_csv(path, index=False, compression='gzip')
        print('  %d: %6d 行 -> %s (%.1f MB, %.0fs)'
              % (y, len(out), os.path.basename(path),
                 os.path.getsize(path) / 1048576.0, time.time() - t0))
        done += 1
        del frames, out
        gc.collect()

    print('\n完成: 新抽 %d 年, 跳过(已存在) %d 年' % (done, skipped))

    # 打 tar：研究环境一次只能下载一个文件，24 个年度文件逐个下载太折磨人。
    # 用 tar 不用 zip —— 里面的 .csv.gz 已经压过了，再压一遍纯浪费 CPU 和磁盘。
    tar = '%s.tar' % OUT
    with tarfile.open(tar, 'w') as tf:
        for f in sorted(os.listdir(OUT)):
            tf.add(os.path.join(OUT, f), arcname=f)
    print('已打包: %s (%.1f MB) —— 下载它' % (tar, os.path.getsize(tar) / 1048576.0))


main()

# ============================================================
# 清理 cell（跑完单独执行，否则 Out[n] 一直占内存）
# ------------------------------------------------------------
# import gc
# from IPython import get_ipython
# get_ipython().user_ns['Out'].clear()
# gc.collect()
# ============================================================
