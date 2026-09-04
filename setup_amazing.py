#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 本文件的 docstring 里有 Windows 路径（那是证据），所以是 raw string
r"""银河 AmazingData 的装配与体检（跨 macOS / Linux / Windows）。

    python3 datalake/setup_amazing.py                    # 体检，什么都不改
    python3 datalake/setup_amazing.py --install raw/amazing
    python3 datalake/setup_amazing.py --probe             # 拿已知真值对数

## 它是什么、为什么单独一个模块

中国银河证券「星耀数智 AmazingData」（手册 V1.0.24 / 2025-12-16，148 页，
正本在 `finacial/AmazingData开发手册.pdf`）。它是**券商行情网关 + 金融资讯**
的 Python SDK：`tgw` 是底层网关，`AmazingData` 是上层数据接口。

**不替代 tdx / 聚宽，是第四个源**（`raw/amazing/`）。判断依据：

| | |
|---|---|
| 🔴 行情起点 **2013 年** | 手册 §2.2 明写。本地面板到 2003，2013 前占 23%（378 万行）；归档回测里 21%（138/649 次）是 2005/2006 起的 |
| ✅ 它更强的地方 | 交易所口径的**涨跌停价 / ST / 停牌**（一张表替掉三个自制模块）、行业分类带**纳入/剔除日**、指数成分带**权重**、**财报"更正前"版本**（能修本项目那个已知缺陷）、融资融券/龙虎榜/大宗/可转债/国债收益率（全新） |

## 🔴 `pip install AmazingData` 装的是【另一个项目】

PyPI 上那个 `AmazingData` 是 **0.0.3**（gitee.com/zhanggao2013，13 KB，
作者字段还是模板占位 `your_email@example.com`），与银河这个 **1.0.24** 无关；
`tgw` 在 PyPI 上根本不存在（`pip index versions tgw` → No matching
distribution）。—— 与 `tdx2db` 那次**同一个坑**：装上去"看着成功"，
然后 API 全对不上。所以只能用银河网盘下载的 wheel 离线装。

## 🔴 macOS 跑不了 —— 已实测定案（2026-09-04，tgw 1.0.9.2）

`tgw` 装的是**预编译原生库**，按 `<os>_py<ver>_<arch>_package/` 分目录，
67 MB 的 wheel 里只有两套：

    tgw/linux_py{36,38,39,310,311,312,313,314}_x64_package/libtgw_python*.so
    tgw/win_py{36,38,39,310,311,312,313,314}_x64_package/_tgw.pyd + tgw.dll

**没有任何 .dylib，也没有 arm64。** 而取数那一整条链（`login` /
`download_data` / `query_api` / `subscribe_api`）连同 `AmazingData/__init__`
本身都引用 tgw —— 所以 macOS 上**一条数据都取不到**。
（不引用 tgw 的那 51 个模块是纯算子：numba 因子分析、Brinson 归因。
本项目自己有引擎，用不上。）

🔴 **而它报出来的错不指向真正的原因。** `tgw/__init__.py` 的平台判定是

    if 'win' in sys.platform:  os_info = 'win'

而 macOS 的 `sys.platform` 是 **`'darwin'`** —— **dar[win] 里含 'win'**，
子串命中，于是 macOS 被判成 **Windows**，去加载 `_tgw.pyd`，报出来的是

    ModuleNotFoundError: No module named '_tgw'

看到这句人会以为"包不全、重装一下"，而真正的原因是这个平台从来就不支持。
（那一行 `else: raise Exception('this system is not supported')` 永远到不了；
`platform.machine()` 返回的 `'arm64'` 也不在它的枚举里，静默落默认 64。）

所以本脚本 **在装之前就扫 wheel 里的平台目录、给出裁决** ——
判据落在"有没有本机这一款原生库"上，而不是落在一句误导性的 import 报错上。

★ 旁证：`.pyc` 里的 `co_filename` 是 `download_data\download_info_data.py`
  （**反斜杠**）—— 这批字节码在 Windows 上编译的，macOS 从来不在测试矩阵里。

**出路**（按可靠性排）：

1. Linux x64 或 Windows x64 机器/云主机跑采集，**只把 parquet 同步回来**
   —— 与 `raw/amazing/<表名>/` 那个分层落点天然吻合（原样冻结、可追溯）
2. Apple Silicon 上跑 `--platform linux/amd64` 容器：要 QEMU 模拟，
   而 tgw 是长连接 SWIG C++ 网关，模拟层下的稳定性未验证
3. 向银河确认有没有 macOS 版（手册推荐环境只列 REDHAT 7.x / Windows 10）

## 🔴 装也要装进独立 venv，不许进主环境

`AmazingData` 要 `numba>=0.65.0`，主环境是 numba 0.61.2；解出来会把
**numpy 2.2.6 顶到 2.5.2**（llvmlite 0.44→0.49）。回测链与 datalake
构建链全都吃 numpy —— 为一个只在 Linux 上跑得起来的采集包去动它不值得。
所以 `--install` 默认装进 `_ingest/venv/`，要装主环境得显式 `--system`。

## AmazingData 按 Python 版本分包，但那不是平台问题

它里面是 59 个 `.pyc` + 3 个 `.py`（numba 算子），**零个二进制** ——
按 cp38~cp314 分包是因为 `.pyc` 的 magic number 绑 Python 版本
（发布字节码而不公开源码），不是因为有 .so。所以

    py3-none-any      本机 ✓（tgw 是这个）
    cp313-none-any    本机 ✓（本机 Python 3.13）
    cp312-none-any    本机 ✗ —— Python 3.13 只认 cp313

★ `cpXX-none-any` 这个组合本身可疑：`cpXX` 声明 CPython ABI，而 `none-any`
  声明"无 ABI 要求、平台无关"。AmazingData 兑现了（真没有二进制），
  **tgw 没兑现**（67 MB 全是 .so/.dll，也标 `py3-none-any`）。
  所以体检永远分两步：① pip 装得上 ② `import` 真的成功。
"""
import argparse
import glob
import io
import json
import os
import platform
import re
import subprocess
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))            # datalake/
REPO = os.path.dirname(ROOT)
HOME = os.path.join(ROOT, 'raw', 'amazing')                  # 数据落这儿
ING = os.path.join(HOME, '_ingest')
STAMP = os.path.join(ING, 'installed.json')
VENV = os.path.join(ING, 'venv')
# 本地缓存目录：SDK 自己的 local_path 参数要绝对路径（手册 §4.4）
CACHE = os.path.join(ING, 'sdk_cache')
PKGS = ('tgw', 'AmazingData')
# 手册里 wheel 的命名，用来认文件（别把别的包当成它）
PAT = {'tgw': re.compile(r'^tgw-[\d.]+-py3-none-any\.whl$'),
       'AmazingData': re.compile(
           r'^AmazingData-[\d.]+-(cp3\d+|py3)-none-any\.whl$')}
