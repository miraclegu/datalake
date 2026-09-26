#!/bin/bash
# ============================================================================
# 每日数据同步 —— **这个文件只是转发**，正本在 `sync_daily.py`。
#
#   bash datalake/sync_daily.sh            # 正常跑
#   bash datalake/sync_daily.sh --no-live  # 只同步数据，不触发信号
#   bash datalake/sync_daily.sh --dry      # 只打印会做什么
#   bash datalake/sync_daily.sh --if-stale # 数据已齐就直接退出（给轮询用）
#
# 🔴 **为什么正本搬去 Python**：这份原来是 270 行 bash，而 Windows 上没有
#   bash，也没有 `date -v-10d` / `tail` / `sed` / `ls -1t | xargs` 这条
#   POSIX 工具链。而「两份实现」是本项目最硬的那条禁令 —— 两台机器跑出
#   不同的面板**且不报错**，比多写一份麻烦得多。
#
# ★ **这个文件保留，因为命令行是产品契约**：CLAUDE.md、launchd 的 plist、
#   以及肌肉记忆里全是 `sync_daily.sh`。同「selftest.py 拆成 tests/ 而入口
#   一个字没改」「live.py 变 66 行门面」那两次。
#
# ★ 13 步的语义、为什么是这个顺序、run 与 run_soft 的分工，全在
#   `sync_daily.py` 的 docstring 里 —— **别在这里再写一份**（分叉的文档
#   比没有文档更危险）。
# ============================================================================
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/sync_daily.py" "$@"
