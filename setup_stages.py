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

    # ① tdx2db（上游 github.com/jing2uo/tdx2db，有 Windows_x86_64 预编译包）
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
        'id': 'tdx2db', 'name': '① 抓数程序 tdx2db',
        'why': '通达信日线与复权因子的抓取程序。上游有 macOS / Linux / '
               'Windows 预编译包，setup_tdx.py --install 会按本机 OS 选。',
        'state': 'ok' if ver else 'todo',
        'detail': ver or '还没装 —— 点右边装（约 25 MB）',
        'cmd': [py, os.path.join(DL, 'setup_tdx.py'), '--install'],
        'eta_min': (1, 1),
        'eta': '约 1 分钟'})

    # ② tdx.db（全量日线）
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
        'id': 'bootstrap', 'name': '② 全量日线 tdx.db',
        'why': '一次性下全市场历史日线并建库（约 1.4 GB）。之后每天只做增量。',
        'state': 'ok' if os.path.isfile(db) else 'todo',
        'detail': ('%.1f GB' % (os.path.getsize(db) / 1e9)) if os.path.isfile(db)
                  else '还没有 —— 要先装好 ①',
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

    # ③ std（交易日历 / 分红 / 财务的规范层）
    cal = os.path.join(DL, 'std', 'trading_calendar.parquet')
    r = _q("SELECT max(date) FROM read_parquet('%s')" % cal) if os.path.isfile(cal) else None
    out.append({
        'id': 'std', 'name': '③ 规范层 std/',
        'why': '交易日历（每次生成都重跑对数，不一致就拒绝写出）等。',
        'state': 'ok' if r and r[0] else 'todo',
        'detail': ('交易日历到 %s' % str(r[0])[:10]) if r and r[0] else '还没有',
        'cmd': [py, os.path.join(DL, 'build', 'load_tdx_kline.py')],
        'eta_min': (2, 2),
        'eta': '约 2 分钟'})

    # ④ 面板 —— **回测与看板的地基**，没有它几乎所有页面都是空的
    pd_ = _panel_day(DL)
    out.append({
        'id': 'panel', 'name': '④ 面板 mart/panel_daily',
        'why': '回测、选股、盘面、个股页全都读它。没有它这些页面都是空的。',
        'state': 'ok' if pd_ else 'todo',
        'detail': ('最新 %s ｜ %d 个年文件' % (pd_, _n_files(
            os.path.join(DL, 'mart', 'panel_daily', 'panel_*.parquet')))) if pd_
            else '还没有 —— 要先有 ③',
        'cmd': [py, os.path.join(DL, 'build', 'build_panel_daily.py')],
        'eta_min': (6, 6),
        'eta': '全量约 6 分钟'})

    # ⑤ 因子面板（研究链，实盘不读）
    nf = _n_files(os.path.join(DL, 'mart', 'factor_daily', 'factor_*.parquet'))
    out.append({
        'id': 'factor', 'name': '⑤ 因子面板 mart/factor_daily',
        'why': '162 个因子 × 24 年（约 9 GB）。因子广场与 feed.factors 读它。'
               '★ 纯研究链 —— 实盘出信号、模拟盘推进一个字都不读。',
        'state': 'ok' if nf else 'todo',
        'detail': ('%d 个年文件' % nf) if nf else '还没有 —— 要先有 ④',
        'cmd': [py, os.path.join(DL, 'build', 'build_factor_daily.py')],
        'eta_min': (30, 30),
        'eta': '全量约 30 分钟'})

    # ⑥ 因子评价分片（增量）
    ns = _n_files(os.path.join(ROOT, 'assay', 'factors', 'ic', '*', '*.parquet'))
    out.append({
        'id': 'faceval', 'name': '⑥ 因子评价 assay/factors',
        'why': '分池 IC 与换手（9 个池 × 24 年）。增量的：稳态每天约 4.5 分钟。',
        'state': 'ok' if ns else 'todo',
        'detail': ('%d 片 IC' % ns) if ns else '还没有 —— 要先有 ⑤',
        'cmd': [py, os.path.join(ROOT, 'assay', 'assay', 'factor_eval.py'), '--build-only'],
        'eta_min': (44, 44),
        'eta': '首建约 44 分钟'})

    # ⑦ B 腿：聚宽财务（**人工**）
    fq = os.path.join(DL, 'raw', 'jq', 'financials', 'income.parquet')
    r2 = _q("SELECT max(pub_date) FROM read_parquet('%s')" % fq) if os.path.isfile(fq) else None
    out.append({
        'id': 'jq', 'name': '⑦ 财务数据（聚宽 · 人工）',
        'why': '聚宽研究环境没有本地 API，必须人工导出再上传。'
               '缺了它 PB/ROE 那类筛选会静默丢票。',
        'state': 'ok' if r2 and r2[0] else 'manual',
        'detail': ('pub_date 到 %s' % str(r2[0])[:10]) if r2 and r2[0]
                  else '用下面「取聚宽代码 → 上传导出的包」那条闭环',
        'cmd': None,
        'eta': '人工'})
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

    ★ 只算**能自动跑**的（`cmd`）：⑦ 财务数据是人工那一步，把它算进
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
            'eta_text': _eta_total(todo),
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