# tgw 里原生库的目录命名 —— 平台裁决的唯一判据
TGW_PKG = re.compile(r'^tgw/(win|linux)_py(\d+)_(x64|x86|arm64|aarch64)'
                     r'_package/')
NETDISK = 'https://cloud.chinastock.com.cn/p/DSG36jYQx2IY_Y8CIAA'

TARGET = None       # --system 时是 sys.executable，否则 venv 里那个


def _say(*a):
    print(*a, flush=True)


def _resolve(src):
    """相对路径按 cwd / 仓库根 / datalake 依次试 —— 三个都是人自然会站的地方。

    ★ 找不到时把**试过的每一条**都列出来。只说"目录不存在"的话，
      人看不出是自己路径写错了还是脚本解错了基准。
    """
    src = os.path.expanduser(src)
    if os.path.isabs(src):
        if os.path.isdir(src):
            return src
        raise SystemExit('🔴 目录不存在：%s' % src)
    tried = [os.path.abspath(src), os.path.join(REPO, src),
             os.path.join(ROOT, src)]
    for c in tried:
        if os.path.isdir(c):
            return c
    raise SystemExit('🔴 找不到目录 %r，试过：\n   %s'
                     % (src, '\n   '.join(tried)))


def _venv_py(create=False):
    """独立 venv 的解释器。见模块 docstring「不许进主环境」那条。"""
    sub, exe = ('Scripts', 'python.exe') if os.name == 'nt' else \
        ('bin', 'python')
    p = os.path.join(VENV, sub, exe)
    if os.path.isfile(p):
        return p
    if not create:
        return None
    import venv as _v
    os.makedirs(ING, exist_ok=True)
    _v.EnvBuilder(with_pip=True).create(VENV)
    if not os.path.isfile(p):
        raise SystemExit('🔴 venv 建出来了但没有 %s' % p)
    return p


