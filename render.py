"""analysis.json → report.html (한 파일, 외부 자료 없이 열림, 다크/라이트 자동, 인쇄 가능).

밖에서 온 글자(PDF·Claude 출력·외부 조회)는 전부 html.escape(따옴표 포함)를 거친 뒤에만 HTML 에 들어간다.
잘라낸 그림은 보고서 안에 그대로 심는다(data:) — report.html 한 파일만 내려받아도 그림이 보인다.
"""
import base64
import html
import json
import re
from pathlib import Path

import numcheck
from analyzer import SECTIONS

TIER_KO = {"flagship": "flagship — NEJM·Lancet·JAMA 급", "top": "top — 분야 대표 저널", "strong": "strong — 탄탄한 전문 저널",
           "solid": "solid — 중견 저널", "low": "low — 심사 강도 낮음", "minimal": "minimal — 심사 최소",
           "preprint": "preprint — 심사 전 원고"}

e = html.escape


# ====================================================================== 간단한 Markdown → HTML
def _inline(s):
    s = e(s, quote=True)          # 따옴표까지 바꿔야 링크 주소가 속성 밖으로 못 나간다
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    s = re.sub(r"\[(p\.\s?\d+(?:\s*[–-]\s*\d+)?(?:\s*,\s*p\.\s?\d+(?:\s*[–-]\s*\d+)?)*)\]", r'<span class="pg">\1</span>', s)
    s = s.replace("⟦⚠ 원문 미확인⟧", '<span class="flag" title="이 숫자가 원문(paper_text.txt)에 없습니다">⚠ 원문 미확인</span>')
    s = s.replace("⟦⚠ 맥락 확인⟧", '<span class="flag" title="원문에 있는 숫자를 어림 표현으로 썼습니다 — 다른 자리의 숫자를 끌어왔을 수 있습니다">⚠ 맥락 확인</span>')
    s = s.replace("[그림 판독]", '<span class="seen">그림을 직접 읽음</span>')
    return s


