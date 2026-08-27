"""聚宽抽取第四轮 B1：日频市场状态 + 指数补齐 + 未来日历。

把整个文件粘进一个 cell。跑完自动打成一个 tar，只需下载一个文件。
**跑完请执行文末的清理，否则 Out[n] 会一直占内存。**

━━━ 为什么这批是硬缺口 ━━━
① high_limit / low_limit（策略 133 处引用）**不能自算**：
   主板 10% / 创业板科创 20% / 北交所 30% / ST 5%，还有注册制改革切换时点、
   上市首日规则。分叉多且都是静默出错 —— 算错不报错，回测直接虚增。
② paused（66 处）从日线断档只能推出"长期停牌"，盘中临停推不出来。
③ is_st（53 处）从名称含 "ST" 只覆盖一部分，get_extras('is_st') 才是权威口径。
④ 策略实际用的基准 000015(上证红利) / 399303(国证2000) / 399101(中小板综) /
   000922(中证红利) 本地一个都没有；已有的 3 个指数行情还停在 2026-06-17。
⑤ trading_calendar 只到最后一个已过交易日，排不了未来调仓日。

━━━ 体积控制 ━━━
· 涨跌停价是稠密数据(每股每日一行), 按年切 + gzip, 单年约 8~12 MB。
· paused / is_st 是**稀疏**的 —— 只导出取值为真的行, 体积降两个数量级。
  这不是抽样, 是无损: 没出现在文件里就等于 False, 加载时补齐。
· START_YEAR 可调。默认 2005; 只回测近十年就设 2015, 体积和时间都减半。

━━━ 约束（前三轮实测出来的）━━━
· 研究环境 Python 3.6 / 老 pandas → 不用 f-string、不用命名聚合、不用 to_parquet
· 内存 800M → 按 (年 × 股票块) 双重切分, 每年落盘后立即 del + gc.collect()
· 磁盘 2G → 每年 gzip 落盘, 不在内存里堆全量
· 文件一次只能下一个 → 结尾打 tar
· from jqdata import * 必须在模块级
· 全市场票池必须 get_all_securities(date=None) —— 带 date 会漏退市股(幸存者偏差)
"""
import gc
import os
import time

import pandas as pd
from jqdata import *          # noqa: F401,F403  星号导入必须在模块级

OUT = 'pitdb_r4'
START_YEAR = 2005
END_YEAR = 2026
CHUNK = 300               # 每次 get_price 的股票数, 控制内存

SEC_LIMITS = True         # ① 涨跌停价(稠密, 最大头)
SEC_PAUSED = True         # ② 停牌(稀疏)
SEC_ST = True             # ③ is_st(稀疏)
SEC_INDEX = True          # ④ 指数成分 + 指数行情
SEC_CALENDAR = True       # ⑤ 含未来的交易日历

# 策略实际用到但本地没有的指数。前 4 个是缺的, 后面几个补行情。
NEW_INDEX_MEMBERS = [
    ('000015.XSHG', '上证红利'),
    ('399303.XSHE', '国证2000'),
    ('399101.XSHE', '中小板综'),
    ('000922.XSHG', '中证红利'),
]
INDEX_PRICES = [c for c, _ in NEW_INDEX_MEMBERS] + [
    '000300.XSHG', '000905.XSHG', '000852.XSHG', '000906.XSHG',
    '000016.XSHG', '399006.XSHE', '000688.XSHG', '399001.XSHE', '000001.XSHG',
]
QUARTER_ENDS = ['%d-%s' % (y, md) for y in range(2005, END_YEAR + 1)
                for md in ('03-31', '06-30', '09-30', '12-31')]


def log(m):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), m), flush=True)


def ensure_out():
    if not os.path.exists(OUT):
        os.makedirs(OUT)


def save_gz(name, df):
    ensure_out()
    path = os.path.join(OUT, name + '.csv.gz')
    df.to_csv(path, index=False, encoding='utf-8', compression='gzip')
    log('  → %s: %d 行, %.1f MB' % (name, len(df), os.path.getsize(path) / 1048576.0))


def save_csv(name, df):
    ensure_out()
    path = os.path.join(OUT, name + '.csv')
    df.to_csv(path, index=False, encoding='utf-8-sig')
    log('  → %s: %d 行, %.1f KB' % (name, len(df), os.path.getsize(path) / 1024.0))


# 全市场票池: date=None 才包含已退市的, 否则就是幸存者偏差
log('=' * 64)
log('票池')
log('=' * 64)
ALL = get_all_securities(types=['stock'], date=None)      # noqa: F405
CODES = sorted(ALL.index)
log('  全部股票(含已退市) %d 只' % len(CODES))
log('  其中已退市 %d 只' % int(ALL['end_date'].astype(str).lt('2262-01-01').sum()))
CHUNKS = [CODES[i:i + CHUNK] for i in range(0, len(CODES), CHUNK)]
log('  分 %d 块, 每块 <=%d 只' % (len(CHUNKS), CHUNK))


