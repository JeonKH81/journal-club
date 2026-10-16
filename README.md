# Journal Club — More than one from a paper

**Local web app that turns a medical-paper PDF into an 8-part Korean journal-club report** — metadata & journal quality, introduction & research question, methods, results (with figures/tables cropped from the PDF and read by Claude), discussion with cited-abstract summaries, message vs. prior work, limitations, and first/corresponding-author activity. Runs only on your machine; analysis uses the Claude API with your own key, and lookups use PubMed, OpenAlex, Crossref and Semantic Scholar (all free, no keys).

## 준비물

- Python 3.11 이상, 그리고 `pip install pymupdf anthropic`
- 환경변수 `ANTHROPIC_API_KEY` (Claude 자동통로 키). 코드나 설정 파일에는 절대 적지 않는다.
- 한 편 분석에 약 $0.6 (Sonnet 기준). `config.json` 에서 모델·세부 설정을 바꿀 수 있다.
- 선택: `config.json` 의 `contact_email` 에 본인 이메일을 넣으면 OpenAlex 조회가 빨라진다.

논문 PDF 를 올리면 8개 항목으로 분석한 한국어 보고서(전문용어는 영어 그대로)를 만들어 주는 화면입니다.
이 컴퓨터 안에서만 열립니다.

## 켜는 법

터미널에서 이 폴더로 와서

```
python3 server.py
```

를 입력한 뒤 브라우저에서 **http://127.0.0.1:8811** 을 엽니다. 끌 때는 그 터미널 창에서 `Ctrl+C`.
(8811 번을 다른 프로그램이 쓰고 있으면 8812, 8813 … 으로 자동으로 옮겨 열리고, 실제 주소가 터미널에 찍힙니다.)

## 쓰는 법

1. 첫 화면에 PDF 를 끌어다 놓거나 눌러서 고릅니다.
2. 어느 단계를 하고 있는지 화면에 차례로 보입니다. 한 편에 **2~3분** 걸립니다.
3. 끝나면 "보고서 열기" / "HTML 로 내려받기". 인쇄 버튼으로 PDF 로도 저장할 수 있습니다.
4. 지난 보고서는 첫 화면 아래 목록에 남습니다.

보고서는 `reports/{날짜}_{제목}/` 폴더에 쌓입니다 — `report.html`(보고서), `paper.pdf`(올린 원문 사본),
`paper_text.txt`(PDF 에서 읽어낸 글자), `analysis.json`(분석 원자료·사용량 기록), `figures/`(잘라낸 Figure·Table 그림).

## 보고서 8항목

1. 메타데이터 — 서지정보, 저널 Quality, 피인용 수
2. Introduction — 배경, 연구질문(PICO), 유사 주제 논문 목록
3. Methods — 설계, inclusion/exclusion, endpoint, 통계 방법 해설
4. Results — 핵심 결과, Figure·Table 하나씩 해설 (잘라낸 그림을 해설 옆에 나란히, 누르면 크게)
5. Discussion — 문단별 핵심, Discussion 인용 문헌의 초록 한 줄 정리
6. 과거 연구와의 차이, 핵심 메시지
7. Limitation — 저자가 쓴 것 + 쓰지 않은 것, 토론 질문
8. 제1저자·교신저자의 연구 활동

## 비용

분석은 Claude(Anthropic)가 합니다. 이 컴퓨터에 저장된 키로 요금이 청구됩니다.
- 기본(Sonnet 5.5, 분석 깊이 medium): **논문 1편당 약 0.6~0.8 달러** (9쪽 Lancet 논문 실측 0.59~0.78 달러).
- 같은 논문을 5분 안에 다시 돌리면 본문 읽기 비용이 거의 들지 않습니다.
- `config.json` 에서 `"model": "claude-opus-5-5"` 로 바꾸면 더 깊게 분석하지만 약 2배 비쌉니다.
  `"effort"` 를 `"high"` 로 올리면 더 꼼꼼해지고 느려집니다.