def md(text):
    lines = (text or "").replace("\r", "").split("\n")
    out, i = [], 0
    para = []

    def flush():
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    while i < len(lines):
        ln = lines[i]
        st = ln.strip()
        if not st:
            flush(); i += 1; continue
        m = re.match(r"^(#{1,6})\s+(.*)", st)
        if m:
            flush()
            lvl = max(3, min(len(m.group(1)), 5))
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>"); i += 1; continue
        if st.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            flush()
            head = [c.strip() for c in st.strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            t = "<div class='tw'><table><thead><tr>" + "".join(f"<th>{_inline(h)}</th>" for h in head) + "</tr></thead><tbody>"
            for r in rows:
                t += "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
            out.append(t + "</tbody></table></div>")
            continue
        if st.startswith(">"):
            flush()
            q = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                q.append(lines[i].strip()[1:].strip()); i += 1
            out.append("<blockquote>" + _inline(" ".join(q)) + "</blockquote>")
            continue
        if re.match(r"^([-*+]|\d+[.)])\s+", st):
            flush()
            html_, i = _list(lines, i)
            out.append(html_)
            continue
        if re.match(r"^(-{3,}|\*{3,})$", st):
            flush(); out.append("<hr>"); i += 1; continue
        para.append(st); i += 1
    flush()
    return "\n".join(out)


def _indent(s):
    return len(s) - len(s.lstrip(" "))


def _list(lines, i):
    base = _indent(lines[i])
    ordered = bool(re.match(r"^\s*\d+[.)]\s", lines[i]))
    tag = "ol" if ordered else "ul"
    items = []
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            # 빈 줄 뒤에 같은 목록이 이어지면 계속
            if i + 1 < len(lines) and re.match(r"^\s*([-*+]|\d+[.)])\s+", lines[i + 1]) and _indent(lines[i + 1]) >= base:
                i += 1; continue
            break
        ind = _indent(ln)
        m = re.match(r"^\s*([-*+]|\d+[.)])\s+(.*)", ln)
        if m and ind == base:
            items.append([_inline(m.group(2)), ""]); i += 1
        elif m and ind > base and items:
            sub, i = _list(lines, i)
            items[-1][1] += sub
        elif ind > base and items:
            items[-1][0] += " " + _inline(ln.strip()); i += 1
        else:
            break
    return f"<{tag}>" + "".join(f"<li>{a}{b}</li>" for a, b in items) + f"</{tag}>", i


# ====================================================================== 조각
def _link_pmid(p):
    return f'<a href="https://pubmed.ncbi.nlm.nih.gov/{e(p)}/" target="_blank" rel="noopener">PMID {e(p)}</a>' if p else ""


def _link_doi(d):
    d = (d or "").replace("https://doi.org/", "")
    return f'<a href="https://doi.org/{e(d)}" target="_blank" rel="noopener">{e(d)}</a>' if d else ""


def _v(x, unit=""):
    return "—" if x in (None, "", []) else f"{e(str(x))}{unit}"


def _sjr_value(rank):
    q = rank.get("q_sjr")
    if not q:
        return "<span class='warn'>미확정</span>"
    m = re.search(r"\d{4}-\d{2}-\d{2}", rank.get("probed", "") or "")
    how = "Consensus 확인" if "consensus" in (rank.get("probed", "") or "").lower() else "등급표"
    return f"SJR Q{e(str(q))}" + (f" · {how}({m.group(0)})" if m else (f" · {how}" if rank.get("probed") else ""))


def _top_pct(v):
    """OpenAlex 백분위(0~1) → '상위 0.04%'. 0.99963 을 '100.0' 으로 반올림하지 않는다."""
    try:
        top = max(0.0, (1 - float(v)) * 100)
    except (TypeError, ValueError):
        return ""
    txt = f"{top:.2f}" if top < 1 else f"{top:.1f}"
    return f"상위 {txt}% (피인용 순)"


# ====================================================================== 그림·표 (항목 4)
def _img_src(f, folder):
    """report.html 에 그대로 심을 그림 주소. 파일이 없으면 상대 경로."""
    rel = f.get("file", "")
    if folder:
        pth = Path(folder) / rel
        if pth.is_file() and pth.resolve().is_relative_to(Path(folder).resolve()):
            return "data:image/png;base64," + base64.b64encode(pth.read_bytes()).decode("ascii")
    return rel


def _fig_key(text):
    m = re.match(r"^\s*(Supplementary |Extended Data )?(Figure|FIGURE|Fig\.?|Table|TABLE)\s*(S?\d+[A-Za-z]?)?", text or "")
    if not m:
        return None
    kind = "table" if m.group(2).lower().startswith("tab") else "figure"
    pre = (m.group(1) or "").lower().replace(" ", "")
    return f"{pre}{kind}{(m.group(3) or '').lower()}"


def _figure_html(f, folder):
    src = _img_src(f, folder)
    badge = "<span class='seen'>그림을 직접 읽음</span> " if f.get("vision") else ""
    fb = "<span class='warn'>자동 잘라내기 실패 — 쪽 전체</span> " if f.get("fallback") else ""
    return (f"<figure class='cap'><button class='zoom' type='button' onclick='jcZoom(this)' aria-label='{e(f['label'])} 크게 보기'>"
            f"<img src='{e(src)}' alt='{e(f['label'])}'></button>"
            f"<figcaption>{fb}{badge}<strong>{e(f['label'])}</strong> <span class='pg'>p.{e(str(f['page']))}</span> "
            f"{e(f.get('caption', '')[:500])}</figcaption></figure>")


def _fold(title_html, body_html, open_=True):
    """소제목 하나 = 접었다 펼 수 있는 덩어리 (기본 펼침). 인쇄할 때는 전부 펼친다."""
    return (f"<details class='sub'{' open' if open_ else ''}><summary><h3>{title_html}</h3>"
            f"<span class='chev' aria-hidden='true'></span></summary><div class='subbody'>{body_html}</div></details>")


def _fig_chunks(lines, figs, used, folder):
    """#### 소제목 단위로 자르고, Figure/Table 소제목 옆에 잘라낸 그림을 나란히 붙인다."""
    by_key = {f["key"]: f for f in figs}
    tables = [f for f in figs if "table" in f["key"]]
    chunks, cur = [], []
    for ln in lines:
        if ln.startswith("#### "):
            chunks.append(cur)
            cur = []
        cur.append(ln)
    chunks.append(cur)
    out = ""
    for ch in chunks:
        if not ch:
            continue
        head = ch[0][5:].strip() if ch[0].startswith("#### ") else ""
        k = _fig_key(head) if head else None
        f = by_key.get(k) if k else None
        if f is None and k and k.startswith("table") and len(tables) == 1:
            f = tables[0]            # "Table 1" 해설 ↔ 번호 없는 "Table:" 처럼 한 개뿐이면 짝짓는다
        body = md("\n".join(ch))
        if f is not None and f["key"] not in used:
            used.add(f["key"])
            out += f"<div class='figpair'><div class='figtext'>{body}</div>{_figure_html(f, folder)}</div>"
        else:
            out += body
    return out


def foldable(text, key, a, folder):
    """### 소제목마다 접기 덩어리로. 항목 4 는 그 안에서 Figure·Table 과 해설을 나란히."""
    figs = (a.get("figures") or []) if key == "s4" else []
    used = set()
    intro, parts, cur = [], [], None
    for ln in (text or "").split("\n"):
        if ln.startswith("### "):
            cur = [ln[4:].strip(), []]
            parts.append(cur)
        elif cur is None:
            intro.append(ln)
        else:
            cur[1].append(ln)

    def body(lines):
        return _fig_chunks(lines, figs, used, folder) if key == "s4" else md("\n".join(lines))

    out = body(intro) if any(x.strip() for x in intro) else ""
    for head, lines in parts:
        out += _fold(_inline(head), body(lines))
    if key == "s4":
        rest = [f for f in figs if f["key"] not in used]
        if rest:
            out += _fold("해설과 짝짓지 못한 그림·표", "".join(_figure_html(f, folder) for f in rest))
        if not figs:
            out += "<p class='muted'>잘라낸 그림·표가 없습니다 (캡션을 찾지 못함).</p>"
    return out


# ====================================================================== 개요 화면의 핵심 한 줄
# 새로 묻지 않고, 이미 쓴 해설에서 뽑는다. 소제목 이름은 analyzer 의 항목별 요청문이 정한 것.
KEY_SPEC = {
    "s2": {"head": r"연구 질문"},
    "s3": {"head": r"연구 설계", "table": True},
    "s4": {"head": r"핵심 결과", "must": r"\b(HR|OR|RR|IRR)\b|hazard ratio|차이", "label": True},
    "s5": {"head": r"논증 구조", "after": r"약한 고리", "prefix": "약한 고리 — "},
    "s6": {"head": r"핵심 메시지|Take-home"},
    "s7": {"head": r"적지 않은|비판"},
}


def _plain(s):
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1", s)
    s = re.sub(r"\[p\.\s*\d[^\]]*\]", "", s)
    s = re.sub(r"\[\d+(?:\s*[,–-]\s*\d+)*\]", "", s)
    s = re.sub(r"⟦[^⟧]*⟧", "", s).replace("[그림 판독]", "")
    s = s.replace("**", "").replace("`", "")
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"\s+([.,;:)])", r"\1", s)


def _short(s, n=150):
    if len(s) > n:
        m = re.search(r"(다|요|음)\.\s|\.\s", s[40:])
        if m and 40 + m.end() <= n:
            return s[:40 + m.end()].strip()
        return s[:n].rstrip() + "…"
    return s


def _key_line(text, spec):
    lines = (text or "").split("\n")
    start = 0
    for i, ln in enumerate(lines):
        if ln.startswith("### ") and re.search(spec["head"], ln):
            start = i + 1
            break
    label, armed = "", "after" not in spec
    for ln in lines[start:]:
        st = ln.strip()
        if st.startswith("### "):
            break
        if not st or st.startswith("#") or re.match(r"^\|?\s*:?-{2,}", st):
            continue
        if not armed:
            armed = bool(re.search(spec["after"], st))
            continue
        if st.startswith("|"):
            if not spec.get("table"):
                continue
            cells = [c.strip() for c in st.strip("|").split("|")]
            if len(cells) < 2 or cells[0] in ("항목", "구분", ""):
                continue
            return _short(_plain(cells[1]))
        m = re.match(r"^\*\*(.+?)\*\*(.*)$", st)
        if m and not re.match(r"^([-*+]|\d+[.)])\s", st):     # 굵은 글씨로 시작하는 줄 = 소소제목
            label = _plain(re.sub(r"\(.*?\)", "", m.group(1)))
            if not m.group(2).strip() or m.group(2).strip().startswith("("):
                continue
        body = re.sub(r"^([-*+]|\d+[.)]|>)\s*", "", st)
        if spec.get("must") and not re.search(spec["must"], body):
            continue
        b = re.match(r"^\*\*(.+?)\*\*", body)
        txt = _plain(b.group(1) if b and len(_plain(b.group(1))) >= 12 else body)
        if len(txt) < 12:
            continue
        if spec.get("label") and label:
            txt = f"{label} — {txt}"
        return _short(spec.get("prefix", "") + txt)
    return ""


