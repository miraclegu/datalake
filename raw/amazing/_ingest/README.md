# raw/amazing/_ingest —— 在 Windows 上把 AmazingData 拉到本地

本目录是**采集端**：九个脚本按手册的九类数据分工，一个总调度串起来跑，
落成 parquet + 逐字段说明。

> macOS 上跑不了取数（`tgw` 没有 `.dylib`，判据见 `datalake/setup_amazing.py`）。
> 但 **`selftest.py` 在 macOS 上能跑** —— 它注入一个假 SDK，把分片/续跑/重试/
> schema/字段说明这些逻辑全验一遍（157 条断言）。取数逻辑与平台无关的部分
> 因此不用等到上机才第一次被执行。

---

## 一、装环境（Windows x64，一次性）

```bat
cd datalake\raw\amazing\_ingest
py -3.13 -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\pip install ..\tgw-1.0.9.2-py3-none-any.whl
venv\Scripts\pip install ..\AmazingData-1.1.9-cp313-none-any.whl
venv\Scripts\python -c "import tgw, AmazingData; print('ok')"
```

四条纪律，每条都对着一个实际会踩的坑：

| | 为什么 |
|---|---|
| **必须用网盘下的离线 wheel** | `pip install AmazingData` 装的是**另一个项目**（PyPI 上那个是 0.0.3、13 KB、与银河的 1.0.24 无关），`tgw` 在 PyPI 上根本不存在。装上去"看着成功"，然后 API 全对不上 —— 与 `tdx2db` 那次同一个坑 |
| **wheel 要选对 Python 版本** | AmazingData 按 cp38~cp314 分包（`.pyc` 的 magic number 绑版本）。装错 pip 会明确报错 —— **这是这条链上唯一一个会明确报错的坑** |
| **装进独立 venv** | 它要 `numba>=0.65`，会把 numpy 顶到 2.5.x；回测链与 datalake 构建链都吃 numpy |
| **`tables` 不能省** | 成分股/复权因子那几个接口**无条件**往 `local_path` 写 HDF5。缺它报的是 pandas 的 ImportError，看着与网络无关 |

★ 体检脚本 `datalake/setup_amazing.py` 会**在装之前**扫 wheel 里有没有本机
这一款原生库并给出裁决 —— 别让判断落在 `ModuleNotFoundError: No module
named '_tgw'` 这句误导性的报错上（macOS 的 `sys.platform` 是 `darwin`，
`dar[win]` 里含 `win`，于是被 tgw 判成 Windows）。

## 二、配账号

写 `_ingest/account.json`（**不会进 git** —— datalake 的 .gitignore 是白名单式的，
只放行 `*.py`/`*.md`/`*.sh`/`*.sql`，`.json` 天然进不去）：

```json
{"username": "tgw_xxx", "password": "***", "host": "1.2.3.4", "port": 8000}
```

或设环境变量 `AMAZING_USER` / `AMAZING_PASSWORD` / `AMAZING_HOST` / `AMAZING_PORT`。

- 账号密码 ip 端口**要联系开户营业部申请开通**（手册 3.5.1.1）。
- 🔴 **账号必须以 `tgw_` 开头**。SDK 里那句检查报的是 `username is illegal`，
  不会告诉你少了前缀 —— 所以 `load_cfg()` 先自查。
- 🔴 **登录失败时 SDK 会 `exit()`**：print 一行 `login fail` 然后直接杀进程，
  不抛异常，`try/except` 接不住。看到进程"莫名退出"先怀疑账号。

## 三、跑

```bat
venv\Scripts\python pull_all.py --dry-run      # 先看任务清单（不登录、不落盘）
venv\Scripts\python pull_all.py                # 全量，断点续跑
venv\Scripts\python pull_all.py --groups 1,2   # 只跑基础数据 + 日K
venv\Scripts\python pull_all.py --retry-failed # 只重跑失败的分片
venv\Scripts\python pull_all.py --sleep 0.2    # 怕限流就整体放慢
venv\Scripts\python pull_03_financial.py       # 单独跑某一组也行
```

**建议第一次这么跑**（别一上来就全量）：

```bat
:: ① 冒烟：只取 5 只票、一个月，看链路通不通、schema 对不对
venv\Scripts\python pull_all.py --limit-codes 5 --start 20240101 --end 20240131

:: ② 看一眼落盘与字段说明
type ..\字段说明总览.md
type ..\history_stock_status\_字段说明.md

:: ③ 再全量（几千万行，按组分批更稳）
venv\Scripts\python pull_all.py --groups 1
venv\Scripts\python pull_all.py --groups 2
...
```

