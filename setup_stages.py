# -*- coding: utf-8 -*-
"""数据装配的【阶段与状态】—— 页面「🔄 数据」与命令行共用的正本。

用户："没有数据也要能启动 server，然后点击数据加载开始同步数据。"

从零到齐要**几个小时**（装 tdx2db -> 下全量日线 -> 建 tdx.db -> raw/std ->
面板 -> 因子面板 9.1 GB -> 因子评价）。所以不做成"一个按钮跑到底"：
中途挂了只能从头来，而且屏幕上看不出卡在哪一步。

🔴 **每个阶段的状态取"现在磁盘上是什么"，不是"跑过没有"。**
  记一个 `done: true` 的标记文件是最省事的做法，然后它会在
  「文件被删了 / 手工跑过 / 换了台机器」时说谎，**而它不报错**
  （同 launchd 那条：判据永远是"现在到底开着没"，不是记录）。

★ 每个阶段都**可续跑**：它们本来就是幂等的（tdx2db cron 增量、
  daily_snapshot 内容哈希去重、面板按年重建、因子面板比对指纹秒退）。
★ 缺的东西要**说清下一步**，不要只说"没有"（同「报错必须指向真正的原因」）。

⚠ **B 腿（聚宽财务）不在这里能自动装** —— 聚宽研究环境没有本地 API，
  必须人工导出。页面上已经有「取聚宽代码 / 上传导出的包」那条闭环，
  这里只报它落后多少。
"""

import glob
import os
import platform
import subprocess
import sys

DL = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(DL)
# ---- datalake 侧路径的正本：`datalake/paths.py` ----
# 🔴 **往上找它，不数 dirname 层数** —— 层数跟着"这个文件放在哪"变，
#   搬一次就要改一次，而改漏了不报错（同 assay/paths.py 那条）。找不到就一路
#   走到文件系统根，导入正本时抛 ImportError —— **响亮失败**，不会静默
#   退回某个猜出来的路径。
_d = os.path.dirname(os.path.abspath(__file__))
while _d != os.path.dirname(_d) and not os.path.isfile(
        os.path.join(_d, 'paths.py')):
    _d = os.path.dirname(_d)
sys.path.insert(0, _d)
from paths import TDX_DIR as TDX, tdx_dir, tdx2db_bin             # noqa: E402


def _bin(tdx=TDX):
    # 🔴 可执行文件名（Windows 上是 .exe）只在 `paths.tdx2db_bin` 一处定义 ——
    #   此前 4 处各写一遍，而 setup_tdx 那份用的还是 `os.name == 'nt'`。
    return os.path.join(tdx, os.path.basename(tdx2db_bin()))


def _q(sql):
    """对 parquet 问一句 —— 拿不到就返回 None（**不抛**）。

    🔴 这一层的调用方是「还没有数据」的页面，抛异常会让整块打不开，
      而那正是这次要修的东西。
    """
    try:
        import duckdb
        return duckdb.connect().execute(sql).fetchone()
    except Exception:                                       # noqa: BLE001
        return None


def _n_files(pat):
    return len(glob.glob(pat))


def _panel_day(DL=DL):
    g = os.path.join(DL, 'mart', 'panel_daily', 'panel_*.parquet')
    if not _n_files(g):
        return None
    r = _q("SELECT max(date) FROM read_parquet('%s')" % g)
    return str(r[0])[:10] if r and r[0] else None


def _vipdoc_url():
    """全量日线包的地址 —— **从 `setup_tdx` 取，不在这里再写一遍**。

    两处各写一份的话，哪天上游换了地址，页面上那个「浏览器下载」按钮
    会指到一个不存在的 URL，**而它不报错**（点了下个 404 回来）。
    """
    try:
        sys.path.insert(0, DL)
        import setup_tdx as _st                             # noqa: E402
        return _st.VIPDOC_URL
    except Exception:                                       # noqa: BLE001
        return None