def key_lines(a):
    """항목별 핵심 한 줄. 마땅한 것이 없으면 빈 글자 (화면에는 항목 제목만)."""
    ex, x = a["extract"], a["external"]
    out = {}
    j = x.get("journal") or {}
    rank = j.get("rank") or {}
    oa = x.get("openalex") or {}
    bits = [ex.get("journal") or ""]
    if rank.get("tier"):
        bits.append(rank["tier"])
    if rank.get("q_sjr"):
        bits.append(f"SJR Q{rank['q_sjr']}")
    if oa.get("cited_by_count") is not None:
        bits.append(f"피인용 {oa['cited_by_count']}회 (OpenAlex)")
    out["s1"] = " · ".join(b for b in bits if b)
    for k, spec in KEY_SPEC.items():
        out[k] = _key_line(a["sections"].get(k, ""), spec)
    au = []
    for p in x.get("authors") or []:
        o = p.get("openalex") or {}
        role = {"first": "제1저자", "corresponding": "교신", "first_and_corresponding": "제1·교신"}.get(p.get("role"), "")
        extra = f", {o['works_count']}편 · h {o['h_index']}" if o.get("works_count") is not None and o.get("h_index") is not None else ""
        au.append(f"{p['name']} ({role}{extra})")
    out["s8"] = " · ".join(au)
    return out


def numcheck_box(a, key):
    fs = [f for f in (a.get("numcheck") or {}).get(key, []) if f["status"] in ("missing", "context")]
    if not fs:
        return ""
    lab = {"missing": "원문 미확인", "context": "맥락 확인"}
    return ("<div class='nc'><strong>수치 대조</strong> — 아래 숫자는 자동 대조에서 걸렸습니다. 원문에서 직접 확인하십시오."
            "<ul>" + "".join(f"<li><span class='flag'>⚠ {lab[f['status']]}</span> <code>{e(f['value'])}</code> — {e(f['sentence'][:220])}</li>"
                             for f in fs) + "</ul></div>")


def failed_lookups(a, services):
    fs = [f for f in a["run"]["lookup_failures"] if f["service"] in services]
    if not fs:
        return ""
    return "<p class='warn'>조회 실패 — " + "; ".join(e(f"{f['service']} {f['what']}: {f['reason']}") for f in fs[:6]) + "</p>"


def section1(a):
    ex, x = a["extract"], a["external"]
    pm = x.get("pubmed") or {}
    oa = x.get("openalex") or {}
    cr = x.get("crossref") or {}
    s2 = x.get("s2") or {}
    j = x.get("journal") or {}
    rank = j.get("rank")
    joa = j.get("openalex") or {}
    authors = ", ".join(f"{e(au['name'])} ({'제1·교신' if au['role'] == 'first_and_corresponding' else ('제1저자' if au['role'] == 'first' else '교신저자')})"
                        for au in ex.get("authors", []))
    rows = [
        ("제목", e(ex.get("title") or a["title"])),
        ("제1·교신저자", authors or "—"),
        ("저널", e(ex.get("journal") or pm.get("journal", "")) + (f" <span class='muted'>({e(pm['journal_abbr'])})</span>" if pm.get("journal_abbr") else "")),
        ("연도 / 발행일", e(ex.get("year", "")) + (f" <span class='muted'>· {e(oa['publication_date'])}</span>" if oa.get("publication_date") else "")),
        ("DOI", _link_doi(ex.get("doi")) or "—"),
        ("PMID", _link_pmid(x.get("pmid")) or "PubMed 에서 찾지 못함"),
        ("연구 설계", e(ex.get("study_design", "")) or "—"),
    ]
    h = "<h3>서지 정보</h3><div class='tw'><table class='kv'>" + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows) + "</table></div>"

    # 저널 Quality — 층별
    h += "<h3>저널 Quality</h3>"
    if rank:
        origin = rank.get("_origin", "")
        layers = [
            ("편집자 등급 (tier)", e(TIER_KO.get(rank.get("tier"), rank.get("tier") or "—")),
             f"{e(origin)} — 사용자가 손으로 관리하는 판단 등급"),
            ("SJR 분위수 (Scimago)", _sjr_value(rank),
             e(rank.get("probed", "")) or "Consensus sjr_max 필터 확인 기록 없음 — 대화창에서 채울 수 있음"),
            ("JCR 분위수 (참고)", f"Q{e(str(rank['q_jcr']))}" if rank.get("q_jcr") else "—", "근사치 · 공식 JCR 아님"),
            ("Impact Factor (참고)", f"≈ {e(str(rank['jif']))}" if rank.get("jif") is not None else "—",
             "근사치 · Clarivate 공식 수치가 아님 — 인용하지 말 것"),
            ("분야", e(rank.get("cat", "")) or "—", e(origin)),
        ]
        if rank.get("note"):
            layers.append(("메모", e(rank["note"]), e(origin)))
        h += "<div class='tw'><table><thead><tr><th>층</th><th>값</th><th>출처</th></tr></thead><tbody>" + \
             "".join(f"<tr><td>{k}</td><td><strong>{v}</strong></td><td class='muted'>{s}</td></tr>" for k, v, s in layers) + \
             f"</tbody></table></div><p class='muted'>등급표 대조 방법: {e(rank.get('matched', ''))} 일치 (표 키 “{e(rank.get('key', ''))}”).</p>"
    else:
        h += ("<p class='warn'><strong>등급 미확정</strong> — 이 저널은 등급표에 없습니다. "
              "미확정 목록(data/pending_journals.json)에 올려 두었습니다. Claude Code 대화창에서 “미확정 저널 등급 채워줘” 라고 하면 "
              "Consensus 로 SJR 분위수를 확인해 채웁니다.</p>")
    ann = j.get("annual_articles")
    if joa or ann:
        h += "<div class='tw'><table><thead><tr><th>저널 규모 · 피인용 (보조)</th><th>값</th><th>출처</th></tr></thead><tbody>"
        if ann:
            h += (f"<tr><td>최근 1년 원저 발행 수 ({e(str(ann['year']))}년)</td><td><strong>{e(str(ann['count']))} 편</strong></td>"
                  f"<td class='muted'>PubMed — journal article·clinical trial, 편지·논평·사설·뉴스 제외. 검색식 <code>{e(ann['query'])}</code></td></tr>")
        elif j.get("iso"):
            h += "<tr><td>최근 1년 원저 발행 수</td><td><span class='warn'>조회 실패</span></td><td class='muted'>PubMed</td></tr>"
        if joa:
            h += (f"<tr><td>2년 평균 피인용</td><td>{_v(round(joa['two_yr_mean_citedness'], 2) if joa.get('two_yr_mean_citedness') is not None else None)}</td>"
                  "<td class='muted'>OpenAlex — IF 와 비슷하나 계산 방식 다름 (모든 문서 종류 포함, 인용 집계 기준 다름)</td></tr>"
                  f"<tr><td>DOAJ 등재(오픈액세스 저널)</td><td>{'예' if joa.get('is_in_doaj') else '아니오'}</td><td class='muted'>OpenAlex</td></tr>"
                  f"<tr><td>출판사</td><td>{_v(joa.get('host'))}</td><td class='muted'>OpenAlex</td></tr>")
        h += "</tbody></table></div>"
    h += failed_lookups(a, {"OpenAlex", "저널 등급표"}) if not joa else ""

    # 인용수
    h += "<h3>이 논문의 피인용 수</h3><div class='tw'><table><thead><tr><th>출처</th><th>피인용</th><th>비고</th></tr></thead><tbody>"
    h += f"<tr><td>OpenAlex</td><td><strong>{_v(oa.get('cited_by_count'))}</strong></td><td class='muted'>" + \
         (f"같은 분야·연도 논문 중 {_top_pct(oa['citation_percentile'])}" if oa.get("citation_percentile") is not None else "") + \
         (f" · FWCI {round(oa['fwci'], 2)}" if oa.get("fwci") is not None else "") + "</td></tr>"
    h += f"<tr><td>Crossref</td><td><strong>{_v(cr.get('is_referenced_by_count'))}</strong></td><td class='muted'>Crossref 회원 출판사 논문만 셈</td></tr>"
    h += f"<tr><td>Semantic Scholar</td><td><strong>{_v(s2.get('citation_count'))}</strong></td><td class='muted'>" + \
         (f"영향력 있는 인용 {e(str(s2['influential']))}" if s2.get("influential") is not None else "") + "</td></tr>"
    h += "</tbody></table></div><p class='muted'>세 곳은 세는 범위가 달라 숫자가 서로 다릅니다. 조회한 날: " + e(a["created"][:10]) + "</p>"
    if oa.get("counts_by_year"):
        h += "<p class='muted'>연도별 피인용 (OpenAlex): " + ", ".join(f"{e(str(c.get('year')))}년 {e(str(c.get('cited_by_count')))}" for c in oa["counts_by_year"]) + "</p>"
    h += failed_lookups(a, {"Crossref", "Semantic Scholar"})
    return h


