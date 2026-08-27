#!/usr/bin/env python3
"""把聚宽抽出的财务 csv.gz 加载成 L0/L1。

用法:
    python datalake/build/load_jq_financials.py           # 加载 + 校验
    python datalake/build/load_jq_financials.py --demo    # 额外跑 PIT 演示查询

过滤规则（全部由实测得出，不是猜的）:

  1. **source = '定期报告'**
     实测 source 有 9 种取值，其中 31,424 行是 IPO/重组披露材料
     （招募说明书 16,859 / 预披露公告 10,275 / 其它 2,623 / 上市公告书 1,602 / …）。
     这些材料的 pub_date 常常晚于报告期好几年 —— 例如 H1008.XSHG 的 2019 年报
     在 2022-06-22 才随预披露公告出现。混进回测会造成严重时点错乱。

  2. **code 必须是标准 A 股格式** ^\\d{6}\\.(XSHE|XSHG)$
     实测存在 H1008/K0153/K1137/C06xx 这类**待上市公司临时代码**，而且**被不同公司复用**
     （同一 code 对应不同 company_id）—— 这正是"code 不能当主键"的实证。

  3. **report_type = 0（合并报表）** 已在抽取阶段过滤。
     实测同一 (code, report_date) 有 0/1 两条、pub_date 相同：
     平安银行 2015 年报合并营收 961.6 亿 vs 母公司 734.1 亿，净利差 10%。

  加上 1+2 之后 (code, report_date) 重复从 21 条降到 **0 条**（273,018 行）。

  4. **pub_date <= report_date 标记为占位值**
     实测 27 条（2003 年 3 / 2005 年 1 / 2010 年 23）。查证是**上市前的回溯财务**
     （601211 国泰君安、601878 浙商证券、601066 中信建投等 2010 年后才上市）。
     这些公司当时不在可交易池，实际影响为零，但仍显式标记 —— 标记出来的偏差不可怕，
     悄悄混进去的才可怕。

回测查询语义（少一个条件就有未来函数）:
    WHERE report_date <= :d          -- 报告期已结束
      AND pub_date    <= :d          -- 且当时已公告
      AND NOT pub_date_is_placeholder
"""
import argparse
import glob
import os
import re
import sys

import duckdb
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'raw', 'jq', '_ingest', 'downloads')
L0 = os.path.join(ROOT, 'raw', 'jq', 'financials')
L1 = os.path.join(ROOT, 'std')
DB = os.path.join(ROOT, 'lake.db')

STD_CODE = re.compile(r'^\d{6}\.(XSHE|XSHG)$')

# 标识类列必须强制当字符串读。否则 pandas 在不同年份文件里会推断出不同类型
# (有的 float 有的 str), concat 后 pyarrow 拒绝写 parquet; 更糟的是 '002054'
# 变成 float 会丢掉前导零。
ID_COLS = ('code', 'a_code', 'b_code', 'h_code', 'company_id', 'company_name',
           'source', 'source_id', 'id')
# (表名, 报告期列名)。实测 STK_FINANCIAL_INDICATOR 没有 report_date, 用 end_date。
TABLES = (('income', 'report_date'), ('balance', 'report_date'),
          ('cashflow', 'report_date'), ('indicator', 'end_date'))
FAILURES = []