# ------------------------------------------------------ 阶段之间的【依赖】
#
# 🔴🔴 **④ 与 ⑤ 读的是【聚宽 B 腿】产的表，而 ① 是人工那一步。**
#   2026-09-27 真机（Windows，全新 clone）一键建库跑到规范层那步当场崩
#   （当时聚宽那步排在**最后**，2026-09-28 挪到第一位）：
#
#       _duckdb.CatalogException: Catalog Error:
#       Table with name security_universe does not exist!
#
#   而清单里**没有任何地方声明这条依赖** —— 于是一键建库在新机器上
#   **结构上就跑不完**，报出来的还是一个表名（技术上准确，却答不了
#   "我现在该做什么"）。
#
# 这几张表的来龙去脉（实测，不是推的）：
#   `std/<表名>.parquet` 由聚宽那条链写出，`build/load_jq_dimensions.py`
#   末尾 **删掉 lake.db 重建**，按 `std/` 下每个 parquet 建一个同名 view。
#   所以在 SQL 里裸写 `security_universe` 能解析，要求两件事同时成立：
#       ① `std/<表名>.parquet` 在
#       ② lake.db 里注册了同名 view
#   缺 ① 报 IOException、缺 ② 报 CatalogException —— **两种都不报"该做什么"**。
#
# ★ 判据用的就是这两件事本身，不是启发式：满足了这条 SQL 一定解析得开。
JQ_STD = ('security_universe', 'security_name', 'security_industry',
          'security_status', 'index_member_asof',
          'fin_indicator_q', 'fin_quarterly', 'share_change')
# 表 -> 谁产的（阶段 id）。
# 🔴 未登记的名字**当场 assert**。⚠ 如实记：没有这句它也会崩
#   （下面 `NEED_OWNER[t]` 抛 KeyError）—— 所以这句买到的不是
#   "从静默变响亮"，而是**报错里有一句话说清该去哪登记**
#   （同「报错必须指向真正的原因」；别把它说成防静默的那一类）。
NEED_OWNER = dict((t, 'jq') for t in JQ_STD)

# 🔴🔴 **还有一条依赖【不是 std 表】，而它差点漏掉。**
#   ⑥ 因子面板的 `build/factors/load.py:58` 调 `fin.asof_sql(root, ...)`，
#   而 `build/factors/fin.py:89-91` 直接 `read_parquet` 读
#       raw/jq/financials/{balance,income,cashflow}.parquet
#   —— 它们同样出自聚宽那条链（`build/load_jq_financials.py` 写 L0），
#   只是**没有 std 层、也不进 lake.db 视图**，所以上面那套
#   「std parquet + 同名 view」的判据**看不见它**。
# ★ 为什么不能只当成文案问题：loader 是**一个一个**跑的。人跑完
#   `load_jq_indicator_q.py`（`fin_quarterly` 就有了）而还没跑
#   `load_jq_financials.py` 时，⑥ 会**解除拦截然后崩在一个 raw 路径上**
#   —— 那是真实可达的中间状态，不是理论上的。
NEED_RAW = {
    'jq_financials': ('raw/jq/financials/income.parquet',
                      'raw/jq/financials/balance.parquet',
                      'raw/jq/financials/cashflow.parquet'),
}
# 报错里印给人看的名字 —— `jq_financials` 是内部 key，写进
# 「缺 jq_financials」那句话里人读不出它是什么。
NEED_LABEL = {'jq_financials': '聚宽三大财务报表（raw/jq/financials/）'}
NEED_OWNER.update(dict((t, 'jq') for t in NEED_RAW))

