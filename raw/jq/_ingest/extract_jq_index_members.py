"""主要指数成分股历史 —— 全面抽取（替代/扩展 extract_jq_round2.py 的 ① 段）。

把整个文件粘进聚宽研究环境的一个 cell 跑完，下载结尾打包的
`index_members.tar`，解到 `datalake/raw/jq/_ingest/downloads/`，再跑
`python3 datalake/build/load_jq_round2.py` 即可并入
`std/index_member.parquet` / `index_member_asof.parquet`。

## 与 extract_jq_round2.py ① 段的关系：**这个取代它**

round2 抽 7 个宽基、**季频**（一年 4 个点）。本脚本覆盖面更广，且采样按
指数性质分两档（见下）。产出文件同名（`index_member_<6位>.csv`），
所以下载解压时会**覆盖**旧的季频文件 —— 那是有意的，不是冲突。
★ round2 的 ②③ 段（财务快照 / ETF 维度）不受影响，照旧单独跑。

## 🔴 采样频率按【指数怎么变】来定，不是一刀切

    定期调整型（沪深300/中证500/红利/风格…）
        成分是【精选子集】，每年 6、12 月定调，期间只有退市/并购等临时调整
        -> **月频**足够（一年 12 个点，比原来的季频密 3 倍）

    连续纳入型（中小板综 399101 / 创业板综 399102）
        新股一上市就进指数，成分【天天在变】
        -> **周频**（一年 ~53 个点）

## 🔴 刻意【不抽】三个"全市场"综合指数，因为它们可以推导

    000001.XSHG 上证指数   = 全部沪市A股
    399106.XSHE 深证综指   = 全部深市A股
    000985.XSHG 中证全指   ≈ 全市场

它们的成分能从 `std/security_universe.parquet` 的
`code 前缀 + list_date/delist_date` 直接推出来，抽它们等于把同一份信息
存两遍（round2 文件头对上证指数已经写过这条判断）。而且体量极大：
中证全指 ~5000 只 × 1105 个周频点 ≈ 550 万行，撞研究环境
"内存 800M"那条限制（round2 文件头实测记录）。
★ 反过来，399101 中小板综【不可推导】—— 用户实测探针显示它是
  002 前缀 + 部分 003 前缀（2020-12-31: 961+29，2026-06-30: 919+39），
  不是任何单一代码段的全集。这正是必须抽它的理由。

## 内存与断点续采

每个指数一个 `_done_<code>.txt` 进度文件（一行一个已完成的 as_of）+
CSV **追加写**。所以：

  · 内存占用与【单个采样点】成正比，不随总行数增长
    （不是"读回整个 CSV 再 concat" —— 那在百万行上是 O(n²) 且吃内存）
  · 中途超时/掐额度，直接【重新整段粘贴执行】，跳过已完成的点继续
  · 不会产生重复行（已完成的点不会重采）

## 🔴 研究环境的约束（`extract_jq_round2.py` 文件头同款，别再破一次）

    Python 3.6 + 老 pandas / 内存 800M / 文件一次只能下一个 /
    `from jqdata import *` 必须在模块级

★ **本文件一行 pandas 都不用**（周/月分桶走标准库 `date.isocalendar()`）。
  第一版用了 `Series.dt.isocalendar()` —— pandas **1.1+** 才有，实测在研究
  环境当场 `AttributeError`，两个周频指数整段失败。而我本地是新版 pandas，
  五个场景测试全绿 —— **测试环境不等于目标环境，那样的测试等于没测**。
  现在的测试把 `pandas` 换成"一访问就抛"的假模块，脚本照跑才算过。

## 额度与体量（本地用真实交易日历 + 各指数标称成分数算过，不是估的）

    全量  7,785 次 get_index_stocks 调用 / 约 404 万行 / CSV 约 121 MB
    其中两个周频的综合指数就占一半多：
        中小板综 1105 点 x ~950 只 = 105 万行
        创业板综  854 点 x ~1300 只 = 111 万行
    其余 26 个月频指数合计约 190 万行，最大的是国证2000（40 万行）

若额度/时间紧张，把 `ONLY` 填成想先抽的那几个代码
（如 `ONLY = ['399101.XSHE']`），分几次跑完 —— 进度文件保证互不干扰。

★ 结尾打的是 **tar.gz**：CSV 全是重复的代码字符串，压缩比很高
  （121 MB 的 CSV 压完通常只有几 MB），研究环境"一次只能下一个文件"
  那条限制下这很关键。
"""
import datetime
import os
import tarfile
import time

from jqdata import *          # noqa: F401,F403  星号导入必须在模块级

OUT = 'pitdb_idx'
START = '2005-01-01'
ONLY = []                 # 空 = 全抽；填代码列表 = 只抽这几个（分批用）