参数一览（九个脚本与总调度共用，定义在 `amazing_common.add_common_args`）：

| 参数 | 作用 |
|---|---|
| `--out DIR` | parquet 落点，默认 `datalake/raw/amazing` |
| `--sdk-cache DIR` | SDK 的 `local_path`（那几个接口无条件往这写 HDF5，**要留几十 GB**） |
| `--start` / `--end` | 覆盖日期区间（各表的日期含义不同，见下） |
| `--codes` / `--limit-codes` | 只取某几只 / 前 N 只，调试用 |
| `--only` / `--skip` / `--groups` | 挑表 / 挑组（**拼错会报错退出**，不会静默跑 0 张表） |
| `--chunk N` | 覆盖每片的代码数。某张表老超时就调小 |
| `--force` | 忽略 manifest 重跑所有分片（默认是续跑） |
| `--retries N` | 单片失败后我们自己再重试几次（SDK 内部已经重试 3 次） |
| `--sleep S` | 每片之间歇几秒 |
| `--dry-run` | 只列任务，不登录不取数 |

## 四、落盘长什么样

```
datalake/raw/amazing/
  字段说明总览.md                   34 张表的索引（pull_all.py 写）
  <表名>/part-<分片id>.parquet      数据，一个分片一个文件
  <表名>/_字段说明.md               这张表的逐字段说明（每次跑都重写）
  _ingest/state/<表名>.json         manifest：哪片好了、哪片失败了、各多少行
  _ingest/state/universe_*.json     **冻结的代码全集**
```

一个目录 = 一张表：

```python
import pandas as pd
df = pd.read_parquet('datalake/raw/amazing/history_stock_status')   # 整表
```
```sql
select * from read_parquet('datalake/raw/amazing/kline_day/*.parquet');
```

🔴 **`_字段说明.md` 的下划线是必须的。** 数据目录里混一个非 parquet 文件时
`pd.read_parquet('<表名>/')` 会直接炸（`Parquet magic bytes not found`）；
pyarrow 的 dataset 默认跳过 `_`/`.` 开头的文件。
★ 这条是自证抓出来的 —— 写文件当然成功，只在**别人读整张表**时才炸。

## 五、九组 / 34 张表

| 组 | 脚本 | 表 |
|---|---|---|
| ① 基础数据 | `pull_01_basic.py` | `trading_calendar` `hist_code_list` `code_info` `stock_basic` `history_stock_status` `backward_factor` `adj_factor` `bj_code_mapping` |
| ② 历史日K | `pull_02_kline_day.py` | `kline_day`（股票/指数/ETF/可转债，`--kinds`） |
| ③ 财务数据 | `pull_03_financial.py` | `balance_sheet` `cash_flow` `income` `profit_express` `profit_notice` |
| ④ 股东股本 | `pull_04_holder.py` | `share_holder` `holder_num` `equity_structure` `equity_pledge_freeze` `equity_restricted` |
| ⑤ 股东权益 | `pull_05_equity.py` | `dividend` `right_issue` |
| ⑥ 融资融券 | `pull_06_margin.py` | `margin_summary` `margin_detail` |
| ⑦ ETF | `pull_07_etf.py` | `etf_pcf_info` `etf_pcf_constituent` `fund_share` `fund_nav` `fund_iopv` |
| ⑧ 交易所指数 | `pull_08_index.py` | `index_constituent` `index_weight` |
| ⑨ 行业指数 | `pull_09_industry.py` | `industry_base_info` `industry_constituent` `industry_weight` `industry_daily` |

每张表**为什么这么分片、日期是什么含义、有哪些坑**，写在各自脚本的 docstring
和 spec 的 `note` 里 —— 而 `note` 会**原样落进那张表的 `_字段说明.md`**，
所以用数据的人不用回来读代码。

## 六、🔴 采集之前必须知道的七件事

1. **行情起点 2013**（手册 §2.2）。更早**不报错、只返回空** —— 那看着像
   "这只票还没上市"。所以 tdx 那份 1990 起的**不能扔**（2013 前占本地面板
   23%、638 万行，而 froec 那条 edge 正是被 2006-2015 的样本外否证的）。
2. **`begin_date` 的含义每张表都不一样**：报告期（财务）/ 交易日（日K、
   历史证券状态、融资融券）/ 公告日（分红、配股、股权质押）/ 变动日（股本、
   ETF 份额）/ **解禁日（限售解禁，在未来）**/ 到期日（十大股东）。
   拿同一个区间套所有表能跑通，**但取回来的集合不是你以为的那个**。