# ============================================== ① 涨跌停价（稠密, 按年落盘）
if SEC_LIMITS:
    log('=' * 64)
    log('① 涨跌停价 high_limit / low_limit  (策略 133 处引用, 不能自算)')
    log('=' * 64)
    for y in range(START_YEAR, END_YEAR + 1):
        path = os.path.join(OUT, 'limits_%d.csv.gz' % y)
        if os.path.exists(path):
            log('  %d: 已存在, 跳过' % y)
            continue
        s, e = '%d-01-01' % y, '%d-12-31' % y
        frames, n_fail = [], 0
        t0 = time.time()
        for ci, ch in enumerate(CHUNKS):
            try:
                df = get_price(ch, start_date=s, end_date=e,          # noqa: F405
                               frequency='daily',
                               fields=['high_limit', 'low_limit'],
                               skip_paused=False, fq=None, panel=False)
            except Exception as ex:                                   # noqa: BLE001
                n_fail += 1
                if n_fail <= 3:
                    log('    %d 块%d: %s: %s' % (y, ci, type(ex).__name__, str(ex)[:70]))
                continue
            if df is None or len(df) == 0:
                continue
            df = df.reset_index() if 'code' not in df.columns else df
            keep = [c for c in ('time', 'code', 'high_limit', 'low_limit') if c in df.columns]
            df = df[keep].dropna(subset=['high_limit'])
            if len(df):
                frames.append(df)
            del df
        if frames:
            allq = pd.concat(frames, ignore_index=True)
            allq.columns = ['date' if c == 'time' else c for c in allq.columns]
            allq['date'] = allq['date'].astype(str).str[:10]
            save_gz('limits_%d' % y, allq)
            log('    %d: %d 只, 耗时 %.0f 秒, 失败块 %d'
                % (y, allq['code'].nunique(), time.time() - t0, n_fail))
            del allq
        else:
            log('  %d: 无数据(该年可能无交易或全部失败, 失败块 %d)' % (y, n_fail))
        del frames
        gc.collect()


# ================================================== ② 停牌（稀疏, 只存 True）
if SEC_PAUSED:
    log('=' * 64)
    log('② 停牌 paused  (稀疏: 只导出停牌当天的行, 未出现 = 未停牌)')
    log('=' * 64)
    path = os.path.join(OUT, 'paused.csv.gz')
    if os.path.exists(path):
        log('  已存在, 跳过')
    else:
        frames = []
        for y in range(START_YEAR, END_YEAR + 1):
            s, e = '%d-01-01' % y, '%d-12-31' % y
            got = 0
            for ch in CHUNKS:
                try:
                    df = get_price(ch, start_date=s, end_date=e,       # noqa: F405
                                   frequency='daily', fields=['paused'],
                                   skip_paused=False, fq=None, panel=False)
                except Exception:                                      # noqa: BLE001
                    continue
                if df is None or len(df) == 0:
                    continue
                df = df.reset_index() if 'code' not in df.columns else df
                df = df[df['paused'] > 0]
                if len(df):
                    kc = [c for c in ('time', 'code') if c in df.columns]
                    frames.append(df[kc])
                    got += len(df)
                del df
            log('  %d: 停牌记录 %d 条' % (y, got))
            gc.collect()
        if frames:
            allp = pd.concat(frames, ignore_index=True)
            allp.columns = ['date' if c == 'time' else c for c in allp.columns]
            allp['date'] = allp['date'].astype(str).str[:10]
            save_gz('paused', allp)
            del allp
        del frames
        gc.collect()


# ================================================ ③ is_st（稀疏, 只存 True）
if SEC_ST:
    log('=' * 64)
    log('③ is_st  (稀疏: 只导出为 True 的行; 名称含 ST 只覆盖一部分, 这个才权威)')
    log('=' * 64)
    path = os.path.join(OUT, 'is_st.csv.gz')
    if os.path.exists(path):
        log('  已存在, 跳过')
    else:
        frames = []
        for y in range(START_YEAR, END_YEAR + 1):
            s, e = '%d-01-01' % y, '%d-12-31' % y
            got = 0
            for ch in CHUNKS:
                try:
                    df = get_extras('is_st', ch, start_date=s,         # noqa: F405
                                    end_date=e, df=True)
                except Exception:                                      # noqa: BLE001
                    continue
                if df is None or len(df) == 0:
                    continue
                # 宽表(行=日期, 列=代码) → 只保留 True 的 (日期, 代码)
                st = df.stack()
                st = st[st.astype(bool)]
                if len(st):
                    r = st.reset_index()
                    r.columns = ['date', 'code', 'v']
                    frames.append(r[['date', 'code']])
                    got += len(r)
                del df, st
            log('  %d: ST 记录 %d 条' % (y, got))
            gc.collect()
        if frames:
            alls = pd.concat(frames, ignore_index=True)
            alls['date'] = alls['date'].astype(str).str[:10]
            save_gz('is_st', alls)
            del alls
        del frames
        gc.collect()


