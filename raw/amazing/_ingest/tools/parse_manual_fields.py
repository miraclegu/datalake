#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""从【手册 PDF + wheel 里的 .pyc】生成 `_ingest/field_docs.py`（字段说明正本）。

    # 在 macOS 上跑（需要 pypdf；不要装进主环境）
    python3 -m venv /tmp/pdfenv && /tmp/pdfenv/bin/pip install pypdf
    /tmp/pdfenv/bin/python tools/parse_manual_fields.py \
        --pdf ../AmazingData开发手册.pdf \
        --wheel ../AmazingData-1.1.9-cp313-none-any.whl \
        --out ../_ingest/field_docs.py

## 为什么要两个源，而不是只抄 PDF

| | 从哪来 | 为什么非它不可 |
|---|---|---|
| **字段名 + 顺序** | wheel 里的 `.pyc` 常量元组 | `download_xxx()` 内部有一份 `columns_list` 硬编码元组，取数后按它 reindex —— 这就是**返回值真实的列与列序**。PDF 里的名字被排版拆了字（见下） |
| **字段中文说明** | PDF | pyc 里只有名字，没有含义 |

🔴 **PDF 里的字段名不能直接用**：pypdf 抽出来的文本在某些字距对上会
插进一个空格 —— 实测 `STA TEMENT_TYPE` / `ANN_DA TE` / `ACC_RECEIV ABLE`
/ `PAY ABLE`，而且长名字还会**跨行断开**（`AGENCY_BUSINESS_LI\nAB`）。
照 PDF 抄下来的列名与真实返回值对不上，**而这不报错** —— 只是那一列的
说明永远匹配不上、文档里一片空白。所以名字一律以 pyc 为准，PDF 只提供说明。

## 匹配算法（以及为什么它是可证的）

pyc 的元组顺序与 PDF 表格里的行序**完全一致**（前 7 个是表头字段，
之后按字母序）。所以按 pyc 的顺序在 PDF 文本里**顺序推进**地找每个字段名
（字符之间允许 0~3 个空白，以吃掉排版插进来的空格和换行），
两个相邻字段名之间的那段文本就是「类型 + 说明 + 备注」。

🔴 **找不到就报错退出，不许静默跳过。** 静默跳过的表现是字段说明缺几行，
而缺的那几行看着就像"手册里本来没写"。同理：`--allow-missing` 要显式给。

## 手工补的三张表（PDF 里没有字段表）

