"""聚宽抽取第二轮：指数成分史 / 财务快照 / ETF+北交所维度。

把整个文件粘进一个 cell。跑完自动打成一个 tar，只需下载一个文件。
**跑完请执行清理（见文末），否则 Out[n] 会一直占内存。**

三段独立，可用下面的开关分次跑（额度或时间不够时分批）：

  ① SEC_INDEX     指数成分历史 —— get_index_stocks(code, date=) 按季抽
                  现有 idx_weight_month 只有 1600 行且不完整(整数过滤 bug)。
                  实测 get_index_stocks 可回溯到 2005，无类型陷阱、无分页问题。

  ② SEC_FINSNAP   财务快照 —— 聚宽只存最新版本，重述历史造不出来，
                  但**从现在开始每季度快照一次**就能积累版本历史（原理同 P0 每日快照）。
                  只抽最近 12 个报告期的核心字段，几万行，很轻。

  ③ SEC_UNIVERSE  ETF 与北交所的 PIT 维度。
                  实测结论(2026-08-25): types 有效值为 etf(1611) / fund(2189) /
                  lof(404) / index(554) / stock(5211); fja/fjb 为 0(分级基金已退出历史);
                  open_fund 是场外基金(26103, .OF 后缀), 本库不需要, 只探测不抽。
                  **北交所确认不可得**: stock 列表后缀只有 XSHE(2897)+XSHG(2314)=5211,
                  无 4/8 开头、无其他后缀 —— 该免费账号没有北交所。

约束（都是第一轮实测出来的）:
  · finance.run_query 单次 5000 行 → 分页
  · 研究环境 Python 3.6 / 老 pandas → 不用命名聚合、不用 to_parquet
  · 内存 800M → 每段抽完立即落盘 + del + gc.collect()
  · 文件一次只能下一个 → 结尾打 tar
  · from jqdata import * 必须在模块级
"""
import gc
import os
import time

import pandas as pd
from jqdata import *          # noqa: F401,F403  星号导入必须在模块级
from jqdata import finance

OUT = 'pitdb_r2'
PAGE = 5000

SEC_INDEX = True
SEC_FINSNAP = True
SEC_UNIVERSE = True

# 指数成分: 只抽做回测真正会用到的宽基。上证综指(000001)成分=全部沪市A股, 无意义, 不抽。
INDEXES = [
    ('000300.XSHG', '沪深300'),
    ('000905.XSHG', '中证500'),
    ('000852.XSHG', '中证1000'),
    ('000906.XSHG', '中证800'),
    ('000016.XSHG', '上证50'),
    ('399006.XSHE', '创业板指'),
    ('000688.XSHG', '科创50'),
]
QUARTER_ENDS = ['%d-%s' % (y, md) for y in range(2005, 2027)
                for md in ('03-31', '06-30', '09-30', '12-31')]

# 财务快照: 只要核心字段与最近 12 个报告期(重述基本都发生在近几年)
SNAP_PERIODS = 12
SNAP_FIELDS = ('code', 'report_date', 'pub_date', 'report_type', 'source',
               'total_operating_revenue', 'net_profit', 'np_parent_company_owners',
               'basic_eps')

TODAY = None          # 由 get_trade_days 推出, 避免依赖 datetime.now


def log(m):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), m), flush=True)


def save(name, df):
    if not os.path.exists(OUT):
        os.makedirs(OUT)
    path = os.path.join(OUT, name + '.csv')
    df.to_csv(path, index=False, encoding='utf-8-sig')
    log('  → %s: %d 行, %.1f KB' % (name, len(df), os.path.getsize(path) / 1024.0))


