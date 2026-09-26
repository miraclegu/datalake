# -*- coding: utf-8 -*-
"""进程启动时把输出编码钉成 UTF-8 —— Windows 上不做这件事整条链会崩。

## 🔴🔴 为什么非做不可（实测，不是推的）

本项目的脚本满屏 `✓ ✗ ⚠ ★ 🔴 ·`，而**中文 Windows 的默认编码是 cp936
（GBK），编不出这些字符**：

    PYTHONIOENCODING=gbk python3 -c 'print("✓")'
      -> UnicodeEncodeError，**退出码 1**

Windows 上**交互控制台**没事（PEP 528 起用 UTF-8），坑在**输出被重定向**
的时候 —— 那时 Python 退回 locale 编码：

    schtasks 把 stdout 写进日志文件      -> 重定向
    serve.py / sync_daily 起子进程抓输出  -> 管道
    setup_stages 跑七个阶段              -> 管道

也就是说**每日同步链在 Windows 上一跑就崩在第一个 `✓` 上**，
而报出来是一句 `UnicodeEncodeError`，指不到"是编码不是逻辑"。

## 做两件事，缺一件都不够

1. **自己的 stdout/stderr 重配成 UTF-8**（`errors='replace'` 兜底：
   宁可显示成 `?` 也不许整个进程崩掉）。
2. **给子进程设 `PYTHONUTF8=1`**（PEP 540）—— 自己重配管不到子进程，
   而这条链上几乎每一步都是子进程。

★ 顺带把控制台代码页也切到 65001，否则终端里看到的是乱码
  （字节是对的、渲染不对，比崩掉更难查）。切不动就算了，不影响正确性。

★ 同一份实现在 `assay/assay/hashseed.py` 也有一份（那边是 assay 各入口
  「进程启动前必须做的第一件事」的家）—— 两个仓库，跨仓共享要引依赖，
  这是明知的取舍。改一处要**两处一起改**。
"""
import os
import sys


def setup():
    """幂等。返回一句话说明做了什么（给调用方打日志用，也可以不管）。"""
    done = []
    # ① 子进程：PEP 540 的 UTF-8 模式。**必须在起子进程之前设**
    if os.environ.get('PYTHONUTF8') != '1':
        os.environ['PYTHONUTF8'] = '1'
        done.append('PYTHONUTF8=1')
    # ② 自己：stdout/stderr 已经建好了，env 对它无效 —— 只能重配
    for name in ('stdout', 'stderr'):
        f = getattr(sys, name, None)
        enc = (getattr(f, 'encoding', '') or '').lower().replace('-', '')
        if f is None or enc in ('utf8', 'utf8mb4'):
            continue
        try:
            f.reconfigure(encoding='utf-8', errors='replace')
            done.append(name)
        except (AttributeError, OSError, ValueError):
            pass            # 装不上就算了，不许因为"日志好看"把主链搞挂
    # ③ 控制台代码页（只影响显示，切不动无所谓）
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
            done.append('chcp 65001')
        except Exception:                                   # noqa: BLE001
            pass
    return ' / '.join(done) or '本来就是 UTF-8'
