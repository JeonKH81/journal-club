"""외부 조회 — PubMed E-utilities · OpenAlex · Crossref · Semantic Scholar · 저널 등급표.

모두 무료·키 불필요. 표준 라이브러리 urllib 만 쓴다.
조회가 실패하면 예외를 던지지 않고 None 을 돌려주며, 실패 사유는 self.failures 에 쌓는다.
숫자를 추정해서 채우지 않는다 — 받아온 값만 쓴다.
"""
import fcntl
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
OPENALEX = "https://api.openalex.org/"
CROSSREF = "https://api.crossref.org/works/"
S2 = "https://api.semanticscholar.org/graph/v1/paper/"


def norm_doi(doi):
    if not doi:
        return ""
    doi = doi.strip()
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", doi, flags=re.I)
    return doi.rstrip(".").lower()


def norm_journal(name):
    name = (name or "").lower()
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    name = re.sub(r"^the ", "", name.strip())
    return re.sub(r"\s+", " ", name).strip()


class Sources:
    # NCBI 간격은 작업끼리 함께 지킨다 (동시에 여러 편을 돌려도 합쳐서 초당 3회 이하)
    _pubmed_lock = threading.Lock()
    _last_pubmed = [0.0]

    def __init__(self, email, ranks_path=None, local_ranks_path=None):
        self.email = email
        self.calls = {"PubMed": 0, "OpenAlex": 0, "Crossref": 0, "Semantic Scholar": 0}
        self.failures = []          # [{"service","what","reason"}]
        self.ranks = load_ranks(ranks_path, local_ranks_path, self.failures)

    # ------------------------------------------------------------------ 공통
    def _get(self, service, url, params=None, what="", as_text=False, method="GET", retries=2):
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        headers = {"User-Agent": f"JournalClub/1.0 (mailto:{self.email})"}
        last = ""
        for attempt in range(retries + 1):
            if service == "PubMed":
                with self._pubmed_lock:   # NCBI: 키 없이 초당 3회 — 0.4초 간격
                    wait = 0.4 - (time.time() - Sources._last_pubmed[0])
                    if wait > 0:
                        time.sleep(wait)
                    Sources._last_pubmed[0] = time.time()
            self.calls[service] = self.calls.get(service, 0) + 1
            try:
                req = urllib.request.Request(url, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read().decode("utf-8", errors="replace")
                return body if as_text else json.loads(body)
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code == 404:
                    break
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
            except Exception as e:  # noqa: BLE001
                last = type(e).__name__ + (f": {e}" if str(e) else "")
                time.sleep(1.0 * (attempt + 1))
        self.failures.append({"service": service, "what": what or url[:120], "reason": last})
        return None

    # ------------------------------------------------------------------ PubMed
    def pubmed_search(self, term, retmax=20, what=""):
        d = self._get("PubMed", EUTILS + "esearch.fcgi",
                      {"db": "pubmed", "term": term, "retmode": "json", "retmax": retmax},
                      what=what or f"검색: {term[:60]}")
        if not d:
            return None
        r = d.get("esearchresult", {})
        return {"count": int(r.get("count", 0) or 0), "ids": r.get("idlist", [])}

    def pubmed_find_pmid(self, doi="", title=""):
        doi = norm_doi(doi)
        if doi:
            r = self.pubmed_search(f"{doi}[doi]", 2, what=f"DOI 로 PMID 찾기 ({doi})")
            if r and len(r["ids"]) == 1:
                return r["ids"][0]
        if title:
            t = re.sub(r"[^\w\s-]", " ", title)
            r = self.pubmed_search(f"{t}[ti]", 3, what="제목으로 PMID 찾기")
            if r and len(r["ids"]) == 1:
                return r["ids"][0]
        return None

    def pubmed_summary(self, pmids):
        pmids = [p for p in pmids if p]
        if not pmids:
            return []
        d = self._get("PubMed", EUTILS + "esummary.fcgi",
                      {"db": "pubmed", "id": ",".join(pmids), "retmode": "json"},
                      what=f"논문 요약정보 {len(pmids)}건")
        if not d:
            return None
        res = d.get("result", {})
        out = []
        for p in pmids:
            it = res.get(p)
            if not it:
                continue
            doi = ""
            for a in it.get("articleids", []):
                if a.get("idtype") == "doi":
                    doi = a.get("value", "")
            authors = [a.get("name", "") for a in it.get("authors", [])]
            out.append({
                "pmid": p,
                "title": it.get("title", "").rstrip("."),
                "journal": it.get("fulljournalname") or it.get("source", ""),
                "journal_abbr": it.get("source", ""),
                "year": (it.get("pubdate", "") or "")[:4],
                "authors": (", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")),
                "doi": doi,
                "pubtypes": it.get("pubtype", []),
                "issn": it.get("issn", ""), "essn": it.get("essn", ""),
            })
        return out

    def pubmed_abstracts(self, pmids):
        pmids = [p for p in pmids if p]
        if not pmids:
            return {}
        out = {}
        for i in range(0, len(pmids), 50):
            chunk = pmids[i:i + 50]
            x = self._get("PubMed", EUTILS + "efetch.fcgi",
                          {"db": "pubmed", "id": ",".join(chunk), "retmode": "xml", "rettype": "abstract"},
                          what=f"초록 {len(chunk)}건", as_text=True)
            if not x:
                continue
            try:
                root = ET.fromstring(x)
            except ET.ParseError as e:
                self.failures.append({"service": "PubMed", "what": "초록 읽기", "reason": str(e)})
                continue
            for art in root.iter("PubmedArticle"):
                pmid = art.findtext(".//PMID")
                parts = []
                for ab in art.iter("AbstractText"):
                    label = ab.get("Label")
                    txt = "".join(ab.itertext()).strip()
                    parts.append(f"{label}: {txt}" if label else txt)
                out[pmid] = " ".join(parts)
        return out

    def pubmed_similar(self, pmid, n=10):
        d = self._get("PubMed", EUTILS + "elink.fcgi",
                      {"dbfrom": "pubmed", "db": "pubmed", "id": pmid, "cmd": "neighbor_score",
                       "linkname": "pubmed_pubmed", "retmode": "json"},
                      what=f"유사 논문 (PMID {pmid})")
        if not d:
            return None
        ids = []
        for ls in d.get("linksets", []):
            for db in ls.get("linksetdbs", []):
                if db.get("linkname") == "pubmed_pubmed":
                    for l in db.get("links", []):
                        lid = l["id"] if isinstance(l, dict) else str(l)
                        if lid != str(pmid):
                            ids.append(lid)
        return ids[:n]

    def pubmed_citmatch(self, refs):
        """ECitMatch — 저널·연도·권·첫 페이지·제1저자로 PMID 찾기. refs: [{key, journal, year, volume, page, author}]"""
        lines = []
        for r in refs:
            if r.get("journal") and r.get("volume") and r.get("page"):
                lines.append("|".join([r["journal"], r.get("year", ""), r["volume"], r["page"],
                                       r.get("author", ""), str(r["key"]), ""]))
        if not lines:
            return {}
        body = self._get("PubMed", EUTILS + "ecitmatch.cgi",
                         {"db": "pubmed", "retmode": "xml", "bdata": "\r".join(lines)},
                         what=f"참고문헌 대조 {len(lines)}건", as_text=True)
        out = {}
        for ln in (body or "").splitlines():
            f = ln.split("|")
            if len(f) >= 7 and f[6].strip().isdigit():
                out[f[5]] = f[6].strip()
        return out

    # ------------------------------------------------------------------ OpenAlex
    def _oa(self, path, params=None, what=""):
        p = dict(params or {})
        p["mailto"] = self.email
        return self._get("OpenAlex", OPENALEX + path, p, what=what)

    def openalex_work(self, doi="", pmid=""):
        doi = norm_doi(doi)
        if doi:
            w = self._oa(f"works/doi:{doi}", what=f"논문 정보 (DOI {doi})")
            if w:
                return w
        if pmid:
            return self._oa(f"works/pmid:{pmid}", what=f"논문 정보 (PMID {pmid})")
        return None

    def openalex_source(self, source_id):
        sid = source_id.rsplit("/", 1)[-1]
        return self._oa(f"sources/{sid}", what="저널 정보")

    def openalex_author(self, author_id):
        aid = author_id.rsplit("/", 1)[-1]
        return self._oa(f"authors/{aid}", what=f"저자 프로필 {aid}")

    def openalex_author_search(self, name, institution="", accept=None):
        """accept(display_name) 이 참인 후보만 쓴다 — 맞는 이름이 없으면 첫 결과를 억지로 쓰지 않고 None."""
        d = self._oa("authors", {"search": name, "per-page": 5}, what=f"저자 검색: {name}")
        if not d or not d.get("results"):
            return None
        res = [a for a in d["results"] if accept is None or accept(a.get("display_name") or "")]
        if not res:
            return None
        if institution:
            inst = institution.lower()
            for a in res:
                for i in a.get("last_known_institutions") or []:
                    if i.get("display_name") and i["display_name"].lower() in inst or \
                       (i.get("display_name") or "").lower().split(" ")[0] in inst:
                        return a
        return res[0]

    def openalex_author_works(self, author_id, n=10):
        aid = author_id.rsplit("/", 1)[-1]
        d = self._oa("works", {"filter": f"author.id:{aid},type:article", "sort": "publication_date:desc",
                               "per-page": n,
                               "select": "title,publication_year,publication_date,primary_location,cited_by_count,doi,ids,authorships"},
                     what=f"저자 최근 논문 {aid}")
        if d is None:
            return None
        out = []
        for w in d.get("results", []):
            src = ((w.get("primary_location") or {}).get("source") or {})
            pos = ""
            for a in w.get("authorships", []):
                if ((a.get("author") or {}).get("id") or "").endswith(aid):
                    pos = a.get("author_position", "")
            out.append({"title": w.get("title"), "year": w.get("publication_year"),
                        "date": w.get("publication_date"), "journal": src.get("display_name", ""),
                        "cited_by": w.get("cited_by_count"), "doi": w.get("doi"),
                        "pmid": ((w.get("ids") or {}).get("pmid") or "").rsplit("/", 1)[-1],
                        "position": pos, "n_authors": len(w.get("authorships", []))})
        return out

    def openalex_author_top_journals(self, author_id, n=8):
        aid = author_id.rsplit("/", 1)[-1]
        d = self._oa("works", {"filter": f"author.id:{aid}", "group_by": "primary_location.source.id"},
                     what=f"저자 주요 저널 {aid}")
        if d is None:
            return None
        g = [x for x in d.get("group_by", []) if x.get("key_display_name") and x.get("key") != "unknown"]
        g.sort(key=lambda x: -x.get("count", 0))
        return [{"journal": x["key_display_name"], "count": x["count"]} for x in g[:n]]

    # ------------------------------------------------------------------ Crossref · S2
    def crossref(self, doi):
        doi = norm_doi(doi)
        if not doi:
            return None
        d = self._get("Crossref", CROSSREF + urllib.parse.quote(doi), {"mailto": self.email},
                      what=f"DOI 정보 ({doi})")
        return (d or {}).get("message")

    def s2_citations(self, doi):
        doi = norm_doi(doi)
        if not doi:
            return None
        d = self._get("Semantic Scholar", S2 + "DOI:" + urllib.parse.quote(doi),
                      {"fields": "citationCount,influentialCitationCount"}, what=f"인용수 ({doi})", retries=1)
        return d

    # ------------------------------------------------------------------ 저널 등급표
    def journal_rank(self, names, issns):
        return find_rank(self.ranks, names, issns)


