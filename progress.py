# -*- coding: utf-8 -*-
"""数据加载的【进度正本】—— 跑的那个进程自己写，谁都读得到。

用户："这些数据加载都应该在 web 端最上侧显示，如果是分步加载的需要显示
一共多少步，当前多少步，每一步的进度，已用多少时间，预计还要多少时间。"

🔴🔴 **为什么是文件，不是让服务端解析子进程的 stdout：**
  每天真正跑同步的是 **launchd**（每 10 分钟一个点位），它**根本不经过
  serve.py** —— `_JOBS` 里没有它。解析 stdout 那条路只覆盖得到"人在页面上
  点了立即同步"这一种，而那是少数情况。**页面上看不到定时任务在跑**，
  正是现在这个功能缺失的核心（同「判据永远是现在的状态，不是记录」）。
★ 顺带还解决一个：子进程写 pipe 是**块缓冲**，进度会滞后几十秒
  （同「`--all` 重定向到文件时 tail -f 看到的永远是几十条之前的」那条）。

🔴 **总步数由 `Runner` 自己数，不写字面量。** 搬运前 `.sh` 里前九步写
  `x/12`、后四步写 `x/13` —— 链从 12 长到 13 时只改了新加的那几行，
  **而它不报错**，只是屏幕上的分母自相矛盾。所以步号一律由这里生成。

⚠ **没有锁**：写进度的可能有两个进程（launchd 那条 + 人在页面上点的）。
  所以落盘走「唯一 tmp 名 + os.replace」—— 与 `factor_eval._write` 同一条
  （那边也没有锁，唯一 tmp 名就是唯一的保护）。谁最后 rename 谁赢，
  而各自那份都是完整的。
"""

import io
import json
import os
import time
import uuid

DL = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(DL, '_manifest', 'progress')
TIMES = os.path.join(DL, '_manifest', 'step_times.json')


def _atomic(path, obj):
    d = os.path.dirname(path)
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = '%s.%d.%s.tmp' % (path, os.getpid(), uuid.uuid4().hex[:8])
    try:
        with io.open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _load(path, dft):
    try:
        with io.open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:                                       # noqa: BLE001
        return dft


