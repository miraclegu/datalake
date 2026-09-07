#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""AmazingData 采集的公共件：登录 / 分片调度 / 断点续跑 / 进度条 / 落盘 / 字段说明。

九个 `pull_0X_*.py` 只负责声明「这张表怎么取、按什么分片」，
其余全在这里 —— 所以九个脚本的行为（重试、续跑、schema、字段说明）必然一致。
分散写的话每加一张表就要抄一遍那五件事，而抄漏一件**不报错**，
只是那张表没有重试、或者 schema 与别的表不一样。

## 只在 Windows / Linux x64 上跑

macOS 一条数据都取不到（`tgw` 只有 `.so`/`.pyd`，没有 `.dylib`），
而它报出来的是 `ModuleNotFoundError: No module named '_tgw'` ——
误导性的报错，真正的原因是这个平台从来不支持。判据与旁证见
`datalake/setup_amazing.py` 的 docstring。

## 🔴 五个平台/编码坑（Windows 上全会踩，且都不是"报错"的样子）

| 坑 | 表现 |
|---|---|
| **控制台是 GBK** | `print('🔴…')` 直接 `UnicodeEncodeError` **崩在进度条上**，看着像取数失败。所以这里 `reconfigure(errors='replace')`，且运行期输出**一律不用 emoji / 制表符**（文档里用，控制台里不用） |
| **`open()` 默认不是 UTF-8** | 字段说明 .md 写出来是乱码（GBK 编不了的字符还会抛异常）。所有读写显式 `encoding='utf-8'` |
| **`ad.login()` 失败会 `exit()`** | 它 print 一行 `login fail` 然后**直接杀进程** —— 不抛异常，`try/except` 接不住。所以配置在登录前自查（尤其账号必须以 `tgw_` 开头，否则它 raise 的是 `username is illegal`） |
| **`local_path` 必须是绝对路径且末尾带分隔符** | 手册的例子是 `'D://AmazingData_local_data//'`。给相对路径时 SDK 拼出来的路径落在当前目录，**不报错**，只是文件散在别处 |
| **有些接口一定会写 HDF5** | 没有 `begin_date/end_date` 参数的那几个（`index_constituent` / `industry_constituent` / `industry_base_info` / 复权因子）无条件 `to_hdf`，缺 `tables` 包时才报错，而报的是 pandas 的 ImportError。所以 requirements 里有 `tables`，且 `local_path` 要有几十 GB 空间 |

## 两组参数不能混用（手册 §4.4.1），这决定了本模块的取数姿势

- 参数组 1：`local_path` + `is_local` —— 走 SDK 自己的 HDF5 增量缓存
- 参数组 2：`begin_date` + `end_date` —— **只从服务端取，不碰本地缓存**

