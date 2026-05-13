#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
更新 14 只基金净值数据，生成给静态网页读取的 JSON 文件。

v6_multi_source 核心修复：
1. 最新正式净值优先使用东方财富 JSON API：api.fund.eastmoney.com/f10/lsjz。
2. 东方财富 F10、AKShare、同花顺、天天基金、新浪、腾讯作为后端候选源；统一择优。
3. 历史净值优先使用东方财富 JSON API，F10/AKShare 备用。
4. 只把正式单位净值写入 fund_latest.json；盘中估算仍由前端“盘中估算优先”模式处理。
5. 每只基金打印候选源，便于排错；候选诊断保持简短，避免撑爆网页表格。
"""
from __future__ import annotations

import html as html_lib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

SCRIPT_VERSION = "v6_multi_source_20260513"

FUNDS = [
    {"code": "014362", "name": "睿远稳进配置两年持有混合A"},
    {"code": "001511", "name": "兴全新视野定期开放混合型发起式"},
    {"code": "007120", "name": "睿远成长价值混合C"},
    {"code": "169101", "name": "东方红睿丰混合"},
    {"code": "007119", "name": "睿远成长价值混合A"},
    {"code": "006608", "name": "泓德研究优选混合"},
    {"code": "163417", "name": "兴全合宜灵活配置混合(LOF)A"},
    {"code": "010340", "name": "易方达高质量严选三年持有混合"},
    {"code": "010273", "name": "嘉实价值长青混合A"},
    {"code": "010186", "name": "嘉实核心成长混合A"},
    {"code": "010027", "name": "景顺长城核心中景一年持有期混合"},
    {"code": "501054", "name": "东方红睿泽三年定开混合A"},
    {"code": "011006", "name": "工银圆丰三年持有期混合"},
    {"code": "501049", "name": "东方红睿玺三年定开混合A"},
]

DATA_DIR = Path("data")
LATEST_PATH = DATA_DIR / "fund_latest.json"
HISTORY_PATH = DATA_DIR / "fund_history.json"
START_DATE = "2020-10-27"
REQUEST_SLEEP = 0.18

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Referer": "https://fund.eastmoney.com/",
    "Accept": "application/json,text/javascript,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
})


def safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        s = str(x).strip().replace("%", "").replace(",", "")
        if s in ("", "--", "nan", "None", "null", "NoneType"):
            return None
        return float(s)
    except Exception:
        return None


def is_reasonable_nav(x: Any) -> bool:
    v = safe_float(x)
    return v is not None and 0.05 <= v <= 20


def clean_date(x: Any) -> str:
    m = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", str(x or ""))
    if not m:
        return ""
    y, mth, d = re.split(r"[-/]", m.group(0))
    return f"{int(y):04d}-{int(mth):02d}-{int(d):02d}"


def beijing_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=8)


def expected_trade_date() -> str:
    forced = os.environ.get("EXPECTED_TRADE_DATE", "").strip()
    if forced:
        return clean_date(forced)
    d = beijing_now().date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def load_old_docs() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    old_latest: Dict[str, Any] = {}
    old_history: Dict[str, Any] = {}
    try:
        if LATEST_PATH.exists():
            old_latest = json.loads(LATEST_PATH.read_text(encoding="utf-8")).get("items", {})
    except Exception:
        pass
    try:
        if HISTORY_PATH.exists():
            old_history = json.loads(HISTORY_PATH.read_text(encoding="utf-8")).get("items", {})
    except Exception:
        pass
    return old_latest, old_history


def normalize_history(df: pd.DataFrame, source: str) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    date_col = "净值日期" if "净值日期" in df.columns else df.columns[0]
    nav_col = "单位净值" if "单位净值" in df.columns else df.columns[1]
    pct_col = "日增长率" if "日增长率" in df.columns else None
    out: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        d = clean_date(row.get(date_col))
        nav = safe_float(row.get(nav_col))
        if not d or nav is None or nav <= 0 or d < START_DATE:
            continue
        pct = safe_float(row.get(pct_col)) if pct_col else None
        out.append({"date": d, "nav": nav, "pct": pct, "source": source})
    by_date = {x["date"]: x for x in out}
    out = list(by_date.values())
    out.sort(key=lambda x: x["date"])
    return out


def strip_jsonp(text: str) -> Any:
    s = (text or "").strip()
    # 纯 JSON
    try:
        return json.loads(s)
    except Exception:
        pass
    # callback({...}) / var apidata={...}
    m = re.search(r"^[\w$.]+\s*\((.*)\)\s*;?\s*$", s, re.S)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"var\s+\w+\s*=\s*(\{.*\})\s*;?\s*$", s, re.S)
    if m:
        return json.loads(m.group(1))
    raise RuntimeError("JSON/JSONP parse failed; head=" + re.sub(r"\s+", " ", s[:160]))


# ---------- 东方财富 JSON API：推荐主源 ----------
def fetch_eastmoney_json_rows(code: str, page_size: int = 20000, start_date: str = START_DATE, end_date: str = "") -> List[Dict[str, Any]]:
    url = "https://api.fund.eastmoney.com/f10/lsjz"
    params = {
        "fundCode": code,
        "pageIndex": "1",
        "pageSize": str(page_size),
        "startDate": start_date,
        "endDate": end_date,
        "_": str(int(time.time() * 1000)),
    }
    headers = dict(SESSION.headers)
    headers["Referer"] = f"https://fundf10.eastmoney.com/jjjz_{code}.html"
    r = SESSION.get(url, params=params, headers=headers, timeout=25)
    r.raise_for_status()
    data = strip_jsonp(r.text)
    rows_raw = (((data or {}).get("Data") or {}).get("LSJZList") or [])
    rows: List[Dict[str, Any]] = []
    for item in rows_raw:
        d = clean_date(item.get("FSRQ"))
        nav = safe_float(item.get("DWJZ"))
        pct = safe_float(item.get("JZZZL"))
        if d and nav is not None and nav > 0 and d >= START_DATE:
            rows.append({"date": d, "nav": nav, "pct": pct, "source": "东方财富JSON"})
    by_date = {x["date"]: x for x in rows}
    rows = list(by_date.values())
    rows.sort(key=lambda x: x["date"])
    return rows


def history_from_eastmoney_json(code: str) -> Tuple[List[Dict[str, Any]], str]:
    rows = fetch_eastmoney_json_rows(code, page_size=20000, start_date=START_DATE, end_date="")
    if not rows:
        raise RuntimeError("Eastmoney JSON no valid history")
    return rows, "东方财富JSON历史净值"


def latest_from_eastmoney_json(code: str) -> Tuple[Dict[str, Any], str]:
    rows = fetch_eastmoney_json_rows(code, page_size=20, start_date="", end_date="")
    if not rows:
        raise RuntimeError("Eastmoney JSON no valid latest rows")
    last = rows[-1]
    return {"nav": last["nav"], "date": last["date"], "date_time": last["date"], "daily_pct": last.get("pct"), "estimated": False}, "东方财富JSON最新净值"


# ---------- 东方财富 F10 文本/HTML：第二主源 ----------
def parse_eastmoney_f10_rows(text: str) -> List[Dict[str, Any]]:
    raw = text or ""
    m = re.search(r'content\s*:\s*"(.*?)"\s*,\s*records\s*:', raw, re.S)
    content = m.group(1) if m else raw
    content = content.replace('\\"', '"').replace("\\'", "'")
    content = content.replace('\\r', '\n').replace('\\n', '\n').replace('\\t', '\t')
    content = html_lib.unescape(content)

    if "<td" in content.lower() or "<tr" in content.lower():
        try:
            tables = pd.read_html(StringIO(f"<table>{content}</table>"))
            if tables:
                rows = normalize_history(tables[0], "东方财富F10")
                if rows:
                    return rows
        except Exception:
            pass

    rows: List[Dict[str, Any]] = []
    line_pattern = re.compile(
        r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2})\s+"
        r"([0-9]+(?:\.[0-9]+)?)\s+"
        r"([0-9]+(?:\.[0-9]+)?)?\s*"
        r"([-+]?\d+(?:\.\d+)?)%?",
        re.S,
    )
    for d, nav, _acc, pct in line_pattern.findall(content):
        d2 = clean_date(d)
        nav_v = safe_float(nav)
        pct_v = safe_float(pct)
        if d2 and nav_v is not None and nav_v > 0 and d2 >= START_DATE:
            rows.append({"date": d2, "nav": nav_v, "pct": pct_v, "source": "东方财富F10"})
    by_date = {x["date"]: x for x in rows}
    rows = list(by_date.values())
    rows.sort(key=lambda x: x["date"])
    return rows


def get_eastmoney_f10_text(code: str, per: int = 20, sdate: str = "", edate: str = "") -> str:
    url = "https://fund.eastmoney.com/f10/F10DataApi.aspx"
    params = {"type": "lsjz", "code": code, "page": "1", "per": str(per), "sdate": sdate, "edate": edate}
    r = SESSION.get(url, params=params, timeout=20)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def history_from_eastmoney_f10(code: str, per: int = 20000) -> Tuple[List[Dict[str, Any]], str]:
    text = get_eastmoney_f10_text(code, per=per, sdate=START_DATE, edate="")
    rows = parse_eastmoney_f10_rows(text)
    if not rows:
        raise RuntimeError("Eastmoney F10 no valid rows; head=" + re.sub(r"\s+", " ", text[:180]))
    return rows, "东方财富F10历史净值"


def latest_from_eastmoney_f10(code: str) -> Tuple[Dict[str, Any], str]:
    text = get_eastmoney_f10_text(code, per=20, sdate="", edate="")
    rows = parse_eastmoney_f10_rows(text)
    if not rows:
        raise RuntimeError("Eastmoney F10 latest no valid rows; head=" + re.sub(r"\s+", " ", text[:180]))
    last = rows[-1]
    return {"nav": last["nav"], "date": last["date"], "date_time": last["date"], "daily_pct": last.get("pct"), "estimated": False}, "东方财富F10最新净值"


# ---------- AKShare ----------
def history_from_akshare(code: str) -> Tuple[List[Dict[str, Any]], str]:
    import akshare as ak
    df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    rows = normalize_history(df, "AKShare")
    if not rows:
        raise RuntimeError("AKShare returned no valid history")
    return rows, "AKShare fund_open_fund_info_em"


def latest_from_akshare(code: str) -> Tuple[Dict[str, Any], str]:
    rows, src = history_from_akshare(code)
    last = rows[-1]
    return {"nav": last["nav"], "date": last["date"], "date_time": last["date"], "daily_pct": last.get("pct"), "estimated": False}, src


# ---------- 天天基金 fundgz：只取正式 dwjz/jzrq ----------
def latest_from_fundgz(code: str) -> Tuple[Dict[str, Any], str]:
    url = f"https://fundgz.1234567.com.cn/js/{code}.js?rt={int(time.time()*1000)}"
    r = SESSION.get(url, timeout=12)
    r.raise_for_status()
    m = re.search(r"jsonpgz\((.*)\)\s*;?", r.text.strip())
    if not m:
        raise RuntimeError("fundgz JSONP parse failed")
    data = json.loads(m.group(1))
    dwjz = safe_float(data.get("dwjz"))
    jzrq = clean_date(data.get("jzrq"))
    if dwjz is None or not jzrq:
        raise RuntimeError("fundgz no official dwjz/jzrq")
    return {"nav": dwjz, "date": jzrq, "date_time": jzrq, "daily_pct": safe_float(data.get("gszzl")), "estimated": False}, "天天基金fundgz确认净值"


# ---------- 同花顺爱基金：后端备用 ----------
def latest_from_10jqka(code: str) -> Tuple[Dict[str, Any], str]:
    url = f"https://fund.10jqka.com.cn/data/fund/nav/{code}.json"
    headers = dict(SESSION.headers)
    headers["Referer"] = f"https://fund.10jqka.com.cn/{code}/"
    r = SESSION.get(url, headers=headers, timeout=12)
    r.raise_for_status()
    txt = r.text.strip()
    try:
        data = json.loads(txt)
    except Exception:
        data = strip_jsonp(txt)

    # 兼容多种嵌套
    candidates = []
    def walk(obj: Any):
        if isinstance(obj, dict):
            candidates.append(obj)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
    walk(data)

    for obj in candidates:
        date = clean_date(obj.get("enddate") or obj.get("date") or obj.get("jzrq") or obj.get("净值日期"))
        nav = safe_float(obj.get("net") or obj.get("dwjz") or obj.get("unit_nav") or obj.get("单位净值"))
        pct = safe_float(obj.get("rate") or obj.get("JZZZL") or obj.get("日增长率") or obj.get("growth"))
        if date and is_reasonable_nav(nav):
            return {"nav": nav, "date": date, "date_time": date, "daily_pct": pct, "estimated": False}, "同花顺爱基金最新净值"
    raise RuntimeError("10jqka no valid nav/date")


# ---------- 新浪/腾讯：仅后端低优先级备用，解析保守 ----------
def latest_from_sina(code: str) -> Tuple[Dict[str, Any], str]:
    errors = []
    for prefix in ("of_", "fu_"):
        url = f"https://hq.sinajs.cn/list={prefix}{code}"
        headers = dict(SESSION.headers)
        headers["Referer"] = "https://finance.sina.com.cn/"
        try:
            r = SESSION.get(url, headers=headers, timeout=10)
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
                r.encoding = r.apparent_encoding or "gbk"
            m = re.search(r'="(.*)"', r.text, re.S)
            if not m:
                raise RuntimeError("empty sina var")
            parts = [p.strip() for p in m.group(1).split(",")]
            date = ""
            for p in parts:
                d = clean_date(p)
                if d:
                    date = max(date, d)
            # 常见 of_ 字段中第 3/4 位附近是净值；不确定时只取合理范围的早期数字
            nav = None
            for idx in (2, 3, 1, 4):
                if idx < len(parts) and is_reasonable_nav(parts[idx]):
                    nav = safe_float(parts[idx]); break
            pct = None
            for p in parts:
                v = safe_float(p)
                if v is not None and -30 <= v <= 30 and "%" in p:
                    pct = v; break
            if date and is_reasonable_nav(nav):
                return {"nav": nav, "date": date, "date_time": date, "daily_pct": pct, "estimated": False}, f"新浪财经{prefix}最新净值"
            raise RuntimeError("sina parsed but no valid date/nav")
        except Exception as e:
            errors.append(f"{prefix}{e}")
    raise RuntimeError("; ".join(errors))


def latest_from_tencent(code: str) -> Tuple[Dict[str, Any], str]:
    errors = []
    for prefix in ("of_", "fu_"):
        url = f"https://qt.gtimg.cn/q={prefix}{code}"
        headers = dict(SESSION.headers)
        headers["Referer"] = "https://finance.qq.com/"
        try:
            r = SESSION.get(url, headers=headers, timeout=10)
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
                r.encoding = r.apparent_encoding or "gbk"
            m = re.search(r'="(.*)"', r.text, re.S)
            if not m:
                raise RuntimeError("empty tencent var")
            parts = [p.strip() for p in m.group(1).split("~")]
            date = ""
            for p in parts:
                d = clean_date(p)
                if d:
                    date = max(date, d)
            nav = None
            # 腾讯字段不稳定，找第一个合理净值，但排除代码/日期/百分比附近噪声
            for p in parts:
                if clean_date(p) or p == code:
                    continue
                v = safe_float(p)
                if is_reasonable_nav(v):
                    nav = v; break
            pct = None
            for p in parts:
                v = safe_float(p)
                if v is not None and -30 <= v <= 30 and ("%" in p or "." in p):
                    pct = v; break
            if date and is_reasonable_nav(nav):
                return {"nav": nav, "date": date, "date_time": date, "daily_pct": pct, "estimated": False}, f"腾讯财经{prefix}最新净值"
            raise RuntimeError("tencent parsed but no valid date/nav")
        except Exception as e:
            errors.append(f"{prefix}{e}")
    raise RuntimeError("; ".join(errors))


def candidate_label(c: Dict[str, Any], src: str) -> str:
    est = "(估)" if c.get("estimated") else ""
    return f"{src}:{clean_date(c.get('date'))}:{c.get('nav')}{est}"


def pick_best_latest(candidates: List[Tuple[Dict[str, Any], str]]) -> Tuple[Dict[str, Any], str, str]:
    if not candidates:
        raise RuntimeError("no latest candidates")

    source_rank = {
        "东方财富JSON最新净值": 80,
        "东方财富F10最新净值": 70,
        "AKShare fund_open_fund_info_em": 60,
        "同花顺爱基金最新净值": 50,
        "天天基金fundgz确认净值": 40,
        "新浪财经of_最新净值": 20,
        "新浪财经fu_最新净值": 18,
        "腾讯财经of_最新净值": 15,
        "腾讯财经fu_最新净值": 13,
    }

    # 先按日期新旧，再按是否正式净值，再按来源可信度。
    def key(item: Tuple[Dict[str, Any], str]) -> Tuple[str, int, int]:
        d, src = item
        date = clean_date(d.get("date"))
        formal = 0 if d.get("estimated") else 1
        return (date, formal, source_rank.get(src, 0))

    best = max(candidates, key=key)
    diag = " | ".join(candidate_label(c, src) for c, src in candidates)
    return best[0], best[1], diag


def merge_latest_into_history(history: List[Dict[str, Any]], latest: Dict[str, Any], latest_source: str) -> List[Dict[str, Any]]:
    date = clean_date(latest.get("date"))
    nav = safe_float(latest.get("nav"))
    if not date or nav is None or nav <= 0:
        return history
    by_date = {x["date"]: dict(x) for x in history if clean_date(x.get("date"))}
    by_date[date] = {"date": date, "nav": nav, "pct": safe_float(latest.get("daily_pct")), "source": latest_source}
    out = list(by_date.values())
    out.sort(key=lambda x: x["date"])
    return out


def recompute_daily_pct_from_history(history: List[Dict[str, Any]], latest: Dict[str, Any]) -> Optional[float]:
    date = clean_date(latest.get("date"))
    nav = safe_float(latest.get("nav"))
    if not date or nav is None or nav <= 0:
        return safe_float(latest.get("daily_pct"))
    valid = [x for x in history if clean_date(x.get("date")) and safe_float(x.get("nav")) is not None]
    valid.sort(key=lambda x: clean_date(x.get("date")))
    prev = None
    for row in valid:
        d = clean_date(row.get("date"))
        if d < date:
            prev = row
        elif d >= date:
            break
    prev_nav = safe_float(prev.get("nav")) if prev else None
    if prev_nav and prev_nav > 0:
        return round((nav / prev_nav - 1) * 100, 4)
    return safe_float(latest.get("daily_pct"))


def fetch_fund(code: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str]:
    errors: List[str] = []

    history: List[Dict[str, Any]] = []
    history_source = ""
    for fn in (history_from_eastmoney_json, history_from_eastmoney_f10, history_from_akshare):
        try:
            history, history_source = fn(code)
            break
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
            time.sleep(REQUEST_SLEEP)

    latest_candidates: List[Tuple[Dict[str, Any], str]] = []
    candidate_errors: List[str] = []
    for fn in (
        latest_from_eastmoney_json,
        latest_from_eastmoney_f10,
        latest_from_akshare,
        latest_from_10jqka,
        latest_from_fundgz,
        latest_from_sina,
        latest_from_tencent,
    ):
        try:
            latest, src = fn(code)
            if safe_float(latest.get("nav")) is not None and clean_date(latest.get("date")):
                latest_candidates.append((latest, src))
        except Exception as e:
            candidate_errors.append(f"{fn.__name__}: {e}")
        time.sleep(REQUEST_SLEEP)

    if not history and latest_candidates:
        latest, src, _ = pick_best_latest(latest_candidates)
        history = [{"date": clean_date(latest["date"]), "nav": latest["nav"], "pct": latest.get("daily_pct"), "source": src}]
        history_source = src
    elif not history:
        raise RuntimeError("; ".join(errors + candidate_errors) or "no history and no latest")

    latest, latest_source, latest_diag = pick_best_latest(latest_candidates)
    history = merge_latest_into_history(history, latest, latest_source)
    recomputed_pct = recompute_daily_pct_from_history(history, latest)
    if recomputed_pct is not None:
        latest["daily_pct"] = recomputed_pct
        history = merge_latest_into_history(history, latest, latest_source)

    diag_parts = [f"script={SCRIPT_VERSION}", f"history={history_source}", f"latest_candidates={latest_diag}"]
    if errors or candidate_errors:
        short = []
        for e in (errors + candidate_errors)[:10]:
            e = re.sub(r"\s+", " ", str(e))
            if len(e) > 180:
                e = e[:180] + "..."
            short.append(e)
        diag_parts.append("errors=" + " || ".join(short))
    return history, {**latest, "source": latest_source}, "; ".join(diag_parts)


def main() -> None:
    print(f"SCRIPT_VERSION={SCRIPT_VERSION}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    old_latest, old_history = load_old_docs()
    latest_items: Dict[str, Any] = {}
    history_items: Dict[str, Any] = {}
    expected = expected_trade_date()
    print(f"Expected trade date: {expected} (Beijing weekday calendar / override by EXPECTED_TRADE_DATE)")

    for fund in FUNDS:
        code = fund["code"]
        name = fund["name"]
        print(f"Fetching {code} {name} ...")
        try:
            history, latest, diagnosis = fetch_fund(code)
            last_date = clean_date(latest.get("date")) or history[-1]["date"]
            date_ok = last_date >= expected
            latest_items[code] = {
                "code": code,
                "name": name,
                "nav": latest.get("nav"),
                "date": last_date,
                "date_time": latest.get("date_time") or last_date,
                "daily_pct": latest.get("daily_pct"),
                "source": latest.get("source") or "多源净值",
                "estimated": bool(latest.get("estimated")),
                "ok": True,
                "date_ok": date_ok,
                "expected_trade_date": expected,
                "script_version": SCRIPT_VERSION,
                "diagnosis": diagnosis + ("" if date_ok else f"; WARNING: latest date {last_date} < expected {expected}"),
            }
            history_items[code] = history
            print(f"  OK {last_date} {latest.get('nav')} via {latest.get('source')} date_ok={date_ok}")
            print("  candidates:", diagnosis.split("latest_candidates=")[-1].split("; errors=")[0])
        except Exception as e:
            print(f"  FAIL {code}: {e}")
            if code in old_latest:
                old = dict(old_latest[code])
                old_date = clean_date(old.get("date"))
                old["diagnosis"] = f"script={SCRIPT_VERSION}; 本次更新失败，沿用旧数据：{e}"
                old["date_ok"] = old_date >= expected if old_date else False
                old["expected_trade_date"] = expected
                old["script_version"] = SCRIPT_VERSION
                latest_items[code] = old
            else:
                latest_items[code] = {
                    "code": code,
                    "name": name,
                    "nav": None,
                    "date": "",
                    "daily_pct": None,
                    "source": "更新失败",
                    "ok": False,
                    "date_ok": False,
                    "expected_trade_date": expected,
                    "script_version": SCRIPT_VERSION,
                    "diagnosis": str(e),
                }
            history_items[code] = old_history.get(code, [])
        time.sleep(REQUEST_SLEEP)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stale = [c for c, v in latest_items.items() if not v.get("date_ok")]
    latest_doc = {
        "updated_at": now,
        "expected_trade_date": expected,
        "script_version": SCRIPT_VERSION,
        "stale_codes": stale,
        "items": latest_items,
    }
    history_doc = {"updated_at": now, "expected_trade_date": expected, "script_version": SCRIPT_VERSION, "items": history_items}
    LATEST_PATH.write_text(json.dumps(latest_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    HISTORY_PATH.write_text(json.dumps(history_doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {LATEST_PATH} and {HISTORY_PATH}")
    if stale:
        print("WARNING stale latest date codes:", ", ".join(stale))
    else:
        print("All latest dates are fresh.")


if __name__ == "__main__":
    main()