# 🔴🔴 **① 其实是两件事，而页面上那条闭环只有后一件：**
#
#     (a) 一次性全量抽取   extract_jq_dimensions / index_members / financials
#                         / round2~4 / indicator_q …  —— 页面上【没有入口】
#     (b) 每次增量补数     extract_jq_increment —— 「取聚宽代码 → 上传包」那条
#
#   实测：增量那个 tar 里只有 `dim_name_history` / `dim_status_change`，
#   **没有 `dim_security`**，而 `security_universe` 正是从它建的
#   （`load_jq_dimensions.py:103` 的 `raw['dim_security']`）。
#   所以在**新机器**上对人说"去页面上传导出的包"是**指了一条走不通的路**
#   （同 backLink 那条：说了不能做却不给出路，最后会变成绕过整个入口）。
#
# 表 -> (在聚宽研究环境跑哪个抽取脚本, 拿回来跑哪个 loader)
JQ_SOURCE = {
    'security_universe': ('extract_jq_dimensions.py',
                          'build/load_jq_dimensions.py'),
    'security_name': ('extract_jq_dimensions.py',
                      'build/load_jq_dimensions.py'),
    'security_industry': ('extract_jq_dimensions.py',
                          'build/load_jq_dimensions.py'),
    'security_status': ('extract_jq_dimensions.py',
                        'build/load_jq_dimensions.py'),
    # 🔴 抽取脚本是 **round2** 不是 index_members —— 第一版按名字猜的，
    #   而 `load_jq_round2.py` 自己的 docstring 第 8 行写着
    #   「输入（downloads/，来自 extract_jq_round2.py）: index_member_*.csv」。
    'index_member_asof': ('extract_jq_round2.py',
                          'build/load_jq_round2.py'),
    'fin_indicator_q': ('extract_jq_indicator_q.py',
                        'build/load_jq_indicator_q.py'),
    # 🔴 第五对，见 NEED_RAW 那段：它不产 std 表，但 ⑥ 少了它必崩。
    #   漏掉它的后果不是「少拦一次」，是**人要去聚宽两趟** ——
    #   而第二趟那句话要等他先白跑三四十分钟的面板才看得到。
    'jq_financials': ('extract_jq_financials.py',
                      'build/load_jq_financials.py'),
    'fin_quarterly': ('extract_jq_indicator_q.py',
                      'build/load_jq_indicator_q.py'),
    'share_change': ('extract_jq_share_change.py',
                     'build/load_jq_share_change.py'),
}


def _lake_views(DL=DL):
    """lake.db 里注册了哪些 view。

    返回 `set()` 表示"一个都没有"（文件不在 —— 那是**事实**）；
    返回 `None` 表示**查不出来**（被锁住 / 文件坏了）。
    🔴 两者要分开：查不出来时**不拦**（不猜），否则会在 lake.db 恰好被
      占着的那一刻把人挡在外面，而那与"缺数据"完全是两回事。
    """
    db = os.path.join(DL, 'lake.db')
    if not os.path.isfile(db):
        return set()
    try:
        import duckdb
        c = duckdb.connect()
        c.execute("ATTACH '%s' AS _blk (READ_ONLY)" % db.replace("'", "''"))
        rows = c.execute("SELECT view_name FROM duckdb_views() "
                         "WHERE database_name='_blk'").fetchall()
        c.close()
        return set(r[0] for r in rows)
    except Exception:                                       # noqa: BLE001
        return None


def _blocked(needs, DL=DL, views=None):
    """这一步现在跑得动吗 —— 跑不动就说清**缺什么、谁产的、下一步做什么**。

    `views` 传 `_lake_views()` 的结果（一次查、多个阶段共用）。
    返回 None（能跑）或一个 dict：by / missing / why。
    """
    if not needs:
        return None
    for t in needs:
        assert t in NEED_OWNER, (
            '阶段声明了 needs=%r，而 %r 没登记在 NEED_OWNER 里 —— '
            '不知道它由哪一步产出，就没法告诉人下一步做什么。' % (needs, t))
    if views is None:
        return None                     # 查不出来 -> 不拦
    miss = []
    for t in needs:
        if t in NEED_RAW:
            # ★ 这一类只查文件在不在：它不进 lake.db，没有视图可查。
            gone = [p for p in NEED_RAW[t]
                    if not os.path.isfile(os.path.join(DL, p))]
            if gone:
                miss.append((t, '还没有这份数据'))
        elif not os.path.isfile(os.path.join(DL, 'std', t + '.parquet')):
            miss.append((t, '还没有这份数据'))
        elif t not in views:
            miss.append((t, 'lake.db 里没注册这个视图'))
    if not miss:
        return None
    return {'by': sorted(set(NEED_OWNER[t] for t, _ in miss)),
            'missing': [{'table': t, 'why': w} for t, w in miss]}


