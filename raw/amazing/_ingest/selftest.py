#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""采集链的自证 —— **在 macOS 上也能跑**（用假 SDK 顶掉 tgw）。

    python3 selftest.py            # 约 10 秒
    python3 selftest.py -v         # 每条断言都打出来

## 为什么必须有这个文件

真 SDK 只在 Windows / Linux x64 上跑得起来（`tgw` 没有 `.dylib`）。
如果验证只能在 Windows 上做，那这九个脚本的**逻辑**就永远没被测过 ——
而逻辑错的表现不是报错，是"跑完了，数据看着也有，只是少了一半"。

所以这里注入一个**假 `AmazingData`**：接口签名、返回类型（dict / DataFrame /
宽表 / 二元组）、列名列序全按真 SDK 来（列名直接取自 `field_docs`，
而它来自 wheel 里的 `columns_list`），数据是造的。
于是这条链上所有**不依赖真数据**的东西都能验：分片、续跑、重试、
schema 统一、宽表 melt、字段说明、`--only` 拼错要报错……

🔴 **这不能替代在 Windows 上跑一次真数据。** 假 SDK 证明不了
"接口真的这么返回" —— 它只证明"如果接口这么返回，我们处理得对"。
第一次上真机时必须做的三件对数，见 `raw/amazing/README.md`。

## 每条断言都对着一个**实测踩过或推理确定**的失效模式

