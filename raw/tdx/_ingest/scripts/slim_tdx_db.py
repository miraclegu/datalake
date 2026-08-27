#!/usr/bin/env python3
"""tdx.db 瘦身工具 —— 只保留 commands/daily/update_and_screener.sh 真正需要的表。

设计原则: **绝不修改原库**。
  DuckDB 的 DROP TABLE 不回收磁盘(实测 402MiB 库删空后仍是 402MiB), 所以"删除"在这里
  等价于"重建时不拷贝"。原 tdx.db 全程只读, 一直是你的备份, 直到你自己确认后手工改名。

保留集来自对日常脚本的静态闭包追溯:
  update_and_screener.sh
    ├── full_update_tdx.sh
    │     ├── ./tdx2db cron            (Go 二进制: 管理全部 raw_* + _meta)
    │     ├── fix_etf_price_scale.py   (raw_kline_daily/raw_basic_daily/raw_symbol_class/_etf_scale_fixed)
    │     ├── check_data_anomaly.py    (raw_kline_daily/raw_symbol_class)
    │     ├── fast_update_indicators.py→indicators.py (v_stock_hfq/v_stock_bfq → stock_indicators)
    │     └── precompute_{volume_ma,high_20d,trailing_stop}.py (stock_indicators)
    └── screener_etf_pit.py / screener_etf_rotation.py / screener.py
          (v_etf_qfq/v_etf_hfq/raw_symbol_name + trend_watcher local_data.py: v_stock_hfq)

用法:
    python3 slim_tdx_db.py                      # dry-run, 只打印计划和预计大小
    python3 slim_tdx_db.py --execute            # 重建到 tdx_new.db (默认只删第 1 批)
    python3 slim_tdx_db.py --execute --float    # 同上 + 派生表 DOUBLE→FLOAT (省 ~43%)
    python3 slim_tdx_db.py --execute --float --drop-factors --drop-collateral --add-index
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import duckdb

BLOCK = 262144

# ── 保留集: 日常脚本 + tdx2db cron 必需, 不可删 ────────────────────────────────
KEEP_CORE = [
    "_meta",                    # schema_version=5.0, 删了 cron 可能重新初始化
    "raw_kline_daily",
    "raw_kline_1min",           # 空表, 但属 cron 的 schema
    "raw_basic_daily",
    "raw_adjust_factor",        # v_*_hfq/qfq 的底层表
    "raw_gbbq",                 # 删了复权因子会算错
    "raw_symbol_name",
    "raw_symbol_class",
    "raw_holidays",
    "raw_tdx_blocks_info",
    "raw_tdx_blocks_member",
    "_etf_scale_fixed",         # ETF ×10 修正的幂等台账, 删了会重复放大
    "stock_indicators",         # 日常指标表, screener 直接读
]
KEEP_VIEWS = ["v_stock_bfq", "v_stock_qfq", "v_stock_hfq",
              "v_etf_bfq", "v_etf_qfq", "v_etf_hfq"]

# ── 分批: 默认只丢第 1 批, 其余靠开关 ──────────────────────────────────────────
TIER1_DROP = [  # L1 研究残留 + 临时备份, 结论已判废, 无争议
    "stock_indicators_backup_20260604_130517",
    "sell_signals", "trades", "daily_stop_loss", "buy_signals",
    "trade_records", "trade_records_t2", "stock_screen_results",
    "strategy_unsettled_holdings", "strategy_summary",
    "market_regime_hs300", "v_index_pick",
]
TIER2_FACTORS = ["factor_daily", "factor_daily_etf"]          # --drop-factors
TIER3_COLLATERAL = ["stock_hfq", "sector_daily",              # --drop-collateral
                    "sector_factor_daily", "etf_indicators"]
# 永久保留: 全库找不到重建脚本, 且没有别处可以推出来
#   index_daily / sector_stock_map —— 无生成脚本, 且 precompute_sector_index.py 反过来依赖它们
#   stock_merge_history            —— 北交所代码合并的历史台账, 不可再生
TIER4_ALWAYS_KEEP = ["index_daily", "sector_stock_map", "stock_merge_history"]

# DOUBLE→FLOAT 只作用于派生指标/因子表; raw_* 是源数据, stock_hfq 是价格, 都不动
FLOAT_TABLES = {"stock_indicators", "factor_daily", "factor_daily_etf",
                "sector_daily", "sector_factor_daily"}

FLOAT_SHRINK = 0.571  # 实测: factor_daily 2020+ 子集 5.233GiB → 2.986GiB


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def table_bytes(con, table: str) -> int:
    """按 distinct block_id 数量估算表的真实磁盘占用。"""
    n = con.execute(
        "select count(distinct block_id) from pragma_storage_info(?) where block_id is not null",
        [f"src.{table}"]).fetchone()[0]
    return (n or 0) * BLOCK


def inventory(con):
    tabs = [r[0] for r in con.execute(
        "select table_name from duckdb_tables() where database_name='src' order by 1").fetchall()]
    views = {r[0]: r[1] for r in con.execute(
        "select view_name, sql from duckdb_views() "
        "where not internal and database_name='src'").fetchall()}
    cols = {}
    for t in tabs:
        cols[t] = con.execute(
            "select column_name, data_type from duckdb_columns() "
            "where database_name='src' and table_name=? order by column_index",
            [t]).fetchall()
    # CREATE TABLE AS SELECT 不保留约束, 必须显式抄过去。
    # 只查 duckdb_indexes() 是不够的: PK/UNIQUE 约束不出现在那里, 而
    # INSERT OR REPLACE / ON CONFLICT 依赖真实约束 (fix_etf_price_scale.py 就靠这个)。
    cons = {}
    for t, ctype, ctext in con.execute(
            "select table_name, constraint_type, constraint_text from duckdb_constraints() "
            "where database_name='src' and constraint_type in ('PRIMARY KEY','UNIQUE') "
            "order by table_name").fetchall():
        cons.setdefault(t, []).append((ctype, ctext))
    return tabs, views, cols, cons


def resolve_sets(tabs, args):
    drop = set(TIER1_DROP)
    if args.drop_factors:
        drop |= set(TIER2_FACTORS)
    if args.drop_collateral:
        drop |= set(TIER3_COLLATERAL)
    keep = [t for t in tabs if t not in drop]
    # 保留集完整性校验
    missing = [t for t in KEEP_CORE if t not in tabs]
    if missing:
        sys.exit(f"❌ 原库缺少必需表: {missing} —— 先确认库是否完整, 不要继续。")
    bad = [t for t in KEEP_CORE + TIER4_ALWAYS_KEEP if t in drop]
    if bad:
        sys.exit(f"❌ 内部错误: 必需表出现在删除集里: {bad}")
    return keep, sorted(drop & set(tabs))


def select_expr(cols, table, use_float):
    """生成 SELECT 列表; 需要时把 DOUBLE 转 FLOAT。"""
    parts, n_cast = [], 0
    for name, dtype in cols[table]:
        if use_float and table in FLOAT_TABLES and dtype == "DOUBLE":
            parts.append(f'"{name}"::FLOAT AS "{name}"')
            n_cast += 1
        else:
            parts.append(f'"{name}"')
    return ", ".join(parts), n_cast


def verify_table(con, table, use_float, cols):
    """行数 / 每列 NULL 数 / min-max 对照。返回 (ok, 说明)。"""
    n_src = con.execute(f"select count(*) from src.{table}").fetchone()[0]
    n_dst = con.execute(f'select count(*) from dst."{table}"').fetchone()[0]
    if n_src != n_dst:
        return False, f"行数不符 {n_src:,} vs {n_dst:,}"
    numeric = [c for c, d in cols[table]
               if d in ("DOUBLE", "FLOAT", "BIGINT", "INTEGER", "DECIMAL")]
    checked = 0
    rtol = 1e-4 if (use_float and table in FLOAT_TABLES) else 1e-9
    for c in numeric:
        a = con.execute(
            f'select count("{c}"), min("{c}"), max("{c}") from src.{table}').fetchone()
        b = con.execute(
            f'select count("{c}"), min("{c}"), max("{c}") from dst."{table}"').fetchone()
        if a[0] != b[0]:
            return False, f'列 {c} 非空数不符 {a[0]:,} vs {b[0]:,}'
        for lbl, x, y in (("min", a[1], b[1]), ("max", a[2], b[2])):
            if x is None and y is None:
                continue
            if x is None or y is None:
                return False, f"列 {c} {lbl} 一侧为 NULL"
            d = abs(float(x) - float(y))
            if d > rtol * max(abs(float(x)), 1e-12):
                return False, f"列 {c} {lbl} 偏差 {d:.3e} 超过 rtol={rtol:.0e}"
        checked += 1
    return True, f"{n_dst:,} 行, {checked} 个数值列 min/max/非空数一致"


def main() -> int:
    ap = argparse.ArgumentParser(description="tdx.db 瘦身: 重建到新文件, 原库只读不动")
    ap.add_argument("--db", default="/Users/guhao/finacial/tdx2db/tdx.db")
    ap.add_argument("--out", default=None, help="默认 <db 同目录>/tdx_new.db")
    ap.add_argument("--execute", action="store_true", help="真正执行; 缺省只 dry-run")
    ap.add_argument("--float", dest="use_float", action="store_true",
                    help="派生指标/因子表 DOUBLE→FLOAT (实测省 43%%, 最大相对误差 6e-08)")
    ap.add_argument("--drop-factors", action="store_true",
                    help="额外丢弃 factor_daily / factor_daily_etf (最大单笔收益, 但那是重建排序规则的原料)")
    ap.add_argument("--drop-collateral", action="store_true",
                    help="额外丢弃 stock_hfq / sector_* / etf_indicators (会打断 tickflow 等旁支)")
    ap.add_argument("--add-index", action="store_true",
                    help="给 stock_indicators 建 (symbol,date) 索引 —— 现在它一个索引都没有")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的输出文件")
    ap.add_argument("--threads", type=int, default=0, help="0=DuckDB 默认")
    args = ap.parse_args()

    db = Path(args.db).resolve()
    if not db.exists():
        sys.exit(f"❌ 找不到 {db}")
    out = Path(args.out).resolve() if args.out else db.with_name("tdx_new.db")

    con = duckdb.connect()
    if args.threads:
        con.execute(f"set threads={args.threads}")
    con.execute(f"attach '{db}' as src (read_only)")
    tabs, views, cols, cons = inventory(con)
    keep, drop = resolve_sets(tabs, args)

    # ── 计划 ──────────────────────────────────────────────────────────────────
    log(f"原库 {db}  文件 {db.stat().st_size / 2**30:.2f} GiB")
    sizes = {t: table_bytes(con, t) for t in tabs}
    tot = sum(sizes.values())

    print(f"\n{'='*74}\n删除 (不拷贝到新库): {len(drop)} 张表\n{'='*74}")
    for t in sorted(drop, key=lambda x: -sizes[x]):
        tier = ("第1批" if t in TIER1_DROP else
                "第2批" if t in TIER2_FACTORS else "第3批")
        print(f"  [{tier}] {t:<44}{sizes[t]/2**30:>8.2f} GiB")
    drop_g = sum(sizes[t] for t in drop) / 2**30
    print(f"  {'小计':<51}{drop_g:>8.2f} GiB")

    dropped_views = [v for v in views if v not in KEEP_VIEWS]
    print(f"\n删除视图: {len(dropped_views)} 个 (trades_* 参数扫描残留等)")

    print(f"\n{'='*74}\n保留: {len(keep)} 张表 + {len(KEEP_VIEWS)} 个视图\n{'='*74}")
    est = 0.0
    for t in sorted(keep, key=lambda x: -sizes[x]):
        g = sizes[t] / 2**30
        will_float = args.use_float and t in FLOAT_TABLES and any(
            d == "DOUBLE" for _, d in cols[t])
        g2 = g * FLOAT_SHRINK if will_float else g
        est += g2
        note = "  ← FLOAT 化" if will_float else ""
        req = " *必需*" if t in KEEP_CORE else ""
        print(f"  {t:<44}{g:>8.2f} → {g2:>6.2f} GiB{note}{req}")
    print(f"  {'小计':<51}{est:>8.2f} GiB")

    print(f"\n{'='*74}")
    print(f"  归属到表合计 {tot/2**30:.2f} GiB → 预计新库表数据 {est:.2f} GiB")
    print(f"  原文件 {db.stat().st_size/2**30:.2f} GiB → 预计新文件 {est:.2f}~{est*1.15:.2f} GiB")
    if not args.drop_factors:
        f = sum(sizes.get(t, 0) for t in TIER2_FACTORS) / 2**30
        print(f"  加 --drop-factors    可再省 {f:.2f} GiB")
    if not args.drop_collateral:
        f = sum(sizes.get(t, 0) for t in TIER3_COLLATERAL) / 2**30
        print(f"  加 --drop-collateral 可再省 {f:.2f} GiB")
    if not args.use_float:
        f = sum(sizes.get(t, 0) for t in keep if t in FLOAT_TABLES) / 2**30
        print(f"  加 --float           可再省 {f*(1-FLOAT_SHRINK):.2f} GiB")
    print("=" * 74)

    free = os.statvfs(out.parent)
    free_g = free.f_bavail * free.f_frsize / 2**30
    print(f"\n目标盘可用 {free_g:.1f} GiB, 需要 ~{est*1.2:.1f} GiB")
    if free_g < est * 1.2:
        sys.exit("❌ 磁盘空间不足")

    if not args.execute:
        print("\n[dry-run] 未做任何改动。确认后加 --execute。")
        print(f"[dry-run] 将写入: {out}")
        return 0

    if out.exists() and not args.force:
        sys.exit(f"❌ {out} 已存在, 加 --force 才覆盖")
    for suffix in ("", ".wal"):
        p = Path(str(out) + suffix)
        if p.exists():
            p.unlink()

    # ── 重建 ──────────────────────────────────────────────────────────────────
    log(f"开始重建 → {out}")
    con.execute(f"attach '{out}' as dst")
    t_all = time.time()
    done = []
    for i, t in enumerate(sorted(keep, key=lambda x: -sizes[x]), 1):
        t0 = time.time()
        expr, n_cast = select_expr(cols, t, args.use_float)
        con.execute(f'create table dst."{t}" as select {expr} from src."{t}"')
        con.execute("checkpoint dst")
        n = con.execute(f'select count(*) from dst."{t}"').fetchone()[0]
        cast_note = f", {n_cast} 列转 FLOAT" if n_cast else ""
        log(f"  [{i}/{len(keep)}] {t:<42} {n:>12,} 行{cast_note}  ({time.time()-t0:.1f}s)")
        done.append(t)

    log("重建约束 (PK / UNIQUE) …")
    n_con = 0
    for t in done:
        for ctype, ctext in cons.get(t, []):
            try:
                con.execute(f'alter table dst."{t}" add {ctext}')
                log(f"  ✓ {t}: {ctext}")
                n_con += 1
            except Exception as e:                                  # noqa: BLE001
                log(f"  ❌ {t}: {ctext} → {e}")
    if not n_con:
        log("  (源库在保留集上没有 PK/UNIQUE 约束)")
    con.execute("checkpoint dst")

    log("重建视图…")
    for v in KEEP_VIEWS:
        if v not in views:
            log(f"  ⚠️ 原库没有视图 {v}, 跳过")
            continue
        sql = views[v]
        con.execute("use dst")
        try:
            con.execute(sql)
            log(f"  ✓ {v}")
        except Exception as e:                                  # noqa: BLE001
            log(f"  ❌ {v} 重建失败: {e}")
        finally:
            con.execute("use memory")

    if args.add_index and "stock_indicators" in keep:
        dup = con.execute(
            'select count(*) from (select symbol, date, count(*) c '
            'from dst."stock_indicators" group by 1,2 having c>1)').fetchone()[0]
        uniq = "unique " if dup == 0 else ""
        if dup:
            log(f"  ⚠️ stock_indicators 有 {dup:,} 组 (symbol,date) 重复, 建普通索引")
        con.execute(f'create {uniq}index idx_stock_indicators_sym_date '
                    f'on dst."stock_indicators"(symbol, date)')
        con.execute("checkpoint dst")
        log(f"  ✓ 索引已建 ({uniq.strip() or 'non-unique'})")

    # ── 校验 ──────────────────────────────────────────────────────────────────
    log("校验…")
    failed = []
    for t in done:
        ok, msg = verify_table(con, t, args.use_float, cols)
        log(f"  {'✓' if ok else '❌'} {t:<42} {msg}")
        if not ok:
            failed.append(t)

    exp_con = sum(len(cons.get(t, [])) for t in done)
    got_con = con.execute(
        "select count(*) from duckdb_constraints() where database_name='dst' "
        "and constraint_type in ('PRIMARY KEY','UNIQUE')").fetchone()[0]
    if got_con != exp_con:
        failed.append(f"约束数 {got_con} != 源库 {exp_con}")
        log(f"  ❌ 约束数 {got_con} != 源库保留集的 {exp_con}")
    else:
        log(f"  ✓ 约束 {got_con} 个, 与源库一致")

    n_views = con.execute(
        "select count(*) from duckdb_views() where not internal and database_name='dst'"
    ).fetchone()[0]
    con.execute("checkpoint dst")
    con.close()

    new_g = out.stat().st_size / 2**30
    old_g = db.stat().st_size / 2**30
    print(f"\n{'='*74}")
    print(f"  耗时 {time.time()-t_all:.0f}s")
    print(f"  {old_g:.2f} GiB → {new_g:.2f} GiB   (省 {old_g-new_g:.2f} GiB, -{100*(1-new_g/old_g):.0f}%)")
    print(f"  表 {len(done)} 张, 视图 {n_views} 个")
    print("=" * 74)

    if failed:
        print(f"\n❌ 校验失败: {failed}")
        print(f"   新库留在 {out}, 原库未动。排查后重跑。")
        return 1

    print(f"""
✅ 全部校验通过。原库未做任何修改。

下一步(手工执行, 这是唯一不可逆的一步):

  1) 换上新库
     mv {db} {db}.old
     mv {out} {db}

  2) 跑一次完整日常流程验证
     bash /Users/guhao/finacial/commands/daily/update_and_screener.sh

  3) 三个策略都出结果后再删旧库
     rm {db}.old

  出问题就回滚:
     mv {db} {out} && mv {db}.old {db}
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
