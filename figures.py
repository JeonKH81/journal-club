"""PDF 의 Figure · Table 캡션 찾기와 잘라내기 (pymupdf, 비용 0).

캡션 찾기 — 저널 형식별
  Lancet   "Figure 2: …"   "Table: …"(번호 없음)
  Nature   "Fig. 1 | …"
  JACC/Circulation  "FIGURE 1 Kaplan-Meier…"  "TABLE 1 Baseline…"(구두점 없음)
  그 밖    "Figure 1. …"  "Table 1 …"
본문 속 "Figure 2. In the arm…" 같은 언급이 먼저 나와도, 같은 번호 후보가 여럿이면 그림·괘선 가까이 있는 것을 고른다.

잘라내기
  Figure: 같은 쪽의 큰 도형 틀(면적 > 20000pt², 캡션을 덮지 않음) 또는 삽입 이미지 중
          캡션과 세로로 겹치거나 가장 가까운 것 + 그것과 겹치는 후보들의 합집합. 캡션은 글로 따로 붙인다.
  Table : (a) 캡션을 품은 색칠 상자가 있으면 그 상자에서 캡션 줄을 뺀 부분 (Lancet)
          (b) 아니면 캡션 아래(또는 위)로 이어지는 가로 괘선 묶음의 끝까지
          (c) 아니면 캡션 아래부터 다음 본문 문단 직전까지
  못 잡으면 그 쪽 전체를 150dpi 로 — 절대 빈 칸으로 두지 않는다.
"""
import re
from pathlib import Path

import pymupdf

_NUM = r"S?\d+[A-Za-z]?"
_WORD = r"(?:Extended Data |Supplementary )?(?:Figure|FIGURE|Fig\.?|FIG\.?|Table|TABLE)"
# 1) 번호 + 구두점  "Figure 1:"  "Fig. 1 |"  "Table 2."
CAP_PUNCT = re.compile(rf"^\s*({_WORD}\s*{_NUM})\s*[:.|—–]\s*")
# 2) 번호 + 공백 + 대문자로 시작하는 말  "FIGURE 1 Kaplan-Meier"  "Table 1 Baseline"
CAP_SPACE = re.compile(rf"^\s*({_WORD}\s*{_NUM})\s+(?=[A-Z])")
# 3) 번호 없는 Lancet 형식  "Table: …"  "Figure: …"
CAP_NONUM = re.compile(r"^\s*((?:Figure|FIGURE|Table|TABLE))\s*[:|]\s+")

MIN_FIG_AREA = 20000      # pt²
DPI = 200
PAGE_DPI = 150


def match_caption(s):
    """블록 글자가 캡션으로 시작하면 (label, key, 본문) 을, 아니면 None."""
    for rx in (CAP_PUNCT, CAP_SPACE, CAP_NONUM):
        m = rx.match(s)
        if m:
            break
    else:
        return None
    raw = re.sub(r"\s+", " ", m.group(1)).strip()
    kind = "table" if re.search(r"tab", raw, re.I) else "fig"
    num = re.search(_NUM + r"$", raw)
    num = num.group(0) if num and re.search(r"\d", raw) else ""
    prefix = "Supplementary " if raw.lower().startswith("supplementary") else ("Extended Data " if raw.lower().startswith("extended") else "")
    label = f"{prefix}{'Table' if kind == 'table' else 'Figure'}{(' ' + num) if num else ''}"
    key = re.sub(r"[^a-z0-9]", "", label.lower())
    body = re.sub(r"\s+", " ", s[m.end():]).strip()
    return label, key, body


def _graphics(page):
    """큰 도형 틀, 가로 괘선, 삽입 이미지."""
    big, rules = [], []
    for d in page.get_drawings():
        r = d["rect"]
        if r.width * r.height > MIN_FIG_AREA:
            big.append({"rect": pymupdf.Rect(r), "fill": d.get("fill")})
        elif r.height < 2.5 and r.width > 60:
            rules.append(pymupdf.Rect(r))
    imgs = []
    for info in page.get_images(full=True):
        try:
            for bb in page.get_image_rects(info[0]):
                if bb.width * bb.height > MIN_FIG_AREA / 2:
                    imgs.append(pymupdf.Rect(bb))
        except Exception:  # noqa: BLE001
            pass
    return big, rules, imgs


