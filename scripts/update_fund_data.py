#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
更新 14 只基金净值数据，生成给静态网页读取的 JSON 文件。
优先使用 AKShare；失败时回退东方财富 F10 历史净值；再失败回退天天基金 fundgz。
这个脚本在 GitHub Actions 里运行，不在浏览器里运行，所以不受 CORS / HTTPS 混合内容限制。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import pandas as pd

FUNDS = [
  {
    "code": "014362",
    "name": "睿远稳进配置两年持有混合A"
  },
  {
    "code": "001511",
    "name": "兴全新视野定期开放混合型发起式"
  },
  {
    "code": "007120",
    "name": "睿远成长价值混合C"
  },
  {
    "code": "169101",
    "name": "东方红睿丰混合"
  },
  {
    "code": "007119",
    "name": "睿远成长价值混合A"
  },
  {
    "code": "006608",
    "name": "泓德研究优选混合"
  },
  {
    "code": "163417",
    "name": "兴全合宜灵活配置混合(LOF)A"
  },
  {
    "code": "010340",
    "name": "易方达高质量严选三年持有混合"
  },
  {
    "code": "010273",
    "name": "嘉实价值长青混合A"
  },
  {
    "code": "010186",
    "name": "嘉实核心成长混合A"
  },
  {
    "code": "010027",
    "name": "景顺长城核心中景一年持有期混合"
  },
  {
    "code": "501054",
    "name": "东方红睿泽三年定开混合A"
  },
  {
    "code": "011006",
    "name": "工银圆丰三年持有期混合"
  },
  {
    "code": "501049",
    "name": "东方红睿玺三年定开混合A"
  }
]
DATA_DIR = Path("data")
LATEST_PATH = DATA_DIR / "fund_latest.json"
HISTORY_PATH = DATA_DIR / "fund_history.json"
START_DATE = "2020-10-27"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Referer": "https://fund.eastmoney.com/",
}


def safe_float(x: Any) -> Optional[float]:
    try:
        if x is None or str(x).strip() in ("", "--", "nan", "None"):
            return None
        return float(str(x).replace("%", "").replace(",", "").strip())
    except Exception:
        return None


def clean_date(x: Any) -> str:
    m = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", str(x or ""))
    if not m:
        return ""
    parts = re.split(r"[-/]", m.group(0))
    return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"


def history_from_akshare(code: str) -> Tuple[List[Dict[str, Any]], str]:
    import akshare as ak
    df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    if df is None or df.empty:
        raise RuntimeError("AKShare returned empty dataframe")
    # 兼容不同版本列名
    date_col = "净值日期" if "净值日期" in df.columns else df.columns[0]
    nav_col = "单位净值" if "单位净值" in df.columns else df.columns[1]
    pct_col = "日增长率" if "日增长率" in df.columns else None
    out = []
    for _, row in df.iterrows():
        d = clean_date(row.get(date_col))
        nav = safe_float(row.get(nav_col))
        if not d or nav is None or nav <= 0:
            continue
        if d < START_DATE:
            continue
        pct = safe_float(row.get(pct_col)) if pct_col else None
        out.append({"date": d, "nav": nav, "pct": pct})
    if not out:
        raise RuntimeError("AKShare no valid rows")
    out.sort(key=lambda x: x["date"])
    return out, "AKShare fund_open_fund_info_em"


def history_from_eastmoney_f10(code: str) -> Tuple[List[Dict[str, Any]], str]:
    url = f"https://fund.eastmoney.com/f10/F10DataApi.aspx?type=lsjz&code={code}&page=1&per=20000&sdate={START_DATE}&edate="
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    text = r.text
    tables = pd.read_html(text)
    if not tables:
        raise RuntimeError("Eastmoney F10 no table")
    df = tables[0]
    date_col = "净值日期" if "净值日期" in df.columns else df.columns[0]
    nav_col = "单位净值" if "单位净值" in df.columns else df.columns[1]
    pct_col = "日增长率" if "日增长率" in df.columns else None
    out = []
    for _, row in df.iterrows():
        d = clean_date(row.get(date_col))
        nav = safe_float(row.get(nav_col))
        if not d or nav is None or nav <= 0:
            continue
        pct = safe_float(row.get(pct_col)) if pct_col else None
        out.append({"date": d, "nav": nav, "pct": pct})
    if not out:
        raise RuntimeError("Eastmoney F10 no valid rows")
    out.sort(key=lambda x: x["date"])
    return out, "东方财富F10历史净值"