# ====================================================================== 저널 등급표 (두 층)
# 원본: Apps/Weekly_pubmed/skill/data/journal_ranks.json — 읽기만 한다.
# 덧붙임: journal club/data/journal_ranks_local.json — tools/set_journal.py 가 기록. 같은 저널이면 로컬 값이 우선.
def rank_key(s):
    """Weekly_pubmed set_journal.py 의 norm() 과 같은 규칙 — PubMed ISO 약어가 열쇠."""
    return re.sub(r"[^a-z0-9 ()]", "", (s or "").lower().replace(".", "")).strip()


def load_ranks(base_path, local_path, failures=None):
    merged = {}
    for path, origin in ((base_path, "Weekly_pubmed 등급표"), (local_path, "Journal Club 덧붙임 등급표")):
        if not path or not Path(path).exists():
            continue
        try:
            js = json.loads(Path(path).read_text(encoding="utf-8")).get("journals", {})
        except Exception as e:  # noqa: BLE001
            if failures is not None:
                failures.append({"service": "저널 등급표", "what": str(path), "reason": str(e)})
            continue
        for k, v in js.items():
            cur = dict(merged.get(k, {}))
            cur.update(v)
            cur["_origin"] = origin if k not in merged else merged[k]["_origin"] + " + " + origin
            merged[k] = cur
    return merged


