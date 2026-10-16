"""Journal Club — 이 컴퓨터에서만 켜는 웹앱.

  python3 server.py     →  브라우저에서 http://127.0.0.1:8811  (config.json 의 port)

이 컴퓨터 안(127.0.0.1)에서만 열리고 다른 컴퓨터에서는 접속할 수 없다.
분석 요청(/api/analyze)은 이 화면에서 온 것만 받는다 — 주소(Host)·출처(Origin)가 127.0.0.1/localhost 이고,
Content-Type 이 application/pdf 이며, 내용이 실제 PDF 로 시작해야 한다. 동시에 도는 분석은 기본 2건까지.
보고서 삭제·되돌리기·완전히 지우기도 같은 주소·출처 검사를 거친다. 삭제는 지우지 않고 reports/_trash/ 로 옮긴다.
"""
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import threading
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import analyzer

HERE = Path(__file__).resolve().parent
REPORTS = HERE / "reports"
TRASH = REPORTS / "_trash"
UPLOADS = HERE / "_uploads"
JOBS = {}
JOBS_LOCK = threading.Lock()
MAX_PDF = 60 * 1024 * 1024
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def _host_ok(value, port, allow_scheme=False):
    """'127.0.0.1:8811' 또는 'http://localhost:8811' 이 이 서버 자신을 가리키는가."""
    v = (value or "").strip().lower()
    if allow_scheme:
        if not v.startswith("http://"):
            return False
        v = v[len("http://"):]
    if "/" in v:
        return False
    host, _, p = v.rpartition(":") if ":" in v else (v, "", "")
    return host in LOCAL_HOSTS and p == str(port)


def load_cfg():
    return json.loads((HERE / "config.json").read_text(encoding="utf-8"))


def list_reports(base=REPORTS):
    out = []
    if not base.is_dir():
        return out
    for d in sorted(base.glob("*/"), reverse=True):
        if d.name.startswith("_"):      # _trash 같은 관리용 폴더는 목록에 넣지 않는다
            continue
        aj = d / "analysis.json"
        if not (d / "report.html").exists() or not aj.exists():
            continue
        try:
            a = json.loads(aj.read_text(encoding="utf-8"))
            ex = a.get("extract", {})
            out.append({"folder": d.name, "title": a.get("title"), "journal": ex.get("journal"),
                        "year": ex.get("year"), "created": a.get("created"),
                        "minutes": round(a["run"]["seconds"] / 60, 1), "cost": a["run"].get("cost_usd")})
        except Exception:  # noqa: BLE001
            out.append({"folder": d.name, "title": d.name})
    out.sort(key=lambda r: r.get("created") or "", reverse=True)
    return out


def _slug_dir(base, slug):
    """slug 가 base 바로 아래 폴더 이름과 정확히 같을 때만 그 폴더를 돌려준다. 아니면 None."""
    if not slug or slug in (".", "..") or slug.startswith("_") or any(c in slug for c in "/\\\0"):
        return None
    names = {d.name for d in base.iterdir() if d.is_dir()} if base.is_dir() else set()
    return base / slug if slug in names else None


def _free_name(base, name):
    """base 안에서 겹치지 않는 이름 — 겹치면 뒤에 ~2, ~3 …"""
    cand, i = name, 2
    while (base / cand).exists():
        cand, i = f"{name}~{i}", i + 1
    return cand


def trash_action(kind, slug):
    """kind: delete(보고서 → 휴지통) / restore(휴지통 → 보고서) / purge(휴지통에서 완전히 지움). (code, body)"""
    if kind == "delete":
        src = _slug_dir(REPORTS, slug)
        if src is None:
            return 404, {"error": "그런 보고서가 없습니다."}
        TRASH.mkdir(exist_ok=True)
        dst = TRASH / _free_name(TRASH, slug)
        shutil.move(str(src), str(dst))
        return 200, {"ok": True, "trash": dst.name}
    src = _slug_dir(TRASH, slug)
    if src is None:
        return 404, {"error": "휴지통에 그런 보고서가 없습니다."}
    if kind == "restore":
        name = re.sub(r"~\d+$", "", slug)
        if (REPORTS / name).exists():
            return 409, {"error": f"같은 이름의 보고서({name})가 이미 있어 되돌리지 못했습니다."}
        shutil.move(str(src), str(REPORTS / name))
        return 200, {"ok": True, "folder": name}
    if kind == "purge":
        shutil.rmtree(src)
        return 200, {"ok": True}
    return 404, {"error": "없는 주소"}