def _py():
    """装到哪、import 用哪个，都以它为准。

    默认优先用 `_ingest/venv` —— 体检也该看那个环境，
    不然"主环境没装"会被报成"没装"，而 venv 里其实是好的。
    """
    if TARGET:
        return TARGET
    return _venv_py() or sys.executable


def _tags():
    try:
        from packaging import tags
        return {str(t) for t in tags.sys_tags()}
    except ImportError:
        return None


def _wheel_tag(fn):
    m = re.match(r'^[^-]+-[^-]+-(.+)\.whl$', os.path.basename(fn))
    return m.group(1) if m else None


# ------------------------------------------------ tgw 的平台裁决（装之前）
def _tgw_matrix(whl):
    """扫 tgw wheel 里带了哪几套原生库 → {(os, pyver, arch)}。

    🔴 **不装就能判断**。让判据落在"有没有本机这一款 .so/.pyd"上 ——
    落在 import 报错上的话，macOS 会拿到一句
    `No module named '_tgw'`（因为 `'win' in 'darwin'` 把它误判成
    Windows），而那句话指不到真正的原因。
    """
    try:
        with zipfile.ZipFile(whl) as z:
            ns = z.namelist()
    except Exception as e:                                  # noqa: BLE001
        return None, str(e)
    out = set()
    for n in ns:
        m = TGW_PKG.match(n)
        if m:
            out.add((m.group(1), m.group(2), m.group(3)))
    return out, ''


def _this_platform():
    """本机对应 tgw 的哪一款 → (os, pyver, arch)；os 为 None 表示它不支持。"""
    p = sys.platform
    if p.startswith('linux'):
        osname = 'linux'
    elif p in ('win32', 'cygwin', 'msys'):
        osname = 'win'
    else:
        osname = None                       # darwin / freebsd / …
    mach = platform.machine().lower()
    arch = {'x86_64': 'x64', 'amd64': 'x64', 'arm64': 'arm64',
            'aarch64': 'arm64', 'i386': 'x86', 'x86': 'x86'}.get(mach, mach)
    return osname, '%d%d' % sys.version_info[:2], arch


def _verdict(matrix):
    """本机能不能跑 tgw → (ok, 一句话原因, 出路列表)。"""
    osname, pyver, arch = _this_platform()
    if osname is None:
        return False, (
            '🔴 tgw 不支持 %s（sys.platform=%r）。它只编了 linux 与 win 两套。'
            % (platform.system(), sys.platform)), [
            '而且它 **不会**告诉你这件事：`tgw/__init__.py` 判平台用的是'
            " `'win' in sys.platform`，而 `'darwin'` 里含 `'win'`（dar[win]）"
            '——',
            'macOS 被误判成 Windows，去加载 `_tgw.pyd`，报出来的是'
            " `ModuleNotFoundError: No module named '_tgw'`。",
            '',
            '出路（按可靠性排）：',
            '  1) Linux x64 / Windows x64 机器或云主机跑采集，'
            '只把 parquet 同步回 raw/amazing/<表名>/',
            '  2) --platform linux/amd64 容器（Apple Silicon 上走 QEMU 模拟，'
            'tgw 是长连接 SWIG C++ 网关，模拟层下稳定性未验证）',
            '  3) 问银河有没有 macOS 版（手册只列 REDHAT 7.x / Windows 10）']
    if not matrix:
        return True, '（拿不到 wheel，跳过平台裁决）', []
    if (osname, pyver, arch) in matrix:
        return True, '✓ 本机这一款在包里：%s_py%s_%s_package' % (
            osname, pyver, arch), []
    same_os = sorted({(a, v) for o, v, a in matrix if o == osname})
    return False, (
        '🔴 包里没有 %s_py%s_%s_package。' % (osname, pyver, arch)), [
        '   同平台它带了这些：%s' % ', '.join(
            '%s/py%s' % (a, v) for a, v in same_os) or '（一个都没有）',
        '   —— 换 Python 版本或架构，或者向银河要对应的包。']


