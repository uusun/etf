#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
更新 14 只基金净值数据，生成给静态网页读取的 JSON 文件。

设计原则：
1. 后端负责抓净值，网页优先读本仓库 data/*.json，避免浏览器 CORS / HTTPS 混合内容问题。
2. 每只基金同时尝试多个来源：AKShare、东方财富 F10、天天基金 fundgz。
3. 历史净值与最新净值分开处理：历史曲线可以来自 AKShare/东财；最新净值必须按日期校验，不能因为历史接口旧就沿用旧日期。
4. 如果最新日期落后于预期交易日，标记 date_ok=false，并把诊断写入 JSON，网页会提示哪些基金仍是旧净值。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

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
REQUEST_SLEEP = 0.35

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Referer": "https://fund.eastmoney.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        s = str(x).strip().replace("%", "").replace(",", "")
        if s in ("", "--", "nan", "None", "null"):
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
    """简化版：按北京时间推算最近一个周一至周五交易日；不处理法定假期。"""
    d = beijing_now().date()
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
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
        out.append({"date": d, "nav": nav, "pct": pct})
    out.sort(key=lambda x: x["date"])
    return out


def history_from_akshare(code: str) -> Tuple[List[Dict[str, Any]], str]:
    import akshare as ak
    df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    out = normalize_history(df, "AKShare")
    if not out:
        raise RuntimeError("AKShare returned no valid history")
    return out, "AKShare fund_open_fund_info_em"


def history_from_eastmoney_f10(code: str, per: int = 20000) -> Tuple[List[Dict[str, Any]], str]:
    url = f"https://fund.eastmoney.com/f10/F10DataApi.aspx?type=lsjz&code={code}&page=1&per={per}&sdate={START_DATE}&edate="
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    tables = pd.read_html(r.text)
    if not tables:
        raise RuntimeError("Eastmoney F10 no table")
    out = normalize_history(tables[0], "东方财富F10")
    if not out:
        raise RuntimeError("Eastmoney F10 no valid history")
    return out, "东方财富F10历史净值"


def latest_from_eastmoney_f10(code: str) -> Tuple[Dict[str, Any], str]:
    # 只取最近 10 条，避免大表慢；用于“最新净值补采”。
    url = f"https://fund.eastmoney.com/f10/F10DataApi.aspx?type=lsjz&code={code}&page=1&per=10&sdate=&edate="
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    tables = pd.read_html(r.text)
    if not tables:
        raise RuntimeError("Eastmoney latest no table")
    hist = normalize_history(tables[0], "东方财富F10")
    if not hist:
        raise RuntimeError("Eastmoney latest no valid rows")
    last = hist[-1]
    return {"nav": last["nav"], "date": last["date"], "daily_pct": last.get("pct"), "estimated": False}, "东方财富F10最新净值"


def latest_from_akshare(code: str) -> Tuple[Dict[str, Any], str]:
    hist, src = history_from_akshare(code)
    last = hist[-1]
    return {"nav": last["nav"], "date": last["date"], "daily_pct": last.get("pct"), "estimated": False}, src


def latest_from_fundgz(code: str) -> Tuple[Dict[str, Any], str]:
    url = f"https://fundgz.1234567.com.cn/js/{code}.js?rt={int(time.time() * 1000)}"
    r = requests.get(url, headers=HEADERS, timeout=12)
    r.raise_for_status()
    m = re.search(r"jsonpgz\((.*)\)\s*;?", r.text.strip())
    if not m:
        raise RuntimeError("fundgz JSONP parse failed")
    data = json.loads(m.group(1))
    dwjz = safe_float(data.get("dwjz"))
    gsz = safe_float(data.get("gsz"))
    gszzl = safe_float(data.get("gszzl"))
    jzrq = clean_date(data.get("jzrq"))
    gztime = str(data.get("gztime") or "")
    gzdate = clean_date(gztime)

    # 夜间如果 gsz/gztime 已经滚到当天，而 jzrq 仍旧，优先保留 gsz/gztime，标记为估算/更新值。
    if gsz is not None and gzdate and (not jzrq or gzdate >= jzrq):
        return {"nav": gsz, "date": gzdate, "date_time": gztime, "daily_pct": gszzl, "estimated": True}, "天天基金fundgz估算"
    if dwjz is not None and jzrq:
        return {"nav": dwjz, "date": jzrq, "date_time": jzrq, "daily_pct": gszzl, "estimated": False}, "天天基金fundgz确认净值"
    if gsz is not None and gzdate:
        return {"nav": gsz, "date": gzdate, "date_time": gztime, "daily_pct": gszzl, "estimated": True}, "天天基金fundgz估算"
    raise RuntimeError("fundgz no valid nav")


def pick_best_latest(candidates: List[Tuple[Dict[str, Any], str]]) -> Tuple[Dict[str, Any], str, str]:
    if not candidates:
        raise RuntimeError("no latest candidates")
    # 日期越新越优先；同日期优先正式净值，其次估算；最后按来源顺序。
    def key(item: Tuple[Dict[str, Any], str]) -> Tuple[str, int]:
        d, _src = item
        date = clean_date(d.get("date"))
        estimated = bool(d.get("estimated"))
        return (date, 0 if estimated else 1)
    best = max(candidates, key=key)
    diag = " | ".join(f"{src}:{c.get('date')}:{c.get('nav')}{'(估)' if c.get('estimated') else ''}" for c, src in candidates)
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


def fetch_fund(code: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str]:
    errors: List[str] = []
    history: List[Dict[str, Any]] = []
    history_source = ""

    for fn in (history_from_akshare, history_from_eastmoney_f10):
        try:
            history, history_source = fn(code)
            break
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
            time.sleep(REQUEST_SLEEP)

    latest_candidates: List[Tuple[Dict[str, Any], str]] = []
    for fn in (latest_from_akshare, latest_from_eastmoney_f10, latest_from_fundgz):
        try:
            latest, src = fn(code)
            if safe_float(latest.get("nav")) is not None and clean_date(latest.get("date")):
                latest_candidates.append((latest, src))
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
        time.sleep(REQUEST_SLEEP)

    if not history and latest_candidates:
        latest, src, diag = pick_best_latest(latest_candidates)
        history = [{"date": clean_date(latest["date"]), "nav": latest["nav"], "pct": latest.get("daily_pct")}]
        history_source = src
    elif not history:
        raise RuntimeError("; ".join(errors) or "no history and no latest")

    latest, latest_source, latest_diag = pick_best_latest(latest_candidates or [({"nav": history[-1]["nav"], "date": history[-1]["date"], "daily_pct": history[-1].get("pct"), "estimated": False}, history_source)])
    history = merge_latest_into_history(history, latest)
    diag = f"history={history_source}; latest_candidates={latest_diag}"
    if errors:
        diag += "; errors=" + " || ".join(errors[:6])
    return history, {**latest, "source": latest_source}, diag


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    old_latest, old_history = load_old_docs()
    latest_items: Dict[str, Any] = {}
    history_items: Dict[str, Any] = {}
    expected = expected_trade_date()

    print(f"Expected trade date: {expected} (Beijing simple weekday calendar)")
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
                "diagnosis": diagnosis + ("" if date_ok else f"; WARNING: latest date {last_date} < expected {expected}"),
            }
            history_items[code] = history
            print(f"  OK {last_date} {latest.get('nav')} via {latest.get('source')} date_ok={date_ok}")
        except Exception as e:
            print(f"  FAIL {code}: {e}")
            if code in old_latest:
                old = dict(old_latest[code])
                old_date = clean_date(old.get("date"))
                old["diagnosis"] = f"本次更新失败，沿用旧数据：{e}"
                old["date_ok"] = old_date >= expected if old_date else False
                old["expected_trade_date"] = expected
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
                    "diagnosis": str(e),
                }
            history_items[code] = old_history.get(code, [])
        time.sleep(REQUEST_SLEEP)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stale = [c for c, v in latest_items.items() if not v.get("date_ok")]
    latest_doc = {
        "updated_at": now,
        "expected_trade_date": expected,
        "stale_codes": stale,
        "items": latest_items,
    }
    history_doc = {"updated_at": now, "expected_trade_date": expected, "items": history_items}
    LATEST_PATH.write_text(json.dumps(latest_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    HISTORY_PATH.write_text(json.dumps(history_doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {LATEST_PATH} and {HISTORY_PATH}")
    if stale:
        print("WARNING stale latest date codes:", ", ".join(stale))


if __name__ == "__main__":
    main()
