"""聚宽 → 股本变动事件表抽取（在研究环境跑）。

把整个文件粘进一个 cell 执行。**跑完请执行文末的清理 cell。**

## 为什么要这个（以及为什么不抽日频 valuation）

目标只有一个：**裁判「流通股本」到底谁对**。

已实测的结论链：
  · `valuation.pb_ratio` 含 **6.58% 前视**（用了当日未公告的财报）→ 本地 pb 更正确
  · `valuation.capitalization`（总股本）**tdx 更准** —— 用 balance.paidin_capital
    当裁判，不吻合的 2,062 行里本地更接近会计口径 70.6%、JQ 仅 29.4%。
    典型：宁波银行 2019-06-30 实收资本 53.7147 亿 = tdx 从 7 月起用的值，
    而 JQ 用的 52.6518 亿【在会计报表里完全不存在】。
  · `circulating_cap`（流通股本）**无法判定** —— 3.75% 的行差 >1%，基本对称，
    而两个源各自 100% 内部自洽（tdx 的 turnover 列 ↔ tdx 股本 100%；
    JQ 的 turnover_ratio ↔ JQ 的 circulating_cap 99.95%），
    资产负债表只有【总】股本、没有流通股本 —— **没有独立裁判**。

所以不抽 1300 万行的日频快照（那里面目前找不到一个该用的字段），
改抽**股份变动事件表**：约 10 万行，给出解禁/增发的权威日期与数量，
一次抽完永久可用，不像日频快照每年都要续。

## ⚠️ 表名先探测，不硬编码

我不确定聚宽这张表的确切名字。脚本会**逐个探测候选表名**，打印存在的那些
及其列名和样例，**然后才抽**。若全都不存在，会把 finance 模块下所有
名字含 SHARE/CAPITAL/STOCK 的属性列出来，供下一轮定位 ——
不猜表名，也不假装抽到了东西。

## 自带裁判用例

抽完会自动打印 **002142.XSHE（宁波银行）2019 年**的全部变动记录 ——
它是已知的分歧案例，直接看这张表里 53.7147 亿 与 52.6518 亿 哪个出现、什么时候，
就能判定 tdx 与聚宽 valuation 谁对。
"""
import os

import pandas as pd
from jqdata import *          # noqa: F401,F403  星号导入必须在模块级
from jqdata import finance

OUT = 'jq_share_change'
PAGE = 3000
JUDGE_CODE = '002142.XSHE'        # 宁波银行：已知分歧案例
JUDGE_YEAR = 2019

# 候选表名（不确定哪个存在，全部探测）
CANDIDATES = ('STK_CAPITAL_CHANGE', 'STK_SHARE_CHANGE', 'STK_CAPITAL_STRUCTURE',
              'STK_SHARE_STRUCTURE', 'STK_EQUITY_CHANGE', 'STK_SHARES_CHANGE',
              'STK_STOCK_STRUCTURE', 'STK_SHARE_LIMIT_LIFTING')

QUOTA_WORDS = ('额度不足', '额度已用', '配额', 'quota exceeded', 'quota limit',
               '超过限制', '调用次数', 'rate limit', 'too many requests')


def is_quota_error(e):
    m = ('%s %s' % (type(e).__name__, e)).lower()
    return any(w.lower() in m for w in QUOTA_WORDS)


def probe():
    """探测哪些候选表存在，返回 [(名字, 表对象, 列名)]。"""
    print('=== 探测候选表 ===')
    found = []
    for nm in CANDIDATES:
        tbl = getattr(finance, nm, None)
        if tbl is None:
            print('  %-26s 不存在（finance 模块无此属性）' % nm)
            continue
        try:
            df = finance.run_query(query(tbl).limit(1))
        except Exception as e:                              # noqa: BLE001
            print('  %-26s 属性存在但查询失败: %s' % (nm, str(e)[:70]))
            continue
        cols = list(df.columns)
        print('  %-26s ✓ 存在, %d 列' % (nm, len(cols)))
        print('      列: %s' % cols)
        found.append((nm, tbl, cols))
    if not found:
        # 一个都没命中 -> 把可能相关的属性名全列出来，供下一轮定位。
        # 不猜、也不假装抽到了东西。
        print('\n  ❌ 候选表全部不存在。finance 模块下名字含 SHARE/CAPITAL/STOCK 的属性：')
        cand = sorted(a for a in dir(finance)
                      if any(k in a.upper() for k in ('SHARE', 'CAPITAL', 'STOCK', 'EQUITY')))
        for a in cand:
            print('      %s' % a)
        print('\n  → 把上面这份清单贴回来，下一轮据此定位。')
    return found


