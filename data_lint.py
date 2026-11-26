#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data-lint —— 一张 CSV 的数据质量体检报告。

零依赖（只用标准库），单文件，可直接放进 CI 当门禁。

    python data_lint.py orders.csv
    python data_lint.py orders.csv --key order_id
    python data_lint.py orders.csv --json > report.json
    python data_lint.py orders.csv --html report.html

退出码：0 = 无严重问题；1 = 存在严重问题；2 = 文件或参数错误。
"""

import argparse
import csv
import io
import json
import math
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path

VERSION = "0.1.0"

# 被当作"空值"的字面量。真实数据里 NULL 的写法五花八门，这是第一类坑。
NULL_TOKENS = {"", "null", "none", "nan", "na", "n/a", "nil", "-"}

# 日期格式白名单。故意只收 ISO 系，避免把 03/04 猜成 3 月 4 日还是 4 月 3 日。
DATE_FORMATS = [
    ("%Y-%m-%d", "YYYY-MM-DD"),
    ("%Y/%m/%d", "YYYY/MM/DD"),
    ("%Y.%m.%d", "YYYY.MM.DD"),
    ("%Y-%m-%d %H:%M:%S", "YYYY-MM-DD HH:MM:SS"),
    ("%Y/%m/%d %H:%M:%S", "YYYY/MM/DD HH:MM:SS"),
    ("%Y-%m-%dT%H:%M:%S", "ISO8601"),
    ("%Y-%m-%d %H:%M", "YYYY-MM-DD HH:MM"),
]

INT_RE = re.compile(r"^[+-]?\d+$")
FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")
GROUPED_NUM_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
LEADING_ZERO_RE = re.compile(r"^0\d+$")
BOOL_TOKENS = {"true", "false", "yes", "no", "y", "n", "是", "否"}

SEVERITY_CN = {"critical": "严重", "warning": "警告", "info": "提示"}
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
CATEGORY_CN = {
    "structure": "结构",
    "type": "类型",
    "completeness": "完整性",
    "uniqueness": "唯一性",
    "consistency": "一致性",
    "distribution": "分布",
}
WEIGHTS = {"critical": 15, "warning": 5, "info": 1}

_CLASSIFY_CACHE = {}
_DATE_CACHE = {}


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def decode_bytes(raw, encoding="auto"):
    """按顺序尝试编码。Excel 导出的中文 CSV 经常是 GBK。"""
    if encoding and encoding != "auto":
        return raw.decode(encoding), encoding
    for enc in ("utf-8-sig", "gbk", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8(replace)"


def sniff_delimiter(text):
    try:
        return csv.Sniffer().sniff(text, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def is_null(value):
    return value.strip() == "" or value.strip().lower() in NULL_TOKENS


def date_format_of(value):
    s = value.strip()
    if s in _DATE_CACHE:
        return _DATE_CACHE[s]
    label = None
    for fmt, name in DATE_FORMATS:
        try:
            datetime.strptime(s, fmt)
            label = name
            break
        except ValueError:
            continue
    _DATE_CACHE[s] = label
    return label


def classify_value(value):
    """把一个原始字符串归类，用于推断列类型。"""
    s = value.strip()
    hit = _CLASSIFY_CACHE.get(s)
    if hit is not None:
        return hit
    low = s.lower()
    if low in BOOL_TOKENS and not INT_RE.match(s):
        kind = "bool"
    elif INT_RE.match(s):
        kind = "int"
    elif GROUPED_NUM_RE.match(s):
        kind = "float"
    elif FLOAT_RE.match(s):
        kind = "float"
    elif date_format_of(s):
        kind = "date"
    else:
        kind = "string"
    _CLASSIFY_CACHE[s] = kind
    return kind


def to_float(value):
    try:
        return float(value.strip().replace(",", ""))
    except ValueError:
        return None


def percentile(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * p
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def dwidth(text):
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text, width):
    return text + " " * max(0, width - dwidth(text))


def loc(row_index):
    """数据行下标 -> 文件里的行号（表头是第 1 行）。"""
    return "第 %d 行" % (row_index + 2)


def make_finding(severity, category, column, title, detail, impact, examples=None):
    return {
        "severity": severity,
        "category": category,
        "column": column,
        "title": title,
        "detail": detail,
        "impact": impact,
        "examples": examples or [],
    }


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------

def load_csv(path, encoding="auto", delimiter=None, max_rows=None):
    raw = path.read_bytes()
    text, used_encoding = decode_bytes(raw, encoding)
    if delimiter is None:
        delimiter = sniff_delimiter(text[:8192])

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    all_rows = [r for r in reader if any(c.strip() for c in r)]
    if not all_rows:
        return None, [], {"encoding": used_encoding, "delimiter": delimiter}

    header = [h.strip() for h in all_rows[0]]
    header = [h if h else "列%d" % (i + 1) for i, h in enumerate(header)]
    body = all_rows[1:]
    if max_rows:
        body = body[:max_rows]

    meta = {
        "encoding": used_encoding,
        "delimiter": delimiter,
        "total_rows_read": len(body),
    }
    return header, body, meta


# --------------------------------------------------------------------------
# 单列分析
# --------------------------------------------------------------------------

def analyze_column(name, values, null_threshold):
    meta = {
        "name": name,
        "type": "empty",
        "nulls": 0,
        "null_ratio": 0.0,
        "distinct": 0,
        "samples": [],
        "mixed": False,
    }
    findings = []
    total = len(values)

    non_null = []
    null_kinds = Counter()
    for idx, v in enumerate(values):
        if is_null(v):
            null_kinds["empty" if v.strip() == "" else "token"] += 1
        else:
            non_null.append((idx, v))

    meta["nulls"] = total - len(non_null)
    meta["null_ratio"] = round(meta["nulls"] / total, 4) if total else 0.0

    if not non_null:
        if total:
            findings.append(make_finding(
                "critical", "completeness", name, "整列为空",
                "该列全部 %d 行都是空值" % total,
                "任何依赖该列的聚合或关联都会得到空结果"))
        return meta, findings

    seen = []
    for _, v in non_null:
        s = v.strip()[:24]
        if s not in seen:
            seen.append(s)
        if len(seen) == 2:
            break
    meta["samples"] = seen

    # ---- 完整性 ----
    ratio = meta["null_ratio"]
    if ratio > null_threshold:
        severity = "critical" if ratio >= 0.5 else "warning"
        findings.append(make_finding(
            severity, "completeness", name, "空值率偏高",
            "%d / %d 行为空（%.1f%%），阈值 %.0f%%" % (meta["nulls"], total, ratio * 100, null_threshold * 100),
            "聚合结果会偏低；关联时可能丢行",
            ["空字符串 %d 处，NULL 类标记 %d 处" % (null_kinds["empty"], null_kinds["token"])]
            if null_kinds["empty"] and null_kinds["token"] else []))
    if null_kinds["empty"] and null_kinds["token"]:
        findings.append(make_finding(
            "info", "completeness", name, "空值有两种写法",
            "空字符串 %d 处、NULL 类标记 %d 处" % (null_kinds["empty"], null_kinds["token"]),
            "下游对空串和 NULL 的处理不同，过滤条件容易漏数"))

    # ---- 类型推断 ----
    type_counts = Counter()
    date_formats = Counter()
    for _, v in non_null:
        kind = classify_value(v)
        type_counts[kind] += 1
        if kind == "date":
            date_formats[date_format_of(v)] += 1

    types = set(type_counts)
    numeric_ratio = (type_counts.get("int", 0) + type_counts.get("float", 0)) / len(non_null)

    if types <= {"int"}:
        family = "int"
    elif types <= {"int", "float"}:
        family = "float"
    elif types <= {"bool"}:
        family = "bool"
    elif types <= {"date"}:
        family = "date"
    elif numeric_ratio >= 0.8:
        family = "float"
    else:
        family = "string"

    meta["type"] = family

    if family in ("int", "float") and (types - {"int", "float"}):
        meta["mixed"] = True
        samples = [(i, v) for i, v in non_null if classify_value(v) not in ("int", "float")][:2]
        findings.append(make_finding(
            "critical", "type", name, "类型混杂",
            "%d 个数值 + %d 个非数值" % (
                type_counts.get("int", 0) + type_counts.get("float", 0),
                len(non_null) - type_counts.get("int", 0) - type_counts.get("float", 0)),
            "按数值导入会让整列退化为文本，SUM 结果为 0 且不报错",
            ["%s \"%s\"" % (loc(i), v) for i, v in samples]))
    elif family == "string" and (types - {"string"}):
        meta["mixed"] = True
        samples = [(i, v) for i, v in non_null if classify_value(v) != "string"][:2]
        findings.append(make_finding(
            "warning", "type", name, "文本列中混有数字或日期",
            "共 %d 行，其中 %d 行看起来是数值或日期" % (len(non_null), len(non_null) - type_counts.get("string", 0)),
            "如果有脚本按类型解析这一列，会在这些行上出错",
            ["%s \"%s\"" % (loc(i), v) for i, v in samples]))

    # 前导零：看起来是数字，实际是编号
    if family == "int":
        zeros = [(i, v) for i, v in non_null if LEADING_ZERO_RE.match(v.strip())]
        if zeros:
            findings.append(make_finding(
                "warning", "consistency", name, "数字列存在前导零",
                "%d 个值以 0 开头，例如 \"%s\"" % (len(zeros), zeros[0][1].strip()),
                "转成整数后前导零丢失，且不可逆；这类列应作为字符串保存",
                ["%s \"%s\"" % (loc(i), v) for i, v in zeros[:3]]))

    # 千分位
    grouped = [v for _, v in non_null if GROUPED_NUM_RE.match(v.strip())]
    if grouped and family in ("int", "float"):
        findings.append(make_finding(
            "info", "consistency", name, "数值含千分位分隔符",
            "%d 个值带逗号，例如 \"%s\"" % (len(grouped), grouped[0].strip()),
            "多数解析器会把带逗号的数字当文本，整列类型可能被推断错"))

    # 日期格式不一致
    if len(date_formats) > 1:
        findings.append(make_finding(
            "warning", "consistency", name, "日期格式不一致",
            "、".join("%s %d 处" % (k, v) for k, v in date_formats.items()),
            "同一列混用多种格式，按字符串排序或比较会出错"))

    # ---- 一致性：大小写与空格 ----
    variants = {}
    for _, v in non_null:
        variants.setdefault(v.strip().lower(), set()).add(v)
    case_groups = {k: vs for k, vs in variants.items() if len(vs) > 1}
    if case_groups:
        samples = [" / ".join('"%s"' % x for x in sorted(vs)[:3]) for vs in list(case_groups.values())[:2]]
        findings.append(make_finding(
            "warning", "consistency", name, "取值大小写或写法不一致",
            "%d 组取值存在多种写法" % len(case_groups),
            "按某个具体写法过滤时会漏数（例如 status='paid' 漏掉 'Paid'）",
            samples))

    spaced = [v for _, v in non_null if v != v.strip()]
    if spaced:
        findings.append(make_finding(
            "warning", "consistency", name, "存在首尾空格",
            "%d 个值首尾带空白，例如 \"%s\"" % (len(spaced), spaced[0]),
            "等值匹配和去重都会失效"))

    # 小数位不一致
    if family == "float":
        scales = Counter()
        for _, v in non_null:
            s = v.strip().replace(",", "")
            if "e" in s.lower():
                continue
            scales[len(s.split(".")[1]) if "." in s else 0] += 1
        if len(scales) > 1:
            findings.append(make_finding(
                "info", "consistency", name, "小数位不一致",
                "、".join("%d 位小数 %d 处" % (k, v) for k, v in sorted(scales.items())),
                "展示精度不统一；入库前应统一 scale"))

    # ---- 唯一性（单列层面） ----
    distinct = Counter(v.strip() for _, v in non_null)
    meta["distinct"] = len(distinct)
    if len(distinct) == 1 and total > 1:
        findings.append(make_finding(
            "info", "distribution", name, "常量列",
            "该列 %d 行全部是同一个值 \"%s\"" % (total, next(iter(distinct))),
            "通常意味着上游没有正确写入，或该字段已废弃"))

    # ---- 分布（数值列） ----
    if family in ("int", "float"):
        nums = [n for n in (to_float(v) for _, v in non_null) if n is not None]
        if nums:
            meta["stats"] = {
                "min": min(nums),
                "p50": round(percentile(nums, 0.50), 4),
                "p95": round(percentile(nums, 0.95), 4),
                "max": max(nums),
                "mean": round(sum(nums) / len(nums), 4),
            }
            # 用 3 倍 IQR 而不是 1.5：小样本上 1.5 会把正常的长尾业务值全标成异常，
            # 宁可按漏，也不要制造噪音——告警一旦不准，就没人看了。
            if len(nums) >= 8:
                q1 = percentile(nums, 0.25)
                q3 = percentile(nums, 0.75)
                iqr = q3 - q1
                if iqr > 0:
                    low, high = q1 - 3.0 * iqr, q3 + 3.0 * iqr
                    outliers = [n for n in nums if n < low or n > high]
                    if outliers:
                        findings.append(make_finding(
                            "warning", "distribution", name, "存在极端值",
                            "%d 个值落在 [%.2f, %.2f] 之外，最大 %.2f" % (len(outliers), low, high, max(outliers)),
                            "可能是录入错误、单位不一致，或真实的极端值——需要业务确认",
                            ["示例：%s" % "、".join("%.2f" % x for x in outliers[:3])]))
            zeros = sum(1 for n in nums if n == 0)
            if zeros and zeros / len(nums) >= 0.1:
                findings.append(make_finding(
                    "info", "distribution", name, "大量 0 值",
                    "%d / %d 个值为 0（%.0f%%）" % (zeros, len(nums), zeros / len(nums) * 100),
                    "常见于默认值填充或缺失被写成 0，会拉低均值"))

    return meta, findings


# --------------------------------------------------------------------------
# 整表分析
# --------------------------------------------------------------------------

def analyze(header, rows, key_cols=None, null_threshold=0.05):
    findings = []
    key_cols = list(key_cols or [])
    width = len(header)

    norm = []
    ragged = 0
    for r in rows:
        if len(r) != width:
            ragged += 1
        norm.append((list(r) + [""] * width)[:width])
    n = len(norm)

    # ---- 结构 ----
    if width == 0:
        findings.append(make_finding(
            "critical", "structure", "", "没有列", "表头为空，无法判断结构", "无法解析"))
    if n == 0:
        findings.append(make_finding(
            "critical", "structure", "", "没有数据行",
            "文件为空或只有表头", "任何下游任务都会拿到空结果，而且不会报错"))
    if width and len(set(header)) != width:
        dup = [h for h, c in Counter(header).items() if c > 1]
        findings.append(make_finding(
            "warning", "structure", "", "表头存在重复列名",
            "重复列名：%s" % "、".join(dup),
            "按列名取值时后一列会覆盖前一列"))
    if ragged:
        findings.append(make_finding(
            "warning", "structure", "", "存在列数不一致的行",
            "%d 行的列数与表头（%d 列）不同" % (ragged, width),
            "常见于字段内分隔符未转义，后续可能整行错位"))

    # ---- 主键（必须由人指定） ----
    key_idx = [header.index(k) for k in key_cols if k in header]
    if key_idx and n:
        counter = Counter(tuple(norm[i][j] for j in key_idx) for i in range(n))
        dups = {k: c for k, c in counter.items() if c > 1}
        if dups:
            examples = ["、".join(x) for x in list(dups)[:3]]
            findings.append(make_finding(
                "critical", "uniqueness", "、".join(key_cols), "主键冲突",
                "%d 组取值重复，涉及 %d 行；例如 %s" % (len(dups), sum(dups.values()), "；".join(examples)),
                "按主键关联或去重时数据会翻倍，且不会报错"))

    # ---- 完全重复的行 ----
    if n > 1:
        row_counter = Counter(tuple(r) for r in norm)
        dup_rows = {k: c for k, c in row_counter.items() if c > 1}
        if dup_rows:
            findings.append(make_finding(
                "warning", "uniqueness", "", "存在完全重复的行",
                "%d 组重复，共 %d 行" % (len(dup_rows), sum(dup_rows.values())),
                "常见于上游重复推送，或 join 后行数放大"))

    # ---- 逐列 ----
    columns_meta = []
    for j in range(width):
        values = [norm[i][j] for i in range(n)]
        meta, col_findings = analyze_column(header[j], values, null_threshold)
        columns_meta.append(meta)
        findings.extend(col_findings)

    # ---- 主键候选提示（只提示，不参与评分） ----
    # 只挑名字像 ID 或完全唯一的列；数值列直接排除（金额不是主键）。
    suggested = []
    if n > 1:
        for j in range(width):
            values = [norm[i][j] for i in range(n)]
            if any(is_null(v) for v in values):
                continue
            unique_ratio = len(set(values)) / n
            if unique_ratio < 0.9:
                continue
            if columns_meta[j]["type"] in ("float",):
                continue
            looks_like_key = re.search(r"(^|_)(id|key|no|code|uuid|guid)$", header[j].lower())
            if looks_like_key or unique_ratio == 1.0:
                suggested.append(header[j])

    # ---- 评分 ----
    counts = Counter(f["severity"] for f in findings)
    summary = {k: counts.get(k, 0) for k in ("critical", "warning", "info")}
    score = 100
    for sev, weight in WEIGHTS.items():
        score -= weight * summary[sev]
    score = max(0, min(100, score))
    if n == 0:
        score = 0

    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["category"], f["column"]))

    return {
        "file": "",
        "rows": n,
        "columns": width,
        "encoding": None,
        "delimiter": None,
        "key_cols": key_cols,
        "score": score,
        "summary": summary,
        "columns_meta": columns_meta,
        "suggested_keys": suggested,
        "findings": findings,
    }


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------

def render_text(report):
    lines = []
    sep = "─" * 74
    name = Path(report.get("file") or "").name or "(内存数据)"
    lines.append("data-lint v%s · %s" % (VERSION, name))
    lines.append(sep)
    lines.append("行 %d · 列 %d · 编码 %s · 分隔符 %r" % (
        report["rows"], report["columns"], report.get("encoding") or "-", report.get("delimiter") or "-"))

    s = report["summary"]
    mark = "✗" if s["critical"] else ("⚠" if s["warning"] else "✓")
    lines.append("")
    lines.append("健康度  %d/100  %s" % (report["score"], mark))
    lines.append("严重 %d · 警告 %d · 提示 %d" % (s["critical"], s["warning"], s["info"]))

    meta_cols = report.get("columns_meta") or []
    if meta_cols:
        lines.append("")
        lines.append("列概览")
        table = [("列名", "类型", "空值率", "去重", "样例")]
        for m in meta_cols:
            kind = m["type"] + (" ⚠" if m.get("mixed") else "")
            table.append((
                m["name"],
                kind,
                "%.1f%%" % (m["null_ratio"] * 100),
                str(m["distinct"]),
                " | ".join(m.get("samples", [])),
            ))
        widths = [max(dwidth(r[i]) for r in table) for i in range(5)]
        for r in table:
            lines.append("  " + "  ".join(pad(c, widths[i]) for i, c in enumerate(r)).rstrip())

    if report["findings"]:
        lines.append("")
        lines.append("问题清单")
        for f in report["findings"]:
            lines.append("")
            lines.append("[%s] %s · %s · %s" % (
                SEVERITY_CN[f["severity"]],
                CATEGORY_CN[f["category"]],
                f["column"] or "-",
                f["title"]))
            lines.append("       " + f["detail"])
            if f["impact"]:
                lines.append("       影响：" + f["impact"])
            for ex in f["examples"][:3]:
                lines.append("       " + ex)

    lines.append("")
    lines.append(sep)
    if report.get("suggested_keys") and not report.get("key_cols"):
        lines.append("提示：疑似可作主键的列 %s，用 --key 指定后可检查主键冲突。" %
                     "、".join(report["suggested_keys"][:5]))
    lines.append("健康分 = 100 - 15×严重 - 5×警告 - 1×提示（启发式，仅供参考）")
    return "\n".join(lines)


def render_html(report):
    name = Path(report.get("file") or "").name or "(内存数据)"
    s = report["summary"]
    rows_html = []
    for m in report.get("columns_meta") or []:
        kind = escape(m["type"] + (" ⚠" if m.get("mixed") else ""))
        rows_html.append(
            "<tr><td>%s</td><td>%s</td><td>%.1f%%</td><td>%d</td><td class='mono'>%s</td></tr>" % (
                escape(m["name"]), kind, m["null_ratio"] * 100, m["distinct"],
                escape(" | ".join(m.get("samples", [])))))

    find_html = []
    for f in report["findings"]:
        ex = "".join("<li>%s</li>" % escape(x) for x in f["examples"][:3])
        find_html.append(
            "<div class='finding %s'>"
            "<div class='head'><span class='tag'>%s</span> %s · <b>%s</b></div>"
            "<div class='detail'>%s</div>"
            "<div class='impact'>影响：%s</div>"
            "%s</div>" % (
                f["severity"], SEVERITY_CN[f["severity"]],
                escape(CATEGORY_CN[f["category"]]), escape(f["column"] or "-"),
                escape(f["detail"]), escape(f["impact"]),
                ("<ul>%s</ul>" % ex) if ex else ""))

    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>data-lint · %(name)s</title>
<style>
  body { font: 15px/1.7 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         max-width: 940px; margin: 40px auto; padding: 0 20px; color: #1f2328; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { color: #6b7280; font-size: 13px; margin-bottom: 24px; }
  .score { font-size: 44px; font-weight: 700; }
  .bar { display: flex; gap: 24px; align-items: baseline; border: 1px solid #e5e7eb;
         border-radius: 10px; padding: 16px 20px; margin-bottom: 24px; }
  .bar span { color: #6b7280; font-size: 14px; }
  table { border-collapse: collapse; width: 100%%; font-size: 13px; margin-bottom: 28px; }
  th, td { border-bottom: 1px solid #eef0f3; padding: 7px 10px; text-align: left; }
  th { background: #f9fafb; font-weight: 600; }
  .mono { font-family: Consolas, monospace; color: #4b5563; }
  .finding { border-left: 4px solid #d1d5db; background: #fafafa;
             padding: 12px 16px; margin-bottom: 10px; border-radius: 0 8px 8px 0; }
  .finding.critical { border-color: #dc2626; background: #fef2f2; }
  .finding.warning  { border-color: #d97706; background: #fffbeb; }
  .finding.info     { border-color: #6b7280; }
  .head { font-size: 14px; }
  .tag { font-size: 12px; padding: 1px 7px; border-radius: 4px; background: #e5e7eb; margin-right: 4px; }
  .critical .tag { background: #dc2626; color: #fff; }
  .warning .tag  { background: #d97706; color: #fff; }
  .detail { margin-top: 6px; }
  .impact { color: #6b7280; font-size: 13px; margin-top: 2px; }
  ul { margin: 6px 0 0; padding-left: 18px; font-size: 13px; color: #4b5563; }
  footer { color: #9ca3af; font-size: 12px; margin-top: 32px; }
</style>
</head>
<body>
<h1>数据质量体检 · %(name)s</h1>
<div class="sub">data-lint v%(version)s · 行 %(rows)d · 列 %(cols)d · 编码 %(enc)s</div>
<div class="bar">
  <div class="score">%(score)d<span style="font-size:16px;color:#9ca3af">/100</span></div>
  <div><span>严重 %(c)d · 警告 %(w)d · 提示 %(i)d</span></div>
</div>
<h2 style="font-size:15px">列概览</h2>
<table><thead><tr><th>列名</th><th>类型</th><th>空值率</th><th>去重</th><th>样例</th></tr></thead>
<tbody>%(rows_html)s</tbody></table>
<h2 style="font-size:15px">问题清单</h2>
%(find_html)s
<footer>健康分 = 100 - 15×严重 - 5×警告 - 1×提示。这是启发式评分，不是精确指标。</footer>
</body>
</html>""" % {
        "name": escape(name),
        "version": VERSION,
        "rows": report["rows"],
        "cols": report["columns"],
        "enc": escape(str(report.get("encoding") or "-")),
        "score": report["score"],
        "c": s["critical"], "w": s["warning"], "i": s["info"],
        "rows_html": "".join(rows_html),
        "find_html": "".join(find_html),
    }


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="data-lint",
        description="给一张 CSV 做数据质量体检：结构、类型、空值、唯一性、一致性、分布。")
    p.add_argument("file", help="CSV 文件路径")
    p.add_argument("--key", action="append", metavar="COL",
                   help="主键列，可多次指定或用逗号分隔。指定后才会检查主键冲突")
    p.add_argument("--null-threshold", type=float, default=0.05, metavar="F",
                   help="空值率告警阈值，默认 0.05")
    p.add_argument("--max-rows", type=int, default=None, metavar="N",
                   help="只检查前 N 行数据")
    p.add_argument("--encoding", default="auto",
                   help="文件编码，默认自动尝试 utf-8/gbk")
    p.add_argument("--delimiter", default=None,
                   help="分隔符，默认自动嗅探")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--html", metavar="FILE", help="额外输出一份 HTML 报告")
    p.add_argument("--version", action="version", version="data-lint " + VERSION)
    return p


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = build_parser().parse_args(argv)
    path = Path(args.file)
    if not path.is_file():
        print("错误：找不到文件 %s" % args.file, file=sys.stderr)
        return 2

    try:
        header, rows, meta = load_csv(path, args.encoding, args.delimiter, args.max_rows)
    except Exception as exc:  # noqa: BLE001 - CLI 边界，统一转成可读错误
        print("错误：读取失败（%s）" % exc, file=sys.stderr)
        return 2

    if header is None:
        print("错误：文件是空的，连表头都没有", file=sys.stderr)
        return 2

    keys = []
    for raw in args.key or []:
        for part in raw.split(","):
            part = part.strip()
            if part:
                keys.append(part)
    missing = [k for k in keys if k not in header]
    if missing:
        print("错误：--key 指定的列不存在：%s" % "、".join(missing), file=sys.stderr)
        print("可用列：%s" % "、".join(header), file=sys.stderr)
        return 2

    report = analyze(header, rows, keys, args.null_threshold)
    report.update({k: meta.get(k) for k in ("encoding", "delimiter")})
    report["file"] = str(path)

    if args.html:
        Path(args.html).write_text(render_html(report), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
        if args.html:
            print("HTML 报告已写入：%s" % args.html)

    return 0 if report["summary"]["critical"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