def _alive(pid):
    """那个进程还在吗 —— 判据是**现在**，不是文件里写的 state。

    🔴 进程被 kill / 机器重启 / 崩掉时，文件里还留着 `running`。
      只看 state 的话页面会永远显示"正在同步"（同 serve.py 那条
      「判据是谁占着端口，不是 PID 文件」）。

    🔴🔴 **不能直接用 `os.kill(pid, 0)`。** POSIX 上那是"只探测不发信号"的
      惯用法；而 **Windows 上 `os.kill` 对任何非 `CTRL_*` 的 sig 都走
      `TerminateProcess`** —— 于是**每打开一次页面、横条读一次进度，
      就把正在跑的同步进程杀掉**，而它不报错。
    ★ 同一份实现在 `assay/serve.py` 也有一份（`--stop` 要确认真的停了）——
      两个仓库，跨仓共享要引依赖，这是明知的取舍。改一处要**两处一起改**。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if not pid:
        return False
    if os.name == 'nt':
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259   # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


class Progress(object):
    """一次数据加载的进度。`step()` 开一步，`finish_step()` 收一步。

    ★ 写坏了、写不动都**不许影响主链**（进度是给人看的，不是数据）——
      所有写操作包在 try 里。同「通知失败也只是不提醒，不该把轮询搞挂」。
    """

    def __init__(self, job, title, total, log=None):
        self.job, self.path = job, os.path.join(DIR, '%s.json' % job)
        self.d = {'job': job, 'title': title, 'total': int(total), 'i': 0,
                  'step': None, 'step_started': None, 'done': [],
                  'pid': os.getpid(), 'started': time.time(),
                  'state': 'running', 'rc': None, 'log': log}
        self._flush()

    def _flush(self):
        try:
            _atomic(self.path, self.d)
        except Exception:                                   # noqa: BLE001
            pass

    def step(self, name, at=None):
        # ★ `at` 给**整条链里的位置**：历史批量那条是"一次点一个阶段"，
        #   而人要看的是「第 4 步 / 共 7 步」，不是"这次点了 1 步"。
        self.d['i'] = int(at) if at else self.d['i'] + 1
        self.d['step'] = name
        self.d['step_started'] = time.time()
        self._flush()
        return self.d['i']

    def finish_step(self, state='ok'):
        t = self.d.get('step_started')
        sec = round(time.time() - t, 1) if t else None
        self.d['done'].append({'name': self.d['step'], 'sec': sec,
                               'state': state})
        self._flush()

    def finish(self, rc=0):
        self.d['state'] = 'done' if rc == 0 else 'failed'
        self.d['rc'] = rc
        self.d['step'] = None
        self.d['ended'] = time.time()
        self._flush()
        # 跑完才把这一次的**耗时剖面**存成下次的 ETA 基准。
        # 🔴 **按 job 存、按位置对齐**，不按步名：两条链（增量 13 步 /
        #   批量 7 阶段）的步名混在一张表里的话，"剩下几步要多久"会把
        #   另一条链的步算进来 —— **而它不报错，只是那个数没有意义**。
        # ★ 只存**跑完整了**的那次（`rc == 0` 且步数对得上）：半截的剖面
        #   会让 ETA 系统性偏小，而偏小的预计比没有预计更误导。
        if rc == 0 and len(self.d['done']) == self.d['total']:
            try:
                t_ = _load(TIMES, {})
                t_[self.job] = {'at': time.time(), 'total': self.d['total'],
                                'secs': [x.get('sec') or 0
                                         for x in self.d['done']],
                                # ★ 名字只给**展开面板**用（"接下来是哪几步"）。
                                #   ETA 仍然按**位置**对齐 —— 步名会改，
                                #   按名字对会静默对不上（见上面那条）。
                                #   所以页面上要标明这是"上次跑的时候"的名字。
                                'names': [x.get('name') or ''
                                          for x in self.d['done']]}
                _atomic(TIMES, t_)
            except Exception:                               # noqa: BLE001
                pass


def read(job=None):
    """读回进度并补上【已用 / 预计剩余】—— 派生量在这里算，不在页面算。

    ★ ETA 用**实测历史**（`step_times.json`）。没跑过的步**不猜**：
      `eta` 给 None，页面显示「未知」（同「拿不到分红那一格标查不到，
      不猜一个数填上去」）。
    """
    if not os.path.isdir(DIR):
        return []
    times, out, now = _load(TIMES, {}), [], time.time()
    names = ['%s.json' % job] if job else sorted(os.listdir(DIR))
    for fn in names:
        if not fn.endswith('.json'):
            continue
        d = _load(os.path.join(DIR, fn), None)
        if not d:
            continue
        # 🔴 进程没了而文件还写着 running -> 说"中断了"，别一直转圈
        if d.get('state') == 'running' and not _alive(d.get('pid')):
            d['state'] = 'stale'
            d['broke_at'] = d.get('step_started') or d.get('started')
        d['elapsed'] = round(now - d.get('started', now), 1)
        d['step_elapsed'] = (round(now - d['step_started'], 1)
                             if d.get('step_started') and d['state'] == 'running'
                             else None)
        # 预计剩余 = 上一次完整跑里【这一步及其之后】那几步的耗时之和
        #   − 当前这步已经用掉的。按**位置**对齐（步号由 Runner 生成，
        #   位置是稳定的）；步名会改，按名字对会静默对不上。
        prof = times.get(d['job']) or {}
        secs = prof.get('secs') or []
        d['eta'] = None
        if d['state'] == 'running' and len(secs) == d.get('total') and d['i']:
            left = sum(secs[d['i'] - 1:]) - (d.get('step_elapsed') or 0)
            d['eta'] = round(max(left, 0), 1)
        # 当前这一步**预计**多久（上次同位置那步的实测）——
        # 进度条要在一步内往前挪一点，靠的就是它；没有历史就不挪
        # （宁可条不动，也不要编一个看着在走的假进度）。
        d['step_eta'] = (secs[d['i'] - 1] if d['state'] == 'running'
                         and len(secs) == d.get('total') and d['i'] else None)
        d['eta_from'] = prof.get('at')      # 基准是哪次跑出来的 —— 说得出处
        # 🔴 **未跑那几步的名字与预计耗时**（展开面板要列出"接下来做什么"）。
        #   来自上一次完整跑的剖面，所以页面必须标「上次」——
        #   链加过步骤的话它就是过期的，而那不报错（同「不猜一个数」）。
        d['plan'] = None
        if len(secs) == d.get('total') and prof.get('names'):
            d['plan'] = [{'name': n, 'sec': c}
                         for n, c in zip(prof['names'], secs)]
        out.append(d)
    return out
