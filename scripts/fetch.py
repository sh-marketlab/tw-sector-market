"""抓取並累積原始資料（每次執行都是冪等的：同一天/同一週重抓只會覆蓋）。

用法：
  python scripts/fetch.py warrants          # 權證當日成交 → data/warrants_daily.csv
  python scripts/fetch.py insti [--days N]  # 三大法人（可回補 N 個日曆日）→ data/insti_daily.csv
  python scripts/fetch.py tdcc              # 集保股權分散表（全市場本週）→ data/tdcc/YYYYMMDD.csv.gz
  python scripts/fetch.py all
每個來源失敗只記錄在 data/fetch_log.json，不會讓整個流程失敗。
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import sys
import time
import traceback

from common import (DATA, decode, http_get, now_tpe, roc_to_date, rows_from_json_or_csv,
                    to_num, upsert_csv, watch_stocks)

# ---------------------------------------------------------------- 權證
WARRANT_MASTER = {
    "TWSE": "https://openapi.twse.com.tw/v1/opendata/t187ap37_L",
    "TPEx": "https://mopsfin.twse.com.tw/opendata/t187ap37_O.csv",
}
WARRANT_TRADE = {
    "TWSE": "https://openapi.twse.com.tw/v1/opendata/t187ap42_L",
    "TPEx": "https://mopsfin.twse.com.tw/opendata/t187ap42_O.csv",
}


def _get(row: dict, *cands: str):
    for c in cands:
        if c in row and row[c] not in (None, ""):
            return row[c]
    # 欄位名稱偶有空白差異
    norm = {k.replace(" ", "").strip(): v for k, v in row.items() if k}
    for c in cands:
        if c in norm and norm[c] not in (None, ""):
            return norm[c]
    return None


def _wcode(v) -> str:
    if isinstance(v, (int, float)):
        return str(int(v))
    return str(v or "").strip()


def _match_underlying(text: str, by_name: dict, by_code: dict) -> str | None:
    t = str(text or "").strip()
    if not t:
        return None
    if t in by_name:
        return by_name[t]
    # 只接受「代號開頭」或「名稱完全相同」，避免 南亞科→南亞、台塑化→台塑 這種誤配
    head = t[:4]
    if head in by_code and (len(t) == 4 or not t[4].isdigit()):
        return head
    t2 = t.replace(" ", "").replace("　", "").replace("*", "")
    for name, code in by_name.items():
        if t2 == name.replace(" ", ""):
            return code
    return None


def fetch_warrants(log: dict) -> None:
    stocks = watch_stocks()
    by_name = {s["name"]: s["code"] for s in stocks}
    by_code = {s["code"]: s for s in stocks}

    master: dict[str, tuple[str, str]] = {}  # 權證代號 -> (標的代號, 認購/認售)
    for mkt, url in WARRANT_MASTER.items():
        try:
            rows = rows_from_json_or_csv(http_get(url, timeout=240).content)
            n = 0
            for r in rows:
                code = _wcode(_get(r, "權證代號"))
                und = _match_underlying(_get(r, "標的證券/指數", "標的證券", "標的代號"), by_name, by_code)
                if code and und:
                    kind = str(_get(r, "權證類型", "認購售別", "認購/售") or "")
                    master[code] = (und, "put" if "售" in kind else "call")
                    n += 1
            log[f"warrant_master_{mkt}"] = f"ok: {len(rows)} 檔權證，{n} 檔對應到觀察名單"
        except Exception as e:  # noqa: BLE001
            log[f"warrant_master_{mkt}"] = f"error: {e}"

    agg: dict[tuple[str, str], dict] = {}
    for mkt, url in WARRANT_TRADE.items():
        try:
            rows = rows_from_json_or_csv(http_get(url, timeout=120).content)
            dates = set()
            n = 0
            for r in rows:
                code = _wcode(_get(r, "權證代號"))
                if code not in master:
                    continue
                d = roc_to_date(_get(r, "交易日期", "出表日期"))
                if not d:
                    continue
                dates.add(d)
                und, kind = master[code]
                amt = to_num(_get(r, "成交金額")) or 0.0
                k = (d.isoformat(), und)
                a = agg.setdefault(k, {"date": d.isoformat(), "code": und,
                                       "call_amount": 0.0, "put_amount": 0.0, "n_traded": 0})
                a[f"{kind}_amount"] += amt
                if amt > 0:
                    a["n_traded"] += 1
                n += 1
            log[f"warrant_trade_{mkt}"] = f"ok: 交易日 {sorted(x.isoformat() for x in dates)}，{n} 筆對應"
        except Exception as e:  # noqa: BLE001
            log[f"warrant_trade_{mkt}"] = f"error: {e}"

    if agg:
        # 沒有任何權證成交的觀察名單股票也補 0，讓歷史序列完整
        for d in {k[0] for k in agg}:
            covered = {v for v, _ in master.values()}
            for code in covered:
                agg.setdefault((d, code), {"date": d, "code": code, "call_amount": 0.0,
                                           "put_amount": 0.0, "n_traded": 0})
        upsert_csv(DATA / "warrants_daily.csv", agg.values(), ("date", "code"),
                   ["date", "code", "call_amount", "put_amount", "n_traded"])


# ---------------------------------------------------------------- 三大法人
T86 = "https://www.twse.com.tw/rwd/zh/fund/T86"
TPEX_INSTI = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"


def _field_idx(fields: list[str], *must: str, exclude: tuple[str, ...] = ()) -> int | None:
    for i, f in enumerate(fields):
        f2 = str(f).replace(" ", "")
        if all(m in f2 for m in must) and not any(x in f2 for x in exclude):
            return i
    return None


# 櫃買中心新版表格欄位只寫「買進股數/賣出股數/買賣超股數」，法人別在上一層表頭，
# 固定順序：代號,名稱, 外資(不含自營)[2-4], 外資自營商[5-7], 外資合計[8-10],
# 投信[11-13], 自營商自行[14-16], 自營商避險[17-19], 自營商合計[20-22], 三大法人合計[23]
TPEX_POS = {"code": 0, "foreign": 4, "trust": 13, "total": 23, "n": 24}


def _parse_insti_table(fields, data, codes, d) -> list[dict]:
    ic = _field_idx(fields, "代號")
    ifo = _field_idx(fields, "外", "買賣超", exclude=("自營商-", "外資自營商買"))
    itr = _field_idx(fields, "投信", "買賣超")
    itot = _field_idx(fields, "三大法人", "買賣超")
    if ifo is None and len(fields) == TPEX_POS["n"] and str(fields[TPEX_POS["foreign"]]).endswith("買賣超股數"):
        ic, ifo, itr, itot = TPEX_POS["code"], TPEX_POS["foreign"], TPEX_POS["trust"], TPEX_POS["total"]
    if ic is None or ifo is None:
        raise ValueError(f"找不到欄位：{fields[:6]}…（共 {len(fields)} 欄）")
    out = []
    for row in data:
        code = str(row[ic]).strip()
        if code not in codes:
            continue
        out.append({
            "date": d.isoformat(), "code": code,
            "foreign_net": int(to_num(row[ifo]) or 0),
            "trust_net": int(to_num(row[itr]) or 0) if itr is not None else "",
            "total_net": int(to_num(row[itot]) or 0) if itot is not None else "",
        })
    return out


def _insti_twse(d: dt.date, codes: set) -> list[dict]:
    j = http_get(T86, params={"date": d.strftime("%Y%m%d"), "selectType": "ALLBUT0999",
                              "response": "json"}).json()
    if j.get("stat") != "OK" or not j.get("data"):
        return []
    return _parse_insti_table(j["fields"], j["data"], codes, d)


def _insti_tpex(d: dt.date, codes: set) -> list[dict]:
    j = http_get(TPEX_INSTI, params={"type": "Daily", "sect": "EW", "date": d.strftime("%Y/%m/%d"),
                                     "response": "json"}).json()
    tables = j.get("tables") or []
    if not tables or not tables[0].get("data"):
        return []
    t = tables[0]
    return _parse_insti_table(t["fields"], t["data"], codes, d)


def fetch_insti(log: dict, days: int = 5) -> None:
    """依市場分別判斷缺哪幾天：上市或上櫃任一邊沒資料就只補那一邊。"""
    import csv
    import json as _json
    from common import CONFIG

    codes = {s["code"] for s in watch_stocks()}
    mm_path = CONFIG / "market_map.json"
    mm = _json.loads(mm_path.read_text(encoding="utf-8")) if mm_path.exists() else {}
    otc = {c for c in codes if mm.get(c) == ".TWO"}
    listed = codes - otc
    path = DATA / "insti_daily.csv"
    have: dict[str, set] = {}
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                have.setdefault(r["date"], set()).add(r["code"])
    today = now_tpe().date()
    rows, msgs, errs = [], [], []
    for i in range(days):
        d = today - dt.timedelta(days=i)
        if d.weekday() >= 5:
            continue
        got_codes = have.get(d.isoformat(), set())
        jobs = []
        if not (got_codes & listed):
            jobs.append(("TWSE", _insti_twse))
        if otc and not (got_codes & otc):
            jobs.append(("TPEx", _insti_tpex))
        got = []
        for name, fn in jobs:
            try:
                got += fn(d, codes)
            except Exception as e:  # noqa: BLE001
                errs.append(f"{d} {name}: {str(e)[:120]}")
            time.sleep(3)  # 證交所有頻率限制
        if got:
            rows += got
            msgs.append(f"{d}+{len(got)}")
    if rows:
        upsert_csv(path, rows, ("date", "code"), ["date", "code", "foreign_net", "trust_net", "total_net"])
    log["insti"] = (f"新增 {len(msgs)} 天：{', '.join(msgs[:5])}{'…' if len(msgs) > 5 else ''}" if msgs
                    else "no new trading days")
    log["insti_errors"] = errs[:5] + ([f"…另 {len(errs) - 5} 筆"] if len(errs) > 5 else [])


# ---------------------------------------------------------------- 集保
TDCC = "https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5"


def fetch_tdcc(log: dict) -> None:
    try:
        text = decode(http_get(TDCC, timeout=180).content)
        first = text.splitlines()[1] if len(text.splitlines()) > 1 else ""
        d = roc_to_date(first.split(",")[0])
        if not d:
            raise ValueError(f"無法解析資料日期：{first[:60]}")
        out = DATA / "tdcc" / f"{d.strftime('%Y%m%d')}.csv.gz"
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            log["tdcc"] = f"{d} 已存在，略過"
            return
        with gzip.open(out, "wt", encoding="utf-8") as f:
            f.write(text)
        log["tdcc"] = f"ok: 存下 {d}（{len(text.splitlines())} 行）"
    except Exception as e:  # noqa: BLE001
        log["tdcc"] = f"error: {e}"


# ----------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["warrants", "insti", "tdcc", "all"])
    ap.add_argument("--days", type=int, default=5, help="三大法人回補的日曆日數")
    a = ap.parse_args()

    log_path = DATA / "fetch_log.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {}
    log["last_run"] = now_tpe().isoformat(timespec="minutes")
    try:
        if a.what in ("warrants", "all"):
            fetch_warrants(log)
        if a.what in ("insti", "all"):
            fetch_insti(log, a.days)
        if a.what in ("tdcc", "all"):
            fetch_tdcc(log)
    except Exception:  # noqa: BLE001
        log["fatal"] = traceback.format_exc()[-800:]
    DATA.mkdir(exist_ok=True)
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(log, ensure_ascii=False, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