- 각 보고서 맨 아래 "이 보고서를 만든 과정" 에 실제 요청 횟수·시간·예상 비용이 적혀 있습니다.

## 어디서 무엇을 받아오나 (모두 무료)

| 자료 | 출처 |
|---|---|
| PMID, 유사 논문(PubMed "Similar articles"), 인용 문헌 초록, 저자 이름 검색 건수 | PubMed |
| 저널의 최근 1년 원저 발행 수(편지·논평·사설·뉴스 제외) | PubMed |
| 피인용 수, 저널 2년 평균 피인용, 저자 프로필(논문 수·h-index·소속·최근 논문·주요 저널) | OpenAlex |
| 피인용 수(보조), 저널 ISSN | Crossref |
| 피인용 수(보조) | Semantic Scholar |
| 저널 등급(tier · SJR 분위수 · JCR 분위수/IF 근사치) | `Apps/Weekly_pubmed/skill/data/journal_ranks.json` (읽기만) + 이 폴더의 `data/journal_ranks_local.json` |

조회가 실패하면 보고서 해당 칸에 "조회 실패 — 사유" 가 적히고 나머지는 계속 진행됩니다. 숫자를 짐작해서 채우지 않습니다.
Impact Factor 는 공식(Clarivate) 수치가 아닌 근사치이므로 인용하지 마십시오.

## 저널 등급 미확정

등급표에 없는 저널이나 SJR 분위수가 비어 있는 저널은 보고서에 **"미확정"** 으로 나오고 `data/pending_journals.json` 에 쌓입니다.
**미확정 저널은 Claude Code 대화창에서 "미확정 저널 등급 채워줘" 라고 하면 Consensus 로 확인해 채웁니다.**
(대화창은 `python3 tools/set_journal.py --list-pending` 으로 목록을 보고, Consensus 의 `sjr_max` 필터로 SJR 분위수를 확인한 뒤
`python3 tools/set_journal.py --iso "약어" --q-sjr N --tier 등급 ...` 으로 `data/journal_ranks_local.json` 에 적습니다.
Weekly_pubmed 의 원본 등급표는 건드리지 않으며, 같은 저널이면 이 폴더의 값이 우선합니다.)

## 알아둘 점

- 스캔 이미지로만 된 PDF 는 글자를 읽지 못해 분석하지 않습니다.
- Figure·Table 은 PDF 에서 잘라내 항목 4 해설 옆에 붙입니다. `config.json` 의 `"figure_vision": true`(기본)이면
  그 그림을 Claude 에게 보여주어 곡선 모양·forest plot·표 값을 직접 읽게 합니다(편당 약 0.03~0.05 달러 추가).
  그림에서 읽은 문장에는 "그림을 직접 읽음" 표시가 붙습니다. 자동으로 못 잘라낸 그림은 그 쪽 전체가 들어가고 그렇게 표시됩니다.
- 해설 속 숫자(소수·백분율)는 원문 글자와 자동 대조합니다. 원문에 없는 숫자는 "⚠ 원문 미확인",
  원문 숫자를 "약 … 정도로 보이는" 식으로 얼버무린 것은 "⚠ 맥락 확인" 으로 표시됩니다. 계산식이 붙은 직접 계산값은 표시하지 않습니다.
  숫자의 존재만 보므로, 맞는 숫자를 틀린 대상에 붙인 것을 모두 잡지는 못합니다.
- 분석 요청은 이 컴퓨터에서 연 화면에서만 받습니다. 동시에 2편까지 분석합니다(`max_concurrent_jobs`).
- 이미 만든 보고서를 Claude 재분석 없이 다시 그리려면 `python3 tools/rerender.py reports/폴더이름`
  (항목 4 만 다시 묻기: `--redo s4`, 소액).
- Claude 의 해석은 틀릴 수 있습니다. 보고서의 `p.N` 표시로 원문을 확인하십시오.
- 저자 프로필은 OpenAlex 가 같은 사람의 논문을 여러 프로필로 나누거나 동명이인을 섞는 경우가 있습니다. 보고서가 의심스러우면 경고합니다.
