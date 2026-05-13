#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
更新 14 只基金净值数据，生成给静态网页读取的 JSON 文件。

v8 设计原则：
1. 晚间正式净值只采纳“正式净值源”：东方财富 JSON / F10 / AKShare / 新浪 of_ / 天天 fundgz 的 dwjz。
2. 天天 fundgz 的 gsz/gztime 与新浪 fu_ 均归入“盘中估算/参考源”，不写入晚间正式净值 JSON。
3. 最新净值按“日期最新优先；同日期按来源可靠性排序”选择。
4. 腾讯、同花顺在本项目中稳定性差，后端正式抓取链路暂不调用，避免无意义错误噪音。
5. 表格诊断只输出短文本，避免把接口返回内容撑爆网页。
"""
from __future__ import annotations

import html
import json
import re
import time
from io import StringIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

SCRIPT_VERSION = "v10_history_guard_20260513"

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
REQUEST_SLEEP = 0.25

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
}
EASTMONEY_HEADERS = {
    **BASE_HEADERS,
    "Referer": "https://fundf10.eastmoney.com/",
    "Origin": "https://fundf10.eastmoney.com",
}
FUND_HEADERS = {
    **BASE_HEADERS,
    "Referer": "https://fund.eastmoney.com/",
}
SINA_HEADERS = {
    **BASE_HEADERS,
    "Referer": "https://finance.sina.com.cn/",
}
TENCENT_HEADERS = {
    **BASE_HEADERS,
    "Referer": "https://finance.qq.com/",
}
TONGHUASHUN_HEADERS = {
    **BASE_HEADERS,
    "Referer": "https://fund.10jqka.com.cn/",
}

# 同日期正式净值来源排序；数值越高越优先。
SOURCE_RANK = {
    "东方财富JSON最新净值": 100,
    "东方财富F10最新净值": 95,
    "AKShare fund_open_fund_info_em": 90,
    "新浪财经of_确认净值": 65,
    "天天基金fundgz确认净值": 60,
}


def short_err(e: Exception | str, limit: int = 180) -> str:
    s = str(e).replace("\n", " ").replace("\r", " ").strip()
    return s[:limit] + ("..." if len(s) > limit else "")


def safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        s = str(x).strip().replace("%", "").replace(",", "")
        if s in ("", "--", "nan", "NaN", "None", "null"):
            return None
        return float(s)
    except Exception:
        return None


def clean_date(x: Any) -> str:
    m = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", str(x or ""))
    if not m:
        return ""
    y, mth, d = re.split(r"[-/]", m.group(0))
    return f"{int(y):04d}-{int(mth):02d}-{int(d):02d}"


def beijing_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=8)


def expected_trade_date() -> str:
    # 简化版：按北京时间推算最近一个周一至周五交易日；不处理法定假期。
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


def parse_jsonish(text: str) -> Dict[str, Any]:
    t = text.strip().lstrip("\ufeff")
    if t.startswith("{"):
        return json.loads(t)
    # JSONP: callback({...})
    m = re.search(r"\((\{.*\})\)\s*;?\s*$", t, re.S)
    if m:
        return json.loads(m.group(1))
    # 兜底提取第一个 JSON 对象
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        return json.loads(t[start:end + 1])
    raise RuntimeError("JSON parse failed")


def normalize_history_rows(rows: List[Dict[str, Any]], source: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        d = clean_date(row.get("FSRQ") or row.get("净值日期") or row.get("date"))
        nav = safe_float(row.get("DWJZ") or row.get("单位净值") or row.get("nav"))
        pct = safe_float(row.get("JZZZL") or row.get("日增长率") or row.get("pct"))
        if not d or nav is None or nav <= 0 or d < START_DATE:
            continue
        out.append({"date": d, "nav": nav, "pct": pct})
    out.sort(key=lambda x: x["date"])
    return out


def normalize_history_df(df: pd.DataFrame) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    date_col = "净值日期" if "净值日期" in df.columns else df.columns[0]
    nav_col = "单位净值" if "单位净值" in df.columns else df.columns[1]
    pct_col = "日增长率" if "日增长率" in df.columns else None
    rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        rows.append({
            "净值日期": row.get(date_col),
            "单位净值": row.get(nav_col),
            "日增长率": row.get(pct_col) if pct_col else None,
        })
    return normalize_history_rows(rows, "df")


def eastmoney_json_list(code: str, page_size: int = 20000, start_date: str = START_DATE, end_date: str = "") -> List[Dict[str, Any]]:
    url = "https://api.fund.eastmoney.com/f10/lsjz"
    params = {
        "fundCode": code,
        "pageIndex": "1",
        "pageSize": str(page_size),
        "startDate": start_date,
        "endDate": end_date,
        "_": str(int(time.time() * 1000)),
    }
    r = requests.get(url, headers=EASTMONEY_HEADERS, params=params, timeout=25)
    r.raise_for_status()
    data = parse_jsonish(r.text)
    items = (((data or {}).get("Data") or {}).get("LSJZList") or [])
    if not isinstance(items, list) or not items:
        raise RuntimeError("Eastmoney JSON no LSJZList")
    return items


def history_from_eastmoney_json(code: str) -> Tuple[List[Dict[str, Any]], str]:
    rows = eastmoney_json_list(code, page_size=20000, start_date=START_DATE, end_date="")
    out = normalize_history_rows(rows, "东方财富JSON")
    if not out:
        raise RuntimeError("Eastmoney JSON no valid history")
    return out, "东方财富JSON历史净值"


def latest_from_eastmoney_json(code: str) -> Tuple[Dict[str, Any], str]:
    rows = eastmoney_json_list(code, page_size=10, start_date="", end_date="")
    hist = normalize_history_rows(rows, "东方财富JSON")
    if not hist:
        raise RuntimeError("Eastmoney JSON no valid latest")
    last = hist[-1]
    return {"nav": last["nav"], "date": last["date"], "daily_pct": last.get("pct"), "estimated": False, "official": True}, "东方财富JSON最新净值"


def extract_f10_content(text: str) -> str:
    # F10DataApi.aspx 常见格式：var apidata={ content:"...",records:...,pages:...}
    m = re.search(r"content:\s*\"((?:\\.|[^\"\\])*)\"\s*,\s*records", text, re.S)
    if not m:
        # 有时不是转义字符串，尝试 content:"...",pages
        m = re.search(r"content:\s*\"(.*?)\"\s*,\s*(?:records|pages|curpage)", text, re.S)
    if not m:
        raise RuntimeError("F10 content not found")
    raw = m.group(1)
    try:
        raw = json.loads("\"" + raw + "\"")
    except Exception:
        raw = raw.replace(r"\/", "/").replace(r"\"", "\"")
    return html.unescape(raw)


def parse_f10_content(content: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if "<table" in content.lower():
        tables = pd.read_html(StringIO(content))
        if tables:
            return normalize_history_df(tables[0])
    for line in content.splitlines():
        parts = [p.strip() for p in re.split(r"\t+|\s{2,}", line.strip()) if p.strip()]
        if len(parts) < 2:
            continue
        d = clean_date(parts[0])
        if not d:
            continue
        nav = safe_float(parts[1])
        pct = safe_float(parts[3]) if len(parts) >= 4 else None
        if nav is not None and nav > 0:
            rows.append({"date": d, "nav": nav, "pct": pct})
    rows.sort(key=lambda x: x["date"])
    return [r for r in rows if r["date"] >= START_DATE]


def f10_rows(code: str, per: int = 20000, sdate: str = START_DATE, edate: str = "") -> List[Dict[str, Any]]:
    url = "https://fund.eastmoney.com/f10/F10DataApi.aspx"
    params = {"type": "lsjz", "code": code, "page": "1", "per": str(per), "sdate": sdate, "edate": edate}
    r = requests.get(url, headers=FUND_HEADERS, params=params, timeout=25)
    r.raise_for_status()
    content = extract_f10_content(r.text)
    rows = parse_f10_content(content)
    if not rows:
        # 少数情况下也可能返回完整 HTML table
        tables = pd.read_html(StringIO(r.text))
        if tables:
            rows = normalize_history_df(tables[0])
    if not rows:
        raise RuntimeError("F10 no valid rows")
    return rows


def history_from_eastmoney_f10(code: str) -> Tuple[List[Dict[str, Any]], str]:
    out = f10_rows(code, per=20000, sdate=START_DATE, edate="")
    return out, "东方财富F10历史净值"


def latest_from_eastmoney_f10(code: str) -> Tuple[Dict[str, Any], str]:
    hist = f10_rows(code, per=10, sdate="", edate="")
    last = hist[-1]
    return {"nav": last["nav"], "date": last["date"], "daily_pct": last.get("pct"), "estimated": False, "official": True}, "东方财富F10最新净值"


def history_from_akshare(code: str) -> Tuple[List[Dict[str, Any]], str]:
    import akshare as ak
    df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    out = normalize_history_df(df)
    if not out:
        raise RuntimeError("AKShare returned no valid history")
    return out, "AKShare fund_open_fund_info_em"


def latest_from_akshare(code: str) -> Tuple[Dict[str, Any], str]:
    hist, src = history_from_akshare(code)
    last = hist[-1]
    return {"nav": last["nav"], "date": last["date"], "daily_pct": last.get("pct"), "estimated": False, "official": True}, src


def latest_from_fundgz_confirm(code: str) -> Tuple[Dict[str, Any], str]:
    # 注意：fundgz 的 dwjz/jzrq 是正式净值，但经常比东财慢一天；gsz/gztime 是估算，不参与晚间正式净值 JSON。
    url = f"https://fundgz.1234567.com.cn/js/{code}.js?rt={int(time.time() * 1000)}"
    r = requests.get(url, headers=FUND_HEADERS, timeout=12)
    r.raise_for_status()
    m = re.search(r"jsonpgz\((.*)\)\s*;?", r.text.strip())
    if not m:
        raise RuntimeError("fundgz JSONP parse failed")
    data = json.loads(m.group(1))
    dwjz = safe_float(data.get("dwjz"))
    jzrq = clean_date(data.get("jzrq"))
    if dwjz is not None and jzrq:
        return {"nav": dwjz, "date": jzrq, "date_time": jzrq, "daily_pct": None, "estimated": False, "official": True}, "天天基金fundgz确认净值"
    raise RuntimeError("fundgz no confirmed dwjz/jzrq")


def latest_from_10jqka(code: str) -> Tuple[Dict[str, Any], str]:
    # 文档中的接口偶尔返回空或 JSONP；作为正式候选，但优先级低。
    url = f"https://fund.10jqka.com.cn/data/fund/nav/{code}.json"
    r = requests.get(url, headers=TONGHUASHUN_HEADERS, timeout=12)
    r.raise_for_status()
    txt = r.text.strip().lstrip("\ufeff")
    data = parse_jsonish(txt)
    nav = safe_float(data.get("net") or data.get("dwjz") or data.get("单位净值"))
    d = clean_date(data.get("enddate") or data.get("date") or data.get("jzrq") or data.get("净值日期"))
    pct = safe_float(data.get("rate") or data.get("JZZZL") or data.get("日增长率"))
    if nav is None or not d:
        raise RuntimeError("10jqka no valid nav/date")
    return {"nav": nav, "date": d, "daily_pct": pct, "estimated": False, "official": True}, "同花顺爱基金最新净值"


def _sina_payload(code: str, prefix: str, timeout: int = 10) -> str:
    url = f"https://hq.sinajs.cn/list={prefix}{code}"
    r = requests.get(url, headers=SINA_HEADERS, timeout=timeout)
    r.raise_for_status()
    # 新浪常见编码为 GBK；requests 有时无法自动识别。
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = "gbk"
    txt = r.text.strip()
    m = re.search(r'="(.*)";?', txt)
    if not m:
        raise RuntimeError(f"sina {prefix} no quoted payload")
    return m.group(1)


def latest_from_sina_official(code: str) -> Tuple[Dict[str, Any], str]:
    # 新浪 of_：场外基金确认净值。字段通常为：名称,日期,单位净值,累计净值,日增长率,申购状态,赎回状态
    payload = _sina_payload(code, "of_", timeout=10)
    parts = [x.strip() for x in payload.split(",")]
    if len(parts) < 4:
        raise RuntimeError("sina of_ payload too short")
    d = clean_date(parts[1])
    nav = safe_float(parts[2])
    pct = safe_float(parts[4]) if len(parts) > 4 else None
    if nav is None or nav <= 0 or not d:
        raise RuntimeError(f"sina of_ no valid official nav/date; head={payload[:80]}")
    return {"nav": nav, "date": d, "daily_pct": pct, "estimated": False, "official": True}, "新浪财经of_确认净值"


def latest_from_fundgz_estimate_reference(code: str) -> Tuple[Dict[str, Any], str]:
    # 天天 gsz/gztime：盘中估算，仅作参考，不写入晚间正式净值。
    url = f"https://fundgz.1234567.com.cn/js/{code}.js?rt={int(time.time() * 1000)}"
    r = requests.get(url, headers=FUND_HEADERS, timeout=12)
    r.raise_for_status()
    m = re.search(r"jsonpgz\((.*)\)\s*;?", r.text.strip())
    if not m:
        raise RuntimeError("fundgz estimate JSONP parse failed")
    data = json.loads(m.group(1))
    gsz = safe_float(data.get("gsz"))
    gzdate = clean_date(data.get("gztime"))
    pct = safe_float(data.get("gszzl"))
    if gsz is not None and gsz > 0 and gzdate:
        return {"nav": gsz, "date": gzdate, "date_time": data.get("gztime"), "daily_pct": pct, "estimated": True, "official": False, "reference_only": True}, "天天基金fundgz估算参考"
    raise RuntimeError("fundgz no valid estimate gsz/gztime")


def latest_from_sina_estimate_reference(code: str) -> Tuple[Dict[str, Any], str]:
    # 新浪 fu_：通常是盘中/收盘估算，不作为正式净值。
    payload = _sina_payload(code, "fu_", timeout=8)
    parts = [x.strip() for x in payload.split(",")]
    date = ""
    for x in parts:
        date = clean_date(x)
        if date:
            break
    # fu_ 字段不稳定，取第一个合理净值作为参考值。
    navs = [safe_float(x) for x in parts]
    navs = [x for x in navs if x is not None and 0 < x < 100]
    if navs and date:
        return {"nav": navs[0], "date": date, "estimated": True, "official": False, "reference_only": True}, "新浪财经fu_估算参考"
    raise RuntimeError("sina fu_ no valid estimate nav/date")

def pick_best_official(candidates: List[Tuple[Dict[str, Any], str]]) -> Tuple[Dict[str, Any], str, str]:
    official = [(c, src) for c, src in candidates if c.get("official", True) and not c.get("reference_only")]
    if not official:
        raise RuntimeError("no official latest candidates")

    def key(item: Tuple[Dict[str, Any], str]) -> Tuple[str, int, int]:
        c, src = item
        d = clean_date(c.get("date"))
        official_rank = 1 if not c.get("estimated") else 0
        return (d, official_rank, SOURCE_RANK.get(src, 1))

    best = max(official, key=key)
    diag = " | ".join(
        f"{src}:{clean_date(c.get('date'))}:{c.get('nav')}{'(估)' if c.get('estimated') else ''}"
        for c, src in candidates
        if not c.get("reference_only")
    )
    return best[0], best[1], diag


def merge_latest_into_history(history: List[Dict[str, Any]], latest: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not latest or safe_float(latest.get("nav")) is None:
        return history
    date = clean_date(latest.get("date"))
    if not date:
        return history
    nav = safe_float(latest.get("nav"))
    pct = safe_float(latest.get("daily_pct"))
    by_date = {x["date"]: dict(x) for x in history if x.get("date")}
    by_date[date] = {"date": date, "nav": nav, "pct": pct}
    out = list(by_date.values())
    out.sort(key=lambda x: x["date"])
    return out



def merge_history_series(*series_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按日期合并多条历史序列，避免某次接口只返回最近一段而覆盖完整历史。

    后面的序列会覆盖同日期旧值；最终按日期升序返回。
    """
    by_date: Dict[str, Dict[str, Any]] = {}
    for series in series_list:
        if not isinstance(series, list):
            continue
        for row in series:
            if not isinstance(row, dict):
                continue
            d = clean_date(row.get("date"))
            nav = safe_float(row.get("nav"))
            if not d or nav is None or nav <= 0 or d < START_DATE:
                continue
            by_date[d] = {"date": d, "nav": nav, "pct": safe_float(row.get("pct"))}
    out = list(by_date.values())
    out.sort(key=lambda x: x["date"])
    return out