def similar_table(a):
    x = a["external"]
    sim = x.get("similar") or []
    h = f"<p class='muted'>출처: {e(x.get('similar_note', ''))}</p>"
    if not sim:
        return _fold("유사 주제 논문 목록", h + "<p class='warn'>조회 실패 또는 결과 없음</p>")
    h += "<div class='tw'><table><thead><tr><th>#</th><th>논문</th><th>저널 · 연도</th><th>링크</th></tr></thead><tbody>"
    for i, s in enumerate(sim):
        h += (f"<tr><td>S{i + 1}</td><td>{e(s['title'])}<div class='muted'>{e(s['authors'])}</div></td>"
              f"<td>{e(s.get('journal_abbr') or s.get('journal') or '')} {e(str(s.get('year', '')))}</td><td>{_link_pmid(s['pmid'])}</td></tr>")
    return _fold("유사 주제 논문 목록", h + "</tbody></table></div>")


def refs_table(a):
    refs = a["external"].get("refs") or []
    t = "Discussion 인용 문헌 — 초록 한 줄 정리"
    if not refs:
        return _fold(t, "<p class='warn'>Discussion 에서 인용 문헌을 찾지 못했습니다.</p>")
    h = "<p class='muted'>번호는 논문의 reference 번호. 초록은 PubMed 에서 받아와 정리했습니다.</p>"
    h += "<div class='tw'><table class='refs'><thead><tr><th>번호</th><th>문헌</th><th>Discussion 에서 쓰인 맥락</th><th>그 연구가 보인 것</th></tr></thead><tbody>"
    for r in refs:
        link = _link_pmid(r.get("pmid")) or f"<span class='warn'>{e(r.get('lookup', ''))}</span>"
        h += (f"<tr><td>{e(r['ref_no'])}</td><td>{e(r['reference_text'])}<div>{link}</div></td>"
              f"<td>{e(r.get('context', ''))}</td><td>{e(r.get('summary', '')) or '<span class=muted>—</span>'}</td></tr>")
    return _fold(t, h + "</tbody></table></div>")


def authors_tables(a):
    h = ""
    for au in a["external"].get("authors") or []:
        o = au.get("openalex") or {}
        role = {"first": "제1저자", "corresponding": "교신저자", "first_and_corresponding": "제1·교신저자"}.get(au["role"], "")
        h += f"<h4>{e(au['name'])} · {role} — 받아온 자료</h4>"
        if not o:
            h += "<p class='warn'>조회 실패 — OpenAlex 에서 저자를 찾지 못했습니다.</p>"
            continue
        pc = au.get("pubmed_count")
        h += ("<div class='tw'><table class='kv'>"
              f"<tr><th>OpenAlex 이름</th><td>{e(o.get('display_name') or '')} <span class='muted'>({'; '.join(e(n) for n in au['notes'])})</span></td></tr>"
              f"<tr><th>최근 소속 (OpenAlex)</th><td>{e(', '.join(o.get('institutions') or [])) or '—'}</td></tr>"
              f"<tr><th>논문에 적힌 소속</th><td>{e(au.get('affiliation_in_paper') or '—')}</td></tr>"
              f"<tr><th>논문 수 · 피인용 · h-index</th><td>{_v(o.get('works_count'))} 편 · {_v(o.get('cited_by_count'))} 회 · h {_v(o.get('h_index'))}</td></tr>"
              + (f"<tr><th>PubMed 검색 건수</th><td>{e(str(pc['count']))} 건 <span class='muted'>(검색식 {e(pc['query'])} — 동명이인 포함 가능)</span></td></tr>" if pc else "")
              + f"<tr><th>주요 주제 (OpenAlex)</th><td>{e(', '.join(o.get('topics') or [])) or '—'}</td></tr>"
              + (f"<tr><th>주로 낸 저널</th><td>{e(', '.join(f'{t['journal']} ({t['count']})' for t in au['top_journals']))}</td></tr>" if au.get("top_journals") else "")
              + "</table></div>")
        if au.get("recent_works"):
            h += "<div class='tw'><table><thead><tr><th>최근 논문</th><th>저널</th><th>연도</th><th>저자 순서</th><th>피인용</th></tr></thead><tbody>"
            for w in au["recent_works"]:
                pos = {"first": "제1", "last": "마지막", "middle": "중간"}.get(w.get("position"), "")
                lk = _link_pmid(w["pmid"]) if w.get("pmid") else (_link_doi(w.get("doi")) if w.get("doi") else "")
                h += (f"<tr><td>{e(w.get('title') or '')}<div>{lk}</div></td><td>{e(w.get('journal') or '')}</td>"
                      f"<td>{_v(w.get('year'))}</td><td>{e(pos)}</td><td>{_v(w.get('cited_by'))}</td></tr>")
            h += "</tbody></table></div>"
    return _fold("받아온 자료 — OpenAlex · PubMed", h) if h else ""


