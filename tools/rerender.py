#!/usr/bin/env python3
"""이미 만든 보고서를 Claude 재분석 없이 다시 그린다.

  python3 tools/rerender.py reports/2026-10-16_..._2              # 다시 그리기만 (비용 0)
  python3 tools/rerender.py reports/2026-10-16_..._2 --redo s4    # 항목 4 만 Claude 에게 다시 묻기 (소액)

하는 일
  - 저널 등급을 현재 등급표(원본 + data/journal_ranks_local.json)로 다시 맞춘다 (인터넷 불필요)
  - 최근 1년 원저 발행 수를 PubMed 에서 받는다 (--offline 이면 건너뜀)
  - Figure·Table 을 다시 잘라 figures/ 에 저장
  - 해설 속 수치를 paper_text.txt 와 대조
  - report.html 을 새로 쓴다
analysis.json 의 기존 분석 결과는 지우지 않는다. 처음 고칠 때 analysis_before_v2.json 으로 한 벌 남기고,
--redo s4 로 바뀐 항목 4 의 이전 글은 analysis.json 의 sections_prev 에 남긴다.
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import analyzer  # noqa: E402
import figures as figmod  # noqa: E402
import numcheck  # noqa: E402
from render import render_report  # noqa: E402
from sources import Sources, find_rank  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--redo", choices=["s4"], help="이 항목만 Claude 에게 다시 묻는다")
    ap.add_argument("--offline", action="store_true", help="PubMed 조회도 하지 않는다")
    a = ap.parse_args()

    folder = Path(a.folder).resolve()
    reports = (HERE / "reports").resolve()
    if reports not in folder.parents:
        sys.exit("reports/ 아래 보고서 폴더만 다시 그릴 수 있습니다.")
    aj = folder / "analysis.json"
    an = json.loads(aj.read_text(encoding="utf-8"))
    backup = folder / "analysis_before_v2.json"
    if not backup.exists():
        shutil.copy2(aj, backup)
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    paper_text = (folder / "paper_text.txt").read_text(encoding="utf-8")
    notes = an.setdefault("rerun_notes", [])
    stamp = time.strftime("%Y-%m-%d %H:%M")

    # 저널 등급 다시 맞추기 + 최근 1년 원저 수
    src = Sources(cfg.get("contact_email", ""), (HERE / cfg.get("journal_ranks_path", "")).resolve(),
                  HERE / "data" / "journal_ranks_local.json")
    x = an["external"]
    j = x.setdefault("journal", {})
    names = [an["extract"].get("journal", ""), (x.get("pubmed") or {}).get("journal", ""), j.get("iso", "")]
    rank = find_rank(src.ranks, [j.get("iso", "")] + names, j.get("issns", []))
    if rank:
        j["rank"] = rank
        j["missing"] = [f for f in ("tier", "q_sjr") if not rank.get(f)]
    if not a.offline and j.get("iso"):
        j["annual_articles"] = analyzer.annual_articles(src, j["iso"])
    an["run"]["lookup_failures"] = an["run"].get("lookup_failures", []) + src.failures

    # 그림·표
    figs = figmod.capture(folder / "paper.pdf", folder / "figures")
    old = {f["key"]: f for f in an.get("figures") or []}
    for f in figs:
        f["vision"] = old.get(f["key"], {}).get("vision", False)
    an["figures"] = figs
    an["captions"] = [{"label": f["label"], "page": f["page"], "caption": f["caption"]} for f in figs]

    # 항목 4 다시 묻기
    if a.redo == "s4":
        claude = analyzer.Claude(cfg, paper_text)
        fig_txt = analyzer.figures_text(an["captions"], an["extract"].get("figures_tables") or [])
        req, sent = analyzer.s4_content(analyzer.SECTION_PROMPTS["s4"].replace("{figures}", fig_txt), figs,
                                        folder / "paper.pdf", bool(cfg.get("figure_vision", True)))
        t0 = time.time()
        text = claude.ask("s4", req)
        an.setdefault("sections_prev", {})[f"s4 ({an['created']})"] = an["sections"].get("s4", "")
        an["sections"]["s4"] = text
        an["errors"].pop("s4", None)
        for f in figs:
            f["vision"] = f["label"] in sent
        an["figure_vision"] = bool(sent)
        u, cost = claude.usage, claude.cost()
        an.setdefault("reruns", []).append({"when": stamp, "section": "s4", "model": claude.model,
                                            "effort": cfg.get("effort"), "figures_sent": sent,
                                            "usage": u, "calls": claude.call_log, "cost_usd": cost,
                                            "seconds": round(time.time() - t0, 1)})
        notes.append(f"{stamp} 항목 4 만 다시 분석 — 그림 {len(sent)}개를 Claude 에게 보여줌. "
                     f"입력 {u['input']} · 캐시쓰기 {u['cache_write']} · 캐시읽기 {u['cache_read']} · 출력 {u['output']} 토큰, 약 ${cost}")
        print(f"항목 4 다시 분석: 그림 {sent}, 토큰 {u}, 약 ${cost}")

    an["numcheck"] = numcheck.check_sections(an["sections"], paper_text)
    notes[:] = [n for n in notes if "v2 로 다시 그림" not in n]
    notes.append(f"{stamp} v2 로 다시 그림 (등급표 재대조 · 그림 잘라내기 · 수치 대조)")
    aj.write_text(json.dumps(an, ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / "report.html").write_text(render_report(an, folder), encoding="utf-8")
    for f in figs:
        print(f"  {f['label']:10s} p.{f['page']}  {f['method']}  → {f['file']}")
    nc = {k: [(f['status'], f['value']) for f in v if f['status'] != 'computed'] for k, v in an["numcheck"].items()}
    print("수치 대조:", {k: v for k, v in nc.items() if v})
    print("저널:", (j.get("rank") or {}).get("q_sjr"), j.get("annual_articles"))
    print("완료:", folder / "report.html")


if __name__ == "__main__":
    main()