def is_history_suspiciously_short(history: List[Dict[str, Any]]) -> bool:
    if not history:
        return True
    # 这些基金至少都有数百个交易日历史。若只剩几十条，基本是接口/缓存问题，不应覆盖旧完整历史。
    first = clean_date(history[0].get("date"))
    return len(history) < 120 or (first and first > "2024-01-01")

def fetch_history(code: str, errors: List[str]) -> Tuple[List[Dict[str, Any]], str]:
    for fn in (history_from_eastmoney_json, history_from_eastmoney_f10, history_from_akshare):
        try:
            hist, src = fn(code)
            return hist, src
        except Exception as e:
            errors.append(f"{fn.__name__}: {short_err(e)}")
            time.sleep(REQUEST_SLEEP)
    raise RuntimeError("; ".join(errors[-3:]) or "no history")


def fetch_latest_candidates(code: str, errors: List[str]) -> Tuple[List[Tuple[Dict[str, Any], str]], List[Tuple[Dict[str, Any], str]]]:
    official_candidates: List[Tuple[Dict[str, Any], str]] = []
    reference_candidates: List[Tuple[Dict[str, Any], str]] = []

    # 正式净值候选。
    # 同花顺、腾讯在本项目实测噪音较大，暂不进入主链路；新浪只用 of_ 确认净值，fu_ 只作参考。
    for fn in (latest_from_eastmoney_json, latest_from_eastmoney_f10, latest_from_akshare, latest_from_sina_official, latest_from_fundgz_confirm):
        try:
            latest, src = fn(code)
            if safe_float(latest.get("nav")) is not None and clean_date(latest.get("date")):
                official_candidates.append((latest, src))
        except Exception as e:
            errors.append(f"{fn.__name__}: {short_err(e)}")
        time.sleep(REQUEST_SLEEP)

    # 参考估算候选：不参与最终正式净值择优，只用于诊断和白天估值对照。
    for fn in (latest_from_fundgz_estimate_reference, latest_from_sina_estimate_reference):
        try:
            latest, src = fn(code)
            if safe_float(latest.get("nav")) is not None and clean_date(latest.get("date")):
                reference_candidates.append((latest, src))
        except Exception as e:
            errors.append(f"{fn.__name__}: {short_err(e)}")
        time.sleep(REQUEST_SLEEP)

    return official_candidates, reference_candidates

