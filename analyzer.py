"""논문 PDF → 8항목 분석.

흐름
  1) PDF 글자 추출 (pymupdf) + Figure/Table 캡션 수집 (figures.py)
  2) Claude 1회: 서지정보·저자·Discussion 인용 번호 → JSON
  3) 보고서 폴더를 만들고 Figure·Table 을 잘라 figures/ 에 저장
  4) 외부 조회: PubMed · OpenAlex · Crossref · Semantic Scholar · 저널 등급표 (조회 하나가 실패해도 나머지는 계속)
  5) Claude 8회(항목 2~8 + 인용문헌 한 줄 요약): 논문 본문은 system 에 두고 prompt caching 으로 재사용.
     figure_vision 이 켜져 있으면 항목 4 요청에 잘라낸 그림을 함께 넣는다.
  6) 해설 속 수치를 원문과 대조 (numcheck.py) → render.py 가 report.html 을 만든다
"""
import base64
import concurrent.futures as cf
import json
import re
import threading
import time
from pathlib import Path

import anthropic
import pymupdf

import figures as figmod
import numcheck
from sources import Sources, add_pending, norm_doi

HERE = Path(__file__).resolve().parent

# 1M 토큰당 달러 (claude-api 스킬 표, 2026-10 기준). 캐시 쓰기는 입력가 1.25배, 읽기는 표의 값.
PRICES = {
    "claude-sonnet-5-5": {"in": 2.0, "out": 10.0, "cache_write": 2.5, "cache_read": 0.2},
    "claude-opus-5-5": {"in": 4.0, "out": 20.0, "cache_write": 5.0, "cache_read": 0.2},
}

SECTIONS = [
    ("s1", "1. 메타데이터"),
    ("s2", "2. Introduction"),
    ("s3", "3. Methods"),
    ("s4", "4. Results"),
    ("s5", "5. Discussion"),
    ("s6", "6. 과거 연구와의 차이 · 핵심 메시지"),
    ("s7", "7. Limitation"),
    ("s8", "8. 제1저자 · 교신저자 연구 활동"),
]


# ====================================================================== PDF
def extract_pdf(path):
    doc = pymupdf.open(path)
    pages = [f"\n[p.{p.number + 1}]\n{p.get_text()}" for p in doc]
    captions = [{"label": c["label"], "page": c["page"], "caption": c["caption"]} for c in figmod.find_captions(doc)]
    meta = doc.metadata or {}
    text = "".join(pages)
    return {"text": text, "n_pages": doc.page_count, "captions": captions,
            "pdf_title": meta.get("title", ""), "pdf_subject": meta.get("subject", ""),
            "pdf_author": meta.get("author", "")}


# ====================================================================== Claude
SYSTEM_RULES = """당신은 순환기내과 교수의 저널클럽 준비를 돕는 의학논문 분석가다.
아래 <paper> 에 의학논문 전문(PDF 에서 추출한 글자, [p.N] 은 쪽 번호)이 있다.

공통 규칙
- 한국어로 쓰되 의학·통계·임상 전문용어는 영어 그대로 쓴다 (예: hazard ratio, intention-to-treat, non-inferiority margin).
- 논문에 있는 숫자만 쓴다. 숫자를 추정하거나 지어내지 않는다. 논문에 없으면 "논문에 기재 없음"이라고 쓴다.
- 외부 조회 자료가 주어지면 그 자료에 있는 것만 인용한다. 주어지지 않은 논문·저자·수치를 기억에서 꺼내 쓰지 않는다.
- 근거가 되는 위치를 [p.N] 으로 표시하면 좋다.
- 독자는 임상의다. 통계는 "무엇을 묻는 방법이고 결과를 어떻게 읽는가" 중심으로 설명한다.
- 출력은 Markdown. 제목은 ### 부터 쓴다 (# 과 ## 은 쓰지 않는다). 표는 Markdown 표를 쓴다.
"""