def _vdist(a, b):
    """세로 거리 (겹치면 0)."""
    if a.y1 < b.y0:
        return b.y0 - a.y1
    if b.y1 < a.y0:
        return a.y0 - b.y1
    return 0.0


def _area(r):
    return max(0.0, r.width) * max(0.0, r.height)


def find_captions(doc):
    """[{label,key,page(1부터),bbox,caption,near}] — 같은 key 는 그림·괘선 가까운 후보 하나만."""
    cands = {}
    order = []
    for p in doc:
        big, rules, imgs = _graphics(p)
        frames = [b["rect"] for b in big] + imgs + rules
        for b in p.get_text("blocks"):
            s = (b[4] or "").strip()
            hit = match_caption(s)
            if not hit:
                continue
            label, key, body = hit
            if len(body) < 8:
                continue
            bb = pymupdf.Rect(b[:4])
            near = any(_vdist(bb, f) < 40 and (min(bb.x1, f.x1) - max(bb.x0, f.x0)) > -20 for f in frames)
            c = {"label": label, "key": key, "page": p.number + 1, "bbox": [round(v, 1) for v in bb],
                 "caption": body[:900], "near": near}
            if key not in cands:
                order.append(key)
                cands[key] = c
            elif near and not cands[key]["near"]:
                cands[key] = c          # 본문 언급보다 그림 옆 캡션을 고른다
    return [cands[k] for k in order]


# ---------------------------------------------------------------------- 잘라내기
def _figure_rect(page, cap):
    big, _, imgs = _graphics(page)
    cands = []
    for r in [b["rect"] for b in big] + imgs:
        inter = pymupdf.Rect(r) & cap
        if _area(inter) > 0.3 * _area(cap):      # 캡션 상자(배경)는 제외
            continue
        if r.contains(cap):
            continue
        if r.width > page.rect.width * 0.97 and r.height > page.rect.height * 0.9:   # 쪽 테두리
            continue
        cands.append(r)
    if not cands:
        return None
    cands.sort(key=lambda r: (_vdist(r, cap), -_area(r)))
    best = pymupdf.Rect(cands[0])
    if _vdist(best, cap) > 250:
        return None
    for r in cands[1:]:
        if best.intersects(r):
            best |= r
    return best


