#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修读 stk_fin_forcast.csv —— 最后一列是自由文本，且有 10 处引号没闭合。

## 缺陷是什么（2026-09-01 定位）

`content` 是大段中文预告正文，含换行。多行字段本身**加了引号、是合规的**，
真正的缺陷只有一处：**10 条记录的双引号数量是奇数**（引号开了没关）。
一个未闭合的引号会让解析器一直往后吞，直到遇到下一个引号 —— 所以：

  · `csv.reader` 读到 **50,650** 行（大量记录被吞进同一个字段）
  · `pandas.read_csv` 不报错，读到 **124,094** 行，其中 **11 行是垃圾**：
    `id` 列里装着正文碎片，`code`/`pub_date` 全 NULL
  · DuckDB 的 `read_csv_auto` 直接报 "state machine reached an invalid state"，
    而 merge 脚本的 per-file try/except 把整张表跳过

按「物理行以 `<数字>,` 开头」切，权威记录数是 **124,088**。

## 三个都很隐蔽的教训

1. **`load_jq_round3.py` 当初只校验「落盘行数 == pandas 读入行数」**。
   两边一样错，检查照样通过 —— 于是 11 行垃圾静默进了 parquet 并存活了一周。
   **行数对上不等于读对了**：必须校验【内容】（id 是纯数字、code 合法）。
2. 三个解析器给出三个不同的行数（50,650 / 124,094 / 报错），而其中两个
   **都不报错**。多解析器交叉验证比单个解析器的"成功"可信得多。
3. 文件是上游写坏的，不是我们读错了。修在读侧、并把修了几处**报出来** ——
   悄悄修好和悄悄读错一样危险。
"""
import csv
import io
import re

csv.field_size_limit(10 ** 7)

# ★ 判据必须够具体。原来用 `^\d+,`（行首数字加逗号）—— 但 content 里有
#   **千分位逗号**（`633,969.04元`、`1,606,637.86元`），于是这类续行被误判成
#   新记录，字段整体错位。实测踩到 4 条。
#   真记录的前三段是 id,company_id,NNNNNN.XSHE —— 正文续行不可能长这样。
REC_START = re.compile(r'^\d+,\d+,\d{6}\.XSH[EG],')


def _read_text(path, encoding='utf-8-sig'):
    """读全文。★ 认 .gz —— 增量包里是 .csv.gz，目标文件是 .csv，
    同一个读法要能吃两种，否则增量路径静默走不通。"""
    if path.endswith('.gz'):
        import gzip
        with gzip.open(path, 'rt', encoding=encoding, newline='') as f:
            return f.read()
    return io.open(path, encoding=encoding, newline='').read()


def split_records(path, encoding='utf-8-sig', start_re=REC_START):
    """按记录头判据切出记录。返回 (header, [记录原文], 拼回的续行数)。"""
    raw = _read_text(path, encoding)
    lines = raw.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    if not lines:
        raise ValueError('%s 是空文件' % path)
    header, body = lines[0], lines[1:]
    recs, joined = [], 0
    for ln in body:
        if recs and not start_re.match(ln):
            recs[-1] += '\n' + ln
            joined += 1
        else:
            recs.append(ln)
    return header, recs, joined


def read_repaired(path, encoding='utf-8-sig', n_head=15):
    """返回 (header, rows, stats)。

    ★ 不用 csv 解析整条记录 —— 这个文件同时有两种破法：
        · 10 条记录引号未闭合
        · 另有若干条 content 是【无引号的多行文本】
      两者都会让任何 RFC 4180 解析器失败或串行。

    改成【按位置切】：前 n_head 个字段都是标量（id/code/日期/中文标签/数字），
    不含逗号；余下全部归 content。也就是切前 n_head 个逗号，剩下的原样拿走。

    这个切法的正确性不是靠假设，而是靠**内容校验**证明的：
    每条记录都要满足 id 是纯数字、code 形如 NNNNNN.XSHE/G、end_date 与
    pub_date 是日期。124,088 条全部通过才算切法成立（见 read_forcast_df）。
    """
    header, recs, joined = split_records(path, encoding)
    hdr = next(csv.reader([header]))
    n = len(hdr)
    if n != n_head + 1:
        raise ValueError('%s 表头 %d 列，与 n_head=%d 不匹配' % (path, n, n_head))
    rows, unquoted = [], 0
    for rec in recs:
        parts = rec.split(',', n_head)
        if len(parts) < n_head + 1:
            parts += [''] * (n_head + 1 - len(parts))
        head, content = parts[:n_head], parts[n_head]
        c = content.strip()
        if c.startswith('"'):
            body = c[1:]
            if body.endswith('"'):
                body = body[:-1]
            content = body.replace('""', '"')
        elif '\n' in content:
            unquoted += 1                    # 无引号的多行文本，原样保留
        rows.append(head + [content])
    return hdr, rows, {'records': len(recs), 'joined_lines': joined,
                       'unquoted_multiline': unquoted}


def read_forcast_df(path, verbose=True):
    """读成全字符串 DataFrame，并校验**内容**（不只是行数）。"""
    import pandas as pd
    hdr, rows, st = read_repaired(path)
    df = pd.DataFrame(rows, columns=hdr)
    bad_id = int((~df['id'].str.fullmatch(r'\d+')).sum())
    if bad_id:
        raise ValueError('%s 有 %d 行 id 不是纯数字 —— 没修干净' % (path, bad_id))
    bad_code = int((~df['code'].str.fullmatch(r'\d{6}\.XSH[EG]')).sum())
    if bad_code:
        raise ValueError('%s 有 %d 行 code 不合法' % (path, bad_code))
    # ★ 这两条日期校验是「按位置切」的**证明** —— 若某条记录的前 15 个字段里
    #   混进了逗号，字段会整体右移，日期列必然对不上。全通过才算切对。
    for col in ('end_date', 'pub_date'):
        nb = int((~df[col].str.fullmatch(r'\d{4}-\d{1,2}-\d{1,2}')).sum())
        if nb:
            raise ValueError('%s 有 %d 行 %s 不是日期 —— 字段错位，切法不成立'
                             % (path, nb, col))
    if verbose:
        print('    修读 %s：%s 条记录，拼回续行 %d，无引号多行 %d 条；'
              'id/code/两个日期列全部合法'
              % (path.split('/')[-1], format(len(df), ','),
                 st['joined_lines'], st['unquoted_multiline']))
    return df, st