def run_box(a):
    r = a["run"]
    u = r["claude_usage"]
    lc = r["lookup_calls"]
    fails = r["lookup_failures"]
    h = ("<details class='run'><summary>이 보고서를 만든 과정</summary>"
         f"<p>분석 모델 {e(r['model'])} (분석 깊이 {e(str(r['effort']))}) · 걸린 시간 {round(r['seconds'] / 60, 1)}분 · "
         f"Claude 요청 {e(str(u['calls']))}회 · 예상 비용 약 ${e(str(r['cost_usd']))}</p>"
         f"<p>외부 조회: " + ", ".join(f"{e(str(k))} {e(str(v))}회" for k, v in lc.items()) + "</p>")
    if fails:
        h += "<p>조회 실패 " + str(len(fails)) + "건:</p><ul>" + "".join(
            f"<li>{e(f['service'])} — {e(f['what'])}: {e(str(f['reason']))}</li>" for f in fails) + "</ul>"
    nc = a.get("numcheck")
    if nc is not None:
        cnt = {st: sum(1 for v in nc.values() for f in v if f["status"] == st) for st in ("missing", "context", "computed")}
        h += (f"<p>수치 자동 대조: 원문 미확인 {cnt['missing']} · 맥락 확인 {cnt['context']} · "
              f"계산식이 붙은 직접 계산값 {cnt['computed']} (표시 안 함). 이 점검은 숫자의 존재와 어림 표현만 보며, "
              "맞는 숫자를 틀린 대상에 붙인 것을 모두 잡지는 못합니다.</p>")
    if a.get("figures"):
        nv = sum(1 for f in a["figures"] if f.get("vision"))
        h += (f"<p>그림·표 잘라내기 {len(a['figures'])}개" + (f", 그중 {nv}개는 항목 4 분석 때 Claude 가 그림을 직접 봄" if nv else ", Claude 에게 그림을 보여주지 않음") + "</p>")
    for note in a.get("rerun_notes") or []:
        h += f"<p>{e(note)}</p>"
    if a.get("errors"):
        h += "<p class='warn'>분석 실패 항목: " + "; ".join(e(f"{k}: {v}") for k, v in a["errors"].items()) + "</p>"
    h += "<p class='muted'>Claude 가 쓴 해석은 틀릴 수 있습니다. 숫자는 원문 쪽 번호(p.N)로 확인하십시오.</p></details>"
    return h


def _fold_h3(h):
    """이미 HTML 로 만든 덩어리(항목 1)를 <h3> 기준으로 접기 덩어리로 바꾼다."""
    parts = re.split(r"<h3>(.*?)</h3>", h)
    out = parts[0]
    for i in range(1, len(parts), 2):
        out += _fold(parts[i], parts[i + 1])
    return out


