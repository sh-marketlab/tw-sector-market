"""讀取累積資料＋抓股價，計算訊號，輸出 docs/data.json。

訊號邏輯（參數在 config/params.json）：
  1. 寧靜期：收盤價距 MA200 或 MA240 在 ±quiet_max_dist_to_ma 內，且 20 日波動率位於近一年低檔
  2. 槓桿先行：觀察名單標的的「認購權證成交金額」> 過去 N 日平均 + kσ
     （權證歷史未滿 N 日前，以「現股成交量 > 20 日平均 + kσ」暫代，頁面會標示「替代」）
  3. 籌碼確認：集保 ≥1000 張大戶持股比例週增 ≥ chip_min_weekly_increase_pp 個百分點
  出場警示：跌破 MA20，或大戶比例連續 exit_big_holder_down_weeks 週下降
  法人進駐：投信「剛開始」連買（inst_trust_buy_days～inst_trust_fresh_max_days 天）且 5 日買超 ≥ 5 日成交量 × inst_trust_5d_pct_of_volume，
            或外資連買 ≥ inst_foreign_buy_days 天且 5 日買超 ≥ 5 日成交量 × inst_foreign_5d_pct_of_volume
"""
from __future__ import annotations

import gzip
import io
import json
import math
import sys

import numpy as np
import pandas as pd

from common import CONFIG, DATA, DOCS, load_params, load_watchlist, now_tpe

MARKET_MAP = CONFIG / "market_map.json"


# ---------------------------------------------------------------- 股價
def load_prices(codes: list[str], log: dict) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    mm = json.loads(MARKET_MAP.read_text(encoding="utf-8")) if MARKET_MAP.exists() else {}
    out: dict[str, pd.DataFrame] = {}

    def dl(tickers: list[str]) -> dict[str, pd.DataFrame]:
        if not tickers:
            return {}
        raw = yf.download(tickers, period="2y", interval="1d", auto_adjust=True,
                          group_by="ticker", progress=False, threads=True)
        res = {}
        for t in tickers:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df[["Close", "Volume"]].dropna()
                if len(df) > 30:
                    res[t] = df
            except Exception:  # noqa: BLE001
                pass
        return res

    known = {c: mm[c] for c in codes if c in mm}
    unknown = [c for c in codes if c not in mm]
    got = dl([c + s for c, s in known.items()])
    for c, s in known.items():
        if c + s in got:
            out[c] = got[c + s]
        else:
            unknown.append(c)
    for suffix in (".TW", ".TWO"):
        if not unknown:
            break
        got = dl([c + suffix for c in unknown])
        still = []
        for c in unknown:
            if c + suffix in got:
                out[c] = got[c + suffix]
                mm[c] = suffix
            else:
                still.append(c)
        unknown = still
    MARKET_MAP.write_text(json.dumps(dict(sorted(mm.items())), indent=1), encoding="utf-8")
    log["missing_prices"] = unknown
    return out


def price_metrics(df: pd.DataFrame, p: dict) -> dict:
    c, v = df["Close"], df["Volume"]
    last = float(c.iloc[-1])

    def chg(n):
        return float(c.iloc[-1] / c.iloc[-1 - n] - 1) if len(c) > n else None

    def ma(n):
        return float(c.rolling(n).mean().iloc[-1]) if len(c) >= n else None

    ret = np.log(c).diff()
    vol20 = ret.rolling(20).std() * math.sqrt(252)
    hist = vol20.dropna().iloc[-250:]
    vol_pct = float((hist <= hist.iloc[-1]).mean()) if len(hist) > 60 else None

    v_hist = v.iloc[-21:-1]
    vol_z = float((v.iloc[-1] - v_hist.mean()) / v_hist.std()) if len(v_hist) == 20 and v_hist.std() > 0 else None

    m20, m200, m240 = ma(p["exit_below_ma"]), ma(200), ma(240)
    d200 = last / m200 - 1 if m200 else None
    d240 = last / m240 - 1 if m240 else None
    dists = [abs(x) for x in (d200, d240) if x is not None]
    near_ma = bool(dists) and min(dists) <= p["quiet_max_dist_to_ma"]
    quiet = near_ma and vol_pct is not None and vol_pct <= p["quiet_max_vol_percentile"]
    return {
        "date": df.index[-1].strftime("%Y-%m-%d"),
        "close": round(last, 2), "chg1": chg(1), "chg5": chg(5), "chg20": chg(20),
        "dist_ma200": d200, "dist_ma240": d240,
        "below_ma20": bool(m20 and last < m20),
        "vol20": float(vol20.iloc[-1]) if not math.isnan(vol20.iloc[-1]) else None,
        "vol_pct": vol_pct, "vol_z": vol_z, "near_ma": near_ma, "quiet": quiet,
        "avg_vol20": float(v.iloc[-20:].mean()) if len(v) >= 20 else None,
    }


