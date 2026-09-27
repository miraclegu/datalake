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


# ------------------------------------------------------------------ 日志
#
# 🔴🔴 **所有日志都在 `finacial/logs/`（两个仓库的同级）。**
#   用户 2026-09-27：「日志文件现在在什么地方，能不能全部放到 assay、
#   datalake 同级的目录下，不然每次找日志都很难找。」
#   改之前是 **7 种散在 4 个目录**：`datalake/_manifest/logs/`、
#   `datalake/_manifest/sync_logs/`、`datalake/_manifest/`（launchd 那四个）、
#   `assay/install.log` —— 要查一件事得先猜它属于哪一类。
#
#   **每一类各占一个目录**（用户同一轮追加：「每种类型的日志要一个单独的
#   目录，以便区分」），分法是【这是哪条链】—— 正好对上人问问题的方式：
#
#       logs/setup/    setup-<天>.log   全量同步：建本地数据那 7 个阶段
#       logs/daily/    daily-<天>.log   每日同步：13 步链 + 信号重算（tick）
#       logs/web/      web-<天>.log     看板 serve.py 日常运行
#       logs/runs/     <时间戳>.log     某一次跑的**完整**子进程输出
#       logs/launchd/  sync.out/.err …  定时器自己的 stdout（只能原地截断）
#       logs/install/  install.log      装机
#
#   ★ `web` 是从 `daily` 里**拆出来**的：原来三个来源混在一个文件里，
#     而「看板报了个错」与「昨晚同步失败了」是两件不同的事。
#   ★ 文件名仍带类型前缀 —— 拷一份出来单看时还认得出它是谁。
LOGS = os.path.join(REPO, 'logs')

# 🔴 **每一类日志各占一个目录**（用户 2026-09-27 定）：平铺在一个目录里时
#   `setup-*.log` / `daily-*.log` / `web-*.log` / 那一次跑的明细混在一起，
#   要找"今天同步说了什么"得先在一堆文件名里挑。
#   文件名仍带类型前缀（`daily-2026-09-27.log`）—— 拷出来单看时还认得出是谁。
LOG_KINDS = ('setup', 'daily', 'web')


def log_dir(kind):
    """<repo>/logs/<kind>/ —— 按天切分的那三类。"""
    return os.path.join(LOGS, kind)


# 某一次跑的**完整**子进程输出（数据页「看完整日志」点的就是它）。
# 主日志只进骨架（每步 / ✅❌ / 失败尾部），合在一起的话 tdx2db init
# 那几万行进度会把"哪一步失败了"整个淹掉。
RUNS_LOGS = os.path.join(LOGS, 'runs')
LAUNCHD_LOGS = os.path.join(LOGS, 'launchd')
INSTALL_LOG = os.path.join(LOGS, 'install', 'install.log')


def launchd_logs(tag):
    """定时器自己的 stdout/stderr（launchd 的 StandardOutPath / schtasks）。

    🔴 它们由 **launchd 持有 fd**，所以只能**原地截断**、不能改名或删除
      （见 `logs.trim_by_days`）。
    """
    return (os.path.join(LAUNCHD_LOGS, '%s.out' % tag),
            os.path.join(LAUNCHD_LOGS, '%s.err' % tag))


def launchd_logs_legacy(tag):
    """搬家之前那一对（`datalake/_manifest/launchd-*.{out,err}`）。

    🔴🔴 **不能只改新路径就完事。** plist 里写着**旧路径**，而它要等人
      重装定时任务才会变 —— 在那之前 launchd 仍然往旧文件写，而按天裁的
      是新文件：**旧的又开始无限涨，且不报错**（`launchd-sync.out` 当初
      就是这么长到 1.6 MB 的）。所以裁的时候两边都裁，直到旧的不再变。
    """
    return (os.path.join(MANIFEST, 'launchd-%s.out' % tag),
            os.path.join(MANIFEST, 'launchd-%s.err' % tag))