class Claude:
    def __init__(self, cfg, paper_text):
        self.cfg = cfg
        self.client = anthropic.Anthropic(max_retries=3, timeout=900)
        self.model = cfg["model"]
        self.system = [
            {"type": "text", "text": SYSTEM_RULES},
            {"type": "text", "text": f"<paper>\n{paper_text}\n</paper>", "cache_control": {"type": "ephemeral"}},
        ]
        self.use_fallback = cfg.get("use_fallback", True)
        self.lock = threading.Lock()
        self.warm = {}            # 출력 형식별: 캐시가 만들어졌다는 신호 (형식이 다르면 캐시도 따로 생긴다)
        self.warm_owner = {}
        self.usage = {"calls": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
        self.call_log = []

    def ask(self, name, prompt, schema=None, max_tokens=None):
        oc = {"effort": self.cfg.get("effort", "medium")}
        if schema:
            oc["format"] = {"type": "json_schema", "schema": schema}
        kw = dict(model=self.model, max_tokens=max_tokens or self.cfg.get("max_tokens", 32000),
                  system=self.system, output_config=oc,
                  messages=[{"role": "user", "content": prompt}])   # prompt: 글자 하나 또는 [글·그림 블록] 목록
        t0 = time.time()
        fmt = json.dumps(schema, sort_keys=True) if schema else ""
        with self.lock:
            ev = self.warm.setdefault(fmt, threading.Event())
            owner = fmt not in self.warm_owner
            if owner:
                self.warm_owner[fmt] = name
        if not owner:
            ev.wait(timeout=120)   # 먼저 간 요청이 캐시를 만들 때까지 기다린다
        msg = None
        for attempt in range(2):
            try:
                if self.use_fallback:
                    cm = self.client.beta.messages.stream(
                        betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
                else:
                    cm = self.client.messages.stream(**kw)
                with cm as st:
                    for _ in st:          # 첫 응답 조각이 오면 캐시는 이미 만들어져 있다
                        ev.set()
                        break
                    msg = st.get_final_message()
                break
            except anthropic.BadRequestError as e:
                # 대체 모델 기능이 이 계정에서 안 되면 끄고 한 번 더
                if self.use_fallback and "fallback" in str(e).lower() and attempt == 0:
                    self.use_fallback = False
                    continue
                ev.set()
                raise
            except Exception:
                ev.set()
                raise
        ev.set()
        u = msg.usage
        with self.lock:
            self.usage["calls"] += 1
            self.usage["input"] += u.input_tokens or 0
            self.usage["output"] += u.output_tokens or 0
            self.usage["cache_write"] += u.cache_creation_input_tokens or 0
            self.usage["cache_read"] += u.cache_read_input_tokens or 0
            self.call_log.append({"name": name, "seconds": round(time.time() - t0, 1),
                                  "input": u.input_tokens, "output": u.output_tokens,
                                  "cache_write": u.cache_creation_input_tokens,
                                  "cache_read": u.cache_read_input_tokens,
                                  "stop": msg.stop_reason, "model": msg.model})
        if msg.stop_reason == "refusal":
            raise RuntimeError("분석 요청이 거절되었습니다 (안전 기준)")
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        if msg.stop_reason == "max_tokens":
            text += "\n\n> (분량 제한에 걸려 이 항목이 중간에 끊겼습니다.)"
        if schema:
            return json.loads(text)
        return text

    def cost(self):
        p = PRICES.get(self.model)
        if not p:
            return None
        u = self.usage
        return round((u["input"] * p["in"] + u["output"] * p["out"] + u["cache_write"] * p["cache_write"]
                      + u["cache_read"] * p["cache_read"]) / 1e6, 3)


S = {"type": "string"}
EXTRACT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "journal", "year", "doi", "pmid", "study_design", "pubmed_query", "authors",
                 "discussion_citations", "figures_tables"],
    "properties": {
        "title": S, "journal": S, "year": S, "doi": S, "pmid": S, "study_design": S, "pubmed_query": S,
        "authors": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "role", "affiliation"],
            "properties": {"name": S, "role": {"type": "string", "enum": ["first", "corresponding", "first_and_corresponding"]},
                           "affiliation": S}}},
        "discussion_citations": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["ref_no", "reference_text", "title", "first_author", "journal_abbr", "year", "volume",
                         "first_page", "doi", "context"],
            "properties": {"ref_no": S, "reference_text": S, "title": S, "first_author": S, "journal_abbr": S,
                           "year": S, "volume": S, "first_page": S, "doi": S, "context": S}}},
        "figures_tables": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["label", "caption"],
            "properties": {"label": S, "caption": S}}},
    },
}

EXTRACT_PROMPT = """논문에서 다음을 찾아 JSON 으로 돌려준다. 없으면 빈 문자열 "".
- title, journal(정식 저널명), year, doi, pmid(논문에 적혀 있을 때만)
- study_design: 연구 설계 한 줄 (영어, 예: "investigator-initiated open-label RCT, 10-year extended follow-up")
- pubmed_query: 이 논문과 같은 주제의 논문을 PubMed 에서 찾기 위한 영어 검색식 (핵심 개념 2~4개를 AND 로)
- authors: 제1저자와 교신저자만. 같은 사람이면 role=first_and_corresponding. affiliation 은 논문에 적힌 소속.
  (공동 제1저자 표시(*)가 있으면 맨 앞 한 명만 first 로 한다.)
- discussion_citations: **Discussion 섹션 본문**에서 인용된 참고문헌 전부 (최대 {maxrefs}개, 인용된 순서).
  번호 인용이면 번호를 reference list 와 대조해 해당 항목을 채운다. 저자-연도 인용이면 ref_no 에 "Kim 2020" 처럼 쓴다.
  reference_text 는 reference list 원문 그대로, title/first_author(성만)/journal_abbr/year/volume/first_page/doi 는 그 원문에서 나눠 적는다.
  context: Discussion 에서 그 문헌이 어떤 주장의 근거로 쓰였는지 한국어 한 줄.
- figures_tables: 본문의 모든 Figure 와 Table (label 예: "Figure 1", "Table 2") 과 캡션 원문 요약(영어 그대로, 300자 이내).
"""

