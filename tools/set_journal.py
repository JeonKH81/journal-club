#!/usr/bin/env python3
"""저널 등급을 Journal Club 덧붙임 등급표(data/journal_ranks_local.json)에 기록한다.

Weekly_pubmed 의 scripts/set_journal.py 와 같은 인터페이스. 원본 등급표
(Apps/Weekly_pubmed/skill/data/journal_ranks.json)는 건드리지 않는다.
Claude Code 대화창이 Consensus 의 sjr_max 필터로 SJR 분위수를 확인한 뒤 이것으로 적는다.

  python3 tools/set_journal.py --list-pending
  python3 tools/set_journal.py --iso "Int J Cardiol Heart Vasc" --q-sjr 2 --tier low \\
      --name "International Journal of Cardiology: Heart & Vasculature" \\
      --issn 2352-9067 --probe "Consensus sjr_max probe 2026-10-16"

기록하면 data/pending_journals.json 의 같은 저널은 목록에서 빠진다.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

TIERS = ["flagship", "top", "strong", "solid", "low", "minimal", "preprint"]
DATA = Path(__file__).resolve().parents[1] / "data"
DEFAULT = DATA / "journal_ranks_local.json"
PENDING = DATA / "pending_journals.json"


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ()]", "", (s or "").lower().replace(".", "")).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-pending", action="store_true", help="등급 미확정 저널 목록을 보여주고 끝낸다")
    ap.add_argument("--iso", help="PubMed ISO abbreviation — the lookup key")
    ap.add_argument("--name", help="full journal title")
    ap.add_argument("--issn", nargs="*", default=[])
    ap.add_argument("--q-sjr", type=int, choices=[1, 2, 3, 4])
    ap.add_argument("--q-jcr", type=int, choices=[1, 2, 3, 4])
    ap.add_argument("--jif", type=float)
    ap.add_argument("--tier", choices=TIERS)
    ap.add_argument("--cat")
    ap.add_argument("--note")
    ap.add_argument("--probe", help="how the quartile was established")
    ap.add_argument("--table", default=str(DEFAULT))
    a = ap.parse_args()

    if a.list_pending:
        pend = json.loads(PENDING.read_text()) if PENDING.exists() else {"journals": []}
        if not pend["journals"]:
            print("미확정 저널 없음")
        for e in pend["journals"]:
            print(f"- {e['journal']} | iso={e.get('iso')} | ISSN {', '.join(e.get('issns', []))} "
                  f"| 빠진 값 {', '.join(e.get('missing', []))} | 논문 {len(e.get('papers', []))}편")
        return 0
    if not a.iso:
        ap.error("--iso 가 필요하다")

    path = Path(a.table)
    table = json.loads(path.read_text()) if path.exists() else {
        "_meta": {"note": "Journal Club 덧붙임 층. 원본 Weekly_pubmed 등급표보다 우선한다. tools/set_journal.py 로 기록."},
        "journals": {}}
    key = norm(a.iso)
    entry = table["journals"].get(key, {})
    if not a.tier and not entry.get("tier"):
        # 원본 표에 이미 tier 가 있는 저널(q_sjr 만 채우는 경우)은 --tier 생략 가능
        print("--tier 없이 기록 — 원본 등급표의 tier 를 그대로 씁니다 (원본에도 없으면 tier 는 미확정으로 남습니다)")
    if a.name:
        entry["name"] = a.name
    elif a.tier and not entry.get("name"):
        entry["name"] = a.iso    # 새 저널인데 정식 이름을 안 줬을 때만. q_sjr 만 채울 때는 원본 이름을 덮지 않는다
    if a.tier:
        entry["tier"] = a.tier
    if a.issn:
        entry["issns"] = sorted(set(entry.get("issns", []) + a.issn))
    for field, val in (("q_sjr", a.q_sjr), ("q_jcr", a.q_jcr), ("jif", a.jif),
                       ("cat", a.cat), ("note", a.note)):
        if val is not None:
            entry[field] = val
    if a.q_sjr is not None:
        entry["probed"] = a.probe or f"Consensus sjr_max probe {date.today():%Y-%m-%d}"

    table["journals"][key] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(table, ensure_ascii=False, indent=1))

    if PENDING.exists():
        pend = json.loads(PENDING.read_text())
        issns = {i.replace("-", "").upper() for i in entry.get("issns", [])}
        keep = [e for e in pend["journals"]
                if not (e.get("key") == key or issns & {i.replace("-", "").upper() for i in e.get("issns", [])})]
        if len(keep) != len(pend["journals"]):
            pend["journals"] = keep
            PENDING.write_text(json.dumps(pend, ensure_ascii=False, indent=1))
            print("대기 목록에서 뺐다")
    print(f"{key} -> tier={entry.get('tier')} q_sjr={entry.get('q_sjr')} q_jcr={entry.get('q_jcr')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