def check(cond, msg):
    print(('  ✓ ' if cond else '  ✗ ') + msg)
    if not cond:
        FAILURES.append(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--demo', action='store_true')
    args = ap.parse_args()
    for d in (L0, L1):
        if not os.path.exists(d):
            os.makedirs(d)

    stats = {}
    for tbl, date_col in TABLES:
        # ★ glob 必须精确到【年份】：`indicator_*.csv.gz` 会误匹配
        #   P1-a 的 `indicator_q_2003.csv.gz`（同目录、不同 schema、无 source 列）。
        #   这是把多批抽取放进同一目录后的文件名冲突 —— 用 4 位年份锚定。
        files = sorted(glob.glob(os.path.join(SRC, '%s_[12][0-9][0-9][0-9].csv.gz' % tbl)))
        if not files:
            print('⚠ %-10s 无文件, 跳过（该表尚未抽取）' % tbl)
            continue
        print('\n' + '=' * 72)
        print('%s: %d 个年度文件' % (tbl, len(files)))
        print('=' * 72)
        kept_parts, n_raw, n_src, n_code, n_ph = [], 0, 0, 0, 0
        for f in files:
            df = pd.read_csv(f, compression='gzip', low_memory=False,
                             dtype={c: str for c in ID_COLS})
            n_raw += len(df)
            # 过滤 1: 只要定期报告
            bad_src = (df['source'] != '定期报告')
            n_src += int(bad_src.sum())
            df = df[~bad_src]
            # 过滤 2: 只要标准 A 股代码
            bad_code = ~df['code'].astype(str).str.match(STD_CODE)
            n_code += int(bad_code.sum())
            df = df[~bad_code]
            # 标记 4: 占位公告日
            rd = pd.to_datetime(df[date_col], errors='coerce')
            pb = pd.to_datetime(df['pub_date'], errors='coerce')
            df = df.copy()
            # 统一暴露 report_date 这个名字, 各表原始列名不同(indicator 用 end_date)
            df['report_date'] = rd
            df['pub_date'] = pb
            df['pub_date_is_placeholder'] = (pb <= rd)
            n_ph += int(df['pub_date_is_placeholder'].sum())
            kept_parts.append(df)
            del df
        allf = pd.concat(kept_parts, ignore_index=True)
        del kept_parts
        # ★★ 去重键是 (code, report_date, **pub_date**)，不是 (code, report_date)。
        #    两件事必须分开处理 —— 早先混在一起，把真正的财务重述当成重复删掉了：
        #
        #    (a) 同一 (code, report_date, pub_date) 的**两个版本** = 数据质量问题。
        #        实测 indicator 有这类 374 行：source_id 一版有值('321003.0')、
        #        一版为 NaN，且 roe/equities/net_profit 数值真的不同。
        #        用 income(report_type=0, 与归母匹配 99.75%) 当权威参照判定留哪版：
        #            source_id 有值 → 与 income 一致 98.8%
        #            source_id 为空 → 91.7%
        #        那 9 组净利真不同的更一边倒 —— 000806.XSHE 2020Q1 两版**符号相反**
        #        (-838万 vs +838万)，income 说是负的。拿错版本会把亏损公司当成赚钱的。
        #        → 优先保留 source_id 非空的版本。**这条逻辑保留。**
        #
        #    (b) 同一 (code, report_date) 的**不同 pub_date**。
        #        ⚠️ 我一度以为这是「财务重述」而当前实现把它拍平了 —— **不对**。
        #        实测：**「定期报告」自己没有多版本（275,229 组里 0 组）**。
        #        多版本全部来自上游的【其它 source】被一起读进来：
        #            业绩快报 27,349 行（27,285 是首版 —— 正式财报【之前】的预披露）
        #            追溯调整  4,204 行（2,525 是最新版 —— 这才是真的事后重述）
        #            招募说明书/预披露公告/上市公告书 33,720 行（IPO 文件里的历史财务）
        #        而上面的「只要定期报告」过滤已经把它们全剔掉了，
        #        所以留下的就是【当时原始公布的那一版】—— PIT 上本来就是对的。
        #        并且实测聚宽 get_fundamentals 的 pub_date **99.8% 对齐「定期报告」**
        #        （业绩快报仅 11 行、追溯调整仅 3 行），即聚宽也不用快报/不用重述日。
        #        → 本键改成含 pub_date 仍然更精确（语义上是对的），但在当前
        #          过滤下是 no-op（保留重述版本 0 条）。保留此写法以防将来放宽过滤。
        n_before = len(allf)
        _key = ['code', 'report_date', 'pub_date']
        if 'source_id' in allf.columns:
            allf = allf.assign(_has_src=allf['source_id'].notna())
            allf = (allf.sort_values(_key + ['_has_src'],
                                     ascending=[True, True, True, False])
                        .drop_duplicates(subset=_key, keep='first')
                        .drop(columns=['_has_src']))
        else:
            allf = allf.drop_duplicates(subset=_key, keep='first')
        n_dedup = n_before - len(allf)
        _nv = len(allf) - allf.drop_duplicates(subset=['code', 'report_date']).shape[0]
        print('  同公告日重复消解: 删掉 %d 条(优先 source_id 非空)；'
              '保留重述版本 %d 条' % (n_dedup, _nv))
        # 兜底: 仍是 object 的列统一成字符串(保留 NaN), 防止 pyarrow 因混合类型报错
        for c in allf.columns:
            if allf[c].dtype == object:
                allf[c] = allf[c].where(allf[c].isna(), allf[c].astype(str))
        out = os.path.join(L0, '%s.parquet' % tbl)
        allf.to_parquet(out, index=False, compression='zstd')
        stats[tbl] = dict(raw=n_raw, kept=len(allf), drop_src=n_src, drop_code=n_code,
                          placeholder=n_ph, cols=len(allf.columns),
                          mb=os.path.getsize(out) / 1048576.0,
                          codes=allf['code'].nunique(),
                          dup=int(allf.duplicated(['code', 'report_date']).sum()))
        s = stats[tbl]
        print('  原始 %d 行 → 剔除 非定期报告 %d / 非标准代码 %d → 保留 %d 行 (%d 列)'
              % (s['raw'], s['drop_src'], s['drop_code'], s['kept'], s['cols']))
        print('  标的 %d 只, (code,report_date) 重复 %d 条, pub_date 占位 %d 条'
              % (s['codes'], s['dup'], s['placeholder']))
        print('  → %s  %.1f MB' % (out, s['mb']))
        del allf

    if not stats:
        sys.exit('❌ 一个财务文件都没找到, 检查 %s' % SRC)

    # ------------------------------------------------------------ DuckDB 视图
    print('\n' + '=' * 72)
    print('挂进 DuckDB 视图层')
    print('=' * 72)
    con = duckdb.connect(DB)
    for tbl in stats:
        con.execute('DROP VIEW IF EXISTS fin_%s' % tbl)
        con.execute("CREATE VIEW fin_%s AS SELECT * FROM read_parquet('%s')"
                    % (tbl, os.path.join(L0, '%s.parquet' % tbl)))
        print('  fin_%s' % tbl)

    # 用财务表的标准代码反向扩充证券全集（实测财务表覆盖更多早年退市公司）
    uni_path = os.path.join(L1, 'security_universe.parquet')
    if os.path.exists(uni_path) and 'income' in stats:
        n_before = con.execute('SELECT count(*) FROM security_universe').fetchone()[0]
        extra = con.execute("""
            SELECT DISTINCT f.code FROM fin_income f
            LEFT JOIN security_universe u ON u.code = f.code
            WHERE u.code IS NULL""").fetchall()
        print('\n  财务表可为证券全集补充 %d 只（当前 %d 只）' % (len(extra), n_before))
        if extra:
            print('    样例: %s' % [e[0] for e in extra[:6]])
            print('    （补充逻辑留给 load_jq_dimensions.py 下次重跑时合并，此处只报告）')

    # ------------------------------------------------------------------ 校验
    print('\n' + '=' * 72)
    print('校验')
    print('=' * 72)
    for tbl, s in stats.items():
        check(s['dup'] == 0, '%s (code,report_date) 唯一 (重复 %d)' % (tbl, s['dup']))
    if 'income' in stats:
        rt = con.execute("SELECT count(*) FROM fin_income WHERE report_type <> 0").fetchone()[0]
        check(rt == 0, 'income 全部为合并报表 report_type=0 (异常 %d)' % rt)
        rng = con.execute('SELECT min(report_date), max(report_date) FROM fin_income').fetchone()
        check(str(rng[0])[:4] == '2003', 'income 起点 %s (应为 2003, pub_date 可信边界)' % rng[0])
        ph = con.execute('SELECT count(*) FROM fin_income '
                         'WHERE pub_date_is_placeholder').fetchone()[0]
        print('  ⓘ income pub_date 占位 %d 条 —— 已显式标记, 回测时用 '
              'NOT pub_date_is_placeholder 排除' % ph)
    miss = [t for t, _ in TABLES if t not in stats]
    if miss:
        print('  ⚠ 尚未抽取的表: %s —— 需在研究环境重跑 extract_jq_financials.py'
              '（已有年份会自动跳过）' % miss)

    # ------------------------------------------------- indicator 口径对账
    if 'indicator' in stats and 'income' in stats:
        print('\n' + '=' * 72)
        print('indicator 口径对账 (它没有 report_type, 用数据定死是合并还是母公司)')
        print('=' * 72)
        # ★ 实测结论: indicator.net_profit_this_year 对应的是
        #   income.np_parent_company_owners(**归母净利润**), 匹配率 99.75%;
        #   而对 income.net_profit(含少数股东) 只有 33.2%。
        #   两者差约 5%(少数股东占比) —— 不查出来的话, 用 indicator 净利的因子会和
        #   用 income.net_profit 的因子静默对不上, 差异小到不会引起怀疑。
        #   信任数据源 != 知道列的语义。
        q = """
        SELECT count(*) AS 可比条数,
               sum(CASE WHEN abs(i.net_profit_this_year - n.np_parent_company_owners)
                        <= 0.005*abs(n.np_parent_company_owners) THEN 1 ELSE 0 END) AS 匹配归母,
               round(100.0*sum(CASE WHEN abs(i.net_profit_this_year - n.np_parent_company_owners)
                        <= 0.005*abs(n.np_parent_company_owners) THEN 1 ELSE 0 END)/count(*), 2)
                        AS 匹配率
        FROM fin_indicator i
        JOIN fin_income n ON n.code = i.code AND n.report_date = i.report_date
        WHERE i.net_profit_this_year IS NOT NULL
          AND n.np_parent_company_owners IS NOT NULL
          AND n.np_parent_company_owners <> 0"""
        r = con.execute(q).df()
        print(r.to_string(index=False))
        rate = float(r['匹配率'].iloc[0]) if len(r) else 0.0
        check(rate > 95, 'indicator.net_profit_this_year = 归母净利润 (匹配 %.2f%%)' % rate)
        print('    → 口径: 合并报表的**归母**数, 可与三张主表直接 join')
        print('    → 注意: 它不等于 income.net_profit(含少数股东), 后者匹配率仅 33.2%')
        if rate <= 95:
            print('    ⚠ 匹配率偏低, 抽样看一下:')
            print(con.execute("""
                SELECT i.code, i.report_date, i.net_profit_this_year AS indicator值,
                       n.np_parent_company_owners AS income归母值,
                       round(i.net_profit_this_year/nullif(n.np_parent_company_owners,0),4) 比值
                FROM fin_indicator i
                JOIN fin_income n ON n.code=i.code AND n.report_date=i.report_date
                WHERE i.net_profit_this_year IS NOT NULL
                  AND n.np_parent_company_owners <> 0
                  AND abs(i.net_profit_this_year - n.np_parent_company_owners)
                      > 0.005*abs(n.np_parent_company_owners)
                LIMIT 8""").df().to_string(index=False))

    # -------------------------------------------------------------------- 演示
    if args.demo and 'income' in stats:
        print('\n' + '=' * 72)
        print('PIT 演示: 2015 年报的披露爬坡 —— "在某一天我究竟能看到几份"')
        print('=' * 72)
        # 注意: 4-30 是年报法定披露截止日, 所以那天必然 100% 可见。
        # 要看出 PIT 的意义, 必须取截止日之前的时点。
        q = """
        SELECT d AS 观察日,
               (SELECT count(*) FROM fin_income
                WHERE report_date = DATE '2015-12-31'
                  AND NOT pub_date_is_placeholder
                  AND pub_date <= d) AS 当时可见,
               (SELECT count(*) FROM fin_income
                WHERE report_date = DATE '2015-12-31'
                  AND NOT pub_date_is_placeholder) AS 最终总数
        FROM (VALUES (DATE '2016-01-31'), (DATE '2016-02-29'), (DATE '2016-03-15'),
                     (DATE '2016-03-31'), (DATE '2016-04-15'), (DATE '2016-04-30')) t(d)
        ORDER BY d"""
        df = con.execute(q).df()
        df['可见比例'] = (100.0 * df['当时可见'] / df['最终总数']).round(1).astype(str) + '%'
        print(df.to_string(index=False))
        print("""
↑ 如果回测在 2016-03-15 就用上全部 2842 份年报, 等于用了 2000+ 份当时还没公告的数据。
  这是 A 股回测假 alpha 的最大单一来源, 而且曲线只会变好看, 你看不出来。
  正确写法必须同时满足: report_date <= 当日 AND pub_date <= 当日""")

    con.close()
    print('\n' + '=' * 72)
    if FAILURES:
        print('❌ %d 项校验失败:' % len(FAILURES))
        for f in FAILURES:
            print('   - %s' % f)
        return 1
    print('✅ 全部校验通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