# ====================================================================== 화면
# 색은 포인트 하나(hematoxylin 남색). 바탕은 차가운 종이색. 글꼴은 이 컴퓨터에 있는 것만 — 내려받아 열어도 같다.
CSS = """
:root{--bg:#f4f5f7;--surface:#fff;--ink:#1a1d23;--ink-2:#394049;--muted:#5d646e;--line:#dcdfe4;--line-2:#eceef1;
--accent:#3949a0;--accent-ink:#2b3880;--accent-soft:#e7eaf6;--warn:#98550f;--warn-soft:#fbf0e2;
--serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,"AppleMyungjo","Noto Serif KR",serif;
--sans:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Pretendard","Noto Sans KR","Malgun Gothic",sans-serif;
--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#121419;--surface:#191c22;--ink:#e3e5e9;--ink-2:#c4c8cf;--muted:#99a0ab;--line:#2c3038;--line-2:#22252c;
--accent:#a6b0f0;--accent-ink:#c3caf7;--accent-soft:#232847;--warn:#e3a862;--warn-soft:#2d2318}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.6 var(--sans);word-break:keep-all;overflow-wrap:anywhere}
a{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
.shell{display:grid;grid-template-columns:272px minmax(0,1fr);min-height:100vh}

/* 왼쪽 메뉴 */
.rail{position:sticky;top:0;height:100vh;overflow-y:auto;padding:28px 18px 24px;border-inline-end:1px solid var(--line);display:flex;flex-direction:column;gap:18px}
.brand{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-weight:600;padding-inline-start:10px}
.ptitle{font:500 15px/1.4 var(--serif);color:var(--ink);padding-inline-start:10px;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.menu ol{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:2px}
.menu a{display:grid;grid-template-columns:20px 1fr;gap:8px;align-items:baseline;padding:8px 10px;border-radius:8px;color:var(--ink-2);text-decoration:none;font-size:15px;line-height:1.35}
.menu a .n{font-family:var(--serif);color:var(--muted);text-align:end;font-variant-numeric:lining-nums}
.menu a:hover{background:var(--line-2);color:var(--ink)}
.menu a[aria-current=page]{background:var(--accent-soft);color:var(--accent-ink);font-weight:600}
.menu a[aria-current=page] .n{color:var(--accent)}
.rail .tools{margin-top:auto;display:flex;flex-direction:column;gap:8px;padding-inline-start:10px}
.btn{display:inline-flex;align-items:center;gap:6px;font:inherit;font-size:14px;color:var(--accent);background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:7px 12px;cursor:pointer;text-decoration:none;width:max-content}
.btn:hover{border-color:var(--accent)}
.kbd{font-size:12.5px;color:var(--muted)}
kbd{font:12px var(--mono);border:1px solid var(--line);border-bottom-width:2px;border-radius:4px;padding:0 4px;background:var(--surface)}

/* 본문 */
main{min-width:0;padding:48px clamp(16px,5vw,64px) 96px;container-type:inline-size}
.view{max-width:1000px}
.vhead{margin:0 0 8px;padding-bottom:18px;border-bottom:1px solid var(--line)}
.eyebrow{font-size:13px;color:var(--muted);letter-spacing:.02em;margin:0}
.vhead h2{font:600 34px/1.2 var(--serif);margin:6px 0 0;letter-spacing:-.01em}
.vhead h2:focus{outline:none}
.prose p,.prose li,.prose blockquote,.prose h4,.prose .nc{max-width:40em}
.prose p{margin:0 0 14px}
.prose ul,.prose ol{padding-inline-start:22px;margin:0 0 14px}
.prose li{margin:3px 0}
.prose li>ul,.prose li>ol{margin:4px 0}
h4{font-size:17px;margin:26px 0 8px;color:var(--ink)}
details.sub{border-bottom:1px solid var(--line)}
details.sub>summary{list-style:none;cursor:pointer;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:20px 0 16px}
details.sub>summary::-webkit-details-marker{display:none}
details.sub>summary h3{font:600 20px/1.35 var(--sans);margin:0;color:var(--ink)}
details.sub>summary:hover h3{color:var(--accent-ink)}
.chev{flex:none;width:10px;height:10px;border-inline-end:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(45deg) translate(-2px,-2px);transition:transform .15s}
details.sub:not([open])>summary .chev{transform:rotate(-45deg)}
.subbody{padding:0 0 22px}
.tw{overflow-x:auto;margin:14px 0 18px;border:1px solid var(--line);border-radius:8px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:15px;line-height:1.5;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line-2);padding:9px 12px;text-align:start;vertical-align:top;overflow-wrap:break-word}
tr:last-child>td,tr:last-child>th{border-bottom:0}
thead th{background:var(--line-2);font-weight:600;font-size:14px;color:var(--ink-2);white-space:nowrap}
table.kv th{width:170px;font-weight:600;color:var(--ink-2);background:transparent}
table.refs td:first-child{white-space:nowrap}
.muted{color:var(--muted);font-size:14px}
.warn{color:var(--warn)}
.pg{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:0 4px;margin:0 2px;white-space:nowrap;font-variant-numeric:tabular-nums}
code{font:.88em var(--mono);background:var(--line-2);padding:1px 4px;border-radius:4px}
blockquote{margin:14px 0;padding:4px 16px;border-inline-start:3px solid var(--line);color:var(--ink-2)}
hr{border:0;border-top:1px solid var(--line);margin:24px 0}
.flag{font-size:12px;color:var(--warn);background:var(--warn-soft);border-radius:4px;padding:1px 5px;margin:0 2px;white-space:nowrap}
.seen{font-size:12px;color:var(--accent-ink);background:var(--accent-soft);border-radius:4px;padding:1px 5px;margin:0 2px;white-space:nowrap}
.nc{margin:20px 0;padding:12px 16px;border-radius:8px;background:var(--warn-soft);font-size:15px}
.nc ul{margin:6px 0 0;padding-inline-start:18px}

/* 항목 4: 그림과 해설을 나란히 */
.figpair{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:28px;align-items:start;margin:4px 0 24px;padding-top:4px}
.figpair .figtext>h4:first-child{margin-top:8px}
.figpair figure.cap{position:sticky;top:20px;margin-top:8px}
figure.cap{margin:14px 0;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:10px}
figure.cap button.zoom{display:block;width:100%;padding:0;border:0;background:#fff;cursor:zoom-in;border-radius:4px;overflow:hidden}
figure.cap img{display:block;width:100%;height:auto}
figure.cap figcaption{font-size:13px;color:var(--muted);margin-top:8px;line-height:1.5}
@container (max-width:780px){.figpair{grid-template-columns:1fr;gap:4px}.figpair figure.cap{position:static}}

/* 개요 */
.ov-head{padding-bottom:4px}
.ov-head h1{font:600 clamp(26px,3.6vw,38px)/1.22 var(--serif);margin:8px 0 14px;letter-spacing:-.01em;max-width:30em}
.byline{color:var(--ink-2);margin:0 0 6px;font-size:16px}
.byline .role{color:var(--muted);font-size:14px}
.ov-meta{color:var(--muted);font-size:15px;margin:0}
.ov-tools{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0 0}
dl.facts{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:10px 24px;margin:28px 0 36px;padding:18px 0;border-block:1px solid var(--line)}
dl.facts dt{color:var(--muted);font-size:14px;padding-top:2px}
dl.facts dd{margin:0}
.chip{display:inline-block;font-size:14px;border:1px solid var(--line);border-radius:999px;padding:1px 10px;margin:0 6px 4px 0;background:var(--surface);white-space:nowrap}
.chip.strong{border-color:var(--accent);color:var(--accent-ink)}
.ov-label{font-size:14px;color:var(--muted);margin:0 0 12px;letter-spacing:.02em}
.keys{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,400px),1fr));gap:12px}
a.key{display:grid;grid-template-columns:34px minmax(0,1fr);grid-template-rows:auto 1fr;align-content:start;column-gap:12px;row-gap:4px;height:100%;padding:16px 18px;background:var(--surface);border:1px solid var(--line);border-radius:10px;color:inherit;text-decoration:none;transition:border-color .15s}
a.key:hover{border-color:var(--accent)}
a.key .n{grid-row:span 2;font:500 30px/1 var(--serif);color:var(--accent);padding-top:2px}
a.key .t{font-weight:600;font-size:15.5px}
a.key .l{font-size:15px;line-height:1.55;color:var(--ink-2)}
a.key .l:empty{display:none}
details.run{margin-top:44px;font-size:14px;color:var(--muted);border-top:1px solid var(--line);padding-top:14px}
details.run summary{cursor:pointer}
details.run p,details.run li{max-width:46em}

/* 이전·다음 */
nav.pager{display:flex;gap:12px;margin-top:48px}
nav.pager a{display:flex;flex-direction:column;gap:2px;padding:12px 16px;border:1px solid var(--line);border-radius:10px;background:var(--surface);text-decoration:none;color:var(--ink);min-width:0;max-width:48%}
nav.pager a:hover{border-color:var(--accent)}
nav.pager small{font-size:13px;color:var(--muted)}
nav.pager span{font-weight:600}
nav.pager .next{margin-inline-start:auto;text-align:end}
.pager-hint{margin-top:12px;text-align:center}

/* 그림 크게 보기 */
#jczoom{position:fixed;inset:0;background:rgba(12,14,18,.96);display:none;overflow:auto;z-index:20}
#jczoom.on{display:block}
#jczoom img{display:block;margin:56px auto 32px;max-width:calc(100vw - 32px);max-height:calc(100vh - 88px);background:#fff;cursor:zoom-in}
#jczoom.full img{max-width:none;max-height:none;cursor:zoom-out}
#jczoom .x{position:fixed;top:12px;inset-inline-end:16px;font:inherit;font-size:15px;color:#fff;background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.3);border-radius:8px;padding:6px 12px;cursor:pointer}
#jczoom .hint{position:fixed;top:18px;inset-inline-start:16px;color:#c9ccd3;font-size:13px}

/* 좁은 화면: 메뉴가 위쪽 가로 띠로 */
@media (max-width:900px){
.shell{grid-template-columns:minmax(0,1fr)}
.rail{min-width:0;position:sticky;top:0;z-index:10;height:auto;overflow:visible;padding:0;border-inline-end:0;border-bottom:1px solid var(--line);background:var(--bg);gap:0}
.brand,.ptitle,.rail .tools{display:none}
.menu{overflow-x:auto;scrollbar-width:none;padding:8px 16px;-webkit-mask-image:linear-gradient(to right,#000 88%,transparent);mask-image:linear-gradient(to right,#000 88%,transparent)}
.menu::-webkit-scrollbar{display:none}
.menu ol{flex-direction:row;width:max-content;gap:4px;padding-inline-end:32px}
.menu a{white-space:nowrap;grid-template-columns:auto auto;gap:6px;padding:7px 12px;font-size:14.5px}
main{padding:24px 16px 72px}
.vhead h2{font-size:28px}
body{font-size:16.5px}
dl.facts{grid-template-columns:1fr;gap:2px}
dl.facts dd{margin-bottom:10px}
table.kv th{width:110px}
.tw table:has(thead){min-width:34em}
}
@media (hover:none){.kbd,.pager-hint{display:none}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}

/* 인쇄: 개요 + 8항목을 한 문서로 */
@media print{
:root{--bg:#fff;--surface:#fff;--ink:#000;--ink-2:#1d1d1d;--muted:#555;--line:#c8c8c8;--line-2:#eee;--accent:#000;--accent-ink:#000;--accent-soft:#fff;--warn:#7a3e00;--warn-soft:#fff}
body{font-size:10.5pt;line-height:1.5;background:#fff}
.shell{display:block}
.rail,nav.pager,.pager-hint,.ov-tools,.kbd,#jczoom,.chev{display:none!important}
main{padding:0}
.view,.view[hidden]{display:block!important;max-width:none}
section.view{break-before:page}
.vhead h2{font-size:18pt}
.vhead,h3,h4{break-after:avoid}
details.sub>summary{padding:12pt 0 6pt}
details.sub>summary h3{font-size:13pt}
a.key,tr,figure.cap{break-inside:avoid}
.keys{grid-template-columns:1fr 1fr}
a.key{padding:8pt 10pt}
.figpair figure.cap{position:static}
.tw{overflow:visible;border-color:#ccc}
table{font-size:9pt}
a{color:inherit;text-decoration:none}
.prose p,.prose li{max-width:none}
}
"""