# (聚宽代码, 名称, 采样频率, 为什么抽它)
#   'W' 周频（连续纳入型）  'M' 月频（定期调整型）
# 🔴 这些代码没有在本地逐个验证过 —— 聚宽不认的会在运行时报出来并跳过，
#    跑完看结尾的「取不到」清单，把它告诉我再修正。
INDEXES = [
    # ---- 连续纳入型：周频 ----
    ('399101.XSHE', '中小板综', 'W', '002+部分003，不可从代码段推导'),
    ('399102.XSHE', '创业板综', 'W', '创业板全收录，新股连续纳入'),

    # ---- 沪深核心宽基：月频 ----
    ('000300.XSHG', '沪深300', 'M', ''),
    ('000905.XSHG', '中证500', 'M', ''),
    ('000852.XSHG', '中证1000', 'M', ''),
    ('000906.XSHG', '中证800', 'M', ''),
    ('000016.XSHG', '上证50', 'M', ''),
    ('000903.XSHG', '中证100', 'M', ''),
    ('000010.XSHG', '上证180', 'M', ''),
    ('000009.XSHG', '上证380', 'M', ''),
    ('000688.XSHG', '科创50', 'M', ''),

    # ---- 深市宽基：月频 ----
    ('399001.XSHE', '深证成指', 'M', ''),
    ('399330.XSHE', '深证100', 'M', ''),
    ('399005.XSHE', '中小板指', 'M', '与 399101 中小板【综】是两回事'),
    ('399006.XSHE', '创业板指', 'M', ''),
    ('399673.XSHE', '创业板50', 'M', ''),

    # ---- 小微盘（本项目小市值策略的基准候选）：月频 ----
    ('399303.XSHE', '国证2000', 'M', 'CLAUDE.md 记：中证2000 本地无点位，用它替代'),
    ('932000.CSI',  '中证2000', 'M', '后缀存疑(.CSI/.XSHG)，取不到就看结尾清单'),
    ('399316.XSHE', '巨潮小盘', 'M', ''),
    ('399314.XSHE', '巨潮大盘', 'M', ''),
    ('399315.XSHE', '巨潮中盘', 'M', ''),
    ('399634.XSHE', '中小等权', 'M', 'CLAUDE.md 的 BENCHMARKS 里有它'),

    # ---- 红利 / 风格（红利策略线要用）：月频 ----
    ('000922.XSHG', '中证红利', 'M', ''),
    ('000015.XSHG', '上证红利', 'M', ''),
    ('399324.XSHE', '深证红利', 'M', ''),
    ('000918.XSHG', '300成长', 'M', ''),
    ('000919.XSHG', '300价值', 'M', ''),
    ('000925.XSHG', '基本面50', 'M', ''),
]


def log(m):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), m), flush=True)


def ensure_out():
    if not os.path.exists(OUT):
        os.makedirs(OUT)


def short(code):
    return code.split('.')[0]


def paths(code):
    ensure_out()
    return (os.path.join(OUT, 'index_member_%s.csv' % short(code)),
            os.path.join(OUT, '_done_%s.txt' % short(code)))


def period_ends(days, freq):
    """每个自然周 / 自然月的最后一个【交易日】。

    ★ 用交易日而不是自然日的月末：`get_index_stocks(date=非交易日)` 未必
      报错，但落在账上的 as_of 就成了一个不存在的交易日，与下游按交易日
      对齐的逻辑错位，而那不报错。

    🔴 **只用标准库，一行 pandas 都不用。** 第一版写的是
      `s.dt.isocalendar()` —— 那是 **pandas 1.1+** 才有的 API，而研究环境是
      Python 3.6 + 老 pandas（`extract_jq_round2.py` 文件头第一条约束就写着
      这个）。实测当场 `AttributeError: 'DatetimeProperties' object has no
      attribute 'isocalendar'`，两个周频指数整段失败。
    ★ `date.isocalendar()` 是**标准库**方法，Python 2.3 起就有 —— 换成它之后
      这个文件不再依赖 pandas 的任何版本特性。
    """
    out = {}
    for d in days:
        ds = str(d)[:10]
        dt = datetime.date(int(ds[:4]), int(ds[5:7]), int(ds[8:10]))
        if freq == 'W':
            iso_y, iso_w, _ = dt.isocalendar()
            key = '%04d-W%02d' % (iso_y, iso_w)
        else:
            key = ds[:7]
        if key not in out or ds > out[key]:
            out[key] = ds
    return sorted(out.values())


def load_done(done_path):
    if not os.path.exists(done_path):
        return set()
    with open(done_path) as f:
        return set(x.strip() for x in f if x.strip())