def start_job(pdf_bytes, filename):
    jid = uuid.uuid4().hex[:12]
    UPLOADS.mkdir(exist_ok=True)
    safe = re.sub(r"[^\w.\-가-힣]", "_", filename or "paper.pdf")[:120]
    path = UPLOADS / f"{jid}_{safe}"
    path.write_bytes(pdf_bytes)
    job = {"id": jid, "status": "running", "steps": [], "file": filename, "folder": None, "error": None}
    JOBS[jid] = job     # 호출하는 쪽(do_POST)이 JOBS_LOCK 을 잡고 자리 수를 확인한 뒤 부른다

    def work():
        try:
            folder, _ = analyzer.run(path, load_cfg(), source_name=filename, progress=lambda steps: job.update(steps=[dict(s) for s in steps]))
            job.update(status="done", folder=folder.name)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            job.update(status="error", error=f"{type(e).__name__}: {e}")
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    threading.Thread(target=work, daemon=True).start()
    return jid


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("  " + (fmt % args) + "\n")

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.unquote(u.path)
        if p in ("/", "/index.html"):
            return self._send(200, (HERE / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
        if p == "/api/reports":
            return self._send(200, list_reports())
        if p == "/api/trash":
            return self._send(200, list_reports(TRASH))
        m = re.match(r"^/api/job/([0-9a-f]+)$", p)
        if m:
            job = JOBS.get(m.group(1))
            return self._send(200, job) if job else self._send(404, {"error": "없는 작업"})
        if p.startswith("/reports/"):
            rel = p[len("/reports/"):]
            target = (REPORTS / rel).resolve()
            if REPORTS.resolve() not in target.parents or not target.is_file():
                return self._send(404, "없는 파일", "text/plain; charset=utf-8")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype == "application/json":
                ctype += "; charset=utf-8"
            extra = {}
            if "download" in urllib.parse.parse_qs(u.query):
                name = f"{target.parent.name}.html" if target.name == "report.html" else target.name
                extra["Content-Disposition"] = "attachment; filename*=UTF-8''" + urllib.parse.quote(name)
            return self._send(200, target.read_bytes(), ctype, extra)
        return self._send(404, "없는 주소", "text/plain; charset=utf-8")

    def _local_only(self):
        """주소(Host)·출처(Origin)가 이 컴퓨터의 이 화면인지. 아니면 403 을 보내고 False."""
        port = self.server.server_address[1]
        if not _host_ok(self.headers.get("Host"), port):
            self._send(403, {"error": "이 컴퓨터의 주소(127.0.0.1 / localhost)로 연 화면에서만 할 수 있습니다."})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and not _host_ok(origin, port, allow_scheme=True):
            self._send(403, {"error": "다른 웹페이지에서 온 요청은 받지 않습니다."})
            return False
        return True

    def do_DELETE(self):
        p = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        m = re.match(r"^/api/report/([^/]+)$", p)
        if not m:
            return self._send(404, {"error": "없는 주소"})
        if not self._local_only():
            return None
        return self._send(*trash_action("delete", m.group(1)))

    def do_POST(self):
        p = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        m = re.match(r"^/api/trash/([^/]+)/(restore|purge)$", p)
        if m:
            if not self._local_only():
                return None
            return self._send(*trash_action(m.group(2), m.group(1)))
        if self.path != "/api/analyze":
            return self._send(404, {"error": "없는 주소"})
        if not self._local_only():
            return None
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/pdf":
            return self._send(415, {"error": "PDF 파일만 받습니다 (Content-Type: application/pdf)."})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_PDF:
            return self._send(400, {"error": "PDF 가 비었거나 60MB 보다 큽니다."})
        data = self.rfile.read(n)
        if not data.startswith(b"%PDF"):
            return self._send(400, {"error": "PDF 파일이 아닙니다."})
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return self._send(500, {"error": "Claude 연결 키가 이 컴퓨터에 설정되어 있지 않습니다 (~/.zshenv 확인)."})
        name = urllib.parse.unquote(self.headers.get("X-Filename") or "paper.pdf")
        limit = int(load_cfg().get("max_concurrent_jobs", 2))
        with JOBS_LOCK:
            running = sum(1 for j in JOBS.values() if j["status"] == "running")
            if running >= limit:
                return self._send(429, {"error": f"이미 {running}편을 분석하고 있습니다. 끝난 뒤 다시 올려 주십시오."})
            jid = start_job(data, name)
        return self._send(200, {"job": jid})


def main():
    cfg = load_cfg()
    port = int(os.environ.get("JC_PORT") or cfg.get("port", 8811))
    REPORTS.mkdir(exist_ok=True)
    srv = None
    for p in range(port, port + 10):   # 다른 프로그램이 쓰고 있으면 다음 번호로
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), H)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        sys.exit(f"  {port}~{port + 9} 번이 모두 사용 중입니다. config.json 의 port 를 바꾸십시오.")
    print(f"\n  Journal Club 이 켜졌습니다 →  http://127.0.0.1:{port}\n  끄려면 이 창에서 Ctrl+C\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  꺼졌습니다.")


if __name__ == "__main__":
    main()