def latest_from_fundgz(code: str) -> Tuple[Dict[str, Any], str]:
    url = f"https://fundgz.1234567.com.cn/js/{code}.js"
    r = requests.get(url, headers=HEADERS, timeout=12)
    r.raise_for_status()
    m = re.search(r"jsonpgz\((.*)\)\s*;?", r.text.strip())
    if not m:
        raise RuntimeError("fundgz JSONP parse failed")
    data = json.loads(m.group(1))
    # 优先正式净值 dwjz + jzrq；如果估算日期更新，则保留估算 gsz/gztime
    dwjz = safe_float(data.get("dwjz"))
    gsz = safe_float(data.get("gsz"))
    gszzl = safe_float(data.get("gszzl"))
    jzrq = clean_date(data.get("jzrq"))
    gztime = str(data.get("gztime") or "")
    gzdate = clean_date(gztime)
    if gsz is not None and gzdate and (not jzrq or gzdate >= jzrq):
        return {"nav": gsz, "date": gztime or gzdate, "daily_pct": gszzl}, "天天基金fundgz估算"
    if dwjz is not None:
        return {"nav": dwjz, "date": jzrq or gzdate, "daily_pct": gszzl}, "天天基金fundgz确认净值"
    raise RuntimeError("fundgz no valid nav")


def get_history(code: str) -> Tuple[List[Dict[str, Any]], str, str]:
    errors = []
    for fn in (history_from_akshare, history_from_eastmoney_f10):
        try:
            history, src = fn(code)
            return history, src, ""
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
            time.sleep(0.5)
    # 最后只拿 fundgz 一条，至少保证最新净值可用
    try:
        latest, src = latest_from_fundgz(code)
        return [{"date": clean_date(latest["date"]), "nav": latest["nav"], "pct": latest.get("daily_pct")}], src, "; ".join(errors)
    except Exception as e:
        errors.append(f"latest_from_fundgz: {e}")
        raise RuntimeError("; ".join(errors))


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    latest_items: Dict[str, Any] = {}
    history_items: Dict[str, Any] = {}
    for fund in FUNDS:
        code = fund["code"]
        name = fund["name"]
        print(f"Fetching {code} {name} ...")
        try:
            history, source, diagnosis = get_history(code)
            last = history[-1]
            latest_items[code] = {
                "code": code,
                "name": name,
                "nav": last["nav"],
                "date": last["date"],
                "daily_pct": last.get("pct"),
                "source": source,
                "ok": True,
                "diagnosis": diagnosis,
            }
            history_items[code] = history
            print(f"  OK {last['date']} {last['nav']} via {source}")
        except Exception as e:
            print(f"  FAIL {code}: {e}")
            # 保留旧数据，避免一次失败把网页打坏
            old_latest = {}
            old_history = {}
            if LATEST_PATH.exists():
                old_latest = json.loads(LATEST_PATH.read_text(encoding="utf-8")).get("items", {})
            if HISTORY_PATH.exists():
                old_history = json.loads(HISTORY_PATH.read_text(encoding="utf-8")).get("items", {})
            if code in old_latest:
                latest_items[code] = old_latest[code]
                latest_items[code]["diagnosis"] = f"本次更新失败，沿用旧数据：{e}"
            else:
                latest_items[code] = {"code": code, "name": name, "nav": None, "date": "", "daily_pct": None, "source": "更新失败", "ok": False, "diagnosis": str(e)}
            if code in old_history:
                history_items[code] = old_history[code]
            else:
                history_items[code] = []
        time.sleep(0.8)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    latest_doc = {"updated_at": now, "items": latest_items}
    history_doc = {"updated_at": now, "items": history_items}
    LATEST_PATH.write_text(json.dumps(latest_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    HISTORY_PATH.write_text(json.dumps(history_doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {LATEST_PATH} and {HISTORY_PATH}")


if __name__ == "__main__":
    main()