def _installed(name):
    """装了没 + 版本。用 importlib.metadata，不猜路径。"""
    r = subprocess.run(
        [_py(), '-c', 'import importlib.metadata as m;'
         'print(m.version(%r))' % name], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _importable(name):
    """🔴 **装上了 ≠ import 得了**。tgw 标着 `py3-none-any` 却全是
    .so/.dll，pip 会照装，而 import 时才崩在动态库上（那时你已经以为
    装好了）。"""
    r = subprocess.run([_py(), '-c', 'import %s' % name],
                       capture_output=True, text=True, timeout=180)
    if r.returncode == 0:
        return True, ''
    err = (r.stderr or '').strip().split('\n')
    return False, (err[-1] if err else '?')[:160]


def check():
    osname, mach = platform.system(), platform.machine()
    _say('本机          %s / %s / Python %s  (sys.platform=%s)'
         % (osname, mach, platform.python_version(), sys.platform))
    _say('解释器        %s%s'
         % (_py(), '' if TARGET or _venv_py() else '（还没建 venv）'))
    t = _tags()
    if t:
        for probe in ('py3-none-any', 'cp%d%d-none-any'
                      % sys.version_info[:2]):
            _say('  wheel tag %-16s %s'
                 % (probe, '✓ 本机可装' if probe in t else '✗ 本机不接受'))

    # ---- 平台裁决：装之前就该知道行不行 ----
    _say('')
    whl = sorted(glob.glob(os.path.join(HOME, 'tgw-*.whl')))
    mat, err = _tgw_matrix(whl[-1]) if whl else (None, '没下载')
    if mat:
        _say('tgw 原生库    %d 套：%s' % (len(mat), ', '.join(sorted(
            {'%s/%s' % (o, a) for o, _, a in mat}))))
    ok_plat, why, more = _verdict(mat)
    _say('平台裁决      %s' % why)
    for l in more:
        _say('  %s' % l)

    _say('')
    ok = ok_plat
    for p in PKGS:
        v = _installed(p)
        if not v:
            _say('%-12s 未安装' % p)
            ok = False
            continue
        imp, e = _importable(p)
        _say('%-12s %-10s import %s' % (p, v, '✓' if imp else '✗ ' + e))
        ok = ok and imp
    if os.path.isfile(STAMP):
        try:
            d = json.load(io.open(STAMP, encoding='utf-8'))
            _say('\n装自          %s' % ', '.join(
                '%s(%s)' % (k, v) for k, v in (d.get('wheels') or {}).items()))
            _say('装的时候      %s  在 %s'
                 % (d.get('at'), d.get('into') or '?'))
        except Exception:                                   # noqa: BLE001
            pass
    _say('\n数据目录      %s%s' % (os.path.relpath(HOME, REPO),
                                   '' if os.path.isdir(HOME) else '（还没建）'))
    if not ok and ok_plat:
        _say('\n下一步：')
        _say('  1) 从银河网盘下载两个 wheel（手册 §3.1.2）：%s' % NETDISK)
        _say('     或公众号「中国银河证券星耀数智」→ 业务介绍 → 安装包下载')
        _say('  2) 🔴 AmazingData 要挑与本机 Python 匹配的那个 ——'
             ' 本机 %s，需要 cp%d%d（或 py3）那版'
             % (platform.python_version(), *sys.version_info[:2]))
        _say('  3) python3 %s --install %s'
             % (os.path.relpath(__file__, REPO),
                os.path.relpath(HOME, REPO)))
        _say('  ⚠️ 不要 pip install AmazingData —— PyPI 上那个是同名的'
             '另一个项目（0.0.3，13 KB，与银河无关）')
    return 0 if ok else 1


def install(src, system=False, anyway=False):
    global TARGET
    src = _resolve(src)
    found = {}
    for p in PKGS:
        hit = [f for f in sorted(glob.glob(os.path.join(src, '*.whl')))
               if PAT[p].match(os.path.basename(f))]
        if not hit:
            raise SystemExit(
                '🔴 %s 下找不到 %s 的 wheel。\n'
                '   期望的命名（手册 §3.1.1）：%s\n'
                '   实际有：%s' % (src, p, PAT[p].pattern,
                                   [os.path.basename(x) for x in
                                    glob.glob(os.path.join(src, '*.whl'))]
                                   or '（一个 .whl 都没有）'))
        # AmazingData 有 cp38~cp314 一整排，要挑本机那个（不是最后一个）
        if p == 'AmazingData' and len(hit) > 1:
            want = ('cp%d%d' % sys.version_info[:2], 'py3')
            pick = [f for f in hit
                    if (_wheel_tag(f) or '').split('-')[0] in want]
            if not pick:
                raise SystemExit(
                    '🔴 %s 下 %d 个 AmazingData wheel 里没有本机这版'
                    '（要 cp%d%d 或 py3）：\n   %s'
                    % (src, len(hit), *sys.version_info[:2],
                       '\n   '.join(os.path.basename(x) for x in hit)))
            hit = pick
        found[p] = hit[-1]

    # ---- 🔴 平台裁决放在最前面：装之前就该拒绝 ----
    mat, _ = _tgw_matrix(found['tgw'])
    ok_plat, why, more = _verdict(mat)
    _say('平台裁决      %s' % why)
    for l in more:
        _say('  %s' % l)
    if not ok_plat and not anyway:
        _say('\n所以这里**不装** —— 装上去只会在第一次 import 时'
             '崩在动态库上，而那句报错指不到真正的原因。')
        _say('要硬装看一眼，加 --anyway。')
        return 2
    _say('')

    t = _tags()
    for p, f in found.items():
        tag = _wheel_tag(f)
        bad = t is not None and tag not in t
        _say('  %-12s %s   tag %s %s'
             % (p, os.path.basename(f), tag,
                '🔴 本机不接受' if bad else '✓'))
        if bad:
            raise SystemExit(
                '🔴 这个 wheel 的 tag（%s）本机装不上。\n'
                '   本机是 Python %s，需要 py3 或 cp%d%d 那版 ——'
                '回网盘挑对应版本。' % (tag, platform.python_version(),
                                        *sys.version_info[:2]))
        # 看一眼里面有没有二进制 —— 有的话 `any` 就是谎的
        try:
            with zipfile.ZipFile(f) as z:
                so = [n for n in z.namelist()
                      if n.endswith(('.so', '.pyd', '.dylib', '.dll'))]
            if so:
                _say('     ⚠️ 包里有 %d 个二进制而 tag 声明 `any` ——'
                     ' 装完必须验 import' % len(so))
        except Exception:                                   # noqa: BLE001
            pass

    os.makedirs(ING, exist_ok=True)
    os.makedirs(CACHE, exist_ok=True)
    # 🔴 默认装进独立 venv：AmazingData 要 numba>=0.65，会把主环境的
    #    numpy 顶上去，而回测链与 datalake 构建链全都吃 numpy
    if system:
        TARGET = sys.executable
        _say('\n装进【主环境】%s' % TARGET)
        _say('  ⚠️ AmazingData 要 numba>=0.65，会顶掉主环境的 numpy —— '
             '回测链吃它。除非你确定，否则别加 --system。')
    else:
        TARGET = _venv_py(create=True)
        _say('\n装进独立 venv  %s' % os.path.relpath(VENV, REPO))
    for p in PKGS:                      # tgw 先装：AmazingData 依赖它
        r = subprocess.run([_py(), '-m', 'pip', 'install', found[p]],
                           capture_output=True, text=True)
        if r.returncode != 0:
            _say((r.stdout or '')[-800:])
            _say((r.stderr or '')[-800:])
            raise SystemExit('🔴 %s 装失败' % p)
        _say('  %-12s ✓ %s' % (p, _installed(p) or '?'))

    # 🔴 装完必须 import 一次 —— 见模块 docstring 那条
    _say('\n验 import：')
    bad = []
    for p in PKGS:
        imp, err = _importable(p)
        _say('  %-12s %s' % (p, '✓' if imp else '✗ ' + err))
        if not imp:
            bad.append((p, err))
    if bad:
        _say('\n🔴 装上了但 import 不了 —— tgw 那个 `py3-none-any` 声明的'
             '"平台无关"没有兑现（里面全是为 Linux/Windows 编的动态库）。')
        for l in _verdict(mat)[2] or ['   见模块 docstring 的「出路」。']:
            _say('  %s' % l)
        return 2
    io.open(STAMP, 'w', encoding='utf-8').write(json.dumps(
        {'wheels': {p: os.path.basename(found[p]) for p in PKGS},
         'versions': {p: _installed(p) for p in PKGS},
         'python': platform.python_version(),
         'platform': '%s/%s' % (platform.system(), platform.machine()),
         'into': 'system' if system else os.path.relpath(VENV, REPO),
         'at': time.strftime('%Y-%m-%dT%H:%M:%S')},
        ensure_ascii=False, indent=2))
    _say('\n✅ 装好了。下一步：python3 %s --probe'
         % os.path.relpath(__file__, REPO))
    _say('   （要账号：手册 §3.5.1.1「需联系您的开户营业部申请开通权限」）')
    return 0


def probe():
    """拿【已知真值】当场对数 —— 照本项目探针的规矩来。

    🔴 "有值但对不上"比"没有值"更危险（f133 那次：招商银行的股息率
      差一倍，而那个数看着完全正常）。所以这里不是"跑通就算"，
      每一项都要和本地已有的数据逐条比。
    """
    imp, err = _importable('AmazingData')
    if not imp:
        ok_plat, why, more = _verdict(
            (_tgw_matrix(sorted(glob.glob(
                os.path.join(HOME, 'tgw-*.whl')))[-1])[0]
             if glob.glob(os.path.join(HOME, 'tgw-*.whl')) else None))
        _say('🔴 AmazingData import 不了：%s' % err)
        _say('   %s' % why)
        for l in more:
            _say('   %s' % l)
        return 2
    cfg = os.path.join(ING, 'account.json')
    if not os.path.isfile(cfg):
        io.open(cfg + '.example', 'w', encoding='utf-8').write(json.dumps(
            {'username': '', 'password': '', 'host': '', 'port': 0},
            ensure_ascii=False, indent=2))
        raise SystemExit(
            '🔴 缺 %s。\n'
            '   照 %s.example 填（账号/密码/ip/端口找开户营业部要）。\n'
            '   ⚠️ 这个文件**不要入 git** —— datalake 的 .gitignore 是白名单式'
            '（只放行 *.py/*.md/*.sh/*.sql），所以 .json 天然不会被提交。'
            % (os.path.relpath(cfg, REPO), os.path.relpath(cfg, REPO)))
    _say('探针还没写实测项 —— 拿到账号能登录之后再补，'
         '并且每一项都要与本地对数（见本函数 docstring）。')
    _say('准备对的三样（按"最容易错在哪"排的）：')
    _say('  ① HIGH_LIMITED / LOW_LIMITED  vs  面板自算的涨跌停价')
    _say('     —— 本项目现在按规则算 + 自校验，limit_rule_ok 99.97%，'
         '漏的全是新股首日/退市整理期')
    _say('  ② ACTUAL_ANN_DATE  vs  聚宽 pub_date')
    _say('     —— PIT 的命门，差一天就是未来函数')
    _say('  ③ 行业分类（LEVEL1_NAME）  vs  申万一级')
    _say('     —— 🔴 手册里**没写是哪一套**，如果不是申万，'
         '红利/板块页的口径就变了')
    return 0


def main():
    global TARGET
    ap = argparse.ArgumentParser(description='银河 AmazingData 装配与体检')
    ap.add_argument('--install', metavar='DIR',
                    help='从这个目录里的 wheel 装（银河网盘下载的）')
    ap.add_argument('--system', action='store_true',
                    help='装进主环境（默认装进 _ingest/venv —— '
                         'AmazingData 要 numba>=0.65，会顶掉主环境 numpy）')
    ap.add_argument('--anyway', action='store_true',
                    help='平台裁决说不行也硬装（只为看一眼报错长什么样）')
    ap.add_argument('--probe', action='store_true', help='拿已知真值对数')
    a = ap.parse_args()
    if a.system:
        TARGET = sys.executable
    if a.install:
        return install(a.install, system=a.system, anyway=a.anyway)
    if a.probe:
        return probe()
    return check()


if __name__ == '__main__':
    sys.exit(main() or 0)