| # | 断言 | 不成立时的表现（都不报错） |
|---|---|---|
| 1 | 九个脚本 import 得进来、`specs()` 建得出任务 | —— |
| 2 | 分片 id 不重复 | 后一片**覆盖**前一片，数据静静少掉一块 |
| 3 | 落盘后所有 part 的 schema 完全一致 | 单个文件读得起来，整表读才炸 |
| 4 | 全 NaN 的列也按声明类型落盘 | 同上，且只在某几片上出现 |
| 5 | 续跑跳过已完成分片、不重取 | 每次重跑都从头，几小时变几天 |
| 6 | 空结果记 `rows:0` 而不是 failed | 空片被当成失败，永远在重试 |
| 7 | 失败的片进 `failed`，`--retry-failed` 只重跑它们 | 失败被忘掉，那块数据永久缺 |
| 8 | 宽表（复权因子）melt 成长表 | 每片一个 schema（列名是代码） |
| 9 | dict 的 key 在没有代码列时被补成 `SRC_CODE` | 分不清哪几行属于谁 |
| 10 | 字段说明 .md 写出来了、字段数对得上、UTF-8 | Windows 下默认编码会写成乱码 |
| 11 | `--only` / `--groups` 拼错要**报错退出** | 静默跑 0 张表，报告全绿 |
| 12 | 代码全集被**冻结**（第二次读缓存，不重新取） | 各表覆盖的代码集不同，join 缺行 |
| 13 | 每个 spec 的 fetch 参数与 shards 的 kwargs 对得上 | 跑起来才 TypeError，而那是几分钟后 |
| 14 | `field_docs` 里每个字段说明都非空、没串行 | 文档看着完整，其实在说谎 |
| 15 | 落盘的列序 == wheel 的 columns_list 列序 | 与手册对照时逐列错位 |
| 16 | 变异测试：不传 `period` 必须被抓到 | 那条断言等于没写，日线悄悄变成分钟线 |
| 17 | **端到端跑一遍 `pull_all.py`**（`--dry-run` 与真跑两种模式各一次） | 单个零件都对、合起来跑却坏（本仓库拆 `serve.py` 时栽过：只验了一种模式） |
| 18 | `get_etf_pcf` 一次调用喂两张表，请求数 == 分片数 | 请求量翻倍，多一倍被限流的机会 |
| 19 | 运行期输出里没有 emoji | Windows 的 GBK 控制台上 `print` 直接 UnicodeEncodeError，崩在进度条上 |
"""
import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILED = []
PASSED = []
VERBOSE = False


def ck(cond, msg):
    (PASSED if cond else FAILED).append(msg)
    if VERBOSE or not cond:
        print(('  ok  ' if cond else '  FAIL ') + msg)
    return bool(cond)


# ============================================================ 假 SDK
def fake_sdk(fail_shards=()):
    """造一个假 `AmazingData` 模块塞进 sys.modules。

    ★ 列名不是手写的，是从 `field_docs` 里取的 —— 那份来自 wheel 的
      `columns_list`，所以假 SDK 的返回**形状与真 SDK 一致**。
      手写列名的话这个自证就变成"我自己跟自己对"，测不出东西。
    """
    import pandas as pd
    import numpy as np
    import field_docs

    calls = {'n': 0, 'login': 0, 'per_table': {}}

    def cols(table):
        return [k for k in field_docs.FIELDS[table] if k != '__source__']

    def frame(table, code, n=3, all_nan_cols=()):
        """按某张表的字段造 n 行。`all_nan_cols` 里的列全给 NaN ——
        这是断言 4 的靶子：真 SDK 对缺的列就是填 np.nan。"""
        d = {}
        for c in cols(table):
            typ = field_docs.FIELDS[table][c][0]
            if c in all_nan_cols:
                d[c] = [np.nan] * n
            elif c in ('MARKET_CODE', 'code', 'CON_CODE', 'ETF_CODE'):
                d[c] = [code] * n
            elif c == 'INDEX_CODE':
                d[c] = [code] * n
            elif typ.startswith('float') or typ in ('double', 'decimal'):
                d[c] = [1.5 + i for i in range(n)]
            elif typ.startswith('int') or typ == 'long':
                d[c] = [10 + i for i in range(n)]
            else:
                d[c] = ['%s-%d' % (c[:6], i) for i in range(n)]
        return pd.DataFrame(d)

    def maybe_fail(tag):
        calls['n'] += 1
        calls['per_table'][tag] = calls['per_table'].get(tag, 0) + 1
        if tag in fail_shards:
            raise Exception('查询失败')      # 真 SDK 重试三次后抛的就是这句

    class BaseData(object):
        def get_calendar(self, data_type='str', market='SH', date=None):
            return [20240102 + i for i in range(0, 30)]

        def get_code_list(self, security_type='EXTRA_STOCK_A_SH_SZ'):
            return ['00000%d.SZ' % i for i in range(1, 7)]

        def get_hist_code_list(self, security_type='EXTRA_STOCK_A_SH_SZ',
                               start_date=None, end_date=None, local_path=None):
            maybe_fail('hist_code_list')
            return ['00000%d.SZ' % i for i in range(1, 7)] + ['60000%d.SH' % i
                                                              for i in range(1, 5)]

        def get_code_info(self, security_type='EXTRA_STOCK_A'):
            df = frame('code_info', '000001.SZ', 3)
            df = df.drop(columns=[c for c in ('code', 'ASOF_DATE', 'SECURITY_TYPE')
                                  if c in df.columns])
            df.index = ['00000%d.SZ' % i for i in range(1, 4)]   # index 是代码
            return df

        def get_etf_pcf(self, code_list):
            maybe_fail('etf_pcf')          # 计数：验 _PCF 缓存有没有真的省下一半请求
            info = frame('etf_pcf_info', code_list[0], len(code_list))
            info = info.drop(columns=[c for c in ('ASOF_DATE', 'code')
                                      if c in info.columns])
            info.index = list(code_list)                        # index 是 ETF 代码
            cons = {}
            for c in code_list:
                d = frame('etf_pcf_constituent', c, 2)
                cons[c] = d.drop(columns=[x for x in ('ASOF_DATE', 'ETF_CODE')
                                          if x in d.columns])
            return info, cons                                    # 🔴 二元组

        def _factor(self, code_list, local_path=None, is_local=True):
            maybe_fail('factor')
            # 🔴 宽表：index=交易日，column=代码
            idx = [20240102 + i for i in range(4)]
            return pd.DataFrame({c: [1.0 + i * 0.1 for i in range(4)]
                                 for c in code_list}, index=idx)

        get_backward_factor = _factor
        get_adj_factor = _factor

    class InfoData(object):
        def _dict_of(self, table, code_list, tag, all_nan_cols=()):
            maybe_fail(tag)
            return {c: frame(table, c, 3, all_nan_cols) for c in code_list}

        # ---- 手册写 dataframe 的（实测 pyc 多是 dict）：这里刻意返回 DataFrame，
        #      验 to_long() 两种都接
        def get_stock_basic(self, code_list):
            maybe_fail('stock_basic')
            import pandas as pd
            return pd.concat([frame('stock_basic', c, 1) for c in code_list],
                             ignore_index=True)

        def get_bj_code_mapping(self, local_path=None, is_local=True):
            return frame('bj_code_mapping', '830001.BJ', 2)

        def get_industry_base_info(self, local_path=None, is_local=True):
            assert is_local is False, 'industry_base_info 必须传 is_local=False'
            import pandas as pd
            return pd.concat([frame('industry_base_info', '80000%d.SI' % i, 1)
                              for i in range(1, 5)], ignore_index=True)

        def get_margin_summary(self, local_path=None, is_local=True, **kw):
            maybe_fail('margin_summary')
            return frame('margin_summary', None, 3)

        # ---- 其余都是 dict[code] -> DataFrame
        def get_history_stock_status(self, code_list, **kw):
            # 🔴 刻意让 IS_ST_SEC 整片全 NaN：真 SDK 对缺的列填 np.nan，
            #    不统一转型的话这一片是 float64、别片是 string
            return self._dict_of('history_stock_status', code_list,
                                 'history_stock_status', all_nan_cols=('IS_ST_SEC',))

        def __getattr__(self, name):
            # 剩下那些 get_xxx 全按 dict[code] -> DataFrame 造
            if not name.startswith('get_'):
                raise AttributeError(name)
            table = name[4:]
            alias = {'dividend': 'dividend', 'right_issue': 'right_issue'}
            table = alias.get(table, table)

            def f(code_list=None, **kw):
                if table not in field_docs.FIELDS:
                    raise AttributeError('假 SDK 不认识表 %s' % table)
                if code_list is None:
                    return frame(table, None, 3)
                return self._dict_of(table, code_list, table)
            return f

    class MarketData(object):
        def __init__(self, calendar):
            assert calendar, 'MarketData 必须用交易日历构造'
            self.calendar = calendar

        def query_kline(self, code_list, begin_date=None, end_date=None,
                        period=None, **kw):
            maybe_fail('kline_day')
            assert period == 'DAY_KLINE', \
                'period 必须显式传日线，收到 %r（真 SDK 默认是 1 分钟线）' % period
            out = {}
            for c in code_list:
                d = frame('kline_day', c, 3)
                d = d.drop(columns=['kline_time'])
                d.index = pd.to_datetime(['2024-01-0%d' % (i + 2) for i in range(3)])
                out[c] = d                       # 🔴 index 是日期，必须留
            return out

        def get_code_info(self, *a, **k):
            return BaseData().get_code_info()

    mod = types.ModuleType('AmazingData')
    mod.login = lambda **kw: calls.__setitem__('login', calls['login'] + 1)
    mod.logout = lambda *a, **k: None
    mod.BaseData = BaseData
    mod.InfoData = InfoData
    mod.MarketData = MarketData
    const = types.ModuleType('AmazingData.constant')

    class _P(object):
        class day(object):
            value = 'DAY_KLINE'
    const.Period = _P
    mod.constant = const
    sys.modules['AmazingData'] = mod
    sys.modules['AmazingData.constant'] = const
    return calls


# ============================================================ 测试骨架
class Args(object):
    """假的命令行参数（照 add_common_args 的字段）。"""

    def __init__(self, out, **kw):
        self.out = out
        self.sdk_cache = os.path.join(out, '_sdk')
        self.account = os.path.join(out, 'account.json')
        self.start = 20240101
        self.end = 20240131
        self.codes = None
        self.limit_codes = None
        self.only = None
        self.skip = None
        self.force = False
        self.retries = 0
        self.chunk = None
        self.sleep = 0.0
        self.dry_run = False
        self.retry_failed = False
        self.kinds = 'stock'
        for k, v in kw.items():
            setattr(self, k, v)


CFG = {'username': 'tgw_test', 'password': 'x', 'host': '127.0.0.1', 'port': 1}


def refake(fail_shards=()):
    """重造假 SDK，并**清掉 `ac._API` 缓存**。

    🔴 不清的话拿到的还是上一轮那套对象（`api()` 只登录一次、结果缓存在
    模块级 `_API` 里）—— 表现是"SDK 已经修好了，重跑还是失败"，
    而那不是被测代码的问题，是这个自证自己的问题。踩过一次。
    """
    import amazing_common as ac
    calls = fake_sdk(fail_shards)
    ac._API.clear()
    return calls


def fresh(tmp):
    """每轮都换一个干净的 out + state，并把模块级缓存清掉。"""
    import amazing_common as ac
    out = tempfile.mkdtemp(dir=tmp)
    ac.STATE_DIR = os.path.join(out, 'state')
    ac._API.clear()
    return out


def run_group(mod, args, cfg, only=None):
    import amazing_common as ac
    specs = mod.specs(args, cfg)
    if only:
        specs = [s for s in specs if s['name'] in only]
    return [ac.run_table(s, args, cfg) for s in specs], specs


def parts_of(out, table):
    d = os.path.join(out, table)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.endswith('.parquet'))


# ============================================================ 各条断言
def t01_import_and_specs(tmp):
    """① 九个脚本都 import 得进来、specs() 都建得出任务，且分片 id 不重复。"""
    import amazing_common as ac
    out = fresh(tmp)
    args = Args(out)
    fake_sdk()
    mods = []
    for name in ('pull_01_basic', 'pull_02_kline_day', 'pull_03_financial',
                 'pull_04_holder', 'pull_05_equity', 'pull_06_margin',
                 'pull_07_etf', 'pull_08_index', 'pull_09_industry'):
        __import__(name)
        mods.append(sys.modules[name])
    ck(len(mods) == 9, '① 九个采集脚本都 import 成功')

    n_tab = 0
    for m in mods:
        specs = m.specs(args, CFG)
        for s in specs:
            n_tab += 1
            ids = [sid for sid, _ in s['shards']]
            # ② 分片 id 重复 = 后一片覆盖前一片，数据静静少一块
            ck(len(ids) == len(set(ids)),
               '② %s 的分片 id 不重复（%d 片）' % (s['name'], len(ids)))
            ck(bool(s['shards']), '②b %s 至少有一片' % s['name'])
            # ⑬ fetch 的形参必须覆盖 shards 里给的 kwargs，否则跑起来才 TypeError
            import inspect
            sig = set(inspect.signature(s['fetch']).parameters)
            for _sid, kw in s['shards'][:1]:
                miss = set(kw) - sig
                ck(not miss, '⑬ %s 的 fetch 参数对得上分片 kwargs（缺 %s）'
                   % (s['name'], miss))
    ck(n_tab >= 28, '① 九组一共 %d 张表（>=28）' % n_tab)
    return mods


def t03_schema_and_docs(tmp, mods):
    """③④⑨⑩⑮ 落盘后：schema 统一、全 NaN 列按类型落、SRC_CODE、字段说明、列序。"""
    import amazing_common as ac
    import pandas as pd
    import pyarrow.parquet as pq
    import field_docs

    out = fresh(tmp)
    args = Args(out, chunk=3)
    fake_sdk()
    res, specs = run_group(sys.modules['pull_01_basic'], args, CFG,
                           only=('history_stock_status', 'stock_basic',
                                 'backward_factor', 'trading_calendar'))

    files = parts_of(out, 'history_stock_status')
    ck(len(files) >= 2, '③ history_stock_status 按 --chunk=3 切出了 %d 个 part（>=2）'
       % len(files))
    schemas = set()
    for f in files:
        t = pq.read_schema(os.path.join(out, 'history_stock_status', f))
        schemas.add(tuple((n, str(ty)) for n, ty in zip(t.names, t.types)))
    ck(len(schemas) == 1,
       '③ 所有 part 的 schema 完全一致（%d 种）' % len(schemas))

    df = pd.read_parquet(os.path.join(out, 'history_stock_status'))
    # ④ 整片全 NaN 的列也必须是声明的类型（string），不能退化成 float64
    ck(str(df['IS_ST_SEC'].dtype) in ('string', 'object'),
       '④ 全 NaN 的 IS_ST_SEC 仍按声明类型落盘（实得 %s）' % df['IS_ST_SEC'].dtype)
    ck(str(df['PRECLOSE'].dtype) == 'float64', '④b float 列是 float64')

    # ⑮ 列序 == wheel 的 columns_list 列序
    want = [c for c in field_docs.columns_of('history_stock_status')]
    ck(list(df.columns)[:len(want)] == want, '⑮ 落盘列序 == columns_list 列序')

    # ⑧ 宽表 melt
    fdf = pd.read_parquet(os.path.join(out, 'backward_factor'))
    ck(list(fdf.columns) == ['date', 'code', 'factor'],
       '⑧ 复权因子宽表被 melt 成 (date, code, factor)，实得 %s' % list(fdf.columns))
    ck(len(fdf) > 0 and fdf['code'].nunique() > 1, '⑧b melt 后每个代码都在')

    # ⑩ 字段说明
    doc = os.path.join(out, 'history_stock_status', '_字段说明.md')
    ck(os.path.exists(doc), '⑩ _字段说明.md 生成了')
    # 🔴 这一条是自证抓出来的真问题：文件名不以下划线开头时，
    #    pd.read_parquet('<表名>/') 会因为读到 .md 而炸（pyarrow 的
    #    ignore_prefixes 默认只跳过 '.' 和 '_' 开头的）。
    ck(all(f.startswith('_') or f.endswith('.parquet')
           for f in os.listdir(os.path.join(out, 'history_stock_status'))),
       '⑩a 数据目录里的非 parquet 文件都以下划线开头（否则整目录读不了）')
    txt = io.open(doc, encoding='utf-8').read()
    ck('| `PRECLOSE` |' in txt and '前收价' in txt, '⑩b 字段说明里有中文说明')
    n_rows = txt.count('\n| ') - 1
    ck(txt.count('| `') >= len(want), '⑩c 字段说明列出了全部 %d 个字段' % len(want))
    ck('交易日' in txt, '⑩d 字段说明写清了 begin_date 的含义')

    # ⑨ dict 的 key 在没有代码列时补 SRC_CODE
    args2 = Args(out, chunk=2)
    res2, _ = run_group(sys.modules['pull_06_margin'], args2, CFG,
                        only=('margin_summary',))
    ms = pd.read_parquet(os.path.join(out, 'margin_summary'))
    ck('EXCHANGE' in ms.columns,
       '⑨b margin_summary 有手册没写的 EXCHANGE 列（按交易所一天一行）')
    return out


def t05_resume_and_retry(tmp):
    """⑤⑥⑦ 续跑跳过、空片记 0 行、失败进 failed 且能只重跑失败的。"""
    import amazing_common as ac
    out = fresh(tmp)
    args = Args(out, chunk=3)

    # 第一轮：让 kline 的所有片都失败
    refake(fail_shards=('kline_day',))
    r1, specs = run_group(sys.modules['pull_02_kline_day'], args, CFG)
    ck(r1[0]['failed'] == r1[0]['shards'] and r1[0]['rows'] == 0,
       '⑦ 全失败时 failed=%d、rows=0' % r1[0]['failed'])
    st = ac.load_state('kline_day')
    ck(len(st['failed']) == r1[0]['shards'], '⑦b 失败的片都进了 manifest 的 failed')
    ck(parts_of(out, 'kline_day') == [], '⑦c 失败时不留半截 parquet')

    # 第二轮：修好 SDK，--retry-failed 只重跑失败的那些
    calls = refake()
    args.retry_failed = True
    spec = [s for s in specs if s['name'] == 'kline_day'][0]
    r2 = ac.retry_failed_only(spec, args)
    ck(r2 and r2['failed'] == 0 and r2['rows'] > 0,
       '⑦d --retry-failed 把失败的片重跑成功了（%d 行）' % (r2 or {}).get('rows', 0))
    st = ac.load_state('kline_day')
    ck(not st['failed'], '⑦e 重跑成功后 failed 被清空')

    # 第三轮：什么都不加，应该全跳过（续跑）
    args.retry_failed = False
    args.force = False
    calls = refake()
    before = calls['n']
    r3 = ac.run_table(spec, args, CFG)
    ck(r3['skipped'] == r3['shards'] and calls['n'] == before,
       '⑤ 续跑时全部跳过、一次接口都没打（skipped=%d/%d，调用 %d 次）'
       % (r3['skipped'], r3['shards'], calls['n'] - before))

    # ⑥ 空结果记 rows:0 而不是 failed
    def empty_fetch(**kw):
        return {}
    spec_empty = dict(spec, name='kline_day', fetch=empty_fetch,
                      shards=[('empty-0', {'code_list': ['x'], 'begin_date': 20240101,
                                           'end_date': 20240131})])
    r4 = ac.run_table(spec_empty, args, CFG)
    ck(r4['failed'] == 0, '⑥ 空结果不算失败')
    st = ac.load_state('kline_day')
    ck(st['shards'].get('empty-0', {}).get('rows') == 0,
       '⑥b 空片在 manifest 里记成 rows:0（留痕，下次不会重取）')
    return out


def t18_pcf_memo(tmp):
    """⑱ `get_etf_pcf` 一次调用喂两张表：请求数必须等于分片数，不是两倍。

    🔴 这一条守的是 `pull_07._PCF` 那个缓存。缓存只留最近一片时，
    第二张表每片都会重新调一次 —— 请求量翻倍，**而这不报错**。
    写过那个版本，所以这里钉一条。
    """
    import amazing_common as ac
    out = fresh(tmp)
    calls = refake()
    sys.modules['pull_07_etf']._PCF.clear()     # 上一轮的缓存不该影响这一轮
    args = Args(out, chunk=3, limit_codes=7)
    res, specs = run_group(sys.modules['pull_07_etf'], args, CFG,
                           only=('etf_pcf_info', 'etf_pcf_constituent'))
    n_shards = len(specs[0]['shards'])
    n_calls = calls['per_table'].get('etf_pcf', 0)
    ck(n_calls == n_shards,
       '⑱ get_etf_pcf 调了 %d 次 == 分片数 %d（两张表共用一次调用）'
       % (n_calls, n_shards))
    ck(all(r['rows'] > 0 for r in res), '⑱b 两张表都落到了数据')


def t11_arg_typos(tmp):
    """⑪ --only / --groups 拼错必须报错退出，不能静默跑 0 张表。"""
    import amazing_common as ac
    out = fresh(tmp)
    args = Args(out, only='balance_shet')     # 少个 e
    fake_sdk()
    try:
        ac.pick_tables(sys.modules['pull_03_financial'].specs(args, CFG), args)
        ck(False, '⑪ --only 拼错时报错退出')
    except SystemExit as e:
        ck('balance_shet' in str(e), '⑪ --only 拼错时报错退出并指出拼错的名字')

    import pull_all
    # ⑪c 跨组挑表必须能用：--only kline_day 只在第 ② 组里有，
    #     逐组严格校验的话会在第 ① 组就报错退出（写过这个 bug）
    sys.argv = ['pull_all.py', '--only', 'kline_day', '--dry-run',
                '--out', out, '--start', '20240101', '--end', '20240131']
    try:
        rc = pull_all.main()
        ck(rc == 0, '⑪c pull_all --only 跨组挑表能用（kline_day 只在第②组）')
    except SystemExit as e:
        ck(False, '⑪c pull_all --only 跨组挑表报错了：%s' % e)

    # ⑪d 但拼错了还是要报错（并集校验）
    sys.argv = ['pull_all.py', '--only', 'kline_dya', '--dry-run',
                '--out', out, '--start', '20240101', '--end', '20240131']
    try:
        pull_all.main()
        ck(False, '⑪d pull_all --only 拼错要报错退出')
    except SystemExit as e:
        ck('kline_dya' in str(e), '⑪d pull_all --only 拼错报错并指出名字')

    sys.argv = ['pull_all.py', '--groups', '99', '--dry-run']
    try:
        pull_all.main()
        ck(False, '⑪b --groups 拼错时报错退出')
    except SystemExit as e:
        ck('99' in str(e), '⑪b --groups 拼错时报错退出并指出组号')


def t12_universe_frozen(tmp):
    """⑫ 代码全集要被冻结：第二次调用读缓存，不再打接口。"""
    import amazing_common as ac
    out = fresh(tmp)
    args = Args(out)
    calls = fake_sdk()
    c1 = ac.universe(args, CFG, 'EXTRA_STOCK_A_SH_SZ', 20240101, 20240131)
    n1 = calls['per_table'].get('hist_code_list', 0)
    c2 = ac.universe(args, CFG, 'EXTRA_STOCK_A_SH_SZ', 20240101, 20240131)
    n2 = calls['per_table'].get('hist_code_list', 0)
    ck(c1 == c2 and n1 == 1 and n2 == 1,
       '⑫ 代码全集冻结：两次拿到同一份，接口只打了 %d 次' % n2)
    cache = [f for f in os.listdir(ac.STATE_DIR) if f.startswith('universe_')]
    ck(bool(cache), '⑫b 冻结的代码全集落在 state/universe_*.json')


def t14_field_docs():
    """⑭ 字段说明本身：每条非空、没串进下一行、表数与字段数对得上。"""
    import re
    import field_docs
    TW = ('float64', 'float32', 'int64', 'int32', 'datetime', 'dataframe',
          'decimal', 'string', 'double', 'float', 'long', 'bool', 'str', 'int')
    empty, bleed = [], []
    for t, rows in field_docs.FIELDS.items():
        for k, v in rows.items():
            if k == '__source__':
                continue
            typ, desc = v
            if not desc.strip():
                empty.append('%s.%s' % (t, k))
            elif '手册的字段表里没有这一列' not in desc:
                for w in TW:
                    if re.search(r'(?<![A-Za-z_])' + w + r'(?![A-Za-z0-9_])', desc):
                        bleed.append('%s.%s' % (t, k))
                        break
    ck(not empty, '⑭ 没有空的字段说明（%d 个）' % len(empty))
    ck(not bleed, '⑭b 没有串进下一行的字段说明（%d 个）' % len(bleed))
    n = sum(len(v) - 1 for v in field_docs.FIELDS.values())
    ck(len(field_docs.FIELDS) >= 34 and n >= 780,
       '⑭c %d 张表 / %d 个字段' % (len(field_docs.FIELDS), n))
    # 关键的几列必须在（拼错列名不报错，只是那一列永远查不到说明）
    for t, f in (('balance_sheet', 'ACTUAL_ANN_DATE'), ('income', 'STATEMENT_TYPE'),
                 ('history_stock_status', 'HIGH_LIMITED'), ('dividend', 'DATE_EX'),
                 ('margin_summary', 'EXCHANGE'), ('index_weight', 'WEIGHT'),
                 ('industry_daily', 'PRE_CLOSE'), ('kline_day', 'amount'),
                 ('equity_restricted', 'SHARE_LST_IS_ANN')):
        ck(f in field_docs.FIELDS.get(t, {}), '⑭d %s 里有 %s' % (t, f))


def t16_kline_period():
    """⑯ 变异测试：把 period 不传（退回 SDK 默认的 1 分钟线）必须被假 SDK 抓到。

    🔴 这一条是**证明前一条断言真的在验东西**：假 SDK 里那句
    `assert period == 'DAY_KLINE'` 如果永远不会失败，它就等于没写。
    """
    import amazing_common as ac
    out = tempfile.mkdtemp()
    ac.STATE_DIR = os.path.join(out, 'state')
    ac._API.clear()
    fake_sdk()
    args = Args(out, chunk=3)
    a = ac.api(CFG, args.sdk_cache)
    try:
        a['market'].query_kline(['000001.SZ'], begin_date=20240101,
                                end_date=20240131)      # 不传 period
        ck(False, '⑯ 不传 period 必须被抓到（变异测试）')
    except AssertionError as e:
        ck('1 分钟线' in str(e), '⑯ 不传 period 被抓到：%s' % str(e)[:40])
    except TypeError:
        ck(True, '⑯ 不传 period 直接 TypeError（也算抓到）')
    shutil.rmtree(out, ignore_errors=True)


def t17_pull_all(tmp):
    r"""⑰ 端到端跑一遍 `pull_all.py`：任务清单 → 逐表取数 → 总览文档。

    🔴 这一条是**最要紧**的：前面每条都在验单个零件，而九个脚本合起来跑
    有它自己的失效模式 —— 组间的 spec 建不出来、某张表把别的表的目录写坏、
    总览文档漏表。而"跑完了、没报错"完全掩盖这些。
    ★ 顺带验了本仓库栽过的那条：`serve.py` 拆分时**只用一种模式验**，
      于是另一种模式下的代码一次都没执行到。所以这里既跑 `--dry-run`
      （不登录的那条路径），也跑真的取数（登录的那条）。
    """
    import amazing_common as ac
    import pull_all
    out = fresh(tmp)
    refake()
    # account.json 走文件这条路也要验一次（平时是环境变量）
    with io.open(os.path.join(out, 'account.json'), 'w', encoding='utf-8') as f:
        f.write(json.dumps(CFG, ensure_ascii=False))

    base = ['--out', out, '--sdk-cache', os.path.join(out, '_sdk'),
            '--account', os.path.join(out, 'account.json'),
            '--start', '20240101', '--end', '20240131', '--chunk', '5',
            '--limit-codes', '6', '--kinds', 'stock']

    # ① --dry-run：不登录、不落盘
    sys.argv = ['pull_all.py'] + base + ['--dry-run']
    rc = pull_all.main()
    ck(rc == 0, '⑰ --dry-run 正常退出')
    ck(not [d for d in os.listdir(out) if os.path.isdir(os.path.join(out, d))
            and not d.startswith('_') and d != 'state'],
       '⑰a --dry-run 一个数据目录都没建')

    # ② 真跑（假 SDK）
    refake()
    sys.argv = ['pull_all.py'] + base
    rc = pull_all.main()
    ck(rc == 0, '⑰b 全量跑完、没有失败分片（rc=%s）' % rc)

    tables = [d for d in sorted(os.listdir(out))
              if os.path.isdir(os.path.join(out, d)) and not d.startswith('_')
              and d != 'state']
    ck(len(tables) >= 28, '⑰c 落了 %d 张表（>=28）' % len(tables))
    empty = [t for t in tables if not parts_of(out, t)]
    ck(not empty, '⑰d 每张表都至少有一个 part（空的：%s）' % empty[:5])
    nodoc = [t for t in tables
             if not os.path.exists(os.path.join(out, t, '_字段说明.md'))]
    ck(not nodoc, '⑰e 每张表都有 _字段说明.md（缺的：%s）' % nodoc[:5])

    ov = os.path.join(out, '字段说明总览.md')
    ck(os.path.exists(ov), '⑰f 总览文档生成了')
    txt = io.open(ov, encoding='utf-8').read()
    miss = [t for t in tables if ('`%s`' % t) not in txt]
    ck(not miss, '⑰g 总览列全了所有表（漏的：%s）' % miss[:5])

    # ③ 每张表都能被**整目录**读出来（这是 _ 前缀那条的真实判据）
    import pandas as pd
    bad = []
    for t in tables:
        try:
            pd.read_parquet(os.path.join(out, t))
        except Exception as e:
            bad.append('%s: %s' % (t, type(e).__name__))
    ck(not bad, '⑰h 每张表都能整目录 read_parquet（读不了的：%s）' % bad[:3])

    # ④ 再跑一次：应该全跳过（幂等 + 续跑）
    calls = refake()
    before = calls['n']
    sys.argv = ['pull_all.py'] + base
    pull_all.main()
    ck(calls['n'] - before <= 2,
       '⑰i 第二次跑几乎不打接口（打了 %d 次；代码全集读缓存）' % (calls['n'] - before))


def t19_no_emoji_in_output():
    r"""⑲ 运行期输出里不许有 emoji。

    🔴 Windows 控制台默认 GBK，`print('🔴…')` 直接 UnicodeEncodeError ——
    **崩在进度条上**，看着像取数失败。文档里用 emoji 没问题（文件是 UTF-8），
    但 `say()` / `print()` 的字面量不行。
    ★ `amazing_common` 在 import 时就 `reconfigure(errors='replace')` 兜了一层，
      这条断言是第二道 —— 兜底会把字符换成 `?`，而那让进度条错位。
    """
    import ast
    import glob
    import re
    emo = re.compile('[\U0001F300-\U0001FAFF\u2600-\u27bf]')
    bad = []
    for p in sorted(glob.glob(os.path.join(HERE, '*.py'))):
        tree = ast.parse(io.open(p, encoding='utf-8').read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)
            if fn not in ('say', 'print'):
                continue
            for a in ast.walk(node):
                if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                        and emo.search(a.value):
                    bad.append('%s:%d' % (os.path.basename(p), node.lineno))
    ck(not bad, '⑲ 运行期输出里没有 emoji（%s）' % (bad[:4] or '干净'))


def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args()
    VERBOSE = a.verbose

    try:
        import pandas, pyarrow, numpy       # noqa
    except ImportError as e:
        raise SystemExit('自证要 pandas / pyarrow / numpy：%s' % e)

    tmp = tempfile.mkdtemp(prefix='amz_selftest_')
    print('自证（假 SDK，macOS 上也能跑）临时目录 %s' % tmp)
    try:
        mods = t01_import_and_specs(tmp)
        t03_schema_and_docs(tmp, mods)
        t05_resume_and_retry(tmp)
        t11_arg_typos(tmp)
        t12_universe_frozen(tmp)
        t14_field_docs()
        t16_kline_period()
        t17_pull_all(tmp)
        t18_pcf_memo(tmp)
        t19_no_emoji_in_output()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print('')
    print('通过 %d 条，失败 %d 条' % (len(PASSED), len(FAILED)))
    for m in FAILED:
        print('  FAIL ' + m)
    if not FAILED:
        print('!! 提醒：假 SDK 只能证明"如果接口这么返回，我们处理得对"。')
        print('   第一次上真机（Windows/Linux x64）必须做三件对数，见 README。')
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
