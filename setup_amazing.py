#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""银河 AmazingData 的装配与体检（跨 macOS / Linux / Windows）。

    python3 datalake/setup_amazing.py                    # 体检，什么都不改
    python3 datalake/setup_amazing.py --install ~/Downloads
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
`tgw` 在 PyPI 上根本不存在。
—— 与 `tdx2db` 那次**同一个坑**：装上去"看着成功"，然后 API 全对不上。
所以只能用银河网盘下载的 wheel 离线装，本脚本会核对文件名与来源。

## 🔴 macOS 能不能用，取决于 wheel 的 tag 与里面有没有 .so

手册 §3.1.1 的示例文件名是 `AmazingData-1.0.0-cp312-none-any.whl`：

    py3-none-any      本机 ✓（tgw 是这个）
    cp312-none-any    本机 ✗ —— Python 3.13 只认 cp313
    cp313-none-any    本机 ✓

★ `cpXX-none-any` 这个组合本身可疑：`cpXX` 声明了 CPython ABI，
  而 `none-any` 声明"无 ABI 要求、平台无关"。两者同时出现说明打包时手工
  指定了 tag —— **如果包里其实有 .so，`any` 就是谎的，装得上、import 才炸**。
  所以体检分两步：① pip 装得上 ② `import` 真的成功。
  只验第一步的话，表现是"装好了"然后第一次调用崩在动态库上。
★ 手册推荐环境是 REDHAT 7.x / Windows 10，**没列 macOS** —— 能不能用是
  开放问题，本脚本就是拿来回答它的。
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
# 本地缓存目录：SDK 自己的 local_path 参数要绝对路径（手册 §4.4）
CACHE = os.path.join(ING, 'sdk_cache')
PKGS = ('tgw', 'AmazingData')
# 手册里 wheel 的命名，用来认文件（别把别的包当成它）
PAT = {'tgw': re.compile(r'^tgw-[\d.]+-py3-none-any\.whl$'),
       'AmazingData': re.compile(
           r'^AmazingData-[\d.]+-(cp3\d+|py3)-none-any\.whl$')}
NETDISK = 'https://cloud.chinastock.com.cn/p/DSG36jYQx2IY_Y8CIAA'


def _say(*a):
    print(*a, flush=True)


def _py():
    """跑本脚本的解释器 —— 装到哪、import 用哪个，都以它为准。"""
    return sys.executable


def _tags():
    try:
        from packaging import tags
        return {str(t) for t in tags.sys_tags()}
    except ImportError:
        return None


def _wheel_tag(fn):
    m = re.match(r'^[^-]+-[^-]+-(.+)\.whl$', os.path.basename(fn))
    return m.group(1) if m else None


def _installed(name):
    """装了没 + 版本。用 importlib.metadata，不猜路径。"""
    r = subprocess.run(
        [_py(), '-c', 'import importlib.metadata as m;'
         'print(m.version(%r))' % name], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _importable(name):
    """🔴 **装上了 ≠ import 得了**。`cpXX-none-any` 里若含 .so，
    pip 会照装，而 import 时才崩在动态库上（那时你已经以为装好了）。"""
    r = subprocess.run([_py(), '-c', 'import %s' % name],
                       capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        return True, ''
    err = (r.stderr or '').strip().split('\n')
    return False, (err[-1] if err else '?')[:160]


def check():
    osname, mach = platform.system(), platform.machine()
    _say('本机          %s / %s / Python %s'
         % (osname, mach, platform.python_version()))
    _say('解释器        %s' % _py())
    t = _tags()
    if t:
        for probe in ('py3-none-any', 'cp%d%d-none-any'
                      % sys.version_info[:2], 'cp312-none-any'):
            _say('  wheel tag %-16s %s'
                 % (probe, '✓ 本机可装' if probe in t else '✗ 本机不接受'))
    _say('')
    ok = True
    for p in PKGS:
        v = _installed(p)
        if not v:
            _say('%-12s 未安装' % p)
            ok = False
            continue
        imp, err = _importable(p)
        _say('%-12s %-10s import %s' % (p, v, '✓' if imp else '✗ ' + err))
        ok = ok and imp
    if os.path.isfile(STAMP):
        try:
            d = json.load(io.open(STAMP, encoding='utf-8'))
            _say('\n装自          %s' % ', '.join(
                '%s(%s)' % (k, v) for k, v in (d.get('wheels') or {}).items()))
            _say('装的时候      %s' % d.get('at'))
        except Exception:                                   # noqa: BLE001
            pass
    _say('\n数据目录      %s%s' % (os.path.relpath(HOME, REPO),
                                   '' if os.path.isdir(HOME) else '（还没建）'))
    if not ok:
        _say('\n下一步：')
        _say('  1) 从银河网盘下载两个 wheel（手册 §3.1.2）：')
        _say('     %s' % NETDISK)
        _say('     或公众号「中国银河证券星耀数智」→ 业务介绍 → 安装包下载')
        _say('  2) 🔴 AmazingData 要挑与本机 Python 匹配的那个 ——'
             ' 本机是 %s，需要 cp%d%d（或 py3）那版'
             % (platform.python_version(), *sys.version_info[:2]))
        _say('  3) python3 %s --install <下载目录>'
             % os.path.relpath(__file__, REPO))
        _say('  ⚠️ 不要 pip install AmazingData —— PyPI 上那个是同名的'
             '另一个项目（0.0.3，13 KB，与银河无关）')
    return 0 if ok else 1


def install(src):
    src = os.path.expanduser(src)
    if not os.path.isdir(src):
        raise SystemExit('🔴 目录不存在：%s' % src)
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
        found[p] = hit[-1]
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
        # 顺手看一眼里面有没有 .so/.pyd —— 有的话 `any` 就是谎的
        try:
            with zipfile.ZipFile(f) as z:
                so = [n for n in z.namelist()
                      if n.endswith(('.so', '.pyd', '.dylib'))]
            if so:
                _say('     ⚠️ 包里有 %d 个二进制（%s…）而 tag 声明 `any` ——'
                     ' 装完必须验 import' % (len(so), so[0][:40]))
        except Exception:                                   # noqa: BLE001
            pass

    os.makedirs(ING, exist_ok=True)
    os.makedirs(CACHE, exist_ok=True)
    _say('\n安装（%s）…' % _py())
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
        _say('\n🔴 装上了但 import 不了 —— `cpXX-none-any` 那个 tag 声明的'
             '"平台无关"没有兑现（包里有为 Linux/Windows 编的动态库）。')
        _say('   macOS 上这条路就断在这里；可行的退路：')
        _say('   · 在 Linux 机器/容器里跑采集，只把 parquet 同步回来')
        _say('   · 或者向银河确认有没有 macOS 版')
        return 2
    io.open(STAMP, 'w', encoding='utf-8').write(json.dumps(
        {'wheels': {p: os.path.basename(found[p]) for p in PKGS},
         'versions': {p: _installed(p) for p in PKGS},
         'python': platform.python_version(),
         'platform': '%s/%s' % (platform.system(), platform.machine()),
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
        raise SystemExit('🔴 AmazingData import 不了（%s）——'
                         ' 先跑 --install' % err)
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
    ap = argparse.ArgumentParser(description='银河 AmazingData 装配与体检')
    ap.add_argument('--install', metavar='DIR',
                    help='从这个目录里的 wheel 装（银河网盘下载的）')
    ap.add_argument('--probe', action='store_true', help='拿已知真值对数')
    a = ap.parse_args()
    if a.install:
        return install(a.install)
    if a.probe:
        return probe()
    return check()


if __name__ == '__main__':
    sys.exit(main() or 0)
