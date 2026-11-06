"""共用工具：路徑、HTTP、日期換算、觀察名單。"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import time
from pathlib import Path
from typing import Iterable

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DATA = ROOT / "data"
DOCS = ROOT / "docs"

TPE = dt.timezone(dt.timedelta(hours=8))

UA = "Mozilla/5.0 (compatible; tw-sector-market/1.0; +https://github.com/sh-marketlab/tw-sector-market)"


def now_tpe() -> dt.datetime:
    return dt.datetime.now(TPE)


def load_watchlist() -> dict:
    return json.loads((CONFIG / "watchlist.json").read_text(encoding="utf-8"))


def load_params() -> dict:
    return json.loads((CONFIG / "params.json").read_text(encoding="utf-8"))


def watch_stocks() -> list[dict]:
    """攤平成 [{code, name, role, group_id, group_name, block}]。"""
    out = []
    for g in load_watchlist()["groups"]:
        for s in g["stocks"]:
            out.append({**s, "group_id": g["id"], "group_name": g["name"], "block": g["block"]})
    return out


def http_get(url: str, *, params=None, timeout=60, retries=3, sleep=3.0) -> requests.Response:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": UA})
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(sleep * (i + 1))
    raise RuntimeError(f"GET failed: {url} ({last})")


def decode(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    for enc in ("utf-8", "big5", "cp950"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def rows_from_json_or_csv(raw: bytes) -> list[dict]:
    """證交所 OpenAPI 有時回 JSON、有時回 CSV，兩種都吃。"""
    text = decode(raw).strip()
    if text.startswith("["):
        return json.loads(text, strict=False)
    return list(csv.DictReader(io.StringIO(text)))


def roc_to_date(s) -> dt.date | None:
    """'1151106' / '115/11/06' / '20261106' / '2026/11/06' -> date。"""
    if s is None:
        return None
    s = str(s).strip().replace("/", "").replace("-", "")
    if not s.isdigit():
        return None
    try:
        if len(s) == 8:
            return dt.date(int(s[:4]), int(s[4:6]), int(s[6:]))
        if len(s) in (6, 7):
            return dt.date(int(s[:-4]) + 1911, int(s[-4:-2]), int(s[-2:]))
    except ValueError:
        return None
    return None


def to_num(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "--", "X"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def upsert_csv(path: Path, rows: Iterable[dict], key: tuple[str, ...], fields: list[str]) -> int:
    """依 key 去重後寫回（同 key 以新資料覆蓋）。回傳新增/更新筆數。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[tuple, dict] = {}
    if path.exists():
        with path.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                existing[tuple(r[k] for k in key)] = r
    n = 0
    for r in rows:
        k = tuple(str(r[x]) for x in key)
        existing[k] = {f: r.get(f, "") for f in fields}
        n += 1
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for k in sorted(existing):
            w.writerow(existing[k])
    return n