def blocked_text(b):
    """把「跑不动」说成一句人话 —— 缺什么、谁产的、**下一步做什么**。

    🔴 只说"表不存在"是不够的（真机上 duckdb 报的就是那个）：
      人要的是"我现在该点哪里"。
    """
    miss = '、'.join('%s（%s）' % (NEED_LABEL.get(m['table'], m['table']),
                                  m['why']) for m in b['missing'])
    tail = ''
    if 'jq' in b['by']:
        # 🔴 **分两种，给的下一步完全不同**（混成一句就会把新机器指到
        #   一条走不通的路上 —— 增量那个 tar 里没有 dim_security）：
        zero = [m for m in b['missing'] if m['why'].startswith('还没有')]
        if zero:
            ex = sorted(set(JQ_SOURCE[m['table']][0] for m in zero
                            if m['table'] in JQ_SOURCE))
            ld = sorted(set(JQ_SOURCE[m['table']][1] for m in zero
                            if m['table'] in JQ_SOURCE))
            tail = ('本地从来没有过这份数据（新机器就是这样）。'
                    '⚠ 页面上那条「取聚宽代码 → 上传导出的包」只管【增量】，'
                    '它的包里只有 dim_name_history / dim_status_change，'
                    '救不了这一步。'
                    '从零要先在聚宽研究环境跑【一次性全量抽取】：'
                    'datalake/raw/jq/_ingest/ 下的 %s（把整个文件粘进一个 '
                    'cell 跑完），下载打包的 tar 放进 '
                    'raw/jq/_ingest/downloads/，再跑 %s。'
                    % ('、'.join(ex) or '那几个 extract_jq_*.py',
                       '、'.join(ld) or '对应的 build/load_jq_*.py'))
        else:
            # 文件在、只是 lake.db 里没这几个视图 —— 重建一次就好
            ld = sorted(set(JQ_SOURCE[m['table']][1] for m in b['missing']
                            if m['table'] in JQ_SOURCE))
            tail = ('数据文件在，只是 lake.db 里没注册这几个视图（它是 '
                    'gitignored 的产物，换机器不会带过来）。跑一次 %s 就会'
                    '重建 lake.db 的视图层。'
                    % ('、'.join(ld) or 'build/load_jq_dimensions.py'))
    return '要先有「%s」—— 缺 %s。%s' % (b.get('by_name') or '/'.join(b['by']),
                                        miss, tail)


def runnable(st):
    """一键建库这一次**真的会跑到**的那几步 —— 遇到第一个跑不动的就停。

    🔴 **不是"把跑不动的跳过去接着跑下一个"**：后面每步都吃前一步的产物
      （⑥ 因子面板吃 ⑤ 的面板），跳过去只是换个地方崩
      （同 `_go_all` 那条「失败就停」）。
    """
    got = []
    for s in st:
        if s.get('blocked'):
            break
        if s.get('cmd') and s['state'] == 'todo':
            got.append(s)
    return got