JS = """(function(){
var ORDER=__ORDER__, NAMES=__NAMES__, BASE=document.title;
var zoom=document.getElementById('jczoom'), zimg=zoom.querySelector('img'), lastFocus=null;
function cur(){var h=decodeURIComponent(location.hash.slice(1));return ORDER.indexOf(h)>=0?h:'ov';}
function show(id,focus){
  ORDER.forEach(function(k){document.getElementById('v-'+k).hidden=(k!==id);});
  document.querySelectorAll('.menu a').forEach(function(a){
    if(a.getAttribute('href')==='#'+id){a.setAttribute('aria-current','page');
      try{a.scrollIntoView({block:'nearest',inline:'center'});}catch(e){}}
    else a.removeAttribute('aria-current');});
  document.title=(id==='ov'?'':NAMES[id]+' — ')+BASE;
  window.scrollTo(0,0);
  if(focus){var h=document.querySelector('#v-'+id+' h1, #v-'+id+' h2');if(h)h.focus({preventScroll:true});}
}
function go(step){var i=ORDER.indexOf(cur())+step;if(i>=0&&i<ORDER.length)location.hash=ORDER[i];}
window.addEventListener('hashchange',function(){show(cur(),true);});
document.addEventListener('keydown',function(ev){
  if(zoom.classList.contains('on')){if(ev.key==='Escape')jcClose();return;}
  if(ev.altKey||ev.ctrlKey||ev.metaKey||ev.shiftKey)return;
  var t=ev.target,tag=(t.tagName||'').toLowerCase();
  if(tag==='input'||tag==='textarea'||tag==='select'||t.isContentEditable)return;
  if(ev.key==='ArrowRight'){go(1);ev.preventDefault();}
  else if(ev.key==='ArrowLeft'){go(-1);ev.preventDefault();}
});
window.jcZoom=function(btn){lastFocus=btn;zimg.src=btn.querySelector('img').src;zimg.alt=btn.querySelector('img').alt;
  zoom.classList.remove('full');zoom.classList.add('on');zoom.querySelector('.x').focus();document.body.style.overflow='hidden';};
window.jcClose=function(){zoom.classList.remove('on');document.body.style.overflow='';if(lastFocus)lastFocus.focus();};
zoom.addEventListener('click',function(ev){if(ev.target===zimg){zoom.classList.toggle('full');}else{jcClose();}});
var opened=[];
window.addEventListener('beforeprint',function(){
  ORDER.forEach(function(k){document.getElementById('v-'+k).hidden=false;});
  opened=[];document.querySelectorAll('details').forEach(function(d){if(!d.open){d.open=true;opened.push(d);}});});
window.addEventListener('afterprint',function(){opened.forEach(function(d){d.open=false;});show(cur(),false);});
show(cur(),false);
})();"""


def _num_name(label):
    n, _, name = label.partition(". ")
    return (n, name) if name else ("", label)


def _pager(i, order):
    """order: [(id, 번호, 이름)] — 개요 포함"""
    h = "<nav class='pager' aria-label='이전·다음 항목'>"
    if i > 0:
        k, n, name = order[i - 1]
        h += f"<a class='prev' href='#{k}'><small>← 이전</small><span>{e((n + '. ') if n else '')}{e(name)}</span></a>"
    if i < len(order) - 1:
        k, n, name = order[i + 1]
        h += f"<a class='next' href='#{k}'><small>다음 →</small><span>{e(n)}. {e(name)}</span></a>"
    return h + "</nav><p class='pager-hint kbd'>키보드 <kbd>←</kbd> <kbd>→</kbd> 로도 넘길 수 있습니다</p>"


