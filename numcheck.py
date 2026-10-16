"""해설 속 수치를 원문(paper_text.txt)과 대조한다 — Claude 를 부르지 않는 기계적 점검.

판정
  missing  원문에 그 값이 없다                         → "⚠ 원문 미확인"
  context  원문에 소수 값은 있으나 추정형으로 썼다        → "⚠ 맥락 확인"
           ("약 8.5% 정도로 보이는" 처럼 원문 숫자에 '약·정도·보이는·추정' 을 붙인 경우.
            원문 숫자는 정확한 값이므로 얼버무렸다면 다른 자리의 숫자를 끌어왔을 가능성이 크다)
  computed 원문에 없지만 같은 문장에 계산식(=, ≈, 1/, ÷, −)이 있다 → 표시하지 않고 목록에만
대상: 소수(0.86, 10.5), 백분율(25.4%, 37%). 정수·날짜·쪽 번호는 보지 않는다.
한계: 값의 '존재'와 얼버무림만 본다. 맞는 숫자를 틀린 대상에 붙인 것을 모두 잡지는 못한다.
"""
import re

NUM_RE = re.compile(r"(?<![\w.·])(\d+(?:\.\d+)?)(\s?%)?(?![\w]*\.\d)")
DATE_RE = re.compile(r"\d{4}\.\d{1,2}(?:\.\d{1,2})?")
CALC_RE = re.compile(r"(≈|=|÷|×|\b1/|\d\s*[−-]\s*\d|\d\s*/\s*\d)")
HEDGE_BEFORE = re.compile(r"(약|대략|추정|어림)\s*$")
HEDGE_AFTER = re.compile(r"^\s*(%\s*)?(정도|가량|내외|쯤|로 보이|으로 보이|로 추정|으로 추정|로 짐작)")


def _paper_values(text):
    t = (text or "").replace("·", ".").replace(" ", " ")
    vals = set()
    for m in re.finditer(r"\d+(?:\.\d+)?", t):
        try:
            vals.add(round(float(m.group(0)), 6))
        except ValueError:
            pass
    return vals


def _units(md):
    """검사 단위: 표의 칸, 그 밖은 문장."""
    for line in (md or "").split("\n"):
        st = line.strip()
        if not st or re.match(r"^\|?\s*:?-{2,}", st):
            continue
        parts = st.strip("|").split("|") if st.startswith("|") else [st]
        for p in parts:
            for sent in re.split(r"(?<=[.!?。])\s+(?=[^\d])|(?<=다\.)\s*", p):
                if sent.strip():
                    yield sent.strip()


def check_text(md, paper_text, _vals=None):
    vals = _vals if _vals is not None else _paper_values(paper_text)
    out, seen = [], set()
    for u in _units(md):
        clean = re.sub(r"\[p\.\d+\]|\(p\.\s?\d+\)|https?://\S+|PMID\s*\d+", " ", u)
        clean = DATE_RE.sub(" ", clean)
        calc = bool(CALC_RE.search(clean))
        for m in NUM_RE.finditer(clean):
            s, pct = m.group(1), m.group(2)
            if "." not in s and not pct:
                continue                      # 정수는 보지 않는다
            v = round(float(s), 6)
            before, after = clean[:m.start()], clean[m.end():]
            if v not in vals:
                status = "computed" if calc else "missing"
            elif "." in s and (HEDGE_BEFORE.search(before[-6:]) or HEDGE_AFTER.search(after[:8])):
                # 소수만 본다 — "약 30%" 같은 정수 어림은 반올림 설명인 경우가 대부분
                status = "context"
            else:
                continue
            k = (s, status, u)
            if k in seen:
                continue
            seen.add(k)
            out.append({"value": s + ("%" if pct else ""), "status": status, "sentence": u[:300]})
    return out


def check_sections(sections, paper_text):
    vals = _paper_values(paper_text)
    return {k: check_text(v, paper_text, vals) for k, v in (sections or {}).items() if isinstance(v, str)}


MARK = {"missing": "⟦⚠ 원문 미확인⟧", "context": "⟦⚠ 맥락 확인⟧"}


def annotate(md, findings):
    """markdown 의 해당 숫자 뒤에 표시를 끼운다 (render 가 ⟦…⟧ 를 작은 표지로 바꾼다)."""
    for f in findings:
        if f["status"] not in MARK:
            continue
        num = f["value"].rstrip("%")
        rx = re.compile(rf"(?<![\w.·]){re.escape(num)}(\s?%)?(?![\d])")
        sent = f["sentence"]
        i = md.find(sent[:60])
        if i < 0:
            continue
        seg_end = i + len(sent) + 5
        m = rx.search(md, i, seg_end)
        if m and MARK[f["status"]] not in md[m.end():m.end() + 20]:
            md = md[:m.end()] + MARK[f["status"]] + md[m.end():]
    return md
