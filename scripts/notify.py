"""比對上一版與這一版的觸發清單，有「新出現」的訊號就開一則 GitHub Issue（GitHub 會寄 email）。

用法（在 GitHub Actions 內）：
  python scripts/notify.py /tmp/prev_data.json docs/data.json
需要環境變數 GITHUB_TOKEN、GITHUB_REPOSITORY；本機沒有 token 時只印出內容。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

PAGE = "https://sh-marketlab.github.io/tw-sector-market/"


def keys(d: dict) -> set[tuple[str, str]]:
    out = set()
    for t in d.get("triggers", []):
        for label in t.get("notify") or []:
            out.add((t["code"], label))
    return out


def fmt_pct(x):
    return "–" if x is None else f"{x * 100:+.1f}%"


def main() -> None:
    prev_p, cur_p = Path(sys.argv[1]), Path(sys.argv[2])
    prev = json.loads(prev_p.read_text(encoding="utf-8")) if prev_p.exists() else {}
    cur = json.loads(cur_p.read_text(encoding="utf-8"))
    first = not any("notify" in t for t in prev.get("triggers", []))  # 上一版還沒有通知功能 → 這次是基準
    new = keys(cur) - keys(prev)
    if not new:
        print("沒有新訊號，不通知")
        return

    by_code = {t["code"]: t for t in cur["triggers"]}
    order = ["完整訊號", "槓桿＋籌碼", "寧靜期＋槓桿", "籌碼集中", "法人進駐", "槓桿爆量"]
    rows = sorted(new, key=lambda k: (order.index(k[1]) if k[1] in order else 9, k[0]))
    lines = ["| 訊號 | 個股 | 板塊 | 依據 | 5日 | 出場警示 |", "|---|---|---|---|---|---|"]
    for code, label in rows:
        t = by_code[code]
        if label == "法人進駐":
            basis = t.get("inst_why") or ""
        else:
            z = t.get("w_z") if t.get("w_z") is not None else t.get("vol_z")
            basis = f"{t.get('lever_src')} z={z:.1f}" if z is not None else ""
            if t.get("big_delta") is not None:
                basis += f"；大戶週變化 {t['big_delta']:+.2f}pp"
        lines.append(f"| **{label}** | {code} {t['name']} | {t['group']} | {basis} | {fmt_pct(t.get('chg5'))} | "
                     f"{'⚠' if t.get('exit') else ''} |")

    date = cur.get("price_date") or cur.get("generated_at", "")[:10]
    if first:
        title = f"台股訊號通知已啟用（{date} 基準：目前 {len(rows)} 則訊號）"
        lines.insert(0, "這是啟用通知後的第一封，列出**目前所有**訊號當作基準；之後只寄新出現的。\n")
    else:
        title = f"台股訊號 {date}：{len(rows)} 則新觸發（{', '.join(sorted({k[1] for k in rows}, key=lambda x: order.index(x) if x in order else 9))}）"
    body = "\n".join([
        f"資料日 {date}，以下是**這次新出現**的訊號（上一次已出現的不重複通知）。",
        "",
        *lines,
        "",
        f"完整頁面：{PAGE}",
        "",
        "> 研究工具，非買賣建議。權證爆量可能是避險、千張大戶包含外資保管與公司派，請搭配流動性儀表板判讀。",
    ])
    print(title)
    print(body)

    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("（沒有 GITHUB_TOKEN，略過開 Issue）")
        return
    owner = repo.split("/")[0]
    url = f"https://api.github.com/repos/{repo}/issues"
    hdr = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    payload = {"title": title, "body": body, "assignees": [owner], "labels": ["signal"]}
    r = requests.post(url, headers=hdr, json=payload, timeout=30)
    if r.status_code == 422:  # 標籤或指派失敗時，退回最簡單的 Issue
        payload.pop("labels")
        r = requests.post(url, headers=hdr, json=payload, timeout=30)
    print("issue:", r.status_code, r.json().get("html_url") or r.text[:300])


if __name__ == "__main__":
    main()