def date_col_of(cols):
    """挑一个日期列做分页/排序键。优先【变动日期】而不是公告日 ——
    我们要的是「股本何时真的变了」。"""
    for k in ('change_date', 'end_date', 'pub_date', 'day', 'date',
              'report_date', 'lifting_date'):
        if k in cols:
            return k
    for c in cols:
        if c.endswith('_date'):
            return c
    return None


def fetch_all(nm, tbl, cols):
    """按 id 分页抽全表（这类事件表约 10 万行，一次抽完）。"""
    frames, last_id, n = [], -1, 0
    has_id = 'id' in cols
    while True:
        q = query(tbl)
        if has_id:
            q = q.filter(tbl.id > last_id).order_by(tbl.id)
        df = finance.run_query(q.limit(PAGE))
        if df is None or len(df) == 0:
            break
        frames.append(df)
        n += len(df)
        if has_id:
            last_id = int(df['id'].max())
        else:
            # 没有 id 就无法安全分页 —— 抽到一页就停并明确说明，
            # 不假装抽全了（静默截断比报错更危险）。
            print('    ⚠ 该表无 id 列，无法分页；只取到首页 %d 行，可能不完整' % len(df))
            break
        if len(df) < PAGE:
            break
        if n % 30000 == 0:
            print('    已抽 %d 行…' % n)
    return pd.concat(frames, ignore_index=True) if frames else None


def main():
    if not os.path.exists(OUT):
        os.makedirs(OUT)
    found = probe()
    if not found:
        return
    for nm, tbl, cols in found:
        print('\n' + '=' * 72)
        print('抽取 %s' % nm)
        print('=' * 72)
        try:
            df = fetch_all(nm, tbl, cols)
        except Exception as e:                              # noqa: BLE001
            if is_quota_error(e):
                print('  额度受限(%s)，明天重跑' % e)
                return
            # 不是额度问题就别谎称是
            print('  ❌ %s: %s' % (type(e).__name__, e))
            continue
        if df is None or df.empty:
            print('  返回空')
            continue
        path = os.path.join(OUT, '%s.csv.gz' % nm.lower())
        df.to_csv(path, index=False, compression='gzip')
        print('  %d 行 %d 列 -> %s (%.1f MB)'
              % (len(df), len(df.columns), os.path.basename(path),
                 os.path.getsize(path) / 1048576.0))
        dc = date_col_of(cols)
        if dc:
            print('  日期列 %s: %s ~ %s' % (dc, df[dc].min(), df[dc].max()))
        if 'code' in df.columns:
            print('  覆盖 %d 只' % df['code'].nunique())

        # ---- 自带裁判：宁波银行 2019 ----
        if 'code' in df.columns:
            sub = df[df['code'] == JUDGE_CODE]
            if dc and len(sub):
                sub = sub[pd.to_datetime(sub[dc], errors='coerce').dt.year == JUDGE_YEAR]
            print('\n  ★ 裁判用例 %s %d 年的记录（%d 条）:'
                  % (JUDGE_CODE, JUDGE_YEAR, len(sub)))
            if len(sub):
                # 只打印可能相关的列，避免几十列刷屏
                keep = [c for c in sub.columns
                        if c == 'code' or c.endswith('_date') or 'share' in c.lower()
                        or 'capital' in c.lower() or 'circul' in c.lower()
                        or 'reason' in c.lower() or 'total' in c.lower()]
                print(sub[keep or list(sub.columns)[:10]].to_string(index=False))
                print('\n  → 看 53.7147亿(=537,147万股, tdx 用的) 与 52.6518亿(JQ valuation 用的)')
                print('    哪个出现在这张表里、对应哪个日期 —— 即可判定谁对。')
            else:
                print('    （该股该年无记录）')

    # 打包下载
    import tarfile
    tar = '%s.tar' % OUT
    with tarfile.open(tar, 'w') as tf:
        for f in sorted(os.listdir(OUT)):
            tf.add(os.path.join(OUT, f), arcname=f)
    print('\n已打包 %s (%.1f MB) —— 下载它'
          % (tar, os.path.getsize(tar) / 1048576.0))


main()

# ============================================================
# 清理 cell（跑完单独执行）
# ------------------------------------------------------------
# import gc
# from IPython import get_ipython
# get_ipython().user_ns['Out'].clear()
# gc.collect()
# ============================================================