# ---------------------------------------------------------------- 權證
def warrant_metrics(p: dict) -> tuple[dict[str, dict], int]:
    path = DATA / "warrants_daily.csv"
    if not path.exists():
        return {}, 0
    df = pd.read_csv(path, dtype={"code": str})
    days = df["date"].nunique()
    n = p["warrant_lookback_days"]
    out = {}
    for code, g in df.sort_values("date").groupby("code"):
        s = g["call_amount"].astype(float).reset_index(drop=True)
        last = float(s.iloc[-1])
        prev = s.iloc[-1 - n:-1]
        z = None
        if len(prev) >= n and prev.std() > 0:
            z = float((last - prev.mean()) / prev.std())
        out[code] = {"w_date": g["date"].iloc[-1], "w_call_amt": last,
                     "w_put_amt": float(g["put_amount"].iloc[-1]), "w_z": z, "w_days": len(s)}
    return out, days


# ---------------------------------------------------------------- 三大法人
def insti_metrics() -> tuple[dict[str, dict], int]:
    path = DATA / "insti_daily.csv"
    if not path.exists():
        return {}, 0
    df = pd.read_csv(path, dtype={"code": str})
    days = df["date"].nunique()
    out = {}
    for code, g in df.sort_values("date").groupby("code"):
        t5 = g.tail(5)
        def streak_of(col):
            n = 0
            for x in reversed(pd.to_numeric(g[col], errors="coerce").fillna(0).tolist()):
                if x > 0:
                    n += 1
                else:
                    break
            return n
        streak = streak_of("trust_net")
        tr5 = pd.to_numeric(t5["trust_net"], errors="coerce")
        out[code] = {"foreign_5d_shares": float(t5["foreign_net"].sum()),
                     "foreign_5d_lots": round(float(t5["foreign_net"].sum()) / 1000),
                     "trust_5d_lots": round(float(tr5.sum()) / 1000) if tr5.notna().any() else None,
                     "trust_5d_shares": float(tr5.sum()) if tr5.notna().any() else None,
                     "trust_streak": streak, "foreign_streak": streak_of("foreign_net"), "insti_date": g["date"].iloc[-1]}
    return out, days


# ---------------------------------------------------------------- 集保
def tdcc_metrics(codes: set[str], p: dict) -> tuple[dict[str, dict], list[str]]:
    files = sorted((DATA / "tdcc").glob("*.csv.gz"))
    recs = []
    for f in files:
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            df = pd.read_csv(io.StringIO(fh.read()), dtype=str)
        df.columns = ["date", "code", "level", "people", "shares", "pct"][: len(df.columns)]
        df["code"] = df["code"].str.strip()
        df = df[df["code"].isin(codes)].copy()
        df["level"] = pd.to_numeric(df["level"], errors="coerce")
        for col in ("people", "shares", "pct"):
            df[col] = pd.to_numeric(df[col].str.replace(",", ""), errors="coerce")
        wk = f.name[:8]
        for code, g in df.groupby("code"):
            lv = g.set_index("level")
            def s(levels, col):
                return float(lv.loc[lv.index.isin(levels), col].sum())
            recs.append({
                "week": wk, "code": code,
                "big1000": s([15], "pct"), "big400": s([12, 13, 14, 15], "pct"),
                "retail_people": s(range(1, 9), "people"),
                "total_people": float(lv.loc[17, "people"]) if 17 in lv.index else None,
            })
    weeks = [f.name[:8] for f in files]
    if not recs:
        return {}, weeks
    d = pd.DataFrame(recs).sort_values("week")
    key = "big1000" if str(p["chip_big_holder_level"]) == "1000" else "big400"
    out = {}
    for code, g in d.groupby("code"):
        g = g.reset_index(drop=True)
        cur = g.iloc[-1]
        delta = float(cur[key] - g[key].iloc[-2]) if len(g) >= 2 else None
        diffs = g[key].diff().dropna().tolist()
        up = down = 0
        for x in reversed(diffs):
            if x > 0 and down == 0:
                up += 1
            elif x < 0 and up == 0:
                down += 1
            else:
                break
        rp = None
        if len(g) >= 2 and g["retail_people"].iloc[-2] > 0:
            rp = float(cur["retail_people"] / g["retail_people"].iloc[-2] - 1)
        out[code] = {"tdcc_week": cur["week"], "big_pct": round(float(cur[key]), 2),
                     "big_delta": delta, "big_up_weeks": up, "big_down_weeks": down,
                     "retail_people_chg": rp,
                     "big_hist": [round(float(x), 2) for x in g[key].tail(26)]}
    return out, weeks