`code_info` / `kline`（附录 4.2.6 K线 Kline）/ `backward_factor`：
PDF 用一段散文而不是表格描述，逐个抄进 `EXTRA_DOCS`。
★ `code_info` 实测比手册多一列 `list_day`（pyc 元组 7 个，手册只列 6 个）
—— 这也是"名字以 pyc 为准"的旁证。
"""
import argparse
import io
import json
import os
import re
import sys
import types
import zipfile
from unittest.mock import MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- 表清单
# 表名 -> (取列的来源方法, PDF 里 "<x>的字段说明" 的那个 x)
# 只覆盖本次要拉的九类数据；期权 / 可转债 / 龙虎榜 / 大宗 / 国债不在范围内。
TABLES = [
    # (parquet 表名,            pyc 里的方法,                        PDF 字段表名)
    ('stock_basic',            'download_stock_basic',              'stock_basic'),
    ('history_stock_status',   'download_hist_stock_status',         'history_stock_status'),
    ('bj_code_mapping',        None,                                 'bj_code_mapping'),
    ('balance_sheet',          'download_balance_sheet',             'balance_sheet'),
    ('cash_flow',              'download_cash_flow',                 'cash_flow'),
    ('income',                 'download_income',                    'income'),
    ('profit_express',         'download_profit_express',            'profit_express'),
    ('profit_notice',          'download_profit_notice',             'profit_notice'),
    ('share_holder',           'download_share_holder',              'share_holder'),
    ('holder_num',             'download_holder_num',                'holder_num'),
    ('equity_structure',       'download_equity_structure',          'equity_structure'),
    ('equity_pledge_freeze',   'download_equity_pledge_freeze',      'equity_pledge_freeze'),
    ('equity_restricted',      'download_equity_restricted',         'equity_restricted'),
    ('dividend',               'download_dividend',                  'dividend'),
    ('right_issue',            'download_right_issue',               'right_issue'),
    ('margin_summary',         'download_margin_summary',            'margin_summary'),
    ('margin_detail',          'download_margin_detail',             'margin_detail'),
    ('etf_pcf_info',           None,                                 'etf_pcf_info'),
    ('etf_pcf_constituent',    None,                                 'etf_pcf_constituent'),
    ('fund_share',             'download_fund_share',                'fund_share'),
    ('fund_nav',               'download_fund_nav',                  'fund_nav'),
    ('fund_iopv',              'download_fund_iopv',                 'fund_iopv'),
    ('index_constituent',      'download_index_constituent',         'index_constituent'),
    ('index_weight',           'download_index_weight',              'index_weight'),
    ('industry_base_info',     None,                                 'industry_base_info'),
    ('industry_constituent',   'download_industry_constituent',      'industry_constituent'),
    ('industry_weight',        'download_industry_weight',           'industry_weight'),
    ('industry_daily',         'download_industry_daily',            'industry_daily'),
]

# 🔴 SDK 有、手册的字段表里【没有】的列 —— 白名单，逐个确认过（grep 全文零命中）。
# 不写白名单的话只有两条路：报错退出（那就永远跑不了）或 --allow-missing
# （那会把**新出现**的缺口也一起放过，而新缺口正是要看见的东西）。
KNOWN_UNDOCUMENTED = {
    ('income', 'ADJ_PREV_YEAR_LOSS_GAIN'),   # 1.1.9 实测：手册 3.5.5.3 的字段表里没有它
    # 🔴 这一列很要紧：融资融券成交汇总是**按交易所**一天一行（沪/深/北各一条），
    #    不知道它存在的话会把"融资余额"当成全市场的一个数用 —— 而那不报错，只是小了一半。
    ('margin_summary', 'EXCHANGE'),
}
UNDOC_DESC = '🔴 手册的字段表里没有这一列（wheel 的 columns_list 里有，说明接口会返回它）'

# get_etf_pcf 的两组列在 BaseData.get_etf_pcf 里（不在 download_info_data）
PCF_FROM_BASEDATA = {'etf_pcf_info': 0, 'etf_pcf_constituent': 1}

# ★ 同一张表里类型词的写法不统一：财务三表写 `float`，业绩快报写 `float64`，
#   证券基础信息写 `string`。少收一个的表现是那张表**整表**匹配不上（实测
#   profit_express 27 个字段一起丢）。长的排前面，免得 `float` 先吃掉 `float64`。
TYPE_WORDS = ('float64', 'float32', 'int64', 'int32', 'datetime', 'dataframe',
              'decimal', 'string', 'double', 'float', 'long', 'bool', 'str', 'int')

# PDF 里没有字段表的三张，逐个抄（来源写在注释里）
EXTRA_DOCS = {
    # 附录 4.2.6 K线 Kline
    'kline_day': {
        '__source__': '手册附录 4.2.6「K线 Kline」+ 3.5.4.2 历史 K线',
        'code':       ('str',      '证券代码+市场（如 600000.SH）'),
        'kline_time': ('datetime', '交易所行情数据时间；日线为该交易日'),
        'open':       ('float',    '今开盘价'),
        'high':       ('float',    '最高价'),
        'low':        ('float',    '最低价'),
        'close':      ('float',    '收盘价'),
        'volume':     ('int',      '成交总量（股）'),
        'amount':     ('float',    '成交总金额（元）'),
    },
    # 3.5.2.1 每日最新证券信息（手册用散文描述；列名与列序取自 pyc）
    'code_info': {
        '__source__': '手册 3.5.2.1；列名与列序取自 BaseData.get_code_info 的 pyc 常量',
        'code':             ('str',   '证券代码+市场（DataFrame 的 index，落盘时提为列）'),
        'symbol':           ('str',   '证券简称'),
        'security_status':  ('str',   '产品状态标志，见手册附录 4.1.6（1 停牌 / 2 除权 / 3 除息 / 4 风险警示 / 5 退市整理期 / 6 上市首日 …）'),
        'pre_close_price':  ('float', '昨收价'),
        'high_limited':     ('float', '涨停价'),
        'low_limited':      ('float', '跌停价'),
        'price_tick':       ('float', '最小价格变动单位'),
        'list_day':         ('int',   '上市日期。🔴 手册的输出说明里【没有列这一列】，实测 pyc 元组里有'),
    },
    # 3.5.2.5 / 3.5.2.6 复权因子：原始返回是宽表（index=交易日, column=代码），
    # 落盘前 melt 成长表 —— 见 amazing_common.melt_wide()
    'backward_factor': {
        '__source__': '手册 3.5.2.5；原始返回为宽表，落盘时 melt 成长表',
        'date':   ('int',   '交易日期（yyyymmdd）；原宽表的 index'),
        'code':   ('str',   '证券代码+市场；原宽表的 column'),
        'factor': ('float', '后复权因子。按交易所行情数据计算得出'),
    },
    'adj_factor': {
        '__source__': '手册 3.5.2.6；原始返回为宽表，落盘时 melt 成长表',
        'date':   ('int',   '交易日期（yyyymmdd）；原宽表的 index'),
        'code':   ('str',   '证券代码+市场；原宽表的 column'),
        'factor': ('float', '单次复权因子（除权除息当日的复权比例，非累乘）'),
    },
    'trading_calendar': {
        '__source__': '手册 3.5.2.8；get_calendar 返回 List[int]',
        'date':   ('int', '交易日（yyyymmdd）'),
        'market': ('str', '市场（本脚本按 market 参数逐个市场取，落盘时标上）'),
    },
    'hist_code_list': {
        '__source__': '手册 3.5.2.7；get_hist_code_list 返回 List[str]',
        'code':          ('str', '证券代码+市场。区间内**存在过**的代码（含已退市）'),
        'security_type': ('str', '取数时用的 security_type（本脚本落盘时标上）'),
        'start_date':    ('int', '取数区间起（本脚本落盘时标上）'),
        'end_date':      ('int', '取数区间止（本脚本落盘时标上）'),
    },
}


# ------------------------------------------------------------ 从 wheel 取列
def cols_from_wheel(wheel_path):
    """解开 wheel、用桩件 import 掉原生依赖，取每个 download_* 里的列元组。"""
    import tempfile
    tmp = tempfile.mkdtemp(prefix='amz_wheel_')
    with zipfile.ZipFile(wheel_path) as z:
        z.extractall(tmp)

    # pydantic / numba / pandas… 全用桩件顶掉：只要能 import 到字节码即可，
    # 不执行任何取数逻辑（tgw 在 macOS 上根本装不起来，见 setup_amazing.py）
    pyd = types.ModuleType('pydantic')

    class _BaseModel:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    def _noop(*a, **k):
        return None

    def _deco(*a, **k):
        def d(f):
            return f
        return d

    pyd.BaseModel = _BaseModel
    pyd.Field = _noop
    pyd.validator = _deco
    pyd.field_validator = _deco
    pyd.model_validator = _deco
    pyd.ValidationError = Exception
    pyd.__version__ = '2.0.0'
    sys.modules['pydantic'] = pyd

    stubs = ['tgw', 'numba', 'numba.core', 'numba.typed', 'pandas', 'numpy',
             'numpy.linalg', 'scipy', 'scipy.signal', 'scipy.stats',
             'scipy.optimize', 'scipy.linalg', 'scipy.cluster',
             'scipy.cluster.hierarchy', 'scipy.spatial', 'scipy.spatial.distance',
             'statsmodels', 'statsmodels.api', 'statsmodels.tsa', 'sklearn',
             'sklearn.linear_model', 'sklearn.decomposition', 'matplotlib',
             'matplotlib.pyplot', 'cvxpy', 'tables', 'h5py', 'tqdm']
    for name in stubs:
        m = MagicMock()
        m.__name__ = name
        m.__spec__ = MagicMock()
        m.__path__ = []
        m.__version__ = '2.0.0'
        sys.modules[name] = m
    for name in stubs:
        if '.' in name:
            parent, child = name.rsplit('.', 1)
            setattr(sys.modules[parent], child, sys.modules[name])

    sys.path.insert(0, tmp)
    import importlib
    dl = importlib.import_module('AmazingData.download_data.download_info_data')
    bd = importlib.import_module('AmazingData.query_api.base_data')

    def tuples(code, acc):
        for c in code.co_consts:
            if isinstance(c, tuple) and len(c) >= 3 and all(isinstance(x, str) for x in c):
                acc.append(list(c))
            elif hasattr(c, 'co_consts'):
                tuples(c, acc)

    def pick(fn):
        acc = []
        tuples(fn.__code__, acc)
        cands = [t for t in acc
                 if sum(1 for x in t if x.isupper() or '_' in x) >= len(t) * 0.6]
        return max(cands, key=len) if cands else None

    out = {}
    for name, meth, _pdf in TABLES:
        if name in PCF_FROM_BASEDATA:
            acc = []
            tuples(bd.BaseData.get_etf_pcf.__code__, acc)
            big = [t for t in acc if len(t) >= 3]
            idx = PCF_FROM_BASEDATA[name]
            out[name] = big[idx] if len(big) > idx else None
            continue
        if not meth:
            out[name] = None
            continue
        fn = getattr(dl.DownloadInfoData, meth, None)
        out[name] = pick(fn) if fn else None
    return out


# -------------------------------------------------------------- 从 PDF 取说明
def pdf_text(pdf_path, known):
    from pypdf import PdfReader
    r = PdfReader(pdf_path)
    parts = []
    for p in r.pages:
        parts.append(p.extract_text() or '')
    txt = '\n'.join(parts)
    lines = []
    for ln in txt.split('\n'):
        # 页眉与纯页码行去掉：它们会插在字段表中间，把一行拆成两半
        if '中国银河证券星耀数智量化平台金融资讯数据说明' in ln:
            continue
        if re.fullmatch(r'\s*\d{1,3}\s*', ln):
            continue
        lines.append(ln.rstrip())
    return repair_split_names('\n'.join(lines), known)


def repair_split_names(text, known):
    r"""修一种【名字被分页切断】：字段名的后半截跑到下一页第一行去了，中间还
    夹着类型词和说明的前半段 —— 实测两处：

        RCV_CED_UNEARNED float 应收分保未到期责     ← 上一页最后一行
        （页眉 / 页码，已在上面剔掉）
        _PREM_RESV 任准备金                          ← 下一页第一行 = 名字的尾巴

        OTH_COMPRE_IN float 其他综合收益
        C                                            ← 尾巴只有一个字母

    真名是 `RCV_CED_UNEARNED_PREM_RESV` / `OTH_COMPRE_INC`。
    ★ 只在**上一行是完整一行**（名字后紧跟类型词）**且拼出来的名字确实在
    pyc 的字段名集合里**时才接 —— 判据落在"拼出来的是不是真字段"上，
    而不落在"看着像不像尾巴"上。所以它不会把一行正常的字段行吃掉。

    ★ 名字只是【单纯换行】的那种（`AMORT_COST_FI` + `N_ASSETS_EAR float …`、
    `RCV_CED_LT_HEALTH` + `_INSUR_RESV float …`）不用在这里修 ——
    flex() 的匹配本来就允许名字里夹换行。
    🔴 不修的话那几列在 PDF 里永远匹配不上，表现是它们的说明是空的（而这不报错）。
    """
    typ = '|'.join(TYPE_WORDS)
    row_re = re.compile(r'^([A-Z][A-Z0-9_]*)([ \t]+(?:' + typ + r')\b.*)$')
    out, fixed = [], []
    for ln in text.split('\n'):
        m = re.match(r'^([A-Z_][A-Z0-9_]*)[ \t]*(.*)$', ln)
        if m and out:
            pm = row_re.match(out[-1])
            if pm and (pm.group(1) + m.group(1)) in known:
                fixed.append(pm.group(1) + m.group(1))
                out[-1] = pm.group(1) + m.group(1) + pm.group(2)
                out.append(m.group(2))
                continue
        out.append(ln)
    if fixed:
        print(f'  （接回 {len(fixed)} 个被分页切断的字段名：{", ".join(fixed)}）')
    return '\n'.join(out)


def flex(name):
    """字段名的宽松匹配：字符之间允许 0~3 个空白（吃掉排版插的空格与换行）。"""
    return r'[\s]{0,3}'.join(re.escape(ch) for ch in name)


def section_of(text, pdf_name):
    """定位 "<pdf_name>的字段说明" 那一段。"""
    m = re.search(re.escape(pdf_name) + r'\s*的字段说明', text)
    if not m:
        return None
    start = m.end()
    tail = text[start:]
    ends = [len(tail)]
    for pat in (r'(?m)^\s*3\.5\.\d+', r'#\s*第一步', r'的字段说明', r'(?m)^\s*4\.\s'):
        mm = re.search(pat, tail)
        if mm and mm.start() > 40:
            ends.append(mm.start())
    return tail[:min(ends)]


def parse_desc(seg):
    """把 "<类型> <说明><备注>" 拆成 (类型, 说明)。说明与备注在 PDF 文本里没有
    分隔符，无法可靠拆开 —— 合成一句，不猜。"""
    s = seg.strip()
    s = re.sub(r'^[,，、:：\'\"”’\s]+', '', s)   # 名字后面粘的标点（`HOLDER_QUANTITY , float`）
    typ = ''
    m = re.match(r'([A-Za-z]+[0-9]*)\b', s)
    if m and m.group(1).lower() in TYPE_WORDS:
        typ = m.group(1).lower()
        s = s[m.end():]
    # 中文之间的换行直接去掉；ASCII 之间保留一个空格
    s = re.sub(r'\s*\n\s*', lambda _m: '', s)
    s = re.sub(r'[ \t]+', ' ', s).strip()
    s = re.sub(r'\s+([，。）])', r'\1', s)
    return typ or 'str', s


def extract(text, pdf_name, expected, allow_missing, table):
    seg = section_of(text, pdf_name)
    if seg is None:
        raise SystemExit(f'🔴 PDF 里找不到 "{pdf_name}的字段说明"')
    if not expected:
        # 没有 pyc 列表的表：自由解析（bj_code_mapping / industry_base_info）
        rows = {}
        for m in re.finditer(
                r'(?m)^\s*([A-Z][A-Z0-9_]{1,60})\s+(' + '|'.join(TYPE_WORDS) + r')\s+(.*)$', seg):
            typ, desc = parse_desc(m.group(2) + ' ' + m.group(3))
            rows[m.group(1)] = (typ, desc)
        if not rows:
            raise SystemExit(f'🔴 {pdf_name}: 自由解析一个字段都没解出来')
        return rows, []

    # 🔴 不假设「pyc 的列序 == 手册表格的行序」。实测 holder_num 就不一致
    # （pyc 是 …HOLDER_NUM, HOLDER_TOTAL_NUM，手册是 …TOTAL 在前）——
    # 顺序推进的匹配在那里会**找不到后一个**，而"找不到"看着就像手册没写。
    # 所以：先各自定位，再**按文本位置**排序，用后一个的位置去截前一个的说明。
    # 🔴 名字后面必须紧跟【类型词】才算命中。少了这个锚点，短名字会命中在
    #    长名字的前缀里 —— 实测 WEIGHT 命中 `WEIGHT_FACTOR`、SHARE_LST 命中
    #    `SHARE_LST_TYPE_NAME`、nav 命中 `nav_per_cu`，于是那两行的说明**互相串了**
    #    （一行空着，另一行里塞着别人的名字和类型）。而这不报错，只是文档在说谎。
    typ = '|'.join(TYPE_WORDS)
    # 名字与类型词之间偶尔夹着排版垃圾 —— 实测 `HOLDER_QUANTITY , float` 多个逗号、
    # `TOTAL_HOLDING_SHR" float` 多个引号。所以分隔符允许标点。
    sep = r'[\s,，、:：\'\"”’]{0,4}'
    found = {}
    for name in expected:
        for pat in (r'(?m)^[ \t]*(' + flex(name) + r')' + sep + r'(?:' + typ + r')\b',
                    r'(' + flex(name) + r')' + sep + r'(?:' + typ + r')\b'):
            m = re.search(pat, seg)
            if m:
                found[name] = (m.start(1), m.end(1))
                break
    # 🔴 说明的右边界要取【下一个字段行的行首】，不能取"下一个我要找的字段"——
    #    长表格跨页时**表头几行会在每页顶部重复**（MARKET_CODE / SECURITY_NAME /
    #    ANN_DATE …）。那些重复行没人认领，就会整段并进上一个字段的说明里
    #    （实测 income.LESS_SELLING_EXP 的说明后面挂着 "MARKET_CODE str 证券代码"）。
    #    而这不报错，只是文档里多出一段别人的内容。
    #    同理它也挡住另一头：手册列了、SDK 不返回的字段（etf_pcf_info 的
    #    net_creation_limit_per_user）也是一个行首，照样能截断。
    #    ★ 行首名字里要容一个空格：排版会在某些字距对上插空格（`ANN_DA TE`
    #      `ACC_RECEIV ABLE` `STA TEMENT_TYPE`）。不容的话那种行认不出是行首，
    #      于是它整行并进上一个字段的说明里（实测 income 的重复表头就是这样漏的）。
    row_starts = sorted(m.start(1) for m in re.finditer(
        r'(?m)^[ \t]*([A-Za-z][A-Za-z0-9_]*(?:[ ][A-Za-z][A-Za-z0-9_]*)?)'
        + sep + r'(?:' + typ + r')\b', seg))
    order = sorted(found.items(), key=lambda kv: kv[1][0])
    bounds = {}
    for i, (name, (st, e)) in enumerate(order):
        nxt = order[i + 1][1][0] if i + 1 < len(order) else len(seg)
        # 🔴 判据是 `rs >= e`（名字**结束**之后），不能是 `rs > st`：名字被换行切开时
        #    （`AGENCY_BUSINESS_LI` / `AB float 代理业务负债`）后半截自己也是个行首，
        #    用 st 比的话右边界会落在名字中间，整段说明变成空的 —— 实测 271 个字段一起空掉。
        for rs in row_starts:
            if rs >= e:
                nxt = min(nxt, rs)
                break
        bounds[name] = (e, max(e, nxt))

    rows, missing = {}, []
    for name in expected:            # 落盘的列序仍然照 pyc
        if name not in bounds:
            missing.append(name)
            rows[name] = ('', UNDOC_DESC)
            continue
        a, b = bounds[name]
        rows[name] = parse_desc(seg[a:b])
    missing = [f for f in missing if (table, f) not in KNOWN_UNDOCUMENTED]
    if missing and not allow_missing:
        raise SystemExit(
            f'🔴 {pdf_name}: 有 {len(missing)} 个 pyc 里的字段在 PDF 文本里没找到：'
            f'{missing[:12]}{" …" if len(missing) > 12 else ""}\n'
            f'   （PDF 排版会把字段名拆字/断行，先确认是不是解析漏了；'
            f'确认手册真的没写就加进 KNOWN_UNDOCUMENTED，'
            f'或显式 --allow-missing 放过这一次）')
    return rows, missing


# ------------------------------------------------------------------ 自证
def selfcheck(docs):
    r"""解析完立刻自证。判据两条，都是实测踩出来的失效模式：

    ① **说明不许是空的** —— 空说明的表现就是"手册好像没写"，而真正的原因
       多半是右边界算错了（`rs >= e` 那条注释里的 271 个字段一起空掉）。
       白名单里的（手册确实没写）除外。
    ② **说明里不许出现裸的类型词**（`str` / `float64` …）—— 出现就说明
       右边界越过了下一行，把下一个字段的"名字 类型 说明"整段吃进来了。

    🔴 这两条必须在**生成时**跑，不能只在改代码时跑一次：手册会出新版本，
    排版一变解析就可能悄悄降级，而降级的产物**看着仍然是一份完整的文档**。
    """
    empty, bleed = [], []
    for tbl, rows in docs.items():
        for fld, v in rows.items():
            if fld == '__source__':
                continue
            typ, desc = v
            if desc == UNDOC_DESC:
                continue
            if not desc.strip():
                empty.append(f'{tbl}.{fld}')
            for w in TYPE_WORDS:
                if re.search(r'(?<![A-Za-z_])' + w + r'(?![A-Za-z0-9_])', desc):
                    bleed.append(f'{tbl}.{fld} -> {desc[:60]!r}')
                    break
    if empty or bleed:
        msg = ['🔴 字段说明自证不通过：']
        if empty:
            msg.append(f'  {len(empty)} 个字段说明是空的：{empty[:8]}')
        if bleed:
            msg.append(f'  {len(bleed)} 个字段的说明里串进了下一行：{bleed[:5]}')
        raise SystemExit('\n'.join(msg))
    n = sum(len(v) - 1 for v in docs.values())
    print(f'  自证通过：{n} 个字段，说明都非空、都没串行')


# ------------------------------------------------------------------ 生成模块
HEADER = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AmazingData 各表的字段说明 —— **生成物，不要手改**。

    重新生成：tools/parse_manual_fields.py（怎么跑、判据是什么都在它的 docstring 里）

来源两处：字段名与列序取自 wheel 里 `download_xxx()` 的 `columns_list` 常量元组
（那就是返回值真实的列），中文说明取自手册 PDF 的字段表。
🔴 不要照 PDF 抄字段名 —— pypdf 抽出来的名字被排版拆了字
（`STA TEMENT_TYPE` / `ACC_RECEIV ABLE`），对不上真实返回值**而不报错**。

结构：FIELDS[表名] = {字段名: (类型, 说明)}；`__source__` 键是那张表说明的出处。
"""