本模块**优先用参数组 2**：断点续跑由我们自己的 manifest 负责，
落盘格式是 parquet（本项目的 raw 层就是 parquet）。多一层 HDF5 缓存
只会让"数据到底是新的还是缓存的"变成一件要记的事。
🔴 但**不是每个接口都有参数组 2** —— `index_constituent` / `industry_constituent`
/ `industry_base_info` 只有参数组 1，且手册明写**第一次必须 `is_local=False`**
（"原始数据的剔除日期会根据最新数据修改"）。传 True 会拿到空的或过期的成分，
**而这不报错**。所以那几张表在 spec 里显式写 `is_local=False`。
"""
import io
import json
import os
import shutil
import sys
import time
import traceback

# 控制台编码：见上表第一行。3.7+ 才有 reconfigure，老版本就算了（只是可能乱码）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import field_docs  # noqa: E402  （同目录的生成物）

# datalake/raw/amazing —— 默认落点，与 raw/amazing/README.md 的分层一致
DEFAULT_OUT = os.path.abspath(os.path.join(HERE, '..'))
STATE_DIR = os.path.join(HERE, 'state')
# SDK 自己的 HDF5 缓存目录（我们不读它，但有几个接口无条件要写）
DEFAULT_SDK_CACHE = os.path.join(HERE, 'sdk_cache')

# 行情起点：手册 §2.2「股票/指数/债券/场内基金 2013 年至今」。
# 🔴 早于它取不到数据，而**接口不报错、只是返回空** —— 那看着像"这只票没上市"。
MARKET_START = 20130101
# 财务/公告类的报告期起点：这类接口按报告期或公告日过滤，不受 2013 限制
INFO_START = 19901231
# 前瞻类表（限售解禁）的终点要伸到未来，见 pull_04 的注释
FUTURE_END = 20991231

# 已经有这些列之一时就不再补 `SRC_CODE`（dict 的 key）——
# 补出来的是与已有代码列重复的脏列，而少补则那张表分不出数据属于谁
CODE_COLS = ('MARKET_CODE', 'code', 'CON_CODE', 'INDEX_CODE', 'OLD_CODE', 'ETF_CODE')


# ============================================================ 配置与登录
def add_common_args(ap):
    """九个脚本共用的命令行参数（一处定义，免得各脚本参数名不一样）。"""
    ap.add_argument('--out', default=DEFAULT_OUT,
                    help='parquet 落点根目录，默认 datalake/raw/amazing')
    ap.add_argument('--sdk-cache', default=DEFAULT_SDK_CACHE,
                    help='SDK 的 local_path（有几个接口无条件往这里写 HDF5）')
    ap.add_argument('--account', default=os.path.join(HERE, 'account.json'))
    ap.add_argument('--start', type=int, default=None, help='覆盖起始日期 yyyymmdd')
    ap.add_argument('--end', type=int, default=None, help='覆盖结束日期 yyyymmdd')
    ap.add_argument('--codes', default=None,
                    help='只取这些代码（逗号分隔），调试用')
    ap.add_argument('--limit-codes', type=int, default=None,
                    help='只取前 N 个代码，冒烟测试用')
    ap.add_argument('--only', default=None, help='只跑这些表（逗号分隔）')
    ap.add_argument('--skip', default=None, help='跳过这些表（逗号分隔）')
    ap.add_argument('--force', action='store_true',
                    help='忽略 manifest，重跑所有分片（默认续跑）')
    ap.add_argument('--retries', type=int, default=2,
                    help='单个分片失败后我们自己再重试几次（SDK 内部已经重试 3 次）')
    ap.add_argument('--chunk', type=int, default=None,
                    help='覆盖每片的代码数（默认按接口自己的批量上限来）。'
                         '某张表老是超时/失败就把它调小')
    ap.add_argument('--sleep', type=float, default=0.0,
                    help='每个分片之间歇几秒（怕被限流时用）')
    ap.add_argument('--dry-run', action='store_true',
                    help='只列任务与分片数，不登录、不取数')
    return ap


def load_cfg(path):
    r"""账号从 `_ingest/account.json` 或环境变量来。

    ```json
    {"username": "tgw_xxx", "password": "***", "host": "1.2.3.4", "port": 8000}
    ```

    ★ `account.json` 不会进 git —— datalake 的 .gitignore 是白名单式的
    （只放行 `*.py` / `*.md` / `*.sh` / `*.sql`），`.json` 天然进不去。
    """
    cfg = {}
    if os.path.exists(path):
        with io.open(path, encoding='utf-8') as f:
            cfg = json.load(f)
    for k, env in (('username', 'AMAZING_USER'), ('password', 'AMAZING_PASSWORD'),
                   ('host', 'AMAZING_HOST'), ('port', 'AMAZING_PORT')):
        if os.environ.get(env):
            cfg[k] = os.environ[env]
    missing = [k for k in ('username', 'password', 'host', 'port') if not cfg.get(k)]
    if missing:
        raise SystemExit(
            '配置缺 %s。\n'
            '写一个 %s：{"username":"tgw_xxx","password":"...","host":"1.2.3.4","port":8000}\n'
            '或设环境变量 AMAZING_USER / AMAZING_PASSWORD / AMAZING_HOST / AMAZING_PORT。\n'
            '账号密码 ip 端口要联系开户营业部申请开通（手册 3.5.1.1）。'
            % (','.join(missing), path))
    cfg['port'] = int(cfg['port'])
    if not str(cfg['username']).startswith('tgw_'):
        # 🔴 SDK 里是 `if not username.startswith('tgw_'): raise Exception('username is illegal')`
        #    —— 那句报错不会告诉你少了前缀，所以在这里先说清楚
        raise SystemExit('账号必须以 tgw_ 开头（SDK 只会报 username is illegal）。'
                         '当前是：%r' % cfg['username'])
    return cfg


_API = {}


def api(cfg, sdk_cache):
    """登录一次，返回 (ad, BaseData, InfoData, MarketData, calendar)。

    🔴 `MarketData` 的构造参数是**交易日历**（`ad.MarketData(calendar)`），
    不是无参 —— 忘了传的话报的是 TypeError，看着像版本不对。
    """
    if _API:
        return _API
    ensure_dir(sdk_cache)
    import AmazingData as ad
    say('登录 %s@%s:%s …' % (cfg['username'], cfg['host'], cfg['port']))
    # 手册的入参表写的是 ip/host，**实际签名是 host/port**（照签名来）
    ad.login(username=cfg['username'], password=cfg['password'],
             host=cfg['host'], port=cfg['port'])
    base = ad.BaseData()
    info = ad.InfoData()
    calendar = base.get_calendar()          # List[int]，1990 起
    market = ad.MarketData(calendar)
    say('登录成功。交易日历 %d 天（%s ~ %s）'
        % (len(calendar), calendar[0], calendar[-1]))
    _API.update(dict(ad=ad, base=base, info=info, market=market,
                     calendar=calendar, sdk_path=local_path(sdk_cache)))
    return _API


def local_path(p):
    """SDK 要的 local_path：**绝对路径 + 末尾带分隔符**（手册的例子是
    `'D://AmazingData_local_data//'`）。相对路径不报错，只是文件落在别处。"""
    p = os.path.abspath(p)
    return p if p.endswith(os.sep) else p + os.sep


# ============================================================ 输出与进度
def ensure_dir(p):
    if not os.path.isdir(p):
        os.makedirs(p)
    return p


def say(msg):
    """运行期输出一律走这里：**纯 ASCII 前缀、不用 emoji**（GBK 控制台编不了）。"""
    sys.stdout.write('[amazing] %s\n' % msg)
    sys.stdout.flush()


class Bar(object):
    """进度条。**没有第三方依赖**（tqdm 装不装取决于那个 venv，而这条链上
    少一个包就整脚本起不来），且非 TTY 时自动退化成逐行打印 ——
    重定向到日志文件时 `\\r` 会把整个日志变成一行不可读的东西。"""

    def __init__(self, total, prefix='', width=34, stream=None):
        self.total = max(int(total), 0)
        self.prefix = prefix
        self.width = width
        self.n = 0
        self.t0 = time.time()
        self.stream = stream or sys.stdout
        self.tty = hasattr(self.stream, 'isatty') and self.stream.isatty()
        self.note = ''
        self._last = -1
        self.draw()

    def draw(self):
        if self.total <= 0:
            return
        pct = self.n * 100.0 / self.total
        el = time.time() - self.t0
        eta = (el / self.n * (self.total - self.n)) if self.n else 0
        filled = int(self.width * self.n / self.total)
        bar = '#' * filled + '-' * (self.width - filled)
        line = ('%-22s [%s] %3d%% %d/%d  %s  ETA %s  %s'
                % (self.prefix[:22], bar, pct, self.n, self.total,
                   hms(el), hms(eta), self.note[:46]))
        if self.tty:
            self.stream.write('\r' + line[:200].ljust(len(line)))
        else:
            step = max(1, self.total // 20)
            if self.n != self._last and (self.n % step == 0 or self.n == self.total):
                self.stream.write(line + '\n')
                self._last = self.n
        self.stream.flush()

    def update(self, n=1, note=''):
        self.n += n
        if note:
            self.note = note
        self.draw()

    def close(self, note=''):
        if note:
            self.note = note
        self.n = self.total
        self.draw()
        if self.tty:
            self.stream.write('\n')
        self.stream.flush()


def dw(s):
    """字符串的**显示宽度**（中文/全角算 2 格）。

    ★ `%-12s` 按**字符数**补空格，于是"⑦ ETF数据"和"③ 财务数据"在终端里
      左右不对齐 —— 而任务清单存在的意义就是能纵向扫。
    """
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in str(s))


def pad(s, n, right=False):
    """按显示宽度补到 n 格。"""
    s = str(s)
    fill = ' ' * max(0, n - dw(s))
    return fill + s if right else s + fill


def hms(sec):
    sec = int(sec)
    if sec < 60:
        return '%ds' % sec
    if sec < 3600:
        return '%dm%02ds' % (sec // 60, sec % 60)
    return '%dh%02dm' % (sec // 3600, (sec % 3600) // 60)


def chunks(seq, n):
    seq = list(seq)
    return [seq[i:i + n] for i in range(0, len(seq), n)] or [[]]


# ============================================================ 结果规整
def to_long(res, table, key_name='SRC_CODE'):
    """把接口返回的东西规整成一张长表。

    🔴 **返回类型必须两种都接**：手册里同一类接口的「输出参数」写得不一致
    —— `dividend` 写 dataframe、`margin_detail` 写 dict、`equity_structure`
    写 dataframe 而 `equity_pledge_freeze` 写 dict（实测 pyc 里大多是
    `dict[code] -> DataFrame`）。照手册写死一种，另一种就会
    `AttributeError` 或者**默默只落一行**。

    ★ 空的 DataFrame 不是失败：那一批代码在这个区间里确实可能没有数据
    （没公告、没上市）。所以空值被过滤掉但**计数返回**，
    让 manifest 里能分清"没数据"和"取失败"。
    """
    import pandas as pd
    frames, n_empty = [], 0
    items = res.items() if isinstance(res, dict) else [(None, res)]
    for key, df in items:
        if df is None:
            n_empty += 1
            continue
        if not isinstance(df, pd.DataFrame):
            df = pd.DataFrame(df)
        if df.empty:
            n_empty += 1
            continue
        df = df.copy()
        # index 是不是有意义要看表：手册写「index为序号（无意义）」的就丢掉，
        # 写「index为日期」的必须留 —— 丢了就再也不知道是哪天的数了。
        if isinstance(df.index, pd.RangeIndex):
            df = df.reset_index(drop=True)          # 手册写「index为序号（无意义）」
        else:
            df.index.name = df.index.name or index_name_of(table)
            df = df.reset_index()                   # 手册写「index为日期」——丢了就不知道是哪天
        # 字典的 key 只在表里**本来没有代码列**时补上（补多了是脏列，
        # 不补则那张表分不出是谁的数据）
        if key is not None and not any(c in df.columns for c in CODE_COLS):
            df.insert(0, key_name, key)
        frames.append(df)
    if not frames:
        return None, n_empty
    out = pd.concat(frames, ignore_index=True, sort=False)
    return out, n_empty


def index_name_of(table):
    """有意义的 index 该叫什么名字（手册的「index为日期」那几张）。"""
    return {'kline_day': 'kline_time'}.get(table, 'IDX_DATE')


def melt_wide(df, value_name='factor'):
    """复权因子那种宽表（index=交易日，column=代码）→ 长表。

    🔴 宽表不能直落 parquet：列名就是 6000 个股票代码，每次取的代码集不同
    → **每个分片一个 schema**，之后整表读不起来（而单个文件读得起来，
    所以问题只在合并时才暴露）。
    """
    import pandas as pd
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None
    d = df.copy()
    d.index.name = 'date'
    out = d.reset_index().melt(id_vars='date', var_name='code',
                              value_name=value_name)
    return out[pd.notna(out[value_name])].reset_index(drop=True)


def merged_fields(table, add_fields=None):
    """字段说明 + 本脚本额外加的列（`spec['add_fields']`）。

    ★ 有些列是**采集脚本自己加的**，手册里当然没有：`code_info` 的
      `ASOF_DATE`（它是快照，不标日期隔天就分不清）、`hist_code_list` 的
      `security_type`。它们要一样进字段说明，也不该被当成"意外多出来的列"
      去报警 —— 每个分片报一次的话真正意外的那次就被淹掉了。
    """
    spec = dict(field_docs.FIELDS.get(table, {}))
    for k, v in (add_fields or {}).items():
        spec.setdefault(k, v)
    return spec


def cast_and_order(df, table, add_fields=None):
    """按字段说明里的类型转型、按字段说明的列序排列。返回 (df, 多出来的列)。

    🔴 **这一步不能省。** SDK 对缺的列填 `np.nan`，于是"某个分片里这一列全是
    NaN"时它是 float64，而别的分片里同一列是字符串 —— 分片各自都能写成
    parquet，**合并读的时候才炸**（`pyarrow` schema 不兼容）。
    统一按手册声明的类型转，整张表的所有分片才是同一个 schema。
    ★ 整数列用 pandas 的可空 `Int64`：股东户数这类字段有缺失，
      用 numpy int64 会在有 NaN 时静默变成 float。
    """
    import pandas as pd
    spec = merged_fields(table, add_fields)
    order = [c for c in spec if c != '__source__']
    extra = [c for c in df.columns if c not in order]
    for col in order:
        if col not in df.columns:
            continue
        typ = spec[col][0]
        try:
            if typ.startswith('float') or typ in ('double', 'decimal'):
                df[col] = pd.to_numeric(df[col], errors='coerce').astype('float64')
            elif typ.startswith('int') or typ == 'long':
                df[col] = pd.to_numeric(df[col], errors='coerce').astype('Int64')
            elif typ == 'datetime':
                df[col] = pd.to_datetime(df[col], errors='coerce')
            else:
                df[col] = df[col].astype('string')
        except Exception as e:                      # 转不了就留原样并报出来
            say('  ! %s.%s 转 %s 失败（留原样）：%s' % (table, col, typ, e))
    cols = [c for c in order if c in df.columns] + extra
    return df[cols], extra


def write_part(out_root, table, shard_id, df):
    """一个分片写一个 parquet part。

    ★ 分片各写一个文件（而不是边取边往一个文件里追加）是**断点续跑的实现**：
    文件在就说明这一片取完了。parquet 本来就是"一个目录 = 一张表"，
    duckdb / pandas 都能直接读 `table/*.parquet`。
    """
    d = ensure_dir(os.path.join(out_root, table))
    path = os.path.join(d, 'part-%s.parquet' % shard_id)
    tmp = path + '.tmp'
    df.to_parquet(tmp, index=False)
    # 先写 .tmp 再改名：中途断电时留下的是 .tmp，不会是一个**能打开但内容不全**
    # 的 part —— 那种坏文件续跑时会被当成"这片已经好了"跳过。
    if os.path.exists(path):
        os.remove(path)
    os.rename(tmp, path)
    return path


# ============================================================ manifest 与调度
def state_path(table):
    return os.path.join(ensure_dir(STATE_DIR), '%s.json' % table)


def load_state(table):
    p = state_path(table)
    if os.path.exists(p):
        try:
            with io.open(p, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            say('  ! %s 的 manifest 读不出来，当作没跑过' % table)
    return {'shards': {}, 'failed': {}}


def save_state(table, st):
    with io.open(state_path(table), 'w', encoding='utf-8') as f:
        f.write(json.dumps(st, ensure_ascii=False, indent=1))


def run_table(spec, args, cfg=None):
    """跑一张表：分片 → 取数 → 规整 → 落盘 → 记 manifest。返回一行汇总。

    `spec` 是个 dict：
        name      表名（= 输出目录名）
        shards    [(分片 id, 取数用的 kwargs), …]
        fetch     fetch(**kwargs) -> dict/DataFrame
        note      这张表的口径备注（写进字段说明 .md）
        wide      True 时先 melt（复权因子）
    """
    table = spec['name']
    shards = spec['shards']
    st = load_state(table)
    if args.force:
        st = {'shards': {}, 'failed': {}}
    out_root = args.out
    done = st['shards']
    todo = []
    for sid, kw in shards:
        part = os.path.join(out_root, table, 'part-%s.parquet' % sid)
        if not args.force and sid in done and os.path.exists(part):
            continue
        todo.append((sid, kw))

    n_skip = len(shards) - len(todo)
    prefix = '%s' % table
    say('%s: %d 片，已完成 %d，待取 %d' % (table, len(shards), n_skip, len(todo)))
    if args.dry_run:
        return {'table': table, 'shards': len(shards), 'skipped': n_skip,
                'rows': sum(v.get('rows', 0) for v in done.values()),
                'failed': 0, 'sec': 0.0, 'dry': True}

    bar = Bar(len(shards), prefix=prefix)
    bar.update(n_skip, 'skip %d' % n_skip)
    rows_total = sum(v.get('rows', 0) for v in done.values())
    n_fail, t0 = 0, time.time()

    for sid, kw in todo:
        last_err = None
        for attempt in range(max(1, args.retries + 1)):
            try:
                res = spec['fetch'](**kw)
                if spec.get('wide'):
                    df = melt_wide(res, spec.get('value_name', 'factor'))
                    n_empty = 0 if df is not None else 1
                else:
                    df, n_empty = to_long(res, table)
                if df is None or df.empty:
                    # 空不是失败：这一片确实可能没有数据。但要**留痕** ——
                    # 不留的话下次续跑会把它当成"没跑过"，永远重跑。
                    st['shards'][sid] = {'rows': 0, 'empty': n_empty,
                                         'ts': now(), 'path': None}
                    save_state(table, st)
                    bar.update(1, '%s 空' % sid)
                    last_err = None
                    break
                df, extra = cast_and_order(df, table, spec.get('add_fields'))
                if extra:
                    say('  ! %s 出现字段说明里没有的列：%s（照原样落盘）'
                        % (table, extra[:6]))
                path = write_part(out_root, table, sid, df)
                rows_total += len(df)
                st['shards'][sid] = {'rows': int(len(df)), 'empty': n_empty,
                                     'ts': now(), 'path': os.path.basename(path)}
                st['failed'].pop(sid, None)
                save_state(table, st)
                bar.update(1, '%s %d行' % (sid, len(df)))
                last_err = None
                break
            except KeyboardInterrupt:
                bar.close('中断')
                save_state(table, st)
                raise
            except Exception as e:
                last_err = '%s: %s' % (type(e).__name__, e)
                if attempt < args.retries:
                    time.sleep(2.0 * (attempt + 1))
        if last_err is not None:
            n_fail += 1
            st['failed'][sid] = {'err': last_err, 'ts': now()}
            save_state(table, st)
            bar.update(1, '%s 失败' % sid)
            say('  ! %s 分片 %s 失败：%s' % (table, sid, last_err))
        if args.sleep:
            time.sleep(args.sleep)

    bar.close('%d 行' % rows_total)
    write_field_doc(out_root, table, spec, st)
    return {'table': table, 'shards': len(shards), 'skipped': n_skip,
            'rows': rows_total, 'failed': n_fail, 'sec': time.time() - t0}


def now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def retry_failed_only(spec, args):
    """把 manifest 里失败的分片挑出来重跑（`--retry-failed`）。"""
    st = load_state(spec['name'])
    bad = set(st.get('failed', {}))
    if not bad:
        say('%s: 没有失败的分片' % spec['name'])
        return None
    spec = dict(spec)
    spec['shards'] = [(sid, kw) for sid, kw in spec['shards'] if sid in bad]
    args.force = True                      # 失败的那几片要真的重取
    return run_table(spec, args)


# ============================================================ 字段说明
def write_field_doc(out_root, table, spec=None, st=None):
    r"""每张表落一份 `_字段说明.md`，与数据放在同一个目录。

    🔴 **文件名必须以下划线开头。** 数据目录里混一个非 parquet 文件时，
    `pd.read_parquet('<表名>/')` 会直接炸：

        ArrowInvalid: Could not open Parquet input source '…/字段说明.md':
        Parquet magic bytes not found in footer

    pyarrow 的 dataset 默认 `ignore_prefixes=['.', '_']`，所以下划线开头的
    被跳过。实测：`字段说明.md` 炸，`_字段说明.md` 正常读出 2 行。
    ★ 这条是本仓库自证抓出来的 —— 写的时候不报错（写文件当然成功），
    只在**别人读整张表**的时候才炸。

    内容三段：这张表是什么（接口、分片、口径备注）、**逐字段的类型与中文说明**
    （来自手册，见 tools/parse_manual_fields.py）、以及本次实际取到的规模。

    ★ 说明与数据放一起，而不是集中在一个大文档里 —— 用的时候人是先打开
      某张表的目录的。总览另有一份（`pull_all.py` 写 `字段说明总览.md`）。
    """
    spec = spec or {}
    rows = merged_fields(table, spec.get('add_fields')) or None
    added = set(spec.get('add_fields') or ())
    d = ensure_dir(os.path.join(out_root, table))
    p = os.path.join(d, '_字段说明.md')
    L = []
    L.append('# %s —— 字段说明\n' % table)
    L.append('> 本文件由 `_ingest/amazing_common.py` 生成，改它没用；')
    L.append('> 字段说明的正本是 `_ingest/field_docs.py`（由手册 PDF + wheel 生成）。\n')
    if spec.get('api'):
        L.append('- **接口**：`%s`' % spec['api'])
    if spec.get('params'):
        L.append('- **取数参数**：%s' % spec['params'])
    if spec.get('date_sem'):
        L.append('- **begin_date / end_date 的含义**：%s' % spec['date_sem'])
    if spec.get('shard_by'):
        L.append('- **分片方式**：%s（共 %d 片，每片一个 `part-*.parquet`）'
                 % (spec['shard_by'], len(spec.get('shards', []))))
    if rows:
        L.append('- **字段说明出处**：%s' % rows.get('__source__', ''))
    if spec.get('note'):
        L.append('\n## 口径与坑\n')
        L.append(spec['note'].strip())
    if st:
        n_rows = sum(v.get('rows', 0) for v in st.get('shards', {}).values())
        n_done = len(st.get('shards', {}))
        n_bad = len(st.get('failed', {}))
        L.append('\n## 本次取到的规模\n')
        L.append('| 分片 | 完成 | 失败 | 行数 |')
        L.append('|---|---|---|---|')
        L.append('| %d | %d | %d | %s |'
                 % (len(spec.get('shards', [])), n_done, n_bad, '{:,}'.format(n_rows)))
        if n_bad:
            L.append('\n**有 %d 片失败**，重跑：`python pull_all.py --retry-failed '
                     '--only %s`。失败清单在 `_ingest/state/%s.json`。'
                     % (n_bad, table, table))
    L.append('\n## 字段\n')
    if not rows:
        L.append('（`field_docs.py` 里没有这张表 —— 说明它是本脚本自己拼出来的，'
                 '列的含义见上面的口径备注）')
    else:
        L.append('| # | 字段 | 类型 | 说明 |')
        L.append('|---:|---|---|---|')
        i = 0
        for k, v in rows.items():
            if k == '__source__':
                continue
            i += 1
            mark = ' ★本脚本加的列' if k in added else ''
            L.append('| %d | `%s` | %s | %s%s |'
                     % (i, k, v[0] or '—', v[1].replace('|', '\\|'), mark))
    L.append('')
    with io.open(p, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(L))
    return p


# ============================================================ 代码全集
def universe(args, cfg, security_type='EXTRA_STOCK_A_SH_SZ', start=None, end=None):
    """取一个**冻结**的代码全集，并缓存到 `_ingest/state/universe_*.json`。

    🔴 **必须冻结**：九个脚本、上百个分片会在不同时刻各自取一次代码表，
    而代码表每天变（新股/退市）。不冻结的话不同的表覆盖的代码集不一样，
    之后 join 出来的面板会缺行 —— **而这不报错**。
    ★ 用 `get_hist_code_list`（区间内存在过的代码，**含已退市**）而不是
      `get_code_list`（只有今天在市的）。用后者会把退市股整段丢掉，
      那是最大的一类幸存者偏差。
    """
    a = api(cfg, args.sdk_cache)
    start = start or MARKET_START
    end = end or a['calendar'][-1]
    key = 'universe_%s_%s_%s' % (security_type, start, end)
    cache = os.path.join(ensure_dir(STATE_DIR), key + '.json')
    if os.path.exists(cache) and not args.force:
        with io.open(cache, encoding='utf-8') as f:
            codes = json.load(f)['codes']
        say('代码全集（缓存）：%s %d 个' % (security_type, len(codes)))
    else:
        codes = a['base'].get_hist_code_list(security_type=security_type,
                                             start_date=int(start), end_date=int(end),
                                             local_path=a['sdk_path'])
        codes = sorted(set(str(c) for c in codes))
        with io.open(cache, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'security_type': security_type, 'start': start,
                                'end': end, 'asof': now(), 'codes': codes},
                               ensure_ascii=False))
        say('代码全集：%s %d 个（%s ~ %s，含已退市）'
            % (security_type, len(codes), start, end))
    n0 = len(codes)
    if args.codes:
        want = set(x.strip() for x in args.codes.split(',') if x.strip())
        codes = [c for c in codes if c in want] or sorted(want)
    if args.limit_codes:
        codes = codes[:args.limit_codes]
    if len(codes) != n0:
        # 🔴 过滤后的数量必须报出来：上面那行日志说的是**过滤前**的，
        #    只打前一个数会让"我明明 --limit-codes 5 了"这件事无从确认
        say('  （--codes/--limit-codes 过滤后 %d 个）' % len(codes))
    return codes


def pick_tables(specs, args, strict=True):
    """`--only` / `--skip` 过滤。写在一处，免得九个脚本各写一遍。

    🔴 `strict=False` 是给 `pull_all.py` 用的：它逐组过滤，而 `--only kline_day`
    在别的八组里当然不存在 —— 逐组严格校验会让**跨组挑表直接报错退出**。
    所以那边关掉逐组校验，改成**跑完九组之后拿并集校验一次**
    （校验不能一起去掉：拼错表名的表现是"跑完了但什么都没取"，报告全绿）。
    """
    names = [s['name'] for s in specs]
    if args.only:
        want = [x.strip() for x in args.only.split(',') if x.strip()]
        bad = [w for w in want if w not in names]
        if bad and strict:
            # 🔴 拼错表名不能静默跳过 —— 那表现为"跑完了但什么都没取"
            raise SystemExit('--only 里有不存在的表：%s\n可选：%s'
                             % (bad, ', '.join(names)))
        specs = [s for s in specs if s['name'] in want]
    if args.skip:
        drop = set(x.strip() for x in args.skip.split(',') if x.strip())
        specs = [s for s in specs if s['name'] not in drop]
    return specs


def summarize(results, title='汇总'):
    """把每张表的结果打成一张表。分片失败要**单独一列**，不能只写在日志里。"""
    rows = [r for r in results if r]
    if not rows:
        say('%s：没有跑任何表' % title)
        return 0
    w = max([dw(r['table']) for r in rows] + [6])
    say('')
    say('%s：' % title)
    say('  %s %8s %8s %8s %14s %8s'
        % (pad('表', w), '分片', '跳过', '失败', '行数', '耗时'))
    n_bad = 0
    for r in rows:
        n_bad += r.get('failed', 0)
        say('  %s %8d %8d %8d %14s %8s'
            % (pad(r['table'], w), r['shards'], r.get('skipped', 0), r.get('failed', 0),
               '{:,}'.format(r.get('rows', 0)), hms(r.get('sec', 0))))
    say('  %s %8s %8s %8d %14s'
        % (pad('合计', w), '', '', n_bad,
           '{:,}'.format(sum(r.get('rows', 0) for r in rows))))
    if n_bad:
        say('  有 %d 个分片失败 —— 重跑：--retry-failed' % n_bad)
    return n_bad


def main_for(specs_fn, desc):
    """九个脚本共用的 main：解析参数 → 建 spec → 跑 → 汇总。

    ★ 每个脚本都能**单独**跑（`python pull_03_financial.py`），
      也被 `pull_all.py` 直接 import 调用 —— 后者不是去 subprocess 起九个
      进程，因为登录一次就够了，而且九个进程各登一次可能被服务端拒。
    """
    import argparse
    ap = argparse.ArgumentParser(description=desc)
    add_common_args(ap)
    ap.add_argument('--retry-failed', action='store_true',
                    help='只重跑 manifest 里失败的分片')
    args = ap.parse_args()
    return run_with_args(specs_fn, args, desc)


def run_with_args(specs_fn, args, desc):
    cfg = None if args.dry_run else load_cfg(args.account)
    specs = pick_tables(specs_fn(args, cfg), args)
    say('=== %s：%d 张表 ===' % (desc, len(specs)))
    results = []
    for spec in specs:
        try:
            if getattr(args, 'retry_failed', False):
                results.append(retry_failed_only(spec, args))
            else:
                results.append(run_table(spec, args, cfg))
        except KeyboardInterrupt:
            say('用户中断（已取到的分片都留着，下次续跑）')
            break
        except Exception:
            say('!! %s 整表失败：\n%s' % (spec['name'], traceback.format_exc()))
            results.append({'table': spec['name'], 'shards': len(spec['shards']),
                            'skipped': 0, 'rows': 0, 'failed': len(spec['shards']),
                            'sec': 0.0})
    return summarize(results, desc), results


def disk_free_gb(p):
    try:
        return shutil.disk_usage(p).free / 1024.0 ** 3
    except Exception:
        return -1.0


# ============================================================ 分片工具
def years_between(start, end):
    """[(年, 该年起, 该年止), …]，两端按 start/end 夹住。

    ★ **按年分片**不是为了好看：一次要 13 年 × 6000 只的日线是几千万行，
      内存直接爆；而且中途失败要从头再来。按年切之后每片几万行，
      断在哪一年就从那一年续。
    """
    y0, y1 = int(str(start)[:4]), int(str(end)[:4])
    out = []
    for y in range(y0, y1 + 1):
        b = max(int(start), y * 10000 + 101)
        e = min(int(end), y * 10000 + 1231)
        if b <= e:
            out.append((y, b, e))
    return out


def chunk_size(args, default):
    """每片多少个代码。默认取接口自己的批量上限（`QueryPara.req_*_len`），
    `--chunk` 可以整体调小 —— 财务三表那种一行 179 列的，50 只就够撑出几万行。"""
    n = getattr(args, 'chunk', None)
    return int(n) if n else int(default)


def shards_codes(codes, size, tag='', extra=None):
    """按代码分片：[(分片id, {'code_list': [...]，**extra}), …]"""
    out = []
    for i, ch in enumerate(chunks(codes, size)):
        if not ch:
            continue
        kw = {'code_list': ch}
        if extra:
            kw.update(extra)
        out.append(('%s%04d' % (tag, i), kw))
    return out


def shards_codes_years(codes, size, start, end, tag='', extra=None):
    """按 (年 × 代码块) 分片，分片 id 形如 `2019-0003`。

    ★ id 里带年份是**故意**的：`ls part-2019-*` 就能看出哪一年缺，
      而纯序号的 id 一旦分片规则变了就对不上了。
    """
    out = []
    for (y, b, e) in years_between(start, end):
        for i, ch in enumerate(chunks(codes, size)):
            if not ch:
                continue
            kw = {'code_list': ch, 'begin_date': b, 'end_date': e}
            if extra:
                kw.update(extra)
            out.append(('%s%d-%04d' % (tag, y, i), kw))
    return out