# ================================================== ④ 指数成分 + 指数行情
if SEC_INDEX:
    log('=' * 64)
    log('④ 指数: 补 4 个缺失指数的成分史 + 全部基准的日线')
    log('=' * 64)
    for code, cname in NEW_INDEX_MEMBERS:
        p = os.path.join(OUT, 'index_member_%s.csv' % code.split('.')[0])
        if os.path.exists(p):
            log('  %s %s: 已存在, 跳过' % (code, cname))
            continue
        rows, n_ok, n_fail = [], 0, 0
        for d in QUARTER_ENDS:
            try:
                for sc in get_index_stocks(code, date=d):              # noqa: F405
                    rows.append({'index_code': code, 'as_of': d, 'stock_code': sc})
                n_ok += 1
            except Exception:                                          # noqa: BLE001
                n_fail += 1        # 指数发布日之前取不到, 属正常
        if rows:
            df = pd.DataFrame(rows)
            save_csv('index_member_%s' % code.split('.')[0], df)
            log('    %s %s: %d 个时点有数据, 最早 %s' % (code, cname, n_ok, df['as_of'].min()))
            del df
        else:
            log('    %s %s: 一个时点都没取到 —— 代码可能不对或无权限' % (code, cname))
        del rows
        gc.collect()

    p = os.path.join(OUT, 'index_daily.csv.gz')
    if os.path.exists(p):
        log('  指数行情: 已存在, 跳过')
    else:
        frames = []
        for code in INDEX_PRICES:
            try:
                df = get_price(code, start_date='%d-01-01' % START_YEAR,   # noqa: F405
                               end_date='%d-12-31' % END_YEAR,
                               frequency='daily',
                               fields=['open', 'high', 'low', 'close', 'volume', 'money'],
                               skip_paused=False, fq=None, panel=False)
            except Exception as ex:                                    # noqa: BLE001
                log('    %s: %s: %s' % (code, type(ex).__name__, str(ex)[:60]))
                continue
            if df is None or len(df) == 0:
                log('    %s: 无数据' % code)
                continue
            df = df.reset_index()
            df.columns = ['date'] + list(df.columns[1:])
            df['code'] = code
            frames.append(df)
            log('    %s: %d 天 (%s ~ %s)'
                % (code, len(df), str(df['date'].min())[:10], str(df['date'].max())[:10]))
        if frames:
            alli = pd.concat(frames, ignore_index=True)
            alli['date'] = alli['date'].astype(str).str[:10]
            save_gz('index_daily', alli)
            del alli
        del frames
        gc.collect()


# ====================================================== ⑤ 含未来的交易日历
if SEC_CALENDAR:
    log('=' * 64)
    log('⑤ 交易日历(含未来日, 用于排调仓日)')
    log('=' * 64)
    p = os.path.join(OUT, 'trade_days.csv')
    if os.path.exists(p):
        log('  已存在, 跳过')
    else:
        try:
            days = get_all_trade_days()                                # noqa: F405
            df = pd.DataFrame({'date': [str(d)[:10] for d in days]})
            save_csv('trade_days', df)
            log('  %s ~ %s, 共 %d 天' % (df['date'].min(), df['date'].max(), len(df)))
            del df
        except Exception as ex:                                        # noqa: BLE001
            log('  ✗ %s: %s' % (type(ex).__name__, str(ex)[:80]))


# ================================================================ 打包
import tarfile

if os.path.exists(OUT):
    files = sorted(f for f in os.listdir(OUT) if f.endswith('.csv') or f.endswith('.csv.gz'))
    if files:
        tar_name = 'pitdb_r4.tar'
        if os.path.exists(tar_name):
            os.remove(tar_name)
        with tarfile.open(tar_name, 'w') as tf:
            for f in files:
                tf.add(os.path.join(OUT, f), arcname=f)
        log('=' * 64)
        log('打包: %s (%d 个文件, %.1f MB)'
            % (tar_name, len(files), os.path.getsize(tar_name) / 1048576.0))
        log('★ 只下载这一个文件。本地解压:')
        log('    tar -xf pitdb_r4.tar -C /Users/guhao/finacial/pitdb/jqdata/')
    else:
        log('⚠ 没有任何输出文件')

print("""
跑完请执行清理(否则 Out[n] 一直占内存):
    import gc
    get_ipython().magic('reset -f out')
    gc.collect()
或直接重启内核。

时间较长(涨跌停价按 年×块 双重循环)。中断了直接重跑即可 ——
每年一个文件, 已完成的年份会自动跳过。
""")