def stages(DL=DL, ROOT=ROOT):
    """七个阶段，每个带：现在什么状态 / 还缺什么 / 下一步跑什么 / 大概多久。

    `state` 只有三种，页面照它上色：
        ok      齐了
        todo    还没有 —— 有 `cmd` 就能点
        manual  要人工（聚宽那条腿）
    """
    py = sys.executable
    # 🔴🔴 **这一行不是"重复的局部"，是这个函数的参数化。**
    #   `stages(DL=临时空目录)` 就是靠它把整条链指过去的（用例在空目录上
    #   验"什么都还没建"）。我上一轮当成分叉删掉 -> 空 lake 上 tdx2db 与
    #   bootstrap 两个阶段去查**真实目录**、报成 `ok`，**而它不报错**。
    #   是守卫当场抓到的。段只在 `paths.tdx_dir` 一处拼。
    TDX = tdx_dir(DL)
    out = []

    # ① B 腿：聚宽财务（**人工** —— 而且它排在最前面，见下）
    #
    # 🔴🔴 **它必须排第一，因为 ②~⑦ 里有三步压在它产的那 8 张表上**
    #   （③ 规范层 3 张、④ 面板 8 张、⑤ 因子面板 1 张）。排在最后的那一版
    #   让屏幕上看着像"还差 6 步、点一下就好"，而真机上一键建库最多跑到
    #   全量日线就崩在一个表名上（`security_universe does not exist`）。
    #
    # 🔴 **提示必须一次点全那 4 个抽取脚本。** 按"这一步缺哪几张"分别提示
    #   的话，人跑完 dimensions 回来，到面板那步又缺 index_member_asof /
    #   fin_indicator_q / fin_quarterly / share_change —— **要去聚宽两趟**，
    #   而第二趟那句话要等他先白跑 30~60 分钟的日线才看得到。
    # ★ 清单从 `JQ_SOURCE` **派生**，不在这里手写一份
    #   （同「照清单拼会漏掉新文件」：手写那份不会跟着 JQ_SOURCE 变）。
    fq = os.path.join(DL, 'raw', 'jq', 'financials', 'income.parquet')
    r2 = _q("SELECT max(pub_date) FROM read_parquet('%s')" % fq) if os.path.isfile(fq) else None
    _ex = '、'.join(sorted(set(v[0] for v in JQ_SOURCE.values())))
    _ld = '、'.join(sorted(set(v[1] for v in JQ_SOURCE.values())))
    out.append({
        'id': 'jq', 'name': '① 财务数据（聚宽 · 人工）',
        'why': '聚宽研究环境没有本地 API，必须人工导出再拿回来。'
               '③④⑤ 三步都读它产的表 —— 没有它那三步一定崩，'
               '而且缺了它 PB/ROE 那类筛选会静默丢票。',
        'state': 'ok' if r2 and r2[0] else 'manual',
        # 🔴 **从零与"有但过期"的下一步不一样**，不能都写"用下面那条闭环"
        #   —— 那条只管增量（见 JQ_SOURCE 上面那段）。
        'detail': ('pub_date 到 %s' % str(r2[0])[:10]) if r2 and r2[0]
                  else ('本地从来没有过（新机器就是这样）。要在聚宽研究环境'
                        '跑【一次性全量抽取】：raw/jq/_ingest/ 下的 %s'
                        '（每个把整份文件粘进一个 cell），把下载的 tar '
                        '全部放进 raw/jq/_ingest/downloads/，再依次跑 %s。'
                        '做完这一步，后面 6 步机器全自动。'
                        '⚠ 页面上那条「取聚宽代码 → 上传导出的包」只管'
                        '【增量】，它的包里只有 dim_name_history / '
                        'dim_status_change，救不了这一步'
                        % (_ex, _ld)),
        'cmd': None,
        'eta': '人工'})

    # ② tdx2db（上游 github.com/jing2uo/tdx2db，有 Windows_x86_64 预编译包）
    b = _bin(TDX)
    ver = None
    if os.path.isfile(b):
        try:
            p = subprocess.run([b, '--version'], capture_output=True,
                               text=True, timeout=20)
            ver = (p.stdout + p.stderr).strip().splitlines()[0][:40]
        except Exception:                                   # noqa: BLE001
            ver = '（装了，但问不出版本）'
    out.append({
        'id': 'tdx2db', 'name': '② 抓数程序 tdx2db',
        'why': '通达信日线与复权因子的抓取程序。上游有 macOS / Linux / '
               'Windows 预编译包，setup_tdx.py --install 会按本机 OS 选。',
        'state': 'ok' if ver else 'todo',
        'detail': ver or '还没装 —— 点右边装（约 25 MB）',
        'cmd': [py, os.path.join(DL, 'setup_tdx.py'), '--install'],
        'eta_min': (1, 1),
        'eta': '约 1 分钟'})

    # ③ tdx.db（全量日线）
    db = os.path.join(TDX, 'tdx.db')
    day = None
    if os.path.isfile(db):
        r = _q("SELECT max(trade_date) FROM read_parquet('%s')"
               % os.path.join(DL, 'raw', 'tdx', 'kline', 'stock_*.parquet')) \
            if _n_files(os.path.join(DL, 'raw', 'tdx', 'kline', 'stock_*.parquet')) else None
        day = str(r[0])[:10] if r and r[0] else '（有库，未导出）'
    # 🔴🔴 **这一步下不下得来，不在我们手上。** 2026-09-27 真机实测：
    #   `data.tdx.com.cn` 前面挂着腾讯云 EdgeOne 的 Bot 管理，脚本拿回来
    #   的是一段混淆 JS（`EO_Bot_Ssid`）而不是 548 MB 的包 ——
    #   **而同一个 URL、同样的请求，在另一台机器上照样下得到**。
    #   也就是说差别在出口 IP 信誉 / TLS 指纹这些我们观察不到的东西上，
    #   靠改 UA 猜不出来（换成浏览器全套头实测毫无区别）。
    #   ★ 所以给一条**不依赖猜**的路：浏览器自己去下（它会执行那段挑战
    #     JS，所以一定过得去），下好之后把文件交回来。
    #   🔴 页面**不许写死**这里的 URL / 文件名 / 接口 —— 清单在服务端
    #     （同「加一个指标，广场上自动就有」那条）。
    _zp = os.path.join(TDX, 'hsjday.zip')
    _have = os.path.isfile(_zp) and os.path.getsize(_zp) > (1 << 20)
    out.append({
        'id': 'bootstrap', 'name': '③ 全量日线 tdx.db',
        'why': '一次性下全市场历史日线并建库（约 1.4 GB）。之后每天只做增量。',
        'state': 'ok' if os.path.isfile(db) else 'todo',
        'detail': ('%.1f GB' % (os.path.getsize(db) / 1e9)) if os.path.isfile(db)
                  else '还没有 —— 要先装好 ②',
        'cmd': [py, os.path.join(DL, 'setup_tdx.py'), '--bootstrap'],
        'upload': {
            'url': _vipdoc_url(),
            'name': 'hsjday.zip',
            'accept': '.zip',
            'api': '/api/setup/vipdoc',
            'dst': _zp,
            'have': _have,
            'have_mb': round(os.path.getsize(_zp) / 1e6, 1) if _have else None,
            'why': ('这一步要下一个 548 MB 的日线包。对方 CDN 会挡掉脚本'
                    '（不同机器不一样），而浏览器一定下得到 —— '
                    '下好之后从这里交回来，建库会直接用它、不再重下。'),
        },
        'eta_min': (30, 60),
        'eta': '约 30~60 分钟（下载为主）'})

    # ④ std（交易日历 / 分红 / 财务的规范层）
    cal = os.path.join(DL, 'std', 'trading_calendar.parquet')
    r = _q("SELECT max(date) FROM read_parquet('%s')" % cal) if os.path.isfile(cal) else None
    out.append({
        'id': 'std', 'name': '④ 规范层 std/',
        'why': '交易日历（每次生成都重跑对数，不一致就拒绝写出）等。',
        'state': 'ok' if r and r[0] else 'todo',
        'detail': ('交易日历到 %s' % str(r[0])[:10]) if r and r[0] else '还没有',
        'cmd': [py, os.path.join(DL, 'build', 'load_tdx_kline.py')],
        # 🔴 它 JOIN 的这三张表由**聚宽 B 腿**产出（见 JQ_STD 那段）——
        #   新机器上没有它们时，duckdb 报的是一个表名，而不是"去做 ①"。
        'needs': ('security_universe', 'security_name', 'security_industry'),
        'eta_min': (2, 2),
        'eta': '约 2 分钟'})

    # ⑤ 面板 —— **回测与看板的地基**，没有它几乎所有页面都是空的
    pd_ = _panel_day(DL)
    out.append({
        'id': 'panel', 'name': '⑤ 面板 mart/panel_daily',
        'why': '回测、选股、盘面、个股页全都读它。没有它这些页面都是空的。',
        'state': 'ok' if pd_ else 'todo',
        'detail': ('最新 %s ｜ %d 个年文件' % (pd_, _n_files(
            os.path.join(DL, 'mart', 'panel_daily', 'panel_*.parquet')))) if pd_
            else '还没有 —— 要先有 ④',
        'cmd': [py, os.path.join(DL, 'build', 'build_panel_daily.py')],
        # ST 标记 / 名称 / 行业 / 指数成分**全部来自聚宽** —— 所以把 ④ 降级
        #   （"先不要那三列"）也救不了 ④，这条链就是压在 ① 上的。
        # 🔴 第一版只声明了前五张 —— **漏了后三张**，而它们同样来自聚宽。
        #   清单是扫出来的不是想出来的：剥掉 docstring（SQL 就写在三引号里，
        #   连它一起剥会把要证的东西自己删掉）之后按 std/ 下的表名逐个搜，
        #   `build_panel_daily.py` 命中 10 张 —— 去掉 ④ 自己产的
        #   `code_map` / `trading_calendar`，剩下这 8 张全压在聚宽那步上。
        'needs': ('security_universe', 'security_name', 'security_industry',
                  'security_status', 'index_member_asof',
                  'fin_indicator_q', 'fin_quarterly', 'share_change'),
        'eta_min': (6, 6),
        'eta': '全量约 6 分钟'})

    # ⑥ 因子面板（研究链，实盘不读）
    nf = _n_files(os.path.join(DL, 'mart', 'factor_daily', 'factor_*.parquet'))
    out.append({
        'id': 'factor', 'name': '⑥ 因子面板 mart/factor_daily',
        'why': '162 个因子 × 24 年（约 9 GB）。因子广场与 feed.factors 读它。'
               '★ 纯研究链 —— 实盘出信号、模拟盘推进一个字都不读。',
        'state': 'ok' if nf else 'todo',
        'detail': ('%d 个年文件' % nf) if nf else '还没有 —— 要先有 ⑤',
        'cmd': [py, os.path.join(DL, 'build', 'build_factor_daily.py')],
        # ★ 它自己**直接**读两样聚宽产物，绕开 ⑤ 的面板：
        #     fin_quarterly            `factors/fin_ttm.py`（std 表）
        #     raw/jq/financials/*      `factors/fin.py:89-91`（**不是** std 表）
        #   排在 ⑤ 后面所以多数时候走不到这道拦截，但**声明要照实写** ——
        #   而第二条恰恰是可达的：loader 一个个跑，跑了 indicator_q 没跑
        #   financials 时它会解除拦截然后崩在一个 raw 路径上。
        'needs': ('fin_quarterly', 'jq_financials'),
        'eta_min': (30, 30),
        'eta': '全量约 30 分钟'})

    # ⑦ 因子评价分片（增量）
    ns = _n_files(os.path.join(ROOT, 'assay', 'factors', 'ic', '*', '*.parquet'))
    out.append({
        'id': 'faceval', 'name': '⑦ 因子评价 assay/factors',
        'why': '分池 IC 与换手（9 个池 × 24 年）。增量的：稳态每天约 4.5 分钟。',
        'state': 'ok' if ns else 'todo',
        'detail': ('%d 片 IC' % ns) if ns else '还没有 —— 要先有 ⑥',
        'cmd': [py, os.path.join(ROOT, 'assay', 'assay', 'factor_eval.py'), '--build-only'],
        'eta_min': (44, 44),
        'eta': '首建约 44 分钟'})

    # ---- 这一步现在跑得动吗：把「要先有 X」填上 ----
    # 🔴 **一次查 lake.db，七个阶段共用** —— 逐个阶段各连一次的话，
    #   这个函数（顶上那条横条每 20 秒问一次）就会连开好几个 duckdb 连接。
    _vs = _lake_views(DL)
    _nm = dict((s['id'], s['name']) for s in out)
    for s in out:
        if s['state'] == 'ok':
            continue                    # 已经建好的，谈不上"跑不动"
        b = _blocked(s.get('needs'), DL, _vs)
        if b:
            b['by_name'] = '、'.join(_nm.get(i, i) for i in b['by'])
            b['text'] = blocked_text(b)
            s['blocked'] = b
    return out