SECTION_PROMPTS = {
    "s2": """[항목 2. Introduction] 다음 구성으로 쓴다.
### 연구 배경
이 분야에서 무엇이 알려져 있었고, 무엇이 불확실했는지 (저자의 논리 흐름을 따라).
### 연구 질문 심층 분석
- 저자가 명시한 연구 질문/가설을 원문 표현과 함께 제시
- PICO 표 (Population · Intervention/Exposure · Comparator · Outcome, 그리고 Time/Setting)
- 이 질문이 임상적으로 왜 중요한가, 질문의 설계가 답할 수 있는 것과 없는 것
### 유사 주제 논문의 흐름
아래 <similar> 는 PubMed 가 이 논문과 비슷하다고 고른 논문 목록이다 (외부 조회 결과).
목록에 있는 논문만, 번호 [S1] 처럼 붙여 이 분야 연구 흐름 안에서 이 논문의 위치를 3~6문장으로 정리한다.
목록이 비어 있거나 조회 실패면 그렇다고만 쓴다. 목록 표 자체는 프로그램이 따로 붙이므로 다시 나열하지 않는다.

<similar>
{similar}
</similar>
""",
    "s3": """[항목 3. Methods] 임상의가 읽고 바로 이해할 수 있게 쓴다.
### 연구 설계
설계 유형, 기간, 기관 수, 무작위배정/눈가림 방법, 등록 정보(NCT 등).
### 대상자
Inclusion / Exclusion criteria 를 표 또는 목록으로 (논문에 적힌 그대로, 핵심은 영어 용어 유지).
### Endpoint
Primary endpoint 와 secondary endpoint 들을 각각 정의(구성 요소, 판정 기준, 추적 시점)와 함께.
### 통계 방법
사용된 각 방법(예: Kaplan-Meier, Cox proportional hazards, Fine-Gray, ITT/per-protocol, 검정력 계산, 결측 처리, 다중비교 보정)을
"무엇을 위해 썼나 → 어떻게 읽나 → 이 논문에서 주의할 점" 순으로 설명한다. 표본수 계산 가정이 있으면 숫자와 함께 평가한다.
### 방법론에서 눈여겨볼 점
설계상의 강점과 약점 (3~6개 bullet).
""",
    "s4": """[항목 4. Results]
### 핵심 결과
대상자 흐름(등록·배정·탈락·추적률), baseline 특성의 특징, primary endpoint 결과(사건 수/비율, 효과크기, 95% CI, P 값),
주요 secondary endpoint 와 안전성 결과를 표로 정리한 뒤 해석을 붙인다. 가능하면 absolute risk difference 와 NNT 를
논문의 숫자로 계산할 수 있을 때만 계산하고, 계산식을 보인다.
### Figure · Table 해설
아래 <figures> 의 각 Figure/Table 마다 소제목(#### Figure 1: 캡션 요약)을 달고
① 무엇을 보여주는가 ② 핵심 수치 ③ 읽을 때 주의할 점 을 2~4문장으로 쓴다. 본문에 있는데 목록에 빠진 Figure/Table 이 있으면 추가한다.
소제목은 반드시 "#### Figure N: …" / "#### Table N: …" (번호 없는 표는 "#### Table: …") 로 시작한다 — 프로그램이 이 소제목 옆에 그림을 붙인다.
{vision_note}
<figures>
{figures}
</figures>
""",
    "s5": """[항목 5. Discussion]
### 문단별 핵심
Discussion 의 각 문단을 순서대로 "문단 N — 한 줄 제목" 과 2~3문장 요약으로 정리한다. 각 문단에서 인용된 참고문헌 번호를 [n] 으로 표시한다.
### 저자의 논증 구조
저자가 결과를 어떤 논리로 해석하고 어떤 근거로 일반화하는지, 그 논증에서 약한 고리는 무엇인지.
(인용 문헌별 한 줄 정리 표는 프로그램이 따로 붙인다.)
""",
    "s6": """[항목 6. 과거 연구와의 차이 · 이 논문의 핵심 메시지]
### 과거 연구와 무엇이 다른가
논문이 비교 대상으로 삼은 선행 연구들과 대비해 설계·대상·추적기간·결과의 차이를 표로 정리한다
(논문 본문과 아래 <prior> 자료에 있는 것만 쓴다).
### 새로 더해진 지식
### 핵심 메시지 (Take-home message)
3개 이내의 bullet, 각 한 문장.
### 진료에 주는 의미
이 결과가 내일 외래·시술 후 처방에 무엇을 바꾸는지, 바꾸지 않는지. 가이드라인 권고와의 관계(논문에 언급된 범위 안에서).

<prior>
{prior}
</prior>
""",
    "s7": """[항목 7. Limitation]
### 저자가 밝힌 Limitation
논문에 적힌 limitation 을 빠짐없이 목록으로 (각각 영향의 방향과 크기를 한 줄로 평가).
### 저자가 적지 않은 Limitation (비판적 평가)
internal validity(선택 편향, 배정 은닉, 눈가림, 교차·중단, 결측, 경쟁위험, 다중비교, 사후 분석 여부),
external validity(대상 인구, 인종·지역, 시대적 진료 변화), 통계적 문제, 이해상충·연구비를 점검한다.
각 항목은 "무엇이 문제인가 → 근거 [p.N] → 결과 해석에 미치는 영향" 으로 쓴다. 근거가 약한 지적은 "가능성" 으로 표시한다.
### 저널클럽 토론 질문
전공의와 토론할 질문 3~5개.
""",
    "s8": """[항목 8. 제1저자 · 교신저자의 연구 활동]
아래 <authors> 는 OpenAlex·PubMed 에서 받아온 저자 자료다 (외부 조회 결과). 이 자료에 있는 것만 쓴다.
저자마다 ### 이름 (역할) 소제목 아래:
- 소속 (논문에 적힌 소속과 OpenAlex 소속이 다르면 둘 다)
- 연구 주제: 최근 논문 제목들에서 보이는 주제 2~4개로 묶어 설명
- 연구 규모: 논문 수·피인용·h-index (자료의 숫자 그대로) 와 주로 발표하는 저널
- 이 논문과의 연결: 이 저자의 연구 흐름에서 이 논문이 어떤 위치인가
동명이인이 섞였을 가능성이 보이면(주제가 동떨어진 논문 등) 반드시 경고한다. 조회 실패면 그 사실만 쓴다.
(최근 논문 표는 프로그램이 따로 붙이므로 다시 나열하지 않는다.)

<authors>
{authors}
</authors>
""",
}