# ============================================================ ① 指数成分历史
if SEC_INDEX:
    log('=' * 60)
    log('① 指数成分历史 (get_index_stocks 按季)')
    log('=' * 60)
    for code, cname in INDEXES:
        path = os.path.join(OUT, 'index_member_%s.csv' % code.split('.')[0])
        if os.path.exists(path):
            log('  %s %s: 已存在, 跳过' % (code, cname))
            continue
        rows, n_ok, n_fail = [], 0, 0
        for d in QUARTER_ENDS:
            try:
                for s in get_index_stocks(code, date=d):        # noqa: F405
                    rows.append({'index_code': code, 'as_of': d, 'stock_code': s})
                n_ok += 1
            except Exception as e:                               # noqa: BLE001
                # 指数发布日之前必然取不到, 属正常; 打印但不中断
                n_fail += 1
                if n_fail <= 2:
                    log('    %s %s: %s: %s' % (code, d, type(e).__name__, str(e)[:60]))
        if rows:
            df = pd.DataFrame(rows)
            save('index_member_%s' % code.split('.')[0], df)
            log('  %s %s: %d 个时点有数据 / %d 个失败, 最早 %s'
                % (code, cname, n_ok, n_fail, df['as_of'].min()))
            del df
        else:
            log('  %s %s: 一个时点都没取到 —— 需人工检查代码是否正确' % (code, cname))
        del rows
        gc.collect()

# ============================================================ ② 财务快照
if SEC_FINSNAP:
    log('=' * 60)
    log('② 财务快照 (为将来识别重述而留底)')
    log('=' * 60)
    try:
        # 用交易日历推"今天", 不依赖 datetime
        tds = get_trade_days(count=1)                            # noqa: F405
        TODAY = str(tds[-1])[:10]
    except Exception as e:                                       # noqa: BLE001
        log('  取交易日失败(%s), 快照标签用 unknown' % type(e).__name__)
        TODAY = 'unknown'
    log('  快照日: %s' % TODAY)

    path = os.path.join(OUT, 'fin_snapshot_%s.csv' % TODAY)
    if os.path.exists(path):
        log('  已存在, 跳过')
    else:
        # 报告期**按算术生成**, 不用查询去推。
        # 原来的写法是 order_by(report_date desc).limit(5000) 再取 distinct ——
        # 但每个报告期本身就有约 5000 行, 5000 行上限只够覆盖 1~2 个期,
        # 所以只拿到 2 个而不是 12 个。用查询推元数据正好撞上那条硬约束。
        y0, m0 = int(TODAY[:4]), int(TODAY[5:7])
        periods = []
        y, q = y0, (m0 - 1) // 3          # 当前所在季(0-3), 从上一个已结束的季往回数
        while len(periods) < SNAP_PERIODS:
            q -= 1
            if q < 0:
                q, y = 3, y - 1
            periods.append('%d-%s' % (y, ('03-31', '06-30', '09-30', '12-31')[q]))
        log('  覆盖报告期: %s' % periods)
        frames = []
        for rp in periods:
            last = -1
            while True:
                try:
                    df = finance.run_query(
                        query(finance.STK_INCOME_STATEMENT)       # noqa: F405
                        .filter(finance.STK_INCOME_STATEMENT.report_date == rp,
                                finance.STK_INCOME_STATEMENT.report_type == 0,
                                finance.STK_INCOME_STATEMENT.id > last)
                        .order_by(finance.STK_INCOME_STATEMENT.id).limit(PAGE))
                except Exception as e:                            # noqa: BLE001
                    log('  %s 抽取失败: %s: %s' % (rp, type(e).__name__, str(e)[:70]))
                    break
                if len(df) == 0:
                    break
                keep = [c for c in SNAP_FIELDS if c in df.columns]
                frames.append(df[keep])
                last = int(df['id'].max()) if 'id' in df.columns else last + PAGE
                if len(df) < PAGE:
                    break
        if frames:
            snap = pd.concat(frames, ignore_index=True)
            snap['snapshot_date'] = TODAY
            save('fin_snapshot_%s' % TODAY, snap)
            del snap
        else:
            log('  ⚠ 一行都没抽到')
        del frames
        gc.collect()