def _table_rect(page, cap):
    big, rules, _ = _graphics(page)
    pr = page.rect
    # (a) 캡션을 품은 색칠 상자 — Lancet 처럼 표·캡션이 한 상자에
    boxes = [b["rect"] for b in big if b["fill"] and b["rect"].contains(cap) and b["rect"].height > cap.height * 3]
    if boxes:
        box = min(boxes, key=_area)
        inside = [pymupdf.Rect(b[:4]) for b in page.get_text("blocks")
                  if box.contains(pymupdf.Rect(b[:4])) and not pymupdf.Rect(b[:4]).intersects(cap)]
        if (cap.y0 + cap.y1) / 2 > (box.y0 + box.y1) / 2:      # 캡션이 상자 아래쪽 → 위가 표
            y1 = max([r.y1 for r in inside if r.y1 <= cap.y0] or [cap.y0 - 6]) + 2
            return pymupdf.Rect(box.x0 + 4, box.y0 + 4, box.x1 - 4, y1), "캡션을 품은 상자"
        y0 = min([r.y0 for r in inside if r.y0 >= cap.y1] or [cap.y1 + 6]) - 2
        return pymupdf.Rect(box.x0 + 4, y0, box.x1 - 4, box.y1 - 4), "캡션을 품은 상자"
    # (b) 괘선 묶음
    col = [r for r in rules if min(r.x1, cap.x1 + 200) - max(r.x0, cap.x0 - 200) > 40]
    below = sorted([r for r in col if r.y0 >= cap.y1 - 1], key=lambda r: r.y0)
    if below and below[0].y0 - cap.y1 < 60:
        last = below[0]
        for r in below[1:]:
            if r.y0 - last.y1 > 120:
                break
            last = r
        x0 = min([cap.x0] + [r.x0 for r in below if r.y0 <= last.y0])
        x1 = max([cap.x1] + [r.x1 for r in below if r.y0 <= last.y0])
        # 괘선 바로 아래 각주(작은 글자 블록)를 40pt 까지 붙인다
        y1 = last.y1
        for b in page.get_text("blocks"):
            bb = pymupdf.Rect(b[:4])
            if 0 <= bb.y0 - y1 < 14 and bb.x0 >= x0 - 5 and bb.x1 <= x1 + 5 and bb.y1 - last.y1 < 40:
                y1 = max(y1, bb.y1)
        return pymupdf.Rect(x0, cap.y1 + 1, x1, y1 + 2), "가로 괘선"
    above = sorted([r for r in col if r.y1 <= cap.y0 + 1], key=lambda r: -r.y0)
    if above and cap.y0 - above[0].y1 < 60:
        first = above[0]
        for r in above[1:]:
            if first.y0 - r.y1 > 120:
                break
            first = r
        x0 = min(r.x0 for r in above if r.y0 >= first.y0)
        x1 = max(r.x1 for r in above if r.y0 >= first.y0)
        return pymupdf.Rect(x0, max(pr.y0, first.y0 - 25), x1, cap.y0 - 1), "가로 괘선"
    # (c) 다음 본문 문단 직전까지
    stop = pr.y1 - 40
    for b in sorted(page.get_text("blocks"), key=lambda b: b[1]):
        bb = pymupdf.Rect(b[:4])
        txt = (b[4] or "").strip()
        if bb.y0 > cap.y1 + 20 and len(txt) > 300 and bb.width > 150 and min(bb.x1, cap.x1) - max(bb.x0, cap.x0) > 0:
            stop = bb.y0 - 2
            break
    if stop - cap.y1 > 40:
        return pymupdf.Rect(cap.x0, cap.y1 + 1, max(cap.x1, pr.x1 - 36), stop), "다음 본문 문단 직전까지"
    return None, ""


def capture(pdf_path, out_dir, rel_prefix="figures"):
    """캡션마다 그림 파일을 만든다. 돌려주는 목록은 analysis.json 의 "figures" 에 그대로 들어간다."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    out = []
    used = set()
    for c in find_captions(doc):
        page = doc[c["page"] - 1]
        cap = pymupdf.Rect(c["bbox"])
        is_table = "table" in c["key"]
        if is_table:
            rect, how = _table_rect(page, cap)
        else:
            rect, how = _figure_rect(page, cap), "도형 틀 / 삽입 이미지"
        name = re.sub(r"[^A-Za-z0-9]", "", c["label"]) or "Item"
        while name in used:
            name += "_2"
        used.add(name)
        fn = out_dir / f"{name}.png"
        rec = {"label": c["label"], "key": c["key"], "page": c["page"], "caption": c["caption"],
               "caption_bbox": c["bbox"]}
        if rect is not None and rect.width > 40 and rect.height > 40:
            pad = (-2, 0, 2, 0) if is_table else (-4, -4, 4, 4)
            rect = (rect + pad) & page.rect
            page.get_pixmap(clip=rect, dpi=DPI).save(fn)
            rec.update(bbox=[round(v, 1) for v in rect], method=how, fallback=False)
        else:
            page.get_pixmap(dpi=PAGE_DPI).save(fn)
            rec.update(bbox=[round(v, 1) for v in page.rect], method="자동 잘라내기 실패 — 쪽 전체", fallback=True)
        rec["file"] = f"{rel_prefix}/{fn.name}"
        out.append(rec)
    return out


def image_for_claude(pdf_path, rec, max_side=1500):
    """같은 영역을 긴 변 max_side px 이하로 다시 그려 PNG 바이트로."""
    doc = pymupdf.open(pdf_path)
    page = doc[rec["page"] - 1]
    r = pymupdf.Rect(rec["bbox"])
    zoom = min(DPI / 72, max_side / max(r.width, r.height))
    return page.get_pixmap(clip=r, matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")