def overview(a, keys, order):
    ex, x = a["extract"], a["external"]
    pm = x.get("pubmed") or {}
    oa = x.get("openalex") or {}
    j = x.get("journal") or {}
    rank = j.get("rank") or {}
    roles = {"first": "제1저자", "corresponding": "교신저자", "first_and_corresponding": "제1·교신저자"}
    by = " · ".join(f"{e(au['name'])} <span class='role'>{roles.get(au['role'], '')}</span>" for au in ex.get("authors", []))
    meta = [e(ex.get("journal") or pm.get("journal", "")), e(str(ex.get("year", "")))]
    if ex.get("doi"):
        meta.append("DOI " + _link_doi(ex.get("doi")))
    if x.get("pmid"):
        meta.append(_link_pmid(x.get("pmid")))
    h = (f"<header class='ov-head'><p class='eyebrow'>저널클럽 보고서 · 분석일 {e(a['created'][:10])}</p>"
         f"<h1 tabindex='-1'>{e(ex.get('title') or a['title'])}</h1>"
         + (f"<p class='byline'>{by}</p>" if by else "")
         + f"<p class='ov-meta'>{' · '.join(m for m in meta if m)}</p>"
         "<div class='ov-tools'><button class='btn' type='button' onclick='window.print()'>인쇄 / PDF 로 저장</button>"
         "<a class='btn' href='paper.pdf' target='_blank' rel='noopener'>원문 PDF</a></div></header>")

    # 저널 등급 · 피인용 · 설계 — 개요에 필요한 세 가지만
    q = []
    if rank.get("tier"):
        q.append(f"<span class='chip strong'>{e(TIER_KO.get(rank['tier'], rank['tier']))}</span>")
    if rank.get("q_sjr"):
        q.append(f"<span class='chip'>SJR Q{e(str(rank['q_sjr']))}</span>")
    ann = j.get("annual_articles")
    if ann:
        q.append(f"<span class='chip'>최근 1년 원저 {e(str(ann['count']))}편</span>")
    if not q:
        q.append("<span class='warn'>등급 미확정</span>")
    c = []
    if oa.get("cited_by_count") is not None:
        c.append(f"<strong>{e(str(oa['cited_by_count']))}회</strong> <span class='muted'>OpenAlex</span>")
        if oa.get("citation_percentile") is not None:
            c.append(f"<span class='muted'>같은 분야·연도 중 {e(_top_pct(oa['citation_percentile']))}</span>")
    cr, s2 = x.get("crossref") or {}, x.get("s2") or {}
    other = [f"Crossref {e(str(cr['is_referenced_by_count']))}" for _ in [0] if cr.get("is_referenced_by_count") is not None] + \
            [f"Semantic Scholar {e(str(s2['citation_count']))}" for _ in [0] if s2.get("citation_count") is not None]
    if other:
        c.append(f"<span class='muted'>({' · '.join(other)})</span>")
    h += ("<dl class='facts'>"
          f"<dt>저널 Quality</dt><dd>{''.join(q)}</dd>"
          f"<dt>피인용</dt><dd>{' '.join(c) or '—'}</dd>"
          f"<dt>연구 설계</dt><dd>{e(ex.get('study_design') or '') or '—'}</dd></dl>")

    h += "<p class='ov-label'>항목별 핵심 — 눌러서 열기</p><ul class='keys'>"
    for k, n, name in order[1:]:
        h += (f"<li><a class='key' href='#{k}'><span class='n'>{e(n)}</span><span class='t'>{e(name)}</span>"
              f"<span class='l'>{e(keys.get(k, ''))}</span></a></li>")
    return h + "</ul>" + run_box(a)


def render_report(a, folder=None):
    """folder 를 주면 그 안의 figures/*.png 를 보고서에 심는다."""
    secs = a["sections"]
    nc = a.get("numcheck") or {}
    order = [("ov", "", "개요")] + [(k,) + _num_name(l) for k, l in SECTIONS]
    keys = key_lines(a)
    body = f"<article class='view' id='v-ov' aria-label='개요'>{overview(a, keys, order)}{_pager(0, order)}</article>"
    for i, (key, label) in enumerate(SECTIONS, start=1):
        inner = ""
        if key == "s1":
            inner = _fold_h3(section1(a))
        else:
            if key in secs:
                txt = numcheck.annotate(secs[key], nc.get(key, []))
                inner = foldable(txt, key, a, folder)
                inner += numcheck_box(a, key)
            elif key in a.get("errors", {}):
                inner = f"<p class='warn'>이 항목은 분석에 실패했습니다 — {e(a['errors'][key])}</p>"
            if key == "s2":
                inner += similar_table(a)
            if key == "s5":
                inner += refs_table(a)
            if key == "s8":
                inner += authors_tables(a)
        n, name = _num_name(label)
        body += (f"<section class='view' id='v-{key}' aria-labelledby='h-{key}' hidden>"
                 f"<header class='vhead'><p class='eyebrow'>항목 {e(n)} / {len(SECTIONS)}</p>"
                 f"<h2 id='h-{key}' tabindex='-1'>{e(name)}</h2></header>"
                 f"<div class='prose'>{inner}</div>{_pager(i, order)}</section>")
    menu = "".join(f"<li><a href='#{k}'><span class='n'>{e(n) if n else '·'}</span><span>{e(name)}</span></a></li>"
                   for k, n, name in order)
    ex = a["extract"]
    js = JS.replace("__ORDER__", json.dumps([k for k, _, _ in order])) \
           .replace("__NAMES__", json.dumps({k: (f"{n}. {name}" if n else name) for k, n, name in order}, ensure_ascii=False).replace("</", "<\\/"))
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(a['title'])} — Journal Club</title><style>{CSS}</style></head><body>
<div class="shell">
<aside class="rail" aria-label="항목 메뉴">
<div class="brand">Journal Club</div>
<div class="ptitle">{e(ex.get('journal', ''))} {e(str(ex.get('year', '')))}<br>{e(ex.get('title') or a['title'])}</div>
<nav class="menu" aria-label="보고서 항목"><ol>{menu}</ol></nav>
<div class="tools"><button class="btn" type="button" onclick="window.print()">인쇄 / PDF 로 저장</button>
<a class="btn" href="paper.pdf" target="_blank" rel="noopener">원문 PDF</a>
<p class="kbd"><kbd>←</kbd> <kbd>→</kbd> 이전·다음 항목</p></div>
</aside>
<main>{body}</main></div>
<div id="jczoom" role="dialog" aria-modal="true" aria-label="그림 크게 보기"><span class="hint">그림을 누르면 원래 크기 · 바깥을 누르거나 Esc 로 닫기</span><button class="x" type="button">닫기</button><img alt=""></div>
<script>{js}</script></body></html>"""