def _fmt_mins(m, unit=None):
    """分钟 -> 人话。90 分钟以上折成小时，免得出现「约 143 分钟」。

    ★ `unit` 是给**区间**用的：两端必须同一个单位，不然会写出
      「69 分钟~1.6 小时」这种要在脑子里换算一次的东西。
    """
    if unit is None:
        unit = 'min' if m < 90 else 'h'
    if unit == 'min':
        return '%d 分钟' % round(m)
    h = m / 60.0
    return ('%d 小时' % round(h)) if abs(h - round(h)) < 0.05 else '%.1f 小时' % h


def _eta_total(todo):
    """把还要跑的那几步的时长**加起来**，给一个总数（不是把文案串起来）。

    ★ 只算**能自动跑**的（`cmd`）：① 财务数据是人工那一步，把它算进
      「还要多久」里没有意义。
    ★ 一步都没声明时长就返回 None —— **不猜一个数**（同「拿不到分红那一格
      标查不到」）。
    """
    lo = hi = 0
    n = 0
    for s in todo:
        if not s.get('cmd'):
            continue
        em = s.get('eta_min')
        if not em:
            continue
        lo += em[0]
        hi += em[1]
        n += 1
    if not n:
        return None
    if lo == hi:
        return _fmt_mins(lo)
    u = 'min' if hi < 90 else 'h'          # 两端同单位
    # ★ 低端**不重复写单位**：「1.9 小时~2.4 小时」里那个「小时」是噪声。
    suf = ' 分钟' if u == 'min' else ' 小时'
    return '%s~%s' % (_fmt_mins(lo, u).replace(suf, ''), _fmt_mins(hi, u))