def _issn_set(xs):
    return {i.replace("-", "").upper() for i in xs if i}


def find_rank(ranks, names, issns):
    want = _issn_set(issns)
    if want:
        for key, v in ranks.items():
            if want & _issn_set(v.get("issns", [])):
                return dict(v, key=key, matched="ISSN")
    for n in names:
        if not n:
            continue
        rk, nn = rank_key(n), norm_journal(n)
        for key, v in ranks.items():
            if rk == key or nn == norm_journal(key) or nn == norm_journal(v.get("name", "")):
                return dict(v, key=key, matched="이름")
    return None


def add_pending(path, journal, iso, issns, slug, missing):
    """등급표에 없거나 SJR 분위수가 빈 저널을 대기 목록에 쌓는다 (같은 저널은 한 줄로 합침).
    동시에 두 편이 끝나도 덮어쓰지 않도록 잠금 파일을 잡고 읽고-고치고-쓴다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _PENDING_LOCK, open(str(path) + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            _add_pending(path, journal, iso, issns, slug, missing)
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


_PENDING_LOCK = threading.Lock()


def _add_pending(path, journal, iso, issns, slug, missing):
    data = {"journals": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    key = rank_key(iso or journal)
    for e in data["journals"]:
        if e.get("key") == key:
            if slug not in e["papers"]:
                e["papers"].append(slug)
            e["issns"] = sorted(set(e.get("issns", []) + [i for i in issns if i]))
            e["missing"] = missing
            break
    else:
        data["journals"].append({"key": key, "journal": journal, "iso": iso,
                                 "issns": sorted({i for i in issns if i}), "missing": missing,
                                 "papers": [slug], "added": time.strftime("%Y-%m-%d")})
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