VISION_NOTE = """
이 요청 끝에 PDF 에서 잘라낸 Figure·Table 이미지가 캡션과 함께 붙어 있다. **캡션만 옮기지 말고 그림을 직접 읽어** 해설한다:
곡선이 언제부터 벌어지고 그 뒤 계속 벌어지는지·평행한지·교차하는지, number at risk 가 줄어드는 시점,
forest plot 에서 점추정치가 1 을 넘나드는 subgroup 과 P for interaction 이 작은 subgroup, flow diagram 의 단계별 탈락 수,
표의 행별 값과 눈에 띄는 칸. 그림에서 직접 읽은 내용을 쓴 문장 끝에는 [그림 판독] 을 붙인다.
그림의 눈금에서 어림한 값은 "그림 눈금으로 약 N%" 처럼 어림임을 밝히고, 본문·표에 정확한 값이 있으면 그 값을 쓴다.
"""


def s4_content(prompt_text, figs, pdf_path, vision):
    """항목 4 요청 내용. vision 이 켜져 있고 잘라낸 그림이 있으면 [글, (설명, 그림)…] 블록 목록을 돌려준다."""
    if not vision or not figs:
        return prompt_text.replace("{vision_note}", ""), []
    blocks = [{"type": "text", "text": prompt_text.replace("{vision_note}", VISION_NOTE)}]
    sent = []
    for f in figs:
        try:
            png = figmod.image_for_claude(pdf_path, f, max_side=1500)
        except Exception as e:  # noqa: BLE001
            f["vision_error"] = f"{type(e).__name__}: {e}"
            continue
        note = " (자동 잘라내기 실패 — 쪽 전체)" if f.get("fallback") else ""
        blocks.append({"type": "text", "text": f"[{f['label']} · p.{f['page']}{note}] 캡션: {f['caption'][:600]}"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                   "data": base64.standard_b64encode(png).decode("ascii")}})
        sent.append(f["label"])
    return (blocks if sent else prompt_text.replace("{vision_note}", "")), sent


def figures_text(captions, extracted):
    cap_txt = "\n".join(f"- {c['label']} [p.{c['page']}]: {c['caption']}" for c in captions)
    return "프로그램이 PDF 에서 찾은 캡션:\n" + (cap_txt or "(없음)") + \
        "\n\n분석기가 본문에서 찾은 Figure/Table:\n" + "\n".join(f"- {f['label']}: {f['caption']}" for f in extracted)


REFSUM_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["ref_no", "summary"],
        "properties": {"ref_no": S, "summary": S}}}},
}

REFSUM_PROMPT = """아래 <refs> 는 이 논문의 Discussion 에서 인용된 문헌들과, PubMed 에서 받아온 각 문헌의 초록이다.
각 문헌마다 summary 에 한국어 한 줄(전문용어는 영어)로: "그 연구가 무엇을 보였는가(핵심 수치 포함 가능)" 를 쓴다.
초록이 없으면 reference 정보만 보고 "초록 없음 — " 뒤에 제목에서 알 수 있는 범위만 쓴다. 초록에 없는 숫자를 만들지 않는다.

<refs>
{refs}
</refs>
"""


# ====================================================================== 파이프라인
class Progress:
    def __init__(self, cb=None):
        self.cb = cb or (lambda *_: None)
        self.steps = []

    def set(self, key, label, state, note=""):
        for s in self.steps:
            if s["key"] == key:
                s.update(label=label, state=state, note=note)
                break
        else:
            self.steps.append({"key": key, "label": label, "state": state, "note": note})
        self.cb(self.steps)


def slugify(title, n=50):
    s = re.sub(r"[^\w\s-]", "", title or "paper", flags=re.U).strip()
    s = re.sub(r"[\s_]+", "_", s)
    return s[:n].strip("_") or "paper"


def make_folder(out_root, title):
    """같은 날 같은 제목이면 _2, _3 … — mkdir 자체로 자리를 잡으므로 동시에 끝나도 겹치지 않는다."""
    out_root.mkdir(parents=True, exist_ok=True)
    base = f"{time.strftime('%Y-%m-%d')}_{slugify(title)}"
    for i in range(1, 1000):
        folder = out_root / (base if i == 1 else f"{base}_{i}")
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            continue
    raise RuntimeError("보고서 폴더 이름을 정하지 못했습니다")