def summary(DL=DL, ROOT=ROOT):
    """一句话：还差几步、下一步该点哪个。"""
    st = stages(DL, ROOT)
    todo = [s for s in st if s['state'] == 'todo']
    auto = [s for s in st if s.get('cmd')]
    # 🔴 **一键这一次真的会跑到哪几步** —— 不是"所有待办"。
    #   ④⑤ 压在人工那步上时，横条写"还差 6 步 · 约需 2 小时"是在说谎：
    #   点下去只会跑完 ②③、停在 ④（同「悄悄截断比查不出来更糟」）。
    run = runnable(st)
    blk = next((s for s in st if s.get('blocked')), None)
    # 🔴 「下一步叫什么、大概多久」也在这里给 —— 页面上那条横条要说这句话，
    #   而让前端照 id 拼一份中文名就是**第二份阶段清单**（同「清单在服务端」）。
    return {'stages': st, 'n_todo': len(todo),
            'next': todo[0]['id'] if todo else None,
            'next_name': todo[0]['name'] if todo else None,
            'n_auto': len(auto),
            'n_auto_todo': len([s for s in todo if s.get('cmd')]),
            'n_manual_todo': len([s for s in todo if not s.get('cmd')]),
            # 粗估总时长：把**能自动跑**的那几步【加起来】给一个数。
            # 🔴 原来是 `' + '.join(各步的中文 eta)` —— 屏幕上就成了
            #   「约需 约 1 分钟 + 约 30~60 分钟（下载为主） + 约 2 分钟 +
            #     全量约 6 分钟 + …」这种读不通的串接，而人要的是**一个总数**。
            #   所以每步另给机器可读的 `eta_min`（下限, 上限），这里求和。
            # ⚠ 仍是各阶段自己声明的估计，不是实测 —— 页面上要写「估」。
            'eta_text': _eta_total(run),
            # 这一次点下去会跑几步 / 卡在哪一步、为什么
            'n_runnable': len(run),
            'n_blocked': len([s for s in st if s.get('blocked')]),
            'blocked_name': blk['name'] if blk else None,
            'blocked_text': blk['blocked']['text'] if blk else None,
            # ★ 横条那条只有一行，塞不下整句 `blocked_text`；而前端
            #   自己拼「要先有聚宽财务数据」就是**把产出方写死在页面上**
            #   （同「清单在服务端」）—— 将来换个产出方它会说谎。
            'blocked_by_name': (blk['blocked'].get('by_name')
                                if blk else None),
            'ready': not todo,
            'os': platform.system()}


if __name__ == '__main__':
    d = summary()
    print('本机 %s ｜ %s' % (d['os'],
          '数据已齐 ✅' if d['ready'] else '还差 %d 步，下一步：%s'
          % (d['n_todo'], d['next'])))
    for s in d['stages']:
        m = {'ok': '✅', 'todo': '⬜', 'manual': '✋'}[s['state']]
        print('%s %-28s %s' % (m, s['name'], s['detail']))