# ==================================================== ③ ETF / 北交所 维度
if SEC_UNIVERSE:
    log('=' * 60)
    log('③ ETF 与北交所维度 (先探测 types 取值与代码格式)')
    log('=' * 60)
    # 探测: 聚宽的 types 到底接受哪些值。
    # open_fund 是场外基金(26103 只, .OF 后缀), 与本库无关 —— 只探测不抽取。
    # 第一轮误抽了 168,540 行 / 23 MB, 占打包体积七成。
    CAND = ['etf', 'fund', 'lof', 'fja', 'fjb', 'open_fund', 'index', 'stock']
    SKIP_EXTRACT = ('stock', 'open_fund')      # stock 第一轮已抽; open_fund 不需要
    ok_types = []
    for t in CAND:
        try:
            df = get_all_securities(types=[t], date='2026-08-20')  # noqa: F405
            log('  types=[%-10s] → %5d 只  样例 %s' % (t, len(df), list(df.index)[:2]))
            if len(df):
                ok_types.append(t)
        except Exception as e:                                    # noqa: BLE001
            log('  types=[%-10s] → ❌ %s: %s' % (t, type(e).__name__, str(e)[:50]))

    # 北交所: 在 stock 列表里找非 XSHE/XSHG 后缀, 以及 4/8 开头的代码
    try:
        st = get_all_securities(types=['stock'], date='2026-08-20')  # noqa: F405
        codes = list(st.index)
        sfx = {}
        for c in codes:
            s = c.split('.')[-1]
            sfx[s] = sfx.get(s, 0) + 1
        log('  stock 列表的后缀分布: %s' % sfx)
        bj = [c for c in codes if c[:1] in ('4', '8') or c.split('.')[-1] not in ('XSHE', 'XSHG')]
        log('  疑似北交所(4/8 开头或非沪深后缀): %d 只, 样例 %s' % (len(bj), sorted(bj)[:5]))
        if not bj:
            log('  → 确认: 聚宽 types=[\'stock\'] **不含北交所**。'
                '需另找 types 取值或该账号无北交所权限')
    except Exception as e:                                        # noqa: BLE001
        log('  ❌ %s: %s' % (type(e).__name__, e))

    # 对能用的 type 抽年末快照(与第一轮 stock 的做法一致)
    for t in ok_types:
        if t in SKIP_EXTRACT:
            log('  %s: 跳过抽取(%s)' % (t, '第一轮已抽' if t == 'stock' else '场外基金, 本库不需要'))
            continue
        path = os.path.join(OUT, 'universe_%s.csv' % t)
        if os.path.exists(path):
            log('  %s: 已存在, 跳过' % t)
            continue
        rows = []
        for y in range(2005, 2027):
            d = '%d-12-31' % y
            try:
                df = get_all_securities(types=[t], date=d)         # noqa: F405
                df = df.reset_index()
                df.columns = ['code'] + list(df.columns[1:])
                df['as_of'] = d
                rows.append(df)
            except Exception as e:                                 # noqa: BLE001
                log('    %s %s: %s' % (t, d, type(e).__name__))
        if rows:
            allr = pd.concat(rows, ignore_index=True)
            save('universe_%s' % t, allr)
            log('  %s: %d 行, 去重 %d 只' % (t, len(allr), allr['code'].nunique()))
            del allr
        del rows
        gc.collect()

# ================================================================ 打包
import tarfile

if os.path.exists(OUT):
    files = sorted(f for f in os.listdir(OUT) if f.endswith('.csv'))
    if files:
        tar_name = 'pitdb_r2.tar'
        if os.path.exists(tar_name):
            os.remove(tar_name)
        with tarfile.open(tar_name, 'w') as tf:
            for f in files:
                tf.add(os.path.join(OUT, f), arcname=f)
        log('=' * 60)
        log('打包: %s (%d 个文件, %.1f MB)'
            % (tar_name, len(files), os.path.getsize(tar_name) / 1048576.0))
        log('★ 只下载这一个文件。本地解压:')
        log('    tar -xf pitdb_r2.tar -C /Users/guhao/finacial/pitdb/jqdata/')
    else:
        log('⚠ 没有任何输出文件')

print("""
跑完请执行清理(否则 Out[n] 一直占内存):
    import gc
    get_ipython().magic('reset -f out')
    gc.collect()
或直接重启内核。
""")