# ---------------------------------------------------------------- 組裝
def clean(x):
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, list):
        return [clean(v) for v in x]
    if isinstance(x, (np.floating,)):
        return clean(float(x))
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


def build(prices: dict[str, pd.DataFrame] | None = None) -> dict:
    p = load_params()
    wl = load_watchlist()
    codes = [s["code"] for g in wl["groups"] for s in g["stocks"]]
    log: dict = {}
    if prices is None:
        try:
            prices = load_prices(codes, log)
        except Exception as e:  # noqa: BLE001
            log["price_error"] = str(e)
            prices = {}
    wm, w_days = warrant_metrics(p)
    im, i_days = insti_metrics()
    tm, t_weeks = tdcc_metrics(set(codes), p)
    warrant_ready = w_days > p["warrant_lookback_days"]

    groups, triggers = [], []
    for g in wl["groups"]:
        rows = []
        for s in g["stocks"]:
            r = {"code": s["code"], "name": s["name"], "role": s["role"]}
            if s["code"] in prices:
                r.update(price_metrics(prices[s["code"]], p))
            r.update(wm.get(s["code"], {}))
            r.update(im.get(s["code"], {}))
            r.update(tm.get(s["code"], {}))

            wz = r.get("w_z")
            if wz is not None:
                r["lever"] = wz >= p["warrant_sigma"] and (r.get("w_call_amt") or 0) >= p["warrant_min_amount_twd"]
                r["lever_src"] = "權證"
            else:
                vz = r.get("vol_z")
                r["lever"] = vz is not None and vz >= p["stock_volume_sigma"]
                r["lever_src"] = "現股量（替代）"
            bd = r.get("big_delta")
            r["chip"] = bd is not None and bd >= p["chip_min_weekly_increase_pp"]

            # 法人進駐（比例都以「5 日成交量」= 20 日均量×5 為分母）：
            #   投信：剛開始連買（N ~ fresh_max 天）且 5 日買超 ≥ 5 日成交量 × 比例 —— 過濾 ETF 長期小額被動買
            #   外資：連買 ≥ inst_foreign_buy_days 天，且 5 日買超 ≥ 5 日成交量 × 比例
            av = r.get("avg_vol20")
            vol5 = av * 5 if av else None
            fs = r.get("foreign_5d_shares")
            ts = r.get("trust_5d_shares")
            r["foreign_5d_ratio"] = fs / vol5 if (vol5 and fs is not None) else None
            r["trust_5d_ratio"] = ts / vol5 if (vol5 and ts is not None) else None
            why = []
            stk = r.get("trust_streak") or 0
            if (p["inst_trust_buy_days"] <= stk <= p["inst_trust_fresh_max_days"]
                    and (r["trust_5d_ratio"] or 0) >= p["inst_trust_5d_pct_of_volume"]):
                why.append(f"投信連買{stk}天（5日買超={r['trust_5d_ratio'] * 100:.1f}%成交量）")
            fstk = r.get("foreign_streak") or 0
            if (r["foreign_5d_ratio"] is not None and r["foreign_5d_ratio"] >= p["inst_foreign_5d_pct_of_volume"]
                    and fstk >= p["inst_foreign_buy_days"]):
                why.append(f"外資連買{fstk}天（5日買超={r['foreign_5d_ratio'] * 100:.0f}%成交量）")
            r["inst"] = bool(why)
            r["inst_why"] = "、".join(why)

            q = bool(r.get("quiet"))
            if q and r["lever"] and r["chip"]:
                r["status"] = "完整訊號"
            elif r["lever"] and r["chip"]:
                r["status"] = "槓桿＋籌碼"
            elif q and r["lever"]:
                r["status"] = "寧靜期＋槓桿"
            elif r["chip"]:
                r["status"] = "籌碼集中"
            elif r["lever"]:
                r["status"] = "槓桿爆量"
            else:
                r["status"] = ""
            # 哪些要寄信：現股量替代的單純槓桿不寄（雜訊太多）
            proxy_only = r["lever_src"] != "權證" and r["status"] in ("寧靜期＋槓桿", "槓桿爆量")
            r["notify"] = [x for x in ([r["status"]] if r["status"] and not proxy_only else []) +
                           (["法人進駐"] if r["inst"] else [])]
            # 出場警示：大戶連續下降一律標示；跌破 MA20 只對有訊號的標的標示（避免整頁都是警示）
            r["exit"] = r.get("big_down_weeks", 0) >= p["exit_big_holder_down_weeks"] or (
                bool(r["status"] or r["inst"]) and bool(r.get("below_ma20")))
            if r["status"] or r["inst"]:
                triggers.append({**r, "group": g["name"]})
            rows.append(r)

        def avg(role):
            xs = [x.get("chg5") for x in rows if x["role"] == role and x.get("chg5") is not None]
            return sum(xs) / len(xs) if xs else None
        a5, s5 = avg("anchor"), avg("signal")
        div = None
        if a5 is not None and s5 is not None:
            if a5 > 0 and s5 < 0:
                div = "權值撐盤、中小撤退"
            elif a5 < 0 and s5 > 0:
                div = "權值走弱、中小轉強"
        groups.append({"id": g["id"], "name": g["name"], "block": g["block"], "us_group": g.get("us_group"),
                       "anchor_chg5": a5, "signal_chg5": s5, "divergence": div, "stocks": rows})

    rank = {"完整訊號": 0, "槓桿＋籌碼": 1, "寧靜期＋槓桿": 2, "籌碼集中": 3, "": 4, "槓桿爆量": 5}
    triggers.sort(key=lambda r: (rank[r["status"]] - (0.5 if r["inst"] else 0), -(r.get("w_z") or r.get("vol_z") or 0)))
    price_dates = [r.get("date") for g in groups for r in g["stocks"] if r.get("date")]
    fetch_log = json.loads((DATA / "fetch_log.json").read_text(encoding="utf-8")) if (DATA / "fetch_log.json").exists() else {}
    return clean({
        "generated_at": now_tpe().isoformat(timespec="minutes"),
        "price_date": max(price_dates) if price_dates else None,
        "params": {k: v for k, v in p.items() if not k.startswith("_")},
        "health": {"warrant_days": w_days, "warrant_ready": warrant_ready,
                   "warrant_need": p["warrant_lookback_days"] + 1,
                   "insti_days": i_days, "tdcc_weeks": t_weeks, "build_log": log, "fetch_log": fetch_log},
        "triggers": [{k: t.get(k) for k in ("code", "name", "group", "role", "status", "lever_src", "w_z",
                                            "vol_z", "big_delta", "quiet", "exit", "close", "chg5", "inst", "inst_why",
                                            "notify", "trust_streak", "foreign_streak", "foreign_5d_ratio", "trust_5d_ratio")}
                     for t in triggers],
        "groups": groups,
    })


def main() -> None:
    out = build()
    DOCS.mkdir(exist_ok=True)
    (DOCS / "data.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    h = out["health"]
    print(f"price_date={out['price_date']} warrant_days={h['warrant_days']} insti_days={h['insti_days']} "
          f"tdcc_weeks={len(h['tdcc_weeks'])} triggers={len(out['triggers'])} missing={h['build_log'].get('missing_prices')}")
    sys.exit(0)


if __name__ == "__main__":
    main()