def grab(code, name, freq, all_days):
    csv_path, done_path = paths(code)
    points = period_ends(all_days, freq)
    done = load_done(done_path)
    todo = [d for d in points if d not in done]
    if not todo:
        log('  %s %s: %d 个点已全部采过，跳过' % (code, name, len(points)))
        return 'skip', 0, 0, 0, None

    log('  %s %s: %s频 %d 个点，已完成 %d，本次采 %d'
        % (code, name, '周' if freq == 'W' else '月',
           len(points), len(done), len(todo)))
    n_ok, n_empty, n_fail, first_err, first_day = 0, 0, 0, None, None
    need_header = not os.path.exists(csv_path)
    for i, d in enumerate(todo, 1):
        try:
            stocks = get_index_stocks(code, date=d)          # noqa: F405
        except Exception as e:                               # noqa: BLE001
            n_fail += 1
            if first_err is None:
                first_err = '%s: %s' % (type(e).__name__, str(e)[:100])
            # 指数发布日之前取不到属正常；但【全程都取不到】说明代码错了，
            # 这两种情况在结尾的汇总里区分开。
            continue
        # 🔴 **空结果不算成功**（本项目纪律：空结果一律当失败）。
        #   实测第一轮日志里科创50（2020-07 才发布）报「261 个点成功、
        #   取不到 0」—— 而已有的季频文件只有 2020-09 起的 26 个时点。
        #   也就是说 `get_index_stocks` 对发布日之前的日期**返回空 list
        #   而不是抛异常**，被记成了成功。那个计数在说谎：它让"指数发布前
        #   本来就没有成分"和"代码写对了、真取到了数据"看起来一模一样。
        #   ★ 空点仍然写进度（那是它的正确答案，重跑不该再花一次额度），
        #     但单独计数，并在汇总里报出**第一个有数据的日期**。
        if not stocks:
            n_empty += 1
            with open(done_path, 'a') as f:
                f.write(d + '\n')
            continue
        if first_day is None:
            first_day = d
        # 🔴 **追加写**，不读回整个 CSV 再 concat —— 后者在百万行上是
        #   O(n²) 且会把内存顶爆（研究环境 800M）。
        with open(csv_path, 'a') as f:
            if need_header:
                f.write('index_code,as_of,stock_code\n')
                need_header = False
            for st in stocks:
                f.write('%s,%s,%s\n' % (code, d, st))
        # 进度落盘要在数据落盘【之后】：反过来的话中途崩溃会把没写成的点
        # 记成已完成，那一个点就永久缺了，而且不报错。
        with open(done_path, 'a') as f:
            f.write(d + '\n')
        n_ok += 1
        if i % 100 == 0 or i == len(todo):
            log('    进度 %d/%d（有数据 %d，空 %d，异常 %d）'
                % (i, len(todo), n_ok, n_empty, n_fail))
    if n_ok == 0:
        # 区分两种"没数据"：抛异常 vs 一直返回空。前者多半是代码写错了，
        # 后者可能是代码错、也可能是这个指数本地确实查不到成分。
        if n_fail:
            log('    ⚠ 一个点都没取到（%d 个异常）—— 多半是代码不对：%s'
                % (n_fail, first_err))
        else:
            log('    ⚠ 一个点都没取到（%d 个点全部返回空，接口不报错）'
                % n_empty)
        return 'dead', 0, n_empty, n_fail, None
    return 'ok', n_ok, n_empty, n_fail, first_day


def main():
    all_days = [str(d) for d in get_trade_days(start_date=START)]   # noqa: F405
    log('交易日历 %d 天：%s ~ %s' % (len(all_days), all_days[0], all_days[-1]))

    targets = [x for x in INDEXES if not ONLY or x[0] in ONLY]
    log('本次目标 %d 个指数' % len(targets))
    dead, summary = [], []
    for code, name, freq, _why in targets:
        try:
            st, n_ok, n_empty, n_fail, first_day = grab(code, name, freq, all_days)
        except Exception as e:                               # noqa: BLE001
            log('  %s %s: 整段失败 %s: %s' % (code, name, type(e).__name__, e))
            dead.append('%s %s（整段异常）' % (code, name))
            continue
        if st == 'dead':
            dead.append('%s %s' % (code, name))
        else:
            summary.append((code, name, n_ok, n_empty, n_fail, first_day))

    log('=' * 60)
    log('汇总')
    log('=' * 60)
    for code, name, n_ok, n_empty, n_fail, first_day in summary:
        p = paths(code)[0]
        sz = os.path.getsize(p) / 1024.0 / 1024.0 if os.path.exists(p) else 0
        # 🔴 报**第一个有数据的日期** —— 它一眼看出"这个指数的成分史其实
        #   从哪年才有"，而那正是空结果被当成成功时被掩盖掉的信息。
        log('  %-14s %-10s 本次有数据 %4d 点%s，%.1f MB%s'
            % (code, name, n_ok,
               ('，空 %d' % n_empty) if n_empty else '',
               sz, ('，最早 %s' % first_day) if first_day else ''))
    if dead:
        log('')
        log('  🔴 完全取不到（代码可能不对，请把这几行告诉我）：')
        for x in dead:
            log('      %s' % x)

    # 🔴 gz 压缩：CSV 里全是重复的代码字符串，压缩比很高。
    #   研究环境一次只能下载一个文件，而未压缩的 CSV 合计 ~121 MB。
    tar_path = os.path.join(OUT, 'index_members.tar.gz')
    with tarfile.open(tar_path, 'w:gz') as tar:
        for f in sorted(os.listdir(OUT)):
            if f.startswith('index_member_') and f.endswith('.csv'):
                tar.add(os.path.join(OUT, f), arcname=f)
    log('')
    log('打包好了：%s（%.1f MB）'
        % (tar_path, os.path.getsize(tar_path) / 1024.0 / 1024.0))
    log('下载它 -> 解到 datalake/raw/jq/_ingest/downloads/ -> '
        '跑 python3 datalake/build/load_jq_round2.py')


main()