def run(pdf_path, cfg, progress=None, out_root=None, source_name=None):
    t_start = time.time()
    pg = Progress(progress)
    out_root = Path(out_root or HERE / "reports")
    ranks_path = (HERE / cfg.get("journal_ranks_path", "")).resolve()

    # 1) PDF
    pg.set("pdf", "논문 글자 읽기", "doing")
    pdf = extract_pdf(pdf_path)
    if len(pdf["text"].strip()) < 2000:
        raise RuntimeError("PDF 에서 글자를 거의 읽지 못했습니다. 스캔 이미지 PDF 일 수 있습니다.")
    pg.set("pdf", "논문 글자 읽기", "done", f"{pdf['n_pages']}쪽, 그림·표 캡션 {len(pdf['captions'])}개")

    claude = Claude(cfg, pdf["text"])
    local_ranks = HERE / "data" / "journal_ranks_local.json"
    src = Sources(cfg.get("contact_email", ""), ranks_path, local_ranks)

    # 2) 서지정보·인용 추출
    pg.set("extract", "서지정보 · 저자 · Discussion 인용문헌 찾기", "doing")
    ex = claude.ask("extract", EXTRACT_PROMPT.replace("{maxrefs}", str(cfg.get("discussion_refs_max", 25))),
                    schema=EXTRACT_SCHEMA)
    if not ex.get("doi"):
        m = re.search(r"10\.\d{4,9}/[^\s;]+", pdf["pdf_subject"] + " " + pdf["text"][:6000])
        if m:
            ex["doi"] = m.group(0).rstrip(".,)")
    ex["doi"] = norm_doi(ex.get("doi"))
    pg.set("extract", "서지정보 · 저자 · Discussion 인용문헌 찾기", "done",
           f"Discussion 인용 {len(ex.get('discussion_citations', []))}건")

    # 3) 보고서 폴더 + 그림·표 잘라내기
    title = ex.get("title") or pdf["pdf_title"] or Path(pdf_path).stem
    folder = make_folder(out_root, title)
    (folder / "paper.pdf").write_bytes(Path(pdf_path).read_bytes())
    (folder / "paper_text.txt").write_text(pdf["text"], encoding="utf-8")
    pg.set("figs", "Figure · Table 잘라내기", "doing")
    try:
        figs = figmod.capture(folder / "paper.pdf", folder / "figures")
        nfb = sum(1 for f in figs if f.get("fallback"))
        pg.set("figs", "Figure · Table 잘라내기", "done", f"{len(figs)}개" + (f" (쪽 전체 {nfb})" if nfb else ""))
    except Exception as e:  # noqa: BLE001
        figs = []
        pg.set("figs", "Figure · Table 잘라내기", "fail", str(e)[:200])

    # 4) 외부 조회
    pg.set("lookup", "외부 자료 받아오기 (PubMed · OpenAlex · Crossref)", "doing")
    ext = lookup_all(src, ex, cfg, lambda note: pg.set("lookup", "외부 자료 받아오기 (PubMed · OpenAlex · Crossref)", "doing", note),
                     paper_text=pdf["text"])
    for k, v in (("similar", []), ("refs", []), ("authors", []), ("journal", {}), ("similar_note", "")):
        ext.setdefault(k, v)
    pg.set("lookup", "외부 자료 받아오기 (PubMed · OpenAlex · Crossref)", "done",
           f"조회 {sum(src.calls.values())}회, 실패 {len(src.failures)}건")

    # 5) 항목별 분석
    fig_txt = figures_text(pdf["captions"], ex.get("figures_tables") or [])
    similar_txt = "\n".join(f"[S{i + 1}] {s['title']} — {s['journal']} {s['year']} (PMID {s['pmid']}, {', '.join(s.get('pubtypes', [])[:2])})"
                            for i, s in enumerate(ext["similar"] or [])) or f"(조회 실패 또는 없음: {ext['similar_note']})"
    prior_txt = similar_txt + "\n\nDiscussion 인용 문헌:\n" + "\n".join(
        f"[{r['ref_no']}] {r['reference_text']}" + (f"\n   초록: {r['abstract'][:700]}" if r.get('abstract') else "")
        for r in ext["refs"])
    authors_txt = json.dumps(ext["authors"], ensure_ascii=False, indent=1)
    refs_txt = "\n\n".join(f"ref_no={r['ref_no']}\nreference: {r['reference_text']}\n본문 맥락: {r['context']}\n"
                           f"초록: {r.get('abstract') or '(없음)'}" for r in ext["refs"])
    s4_req, vision_sent = s4_content(SECTION_PROMPTS["s4"].replace("{figures}", fig_txt), figs,
                                     folder / "paper.pdf", bool(cfg.get("figure_vision", True)))
    for f in figs:
        f["vision"] = f["label"] in vision_sent

    jobs = {
        "s2": SECTION_PROMPTS["s2"].replace("{similar}", similar_txt),
        "s3": SECTION_PROMPTS["s3"],
        "s4": s4_req,
        "s5": SECTION_PROMPTS["s5"],
        "s6": SECTION_PROMPTS["s6"].replace("{prior}", prior_txt),
        "s7": SECTION_PROMPTS["s7"],
        "s8": SECTION_PROMPTS["s8"].replace("{authors}", authors_txt),
    }
    labels = dict(SECTIONS)
    results, errors = {}, {}
    for k in jobs:
        pg.set(k, f"분석: {labels[k]}", "wait")
    if ext["refs"]:
        pg.set("refsum", "분석: 인용문헌 초록 한 줄 정리", "wait")

    def do(k):
        lab = labels.get(k, "인용문헌 초록 한 줄 정리")
        pg.set(k, f"분석: {lab}", "doing")
        try:
            if k == "refsum":
                r = claude.ask(k, REFSUM_PROMPT.replace("{refs}", refs_txt), schema=REFSUM_SCHEMA)
            else:
                r = claude.ask(k, jobs[k])
            results[k] = r
            pg.set(k, f"분석: {lab}", "done")
        except Exception as e:  # noqa: BLE001
            errors[k] = f"{type(e).__name__}: {e}"
            pg.set(k, f"분석: {lab}", "fail", str(e)[:200])

    keys = list(jobs) + (["refsum"] if ext["refs"] else [])
    with cf.ThreadPoolExecutor(max_workers=int(cfg.get("parallel_sections", 4))) as pool:
        list(pool.map(do, keys))

    # 인용문헌 한 줄 요약 붙이기
    if isinstance(results.get("refsum"), dict):
        m = {i["ref_no"]: i["summary"] for i in results["refsum"].get("items", [])}
        for r in ext["refs"]:
            r["summary"] = m.get(r["ref_no"], "")

    # 6) 저장
    pg.set("save", "보고서 만들기", "doing")
    j = ext["journal"]
    if j.get("missing"):
        add_pending(HERE / "data" / "pending_journals.json", j["name_for_pending"], j["iso"], j["issns"],
                    folder.name, j["missing"])
    sections = {k: v for k, v in results.items() if k != "refsum"}
    elapsed = round(time.time() - t_start, 1)
    analysis = {
        "created": time.strftime("%Y-%m-%d %H:%M"),
        "source_file": source_name or Path(pdf_path).name,
        "title": title, "extract": ex, "external": ext, "captions": pdf["captions"],
        "figures": figs, "figure_vision": bool(vision_sent),
        "sections": sections,
        "numcheck": numcheck.check_sections(sections, pdf["text"]),
        "errors": errors,
        "run": {"seconds": elapsed, "model": claude.model, "effort": cfg.get("effort"),
                "claude_usage": claude.usage, "claude_calls": claude.call_log, "cost_usd": claude.cost(),
                "lookup_calls": src.calls, "lookup_failures": src.failures},
    }
    (folder / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=1), encoding="utf-8")
    from render import render_report
    (folder / "report.html").write_text(render_report(analysis, folder), encoding="utf-8")
    pg.set("save", "보고서 만들기", "done", folder.name)
    return folder, analysis


