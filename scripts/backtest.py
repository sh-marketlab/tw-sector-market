"""回測紀錄：每次出現新訊號就記一筆，之後自動算「進場後 5 / 10 / 20 個交易日」的表現。

用法（在 GitHub Actions 內，build.py 之後）：
  python scripts/backtest.py /tmp/prev_data.json docs/data.json
  python scripts/backtest.py --rebackfill /tmp/prev_data.json docs/data.json   # 改了法人參數後重算回推紀錄

流程：
  1. 紀錄 data/signal_log.csv
     - 即時：比對上一版與這一版 data.json，「新出現」的訊號各記一筆（與 email 同一套判斷，但不寄信的也記，欄位 notified 區分）
     - 回推：第一次執行時，用 data/insti_daily.csv 的歷史把過去的「法人進駐」觸發重算出來（source=回推）
     - 同一檔、同一種訊號在冷卻期（backtest_cooldown_days 個交易日）內重複出現，只算第一次
  2. 評估：進場價＝訊號日的「次一交易日收盤」（收到 email 後最早能買到的價格，比用訊號日收盤保守），
     算之後 N 個交易日的報酬、同期加權指數報酬、超額報酬、期間最大跌幅
  3. 輸出 docs/backtest.json → docs/backtest.html
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from common import DATA, DOCS, load_params, now_tpe, watch_stocks

LOG = DATA / "signal_log.csv"
FIELDS = ["signal_date", "code", "name", "group", "label", "sub", "quiet", "notified", "source", "detail", "logged_at"]
BENCH = "^TWII"
HORIZONS = (5, 10, 20)


# ---------------------------------------------------------------- 紀錄檔
def read_log() -> list[dict]:
    if not LOG.exists():
        return []
    with LOG.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_log(rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: (r["signal_date"], r["code"], r["label"]))
    with LOG.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def tdays_between(a: str, b: str) -> int:
    """a、b 之間相隔幾個平日（近似交易日）。"""
    return abs(int(np.busday_count(min(a, b), max(a, b))))


def add_events(log: list[dict], new: list[dict], cooldown: int) -> int:
    """依日期順序加入，冷卻期內同檔同訊號只留第一次。回傳新增筆數。"""
    last: dict[tuple, str] = {}
    for r in sorted(log, key=lambda r: r["signal_date"]):
        last[(r["code"], r["label"])] = r["signal_date"]
    n = 0
    for e in sorted(new, key=lambda r: r["signal_date"]):
        k = (e["code"], e["label"])
        prev = last.get(k)
        if prev is not None and (prev == e["signal_date"] or tdays_between(prev, e["signal_date"]) <= cooldown):
            continue
        log.append(e)
        last[k] = e["signal_date"]
        n += 1
    return n


def inst_sub(trust_hit: bool, foreign_hit: bool) -> str:
    return "投信＋外資" if trust_hit and foreign_hit else "投信" if trust_hit else "外資"


# ---------------------------------------------------------------- 即時：比對兩版 data.json
def live_events(prev: dict, cur: dict) -> list[dict]:
    p = cur.get("params") or load_params()

    def labels(t: dict) -> list[str]:
        return ([t["status"]] if t.get("status") else []) + (["法人進駐"] if t.get("inst") else [])

    before = {(t["code"], lab) for t in prev.get("triggers", []) for lab in labels(t)}
    stamp = cur.get("generated_at", "")
    out = []
    for t in cur.get("triggers", []):
        for lab in labels(t):
            if (t["code"], lab) in before:
                continue
            if lab == "法人進駐":
                date = t.get("insti_date") or cur.get("price_date")
                th = (t.get("trust_5d_ratio") or 0) >= p["inst_trust_5d_pct_of_volume"] and \
                    p["inst_trust_buy_days"] <= (t.get("trust_streak") or 0) <= p["inst_trust_fresh_max_days"]
                fh = (t.get("foreign_5d_ratio") or 0) >= p["inst_foreign_5d_pct_of_volume"]
                sub, detail = inst_sub(th, fh), t.get("inst_why") or ""
            else:
                date = t.get("date") or cur.get("price_date")
                sub = t.get("lever_src") or ""
                z = t.get("w_z") if t.get("w_z") is not None else t.get("vol_z")
                detail = f"z={z:.1f}" if z is not None else ""
                if t.get("big_delta") is not None:
                    detail += f"；大戶週變化 {t['big_delta']:+.2f}pp"
            if not date:
                continue
            out.append({"signal_date": date, "code": t["code"], "name": t["name"], "group": t.get("group", ""),
                        "label": lab, "sub": sub, "quiet": int(bool(t.get("quiet"))),
                        "notified": int(lab in (t.get("notify") or [])), "source": "即時",
                        "detail": detail, "logged_at": stamp})
    return out


# ---------------------------------------------------------------- 回推：用三大法人歷史重算「法人進駐」
def backfill_inst(prices: dict[str, pd.DataFrame], p: dict) -> list[dict]:
    path = DATA / "insti_daily.csv"
    if not path.exists():
        return []
    meta = {s["code"]: s for s in watch_stocks()}
    df = pd.read_csv(path, dtype={"code": str})
    stamp = now_tpe().isoformat(timespec="minutes")
    lo, hi = p["inst_trust_buy_days"], p["inst_trust_fresh_max_days"]
    out = []
    for code, g in df.sort_values("date").groupby("code"):
        if code not in meta or code not in prices:
            continue
        g = g.reset_index(drop=True)
        trust = pd.to_numeric(g["trust_net"], errors="coerce")
        foreign = pd.to_numeric(g["foreign_net"], errors="coerce")
        av20 = prices[code]["Volume"].rolling(20).mean()
        av20.index = av20.index.strftime("%Y-%m-%d")
        streak, prev_hit = 0, False
        for i in range(len(g)):
            streak = streak + 1 if (trust.iloc[i] or 0) > 0 else 0
            d = g.at[i, "date"]
            past = av20[av20.index <= d].dropna()
            if i < hi + 1 or past.empty:  # 歷史開頭的連買天數看不全，不判斷
                prev_hit = False
                continue
            vol5 = float(past.iloc[-1]) * 5
            t5 = trust.iloc[i - 4:i + 1]
            tr = float(t5.sum()) / vol5 if t5.notna().any() else 0.0
            fr = float(foreign.iloc[i - 4:i + 1].sum()) / vol5
            th = lo <= streak <= hi and tr >= p["inst_trust_5d_pct_of_volume"]
            fh = fr >= p["inst_foreign_5d_pct_of_volume"]
            hit = th or fh
            if hit and not prev_hit:
                why = []
                if th:
                    why.append(f"投信連買{streak}天（5日買超={tr * 100:.1f}%成交量）")
                if fh:
                    why.append(f"外資5日買超={fr * 100:.0f}%成交量")
                m = meta[code]
                out.append({"signal_date": d, "code": code, "name": m["name"], "group": m["group_name"],
                            "label": "法人進駐", "sub": inst_sub(th, fh), "quiet": "", "notified": 0,
                            "source": "回推", "detail": "、".join(why), "logged_at": stamp})
            prev_hit = hit
    return out


# ---------------------------------------------------------------- 評估
def evaluate(log: list[dict], prices: dict[str, pd.DataFrame], bench: pd.Series | None) -> list[dict]:
    out = []
    for r in log:
        e = {k: r.get(k) for k in FIELDS if k != "logged_at"}
        e["quiet"] = None if r.get("quiet") in ("", None) else bool(int(r["quiet"]))
        e["notified"] = bool(int(r.get("notified") or 0))
        df = prices.get(r["code"])
        if df is None:
            e["state"] = "無股價"
            out.append(e)
            continue
        c = df["Close"]
        idx = c.index.strftime("%Y-%m-%d")
        after = np.where(idx > r["signal_date"])[0]
        if len(after) == 0:
            e["state"] = "等待進場"
            out.append(e)
            continue
        k0 = int(after[0])
        entry = float(c.iloc[k0])
        e["entry_date"], e["entry_close"] = idx[k0], round(entry, 2)
        b0 = None
        if bench is not None:
            bi = bench.index.strftime("%Y-%m-%d")
            bm = np.where(bi >= idx[k0])[0]
            b0 = int(bm[0]) if len(bm) else None
        done = 0
        for h in HORIZONS:
            k = k0 + h
            if k >= len(c):
                e[f"r{h}"] = e[f"x{h}"] = None
                continue
            ret = float(c.iloc[k] / entry - 1)
            e[f"r{h}"] = ret
            bret = None
            if b0 is not None and b0 + h < len(bench):
                bret = float(bench.iloc[b0 + h] / bench.iloc[b0] - 1)
            e[f"x{h}"] = ret - bret if bret is not None else None
            done += 1
        win = c.iloc[k0:k0 + max(HORIZONS) + 1]
        e["mdd"] = float(win.min() / entry - 1) if len(win) > 1 else None
        e["days_held"] = len(win) - 1
        e["state"] = "完成" if done == len(HORIZONS) else f"進行中（{len(win) - 1} 日）"
        out.append(e)
    return out


def summarize(ev: list[dict]) -> list[dict]:
    def stats(rows: list[dict], name: str, kind: str) -> dict:
        s = {"name": name, "kind": kind, "n": len(rows)}
        for h in HORIZONS:
            r = [x[f"r{h}"] for x in rows if x.get(f"r{h}") is not None]
            xs = [x[f"x{h}"] for x in rows if x.get(f"x{h}") is not None]
            s[f"n{h}"] = len(r)
            s[f"mean{h}"] = float(np.mean(r)) if r else None
            s[f"median{h}"] = float(np.median(r)) if r else None
            s[f"up{h}"] = float(np.mean([v > 0 for v in r])) if r else None       # 上漲比例
            s[f"xmean{h}"] = float(np.mean(xs)) if xs else None
            s[f"beat{h}"] = float(np.mean([v > 0 for v in xs])) if xs else None   # 贏大盤比例
        m = [x["mdd"] for x in rows if x.get("mdd") is not None]
        s["mdd_mean"] = float(np.mean(m)) if m else None
        return s

    out = [stats(ev, "全部訊號", "all")]
    labels = sorted({e["label"] for e in ev}, key=lambda x: ["完整訊號", "槓桿＋籌碼", "寧靜期＋槓桿", "籌碼集中",
                                                             "法人進駐", "槓桿爆量"].index(x) if x in
                    ["完整訊號", "槓桿＋籌碼", "寧靜期＋槓桿", "籌碼集中", "法人進駐", "槓桿爆量"] else 9)
    for lab in labels:
        out.append(stats([e for e in ev if e["label"] == lab], lab, "label"))
    inst = [e for e in ev if e["label"] == "法人進駐"]
    for sub in ("投信", "外資", "投信＋外資"):
        rows = [e for e in inst if e.get("sub") == sub]
        if rows:
            out.append(stats(rows, f"法人進駐｜{sub}", "sub"))
    for src in ("即時", "回推"):
        rows = [e for e in inst if e.get("source") == src]
        if rows and len(rows) != len(inst):
            out.append(stats(rows, f"法人進駐｜{src}", "sub"))
    q = [e for e in ev if e["label"] != "法人進駐" and e.get("quiet") is not None]
    if q:
        out.append(stats([e for e in q if e["quiet"]], "其他訊號｜寧靜期中", "cond"))
        out.append(stats([e for e in q if not e["quiet"]], "其他訊號｜非寧靜期", "cond"))
    return [s for s in out if s["n"]]


# ---------------------------------------------------------------- 股價
def load_all_prices(codes: list[str]) -> tuple[dict[str, pd.DataFrame], pd.Series | None, dict]:
    from build import load_prices
    import yfinance as yf

    log: dict = {}
    prices = load_prices(codes, log)
    bench = None
    try:
        b = yf.download(BENCH, period="2y", interval="1d", auto_adjust=True, progress=False)
        col = b["Close"]
        bench = (col.iloc[:, 0] if isinstance(col, pd.DataFrame) else col).dropna()
        if bench.empty:
            bench = None
    except Exception as e:  # noqa: BLE001
        log["bench_error"] = str(e)
    return prices, bench, log


# ---------------------------------------------------------------- 主程式
def run(prev: dict, cur: dict, prices: dict[str, pd.DataFrame], bench: pd.Series | None,
        rebackfill: bool = False, price_log: dict | None = None) -> dict:
    p = {**load_params(), **(cur.get("params") or {})}
    cooldown = int(p.get("backtest_cooldown_days", 10))
    log = read_log()
    notes = []
    if rebackfill:
        log = [r for r in log if r.get("source") != "回推"]
    if prices and not any(r.get("source") == "回推" for r in log):
        bf = backfill_inst(prices, p)
        notes.append(f"回推法人進駐 {add_events(log, bf, cooldown)} 筆")
    n_live = add_events(log, live_events(prev, cur), cooldown)
    notes.append(f"即時新增 {n_live} 筆")
    write_log(log)

    ev = evaluate(log, prices, bench) if prices else []
    ev.sort(key=lambda e: (e["signal_date"], e["code"]), reverse=True)
    return {
        "generated_at": now_tpe().isoformat(timespec="minutes"),
        "method": {"entry": "訊號日的次一交易日收盤", "horizons": list(HORIZONS), "bench": "加權指數 ^TWII",
                   "cooldown_days": cooldown},
        "notes": notes + ([f"股價問題：{price_log}"] if price_log and any(price_log.values()) else []),
        "has_prices": bool(prices), "has_bench": bench is not None,
        "summary": summarize(ev) if ev else [],
        "events": ev,
    }


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rebackfill = "--rebackfill" in sys.argv
    prev_p, cur_p = Path(args[0]), Path(args[1])
    prev = json.loads(prev_p.read_text(encoding="utf-8")) if prev_p.exists() else {}
    cur = json.loads(cur_p.read_text(encoding="utf-8"))
    codes = [s["code"] for s in watch_stocks()]
    try:
        prices, bench, plog = load_all_prices(codes)
    except Exception as e:  # noqa: BLE001  股價抓不到也要把紀錄存下來
        prices, bench, plog = {}, None, {"price_error": str(e)}
    out = run(prev, cur, prices, bench, rebackfill, plog)
    if not out["has_prices"] and (DOCS / "backtest.json").exists():
        old = json.loads((DOCS / "backtest.json").read_text(encoding="utf-8"))  # 保留上次的評估結果
        out["summary"], out["events"] = old.get("summary", []), old.get("events", [])
    from build import clean
    (DOCS / "backtest.json").write_text(json.dumps(clean(out), ensure_ascii=False, indent=1), encoding="utf-8")
    print("；".join(out["notes"]), f"｜紀錄 {len(read_log())} 筆，評估 {len(out['events'])} 筆")


if __name__ == "__main__":
    main()
