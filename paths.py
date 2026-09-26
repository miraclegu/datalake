# -*- coding: utf-8 -*-
"""datalake 侧路径的【唯一】解析 —— 与 `assay/assay/paths.py` 同一条纪律。

🔴 **为什么值得单独一个模块**：`tdx.db` 在哪，此前在 **12 处**各解析了一遍，
  而其中 **6 处是错的**，两种错法都**不报错**（换台机器才崩）：

      ① 绝对路径      `/Users/guhao/finacial/tdx2db/tdx.db`
                      load_tdx_kline / load_tdx_gbbq / daily_snapshot
      ② 走符号链接    `os.path.join(dirname(ROOT), 'tdx2db', 'tdx.db')`
                      build_trade_calendar / sync_status / load_jq_round3

  🔴🔴 **`finacial/tdx2db` 是一个符号链接**（-> `datalake/raw/tdx/_ingest`，
  实测同一个 inode），而它**不在任何仓库里** —— 同 `finacial/CLAUDE.md`
  那条。新机器 clone 出来根本没有它（Windows 还要管理员/开发者模式才建得
  了），于是 ① ② 两类一律崩在 `IOException`，而它们是 13 步链的
  **2/13、5/13、6/13、10/13** 四步。

★ **不数 dirname 层数** —— 层数跟着"这个文件放在哪"变，搬一次就要改一次，
  而改漏了不报错，只是那个模块从此解到一个不存在的路径（拆 `srv/` 时实测
  踩过：`/api/marks` 返回 `{}`、7 个实盘接口 500）。这里只数一次，
  而 `paths.py` 自己的位置是固定的（datalake 根）。

★ **调用方【往上找 `paths.py`】，也不数层数**（见下面那段 bootstrap 注释）：

      _d = os.path.dirname(os.path.abspath(__file__))
      while _d != os.path.dirname(_d) and not os.path.isfile(
              os.path.join(_d, 'paths.py')):
          _d = os.path.dirname(_d)
      sys.path.insert(0, _d)
      from paths import TDX_DB                              # noqa: E402

  找不到就一路走到文件系统根，导入时抛 ImportError ——
  **响亮失败**，不会静默退回某个猜出来的路径。

★ **不做存在性检查、不抛异常**（同 `assay/paths.py`）：各调用方的异常类型
  与措辞是它们自己的 UX，混成一个反而让报错指不到地方。这里只回答
  "路径是哪个"。

★ 依赖为零（只 `import os` / `platform`）—— 它被 build/ 下十来个脚本、
  `sync_daily.py`、`setup_tdx.py` 都用到，引进任何东西都可能兜出循环。
"""
import os
import platform

# datalake/paths.py -> datalake/（本文件就在根上，所以一层都不用数）
DL = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(DL)                        # finacial/（两个仓库的爹）

# tdx2db 可执行文件与 tdx.db 的家。
# 🔴 **直接解到真实路径，不走 `REPO/tdx2db` 那个符号链接**（见文件头）。
def tdx_dir(dl=None):
    """tdx2db 与 tdx.db 的家。

    🔴 **带 `dl` 参数不是多余的**：`setup_stages.stages(DL=...)` 靠它
      把整条链指到别的根上（用例就是这么在**空目录**上验"什么都还没建"的）。
      我上一轮把那处"看着重复"的局部变量删掉 —— 它其实是**参数化**，
      于是空 lake 上两个阶段去查真实目录、报成 `ok`，**而它不报错**。
      判断该不该合要看**谁在用它、当什么用**（同 `_KIND_FILE` 那条）。
    """
    return os.path.join(dl or DL, 'raw', 'tdx', '_ingest')


def tdx_db(dl=None):
    return os.path.join(tdx_dir(dl), 'tdx.db')


def tdx2db_bin(dl=None):
    """tdx2db 可执行文件 —— Windows 上是 `.exe`。

    ★ 上游（github.com/jing2uo/tdx2db）**有 Windows_x86_64 预编译包**，
      `setup_tdx.py --install` 的 ASSETS 表里本来就映射着它。
    ★ 找不到时**不在这里报错**：照常返回路径，让调用那一步响亮失败 ——
      于是 sync 链 5~13 跳过、当天不出信号，那正是想要的行为。
    """
    return os.path.join(
        tdx_dir(dl),
        'tdx2db.exe' if platform.system() == 'Windows' else 'tdx2db')


MANIFEST = os.path.join(DL, '_manifest')
TDX_DIR = tdx_dir()
TDX_DB = tdx_db()


def launchd_logs(tag):
    """定时任务的 stdout/stderr 落点 -> `(out, err)`。

    🔴 名字的正本在这里，`setup_tdx.py` 生成 plist 时也取它 ——
      两处各拼一遍的话，改了名字之后**清理的与写入的就不是同一个文件**，
      于是日志照旧无限涨，**而它不报错**。
    """
    return (os.path.join(MANIFEST, 'launchd-%s.out' % tag),
            os.path.join(MANIFEST, 'launchd-%s.err' % tag))