# ====================================================================== 외부 조회 묶음
def _try(src, what, fn, default=None):
    """조회 한 건을 감싼다 — 예상 밖 응답으로 멈춰도 그 건만 '조회 실패' 로 남기고 다음으로 간다."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        src.failures.append({"service": "외부 조회", "what": what, "reason": f"{type(e).__name__}: {e}"[:300]})
        return default


def lookup_all(src, ex, cfg, note, paper_text=""):
    out = {"pmid": ex.get("pmid") or "", "pubmed": None, "openalex": None, "crossref": None, "s2": None,
           "journal": {}, "similar": [], "similar_note": "", "refs": [], "authors": []}
    doi = ex.get("doi", "")

    note("PubMed 에서 이 논문 찾는 중")
    if not out["pmid"]:
        out["pmid"] = _try(src, "PubMed 에서 이 논문 찾기", lambda: src.pubmed_find_pmid(doi, ex.get("title", ""))) or ""
    if out["pmid"]:
        out["pubmed"] = _try(src, "PubMed 논문 요약정보", lambda: (src.pubmed_summary([out["pmid"]]) or [None])[0])

    note("OpenAlex · Crossref · Semantic Scholar 인용수")
    w = _try(src, "OpenAlex 논문 정보", lambda: src.openalex_work(doi, out["pmid"]))
    if w:
        out["openalex"] = _try(src, "OpenAlex 논문 정보 읽기", lambda: _oa_work(w))
    if doi:
        out["crossref"] = _try(src, "Crossref DOI 정보", lambda: _cr_work(src.crossref(doi)))
        out["s2"] = _try(src, "Semantic Scholar 인용수", lambda: _s2_work(src.s2_citations(doi)))

    note("저널 등급 확인")
    out["journal"] = _try(src, "저널 등급", lambda: journal_block(src, ex, out), {}) or {}

    note("유사 주제 논문 찾는 중")
    _try(src, "유사 주제 논문", lambda: _similar(src, ex, cfg, out))

    note("Discussion 인용문헌의 초록 받아오는 중")
    out["refs"] = _try(src, "Discussion 인용문헌", lambda: _refs(src, ex, cfg, paper_text), []) or []

    note("저자 연구 활동 받아오는 중")
    oa_auth = _try(src, "OpenAlex 저자 목록 읽기", lambda: [
        {"name": (a.get("author") or {}).get("display_name", ""), "id": (a.get("author") or {}).get("id", ""),
         "position": a.get("author_position"), "is_corresponding": a.get("is_corresponding"),
         "institutions": [i.get("display_name") for i in a.get("institutions", [])]}
        for a in (w or {}).get("authorships", [])], []) or []
    for au in ex.get("authors", []):
        rec = _try(src, f"저자 {au.get('name', '')}", lambda au=au: _author(src, au, oa_auth, cfg))
        if rec is None:
            rec = {"name": au.get("name", ""), "role": au.get("role", ""), "affiliation_in_paper": au.get("affiliation", ""),
                   "openalex": None, "recent_works": None, "top_journals": None, "pubmed_count": None,
                   "notes": ["조회 중 예상 밖 응답 — 이 저자는 건너뜀"]}
        out["authors"].append(rec)
    return out


def _oa_work(w):
    src_ = ((w.get("primary_location") or {}).get("source") or {})
    return {"id": w.get("id"), "cited_by_count": w.get("cited_by_count"),
            "fwci": w.get("fwci"), "counts_by_year": (w.get("counts_by_year") or [])[:6],
            "citation_percentile": (w.get("citation_normalized_percentile") or {}).get("value"),
            "source_id": src_.get("id"), "source_name": src_.get("display_name"),
            "issns": src_.get("issn") or [], "publication_date": w.get("publication_date"),
            "type": w.get("type")}


def _cr_work(cr):
    if not cr:
        return None
    return {"is_referenced_by_count": cr.get("is-referenced-by-count"),
            "container": (cr.get("container-title") or [""])[0], "issn": cr.get("ISSN", []),
            "published": "-".join(str(x) for x in ((cr.get("published") or {}).get("date-parts") or [[""]])[0]),
            "reference_count": cr.get("reference-count"), "publisher": cr.get("publisher")}


def _s2_work(s2):
    if not s2:
        return None
    return {"citation_count": s2.get("citationCount"), "influential": s2.get("influentialCitationCount")}


def journal_block(src, ex, out):
    """저널 등급(표) + 최근 1년 원저 수(PubMed) + OpenAlex 2년 평균 피인용."""
    names = [ex.get("journal", "")]
    issns = []
    if out.get("openalex"):
        names.append(out["openalex"]["source_name"] or "")
        issns += out["openalex"]["issns"]
    if out.get("crossref"):
        names.append(out["crossref"]["container"])
        issns += out["crossref"]["issn"]
    if out.get("pubmed"):
        names += [out["pubmed"]["journal"], out["pubmed"]["journal_abbr"]]
        issns += [out["pubmed"]["issn"], out["pubmed"]["essn"]]
    iso = (out.get("pubmed") or {}).get("journal_abbr", "")
    rank = src.journal_rank([iso] + names, issns)
    j = {"rank": rank, "iso": iso, "issns": sorted({i for i in issns if i}),
         "name_for_pending": next((n for n in names if n), iso),
         # 등급표에 없으면 전부 미확정, 있는데 SJR 분위수가 비었으면 그것만 미확정 → 대기 목록에 쌓는다
         "missing": [f for f in ("tier", "q_sjr") if not (rank or {}).get(f)]}
    if iso:
        j["annual_articles"] = _try(src, "최근 1년 원저 발행 수", lambda: annual_articles(src, iso))
    if out.get("openalex") and out["openalex"]["source_id"]:
        s = src.openalex_source(out["openalex"]["source_id"])
        if s:
            st = s.get("summary_stats") or {}
            j["openalex"] = {"name": s.get("display_name"),
                             "two_yr_mean_citedness": st.get("2yr_mean_citedness"),
                             "is_in_doaj": s.get("is_in_doaj"), "host": s.get("host_organization_name"),
                             "type": s.get("type")}
    return j


def annual_articles(src, iso, year=None):
    """작년 한 해 그 저널의 원저(journal article·clinical trial, 편지·논평·사설·뉴스 제외) 수 — PubMed 건수."""
    year = year or (int(time.strftime("%Y")) - 1)
    q = (f'"{iso}"[Journal] AND {year}[dp] AND (journal article[pt] OR clinical trial[pt]) '
         f'NOT (letter[pt] OR comment[pt] OR editorial[pt] OR news[pt])')
    r = src.pubmed_search(q, 0, what=f"최근 1년 원저 수 ({iso} {year})")
    return {"year": year, "count": r["count"], "query": q} if r else None


def _similar(src, ex, cfg, out):
    n_sim = int(cfg.get("similar_papers", 10))
    ids = None
    if out["pmid"]:
        ids = src.pubmed_similar(out["pmid"], n_sim)
        out["similar_note"] = "PubMed 'Similar articles' (자동 유사도 순)"
    if not ids and ex.get("pubmed_query"):
        r = src.pubmed_search(ex["pubmed_query"], n_sim + 1, what="주제 검색")
        ids = [i for i in (r or {}).get("ids", []) if i != out["pmid"]][:n_sim] if r else None
        out["similar_note"] = f"PubMed 주제 검색: {ex['pubmed_query']}"
    if ids:
        out["similar"] = src.pubmed_summary(ids) or []
    elif ids is None:
        out["similar_note"] = "조회 실패"


# ---------------------------------------------------------------------- 인용 번호 펼치기
def expand_ref_no(ref_no):
    """'3–5' → ['3','4','5'], '1,2' → ['1','2'], '7' → ['7']. 저자-연도 인용은 그대로."""
    s = (ref_no or "").strip()
    if not re.fullmatch(r"[\d\s,–—\-]+", s) or not re.search(r"\d", s):
        return [s]
    out = []
    for part in re.split(r"\s*,\s*", s):
        m = re.fullmatch(r"(\d+)\s*[–—\-]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a <= b and b - a <= 30:
                out += [str(i) for i in range(a, b + 1)]
                continue
        if part.isdigit():
            out.append(part)
    return out or [s]


def _reference_list_entry(paper_text, n):
    """원문 References 에서 n 번 항목 글자를 찾는다 (못 찾으면 "")."""
    i = paper_text.rfind("References")
    if i < 0:
        return ""
    tail = paper_text[i:]
    m = re.search(rf"(?:^|\n){n}\s+(\S[^\n]*(?:\n(?!\d{{1,3}}\s+[A-Z])(?!\[p\.)[^\n]+){{0,4}})", tail)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:400] if m else ""


def expand_citations(cits, paper_text=""):
    """범위 인용을 번호별로 펼치고 같은 번호는 한 번만."""
    seen, out = set(), []
    for c in cits:
        nums = expand_ref_no(c.get("ref_no", ""))
        for k, n in enumerate(nums):
            if n in seen:
                continue
            seen.add(n)
            if len(nums) == 1:
                out.append(dict(c, ref_no=n))
            elif k == 0:
                out.append(dict(c, ref_no=n, range_from=c.get("ref_no", "")))
            else:
                txt = _reference_list_entry(paper_text, n)
                out.append({"ref_no": n, "reference_text": txt or f"(범위 인용 [{c.get('ref_no')}] 의 {n}번 — reference list 에서 찾지 못함)",
                            "title": "", "first_author": "", "journal_abbr": "", "year": "", "volume": "",
                            "first_page": "", "doi": "", "context": c.get("context", ""),
                            "range_from": c.get("ref_no", "")})
    # 펼친 번호는 원래 순서(번호 순)로 따로 나온 항목과 겹칠 수 있으니 한 번 더 번호로 정리
    return out


def _refs(src, ex, cfg, paper_text):
    cits = expand_citations(ex.get("discussion_citations", []), paper_text)
    refs = [dict(c, pmid="", abstract="", lookup="") for c in cits[: int(cfg.get("discussion_refs_max", 25))]]
    keyed = [{"key": i, "journal": r["journal_abbr"], "year": r["year"], "volume": r["volume"],
              "page": re.sub(r"[^\w].*$", "", r["first_page"] or ""), "author": r["first_author"]}
             for i, r in enumerate(refs) if not norm_doi(r["doi"]) and r["journal_abbr"]]
    cm = _try(src, "참고문헌 대조 (ECitMatch)", lambda: src.pubmed_citmatch(keyed) if keyed else {}, {}) or {}
    for i, r in enumerate(refs):
        def one(r=r, i=i):
            pmid = None
            if norm_doi(r["doi"]):
                pmid = src.pubmed_find_pmid(r["doi"], "")
            if not pmid:
                pmid = cm.get(str(i))
            if not pmid and r["title"]:
                pmid = src.pubmed_find_pmid("", r["title"])
            if not pmid and not r["title"] and r.get("range_from") and r["reference_text"] and not r["reference_text"].startswith("("):
                # 펼친 번호: reference list 글자의 앞부분(제목 포함)으로 한 번 찾아본다
                t = re.sub(r"^[^.]+\.\s*", "", r["reference_text"]).split(". ")[0]
                if len(t) > 25:
                    pmid = src.pubmed_find_pmid("", t)
            return pmid
        pmid = _try(src, f"인용문헌 [{r['ref_no']}] PMID", one)
        r["pmid"] = pmid or ""
        r["lookup"] = "PubMed" if pmid else "PubMed 에서 찾지 못함 (가이드라인·웹문서·미등재 문헌일 수 있음)"
    # 같은 PMID 가 두 번 나오면 뒤의 것은 PMID 를 비우고 표시
    seen = {}
    for r in refs:
        if r["pmid"] and r["pmid"] in seen:
            r["lookup"] = f"[{seen[r['pmid']]}] 과 같은 문헌으로 연결됨 — 확인 필요"
            r["pmid"] = ""
        elif r["pmid"]:
            seen[r["pmid"]] = r["ref_no"]
    abs_ = _try(src, "인용문헌 초록", lambda: src.pubmed_abstracts([r["pmid"] for r in refs if r["pmid"]]), {}) or {}
    for r in refs:
        if r["pmid"]:
            r["abstract"] = abs_.get(r["pmid"], "")
            if not r["abstract"]:
                r["lookup"] = "PubMed 에 초록 없음"
    return refs


def _author(src, au, oa_auth, cfg):
    rec = {"name": au["name"], "role": au["role"], "affiliation_in_paper": au["affiliation"],
           "openalex": None, "recent_works": None, "top_journals": None, "pubmed_count": None, "notes": []}
    match = _match_author(au["name"], oa_auth)
    prof = None
    if match and match["id"]:
        prof = _try(src, f"OpenAlex 저자 프로필 {au['name']}", lambda: src.openalex_author(match["id"]))
        rec["notes"].append("OpenAlex: 이 논문의 저자 목록에서 직접 연결 (성 + 이름 첫 글자 일치)")
    else:
        prof = _try(src, f"OpenAlex 저자 검색 {au['name']}",
                    lambda: src.openalex_author_search(au["name"], au["affiliation"], accept=lambda n: name_compatible(au["name"], n)))
        if prof:
            rec["notes"].append("OpenAlex: 이름 검색으로 찾음 (성 + 이름 첫 글자 일치) — 동명이인 가능성 확인 필요")
    if prof:
        st = prof.get("summary_stats") or {}
        rec["openalex"] = {"id": prof.get("id"), "display_name": prof.get("display_name"),
                           "works_count": prof.get("works_count"), "cited_by_count": prof.get("cited_by_count"),
                           "h_index": st.get("h_index"), "i10_index": st.get("i10_index"),
                           "institutions": [i.get("display_name") for i in (prof.get("last_known_institutions") or [])],
                           "topics": [t.get("display_name") for t in (prof.get("topics") or [])[:6]],
                           "orcid": prof.get("orcid")}
        rec["recent_works"] = _try(src, f"저자 최근 논문 {au['name']}",
                                   lambda: src.openalex_author_works(prof["id"], int(cfg.get("author_recent_works", 10))))
        rec["top_journals"] = _try(src, f"저자 주요 저널 {au['name']}", lambda: src.openalex_author_top_journals(prof["id"]))
    else:
        rec["notes"].append("OpenAlex 에서 성 + 이름 첫 글자가 맞는 저자를 찾지 못함")
    surname, given = _pubmed_author_name(au["name"])
    if surname:
        q = f'"{surname}, {given}"[FAU]'
        r = _try(src, f"PubMed 저자 논문 수 {au['name']}", lambda: src.pubmed_search(q, 0, what=f"PubMed 저자 논문 수: {au['name']}"))
        if r:
            rec["pubmed_count"] = {"query": q, "count": r["count"]}
    return rec


def _simple(n):
    return re.sub(r"[^a-z]", "", (n or "").lower())


def _name_parts(name):
    """(성, 이름 첫 글자). 'Kang, Jeehoon' 과 'Jeehoon Kang' 둘 다."""
    name = (name or "").strip()
    if "," in name:
        last, given = name.split(",", 1)
    else:
        ps = name.split()
        last, given = (ps[-1], " ".join(ps[:-1])) if len(ps) > 1 else (name, "")
    return _simple(last), _simple(given)[:1]


def name_compatible(a, b):
    """성이 같고 이름 첫 글자도 같아야 같은 사람으로 본다."""
    la, ga = _name_parts(a)
    lb, gb = _name_parts(b)
    return bool(la) and la == lb and bool(ga) and ga == gb


def _match_author(name, oa_auth):
    for a in oa_auth:
        if name_compatible(name, a["name"]):
            return a
    return None


def _pubmed_author_name(name):
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if len(parts) < 2:
        return "", ""
    return parts[-1], " ".join(parts[:-1])