# 由 tools/parse_manual_fields.py 生成，共 {n_tables} 张表 / {n_fields} 个字段
FIELDS = {
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pdf', default=os.path.join(HERE, '..', '..', 'AmazingData开发手册.pdf'))
    ap.add_argument('--wheel', default=None, help='默认自动找 ../../AmazingData-*-cp313-none-any.whl')
    ap.add_argument('--out', default=os.path.join(HERE, '..', 'field_docs.py'))
    ap.add_argument('--allow-missing', action='store_true')
    a = ap.parse_args()

    wheel = a.wheel
    if not wheel:
        import glob
        cands = sorted(glob.glob(os.path.join(HERE, '..', '..', 'AmazingData-*-cp313-none-any.whl')))
        if not cands:
            raise SystemExit('🔴 没找到 AmazingData 的 cp313 wheel，用 --wheel 指定')
        wheel = cands[0]

    print(f'wheel : {wheel}')
    print(f'pdf   : {a.pdf}')
    cols = cols_from_wheel(wheel)
    known = {f for v in cols.values() if v for f in v}
    text = pdf_text(a.pdf, known)

    docs = {}
    for name, _meth, pdf_name in TABLES:
        expected = cols.get(name)
        rows, missing = extract(text, pdf_name, expected, a.allow_missing, name)
        src = f'手册「{pdf_name}的字段说明」'
        if expected:
            src += f'；列名与列序取自 wheel 的 columns_list（{len(expected)} 列）'
        rows = dict(rows)
        rows['__source__'] = src
        docs[name] = rows
        n_undoc = sum(1 for k, v in rows.items() if k != '__source__' and v[1] == UNDOC_DESC)
        flag = '' if not n_undoc else f'  ⚠ {n_undoc} 个字段手册未列说明'
        print(f'  {name:22s} {len(rows) - 1:4d} 字段{flag}')

    for name, rows in EXTRA_DOCS.items():
        docs[name] = dict(rows)
        print(f'  {name:22s} {len(rows) - 1:4d} 字段  (手工抄)')

    selfcheck(docs)
    n_fields = sum(len(v) - 1 for v in docs.values())
    out = [HEADER.replace('{n_tables}', str(len(docs))).replace('{n_fields}', str(n_fields))]
    for tbl in docs:
        out.append(f'    {tbl!r}: {{\n')
        rows = docs[tbl]
        out.append(f'        {"__source__"!r}: {rows["__source__"]!r},\n')
        for fld, v in rows.items():
            if fld == '__source__':
                continue
            typ, desc = v
            out.append(f'        {fld!r}: ({typ!r}, {desc!r}),\n')
        out.append('    },\n')
    out.append('}\n\n\n')
    out.append('''def columns_of(table):
    """表的字段名列表（列序 = wheel 里 columns_list 的列序）。"""
    return [k for k in FIELDS[table] if k != '__source__']


def describe(table, field):
    """(类型, 说明)；查不到返回 ('', '')——**不猜**。"""
    return FIELDS.get(table, {}).get(field, ('', ''))
''')
    path = os.path.abspath(a.out)
    with io.open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(''.join(out))
    print(f'\n写出 {path}  ({len(docs)} 张表 / {n_fields} 个字段)')


if __name__ == '__main__':
    main()