def fetch_fund(code: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str]:
    errors: List[str] = []
    history, history_source = fetch_history(code, errors)
    official_candidates, reference_candidates = fetch_latest_candidates(code, errors)

    if not official_candidates and history:
        last = history[-1]
        official_candidates = [({"nav": last["nav"], "date": last["date"], "daily_pct": last.get("pct"), "estimated": False, "official": True}, history_source)]

    latest, latest_source, latest_diag = pick_best_official(official_candidates)
    history = merge_latest_into_history(history, latest)

    reference_diag = " | ".join(
        f"{src}:{clean_date(c.get('date'))}:{c.get('nav')}(参考)" for c, src in reference_candidates
    )
    diag = f"script={SCRIPT_VERSION}; history={history_source}; official_candidates={latest_diag}"
    if reference_diag:
        diag += f"; reference_only={reference_diag}"
    # 成功取到正式净值时，不再把失败备用源的完整错误写进网页 JSON，避免表格被 HTML/乱码撑爆。
    # 备用源错误只保留在 Actions 日志中排查；最终净值已由 official_candidates 决定。
    return history, {**latest, "source": latest_source}, diag


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    old_latest, old_history = load_old_docs()
    latest_items: Dict[str, Any] = {}
    history_items: Dict[str, Any] = {}
    expected = expected_trade_date()

    print(f"SCRIPT_VERSION={SCRIPT_VERSION}")
    print(f"Expected trade date: {expected} (Beijing simple weekday calendar)")
    for fund in FUNDS:
        code = fund["code"]
        name = fund["name"]
        print(f"Fetching {code} {name} ...")
        try:
            history, latest, diagnosis = fetch_fund(code)
            old_hist = old_history.get(code, []) if isinstance(old_history, dict) else []
            fetched_len = len(history)
            old_len = len(old_hist) if isinstance(old_hist, list) else 0
            # 防止 GitHub Actions 某次接口只抓到最近一小段历史，覆盖掉完整曲线。
            # 只要旧历史更长，就合并保留；如果新历史异常短，也在诊断里明确提示。
            if old_len:
                merged = merge_history_series(old_hist, history)
                if len(merged) > len(history):
                    diagnosis += f"; history_guard=merged_old_history old_len={old_len} fetched_len={fetched_len} merged_len={len(merged)}"
                history = merged
            if is_history_suspiciously_short(history):
                diagnosis += f"; WARNING: history seems short len={len(history)} first={history[0]['date'] if history else '--'}"
            last_date = clean_date(latest.get("date")) or history[-1]["date"]
            date_ok = last_date >= expected
            latest_items[code] = {
                "code": code,
                "name": name,
                "nav": latest.get("nav"),
                "date": last_date,
                "date_time": latest.get("date_time") or last_date,
                "daily_pct": latest.get("daily_pct"),
                "source": latest.get("source") or "多源正式净值",
                "estimated": bool(latest.get("estimated")),
                "official": True,
                "ok": True,
                "date_ok": date_ok,
                "expected_trade_date": expected,
                "script_version": SCRIPT_VERSION,
                "diagnosis": diagnosis + ("" if date_ok else f"; WARNING: latest date {last_date} < expected {expected}"),
            }
            history_items[code] = history
            print(f"  OK {last_date} {latest.get('nav')} via {latest.get('source')} date_ok={date_ok} history_len={len(history)} first={history[0]['date'] if history else '--'}")
            # 单独打印候选，方便排查。
            m = re.search(r"official_candidates=([^;]+)", diagnosis)
            if m:
                print(f"  official candidates: {m.group(1)}")
            m2 = re.search(r"reference_only=([^;]+)", diagnosis)
            if m2:
                print(f"  reference only: {m2.group(1)}")
        except Exception as e:
            print(f"  FAIL {code}: {short_err(e)}")
            if code in old_latest:
                old = dict(old_latest[code])
                old_date = clean_date(old.get("date"))
                old["diagnosis"] = f"script={SCRIPT_VERSION}; 本次更新失败，沿用旧数据：{short_err(e)}"
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
                    "diagnosis": short_err(e),
                }
            history_items[code] = old_history.get(code, [])
        time.sleep(REQUEST_SLEEP)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stale = [c for c, v in latest_items.items() if not v.get("date_ok")]
    latest_doc = {
        "script_version": SCRIPT_VERSION,
        "updated_at": now,
        "expected_trade_date": expected,
        "stale_codes": stale,
        "items": latest_items,
    }
    history_doc = {"script_version": SCRIPT_VERSION, "updated_at": now, "expected_trade_date": expected, "items": history_items}
    LATEST_PATH.write_text(json.dumps(latest_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    HISTORY_PATH.write_text(json.dumps(history_doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {LATEST_PATH} and {HISTORY_PATH}")
    if stale:
        print("WARNING stale latest date codes:", ", ".join(stale))


if __name__ == "__main__":
    main()