3. **限售解禁的终点要伸到未来**（本脚本用 20991231）。用今天当终点会把
   未来的解禁计划整段丢掉，而那正是这张表的全部价值。
4. **财务三表同一 `(code, 报告期)` 有多行**，靠 `STATEMENT_TYPE` 区分
   （26+ 种）。raw 层原样全存；用之前必须先筛口径，否则 join 一行变多行，
   **而这不报错**，只是所有除法都被放大了几倍。
5. **PIT 用 `ACTUAL_ANN_DATE`** 而不是 `ANN_DATE`。差一天就是未来函数。
6. **成分股类表的 `is_local` 必须传 False**（这几个接口的 True 是"**只看
   本地**"，第一次跑传 True 拿到的是空），而且**每次全量重取、不要增量合并**
   —— 手册说剔除日期会被最新数据改写，增量会留下永远不被剔除的僵尸成分。
7. **`margin_summary` 是按交易所一天一行**（`EXCHANGE` 这一列**手册的字段表
   里没写**，是从 wheel 的 `columns_list` 里发现的）。当成全市场用会小一半。

## 七、字段说明是怎么来的（以及为什么不是抄手册）

```
手册 PDF ─┐
          ├─→ tools/parse_manual_fields.py ─→ field_docs.py ─→ 每张表的 _字段说明.md
wheel .pyc┘
```

| | 来源 | 为什么非它不可 |
|---|---|---|
| **字段名 + 列序** | wheel 里 `download_xxx()` 的 `columns_list` 常量元组 | 那是**返回值真实的列与列序**（SDK 取数后按它 reindex） |
| **中文说明** | 手册 PDF 的字段表 | pyc 里只有名字，没有含义 |

🔴 **字段名不能照 PDF 抄**：pypdf 抽出来的文本会在某些字距对上插空格
（`STA TEMENT_TYPE` / `ACC_RECEIV ABLE` / `PAY ABLE`），长名字还会跨行、
甚至**跨页**断开（`RCV_CED_UNEARNED` + `_PREM_RESV`）。照 PDF 抄的列名与真实
返回值对不上，**而这不报错** —— 只是那一列的说明永远匹配不上、文档里一片空白。

解析完**当场自证**（783 个字段：说明都非空、都没串进下一行），
不通过就拒绝写出。手册出新版本时重新生成：

```sh
python3 -m venv /tmp/pdfenv && /tmp/pdfenv/bin/pip install pypdf
/tmp/pdfenv/bin/python tools/parse_manual_fields.py     # 在 macOS 上跑就行
```

顺带发现两处**手册没写而 SDK 会返回**的列（进了 `KNOWN_UNDOCUMENTED` 白名单）：
`margin_summary.EXCHANGE`、`income.ADJ_PREV_YEAR_LOSS_GAIN`。
★ 白名单而不是 `--allow-missing`：后者会把**新出现**的缺口也一起放过，
而新缺口正是要看见的东西。

## 八、自证

```sh
python3 selftest.py        # macOS 上也能跑，约 10 秒，157 条断言
python3 selftest.py -v     # 每条都打出来
```

它注入假 SDK（列名直接取自 `field_docs`，所以返回**形状**与真 SDK 一致），
把这些验一遍：分片 id 不重复 / 所有 part 的 schema 完全一致 / 全 NaN 的列
也按声明类型落盘 / 续跑跳过已完成分片且一次接口都不打 / 空结果记 `rows:0`
而不是 failed / 失败进 manifest 且 `--retry-failed` 只重跑它们 / 宽表 melt /
字段说明生成且能整目录 read_parquet / `--only` 拼错要报错 / 代码全集被冻结 /
每个 spec 的 fetch 参数与分片 kwargs 对得上 / **端到端跑一遍 `pull_all.py`**。

🔴 **它证明不了"接口真的这么返回"** —— 只证明"如果接口这么返回，我们处理得对"。
所以第一次上真机之后，必须做三件对数（判据见 `raw/amazing/README.md`）：

| 对什么 | 与谁比 | 为什么它最容易错 |
|---|---|---|
| `HIGH_LIMITED` / `LOW_LIMITED` | 面板自算的涨跌停价 | 本项目自算命中率 99.97%，漏的是新股首日/退市整理期 —— 正好互相验 |
| `ACTUAL_ANN_DATE` | 聚宽 `pub_date` | PIT 的命门，差一天就是未来函数 |
| `LEVEL1_NAME` | 申万一级 | 手册没写是哪一套分类；不是申万的话红利策略与板块页的口径就变了 |

★ 照本项目探针的规矩：**探针只做验证，且带已知真值当场对数** ——
"有值但对不上"比"没有值"更危险。
