"""A dependency-free local HTTP service and single-page console.

Run with an explicit state path so no generated state is written into the
materials bundle::

    python3 -m reading_review --materials materials --state var/state.json serve
"""
from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import analysis, reporting
from .bundle import BundleError
from .domain import DomainError, Workspace

STATIC = Path(__file__).with_name("static")
_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    workspace: Workspace

    def log_message(self, fmt, *args):  # quieter console
        print("  " + fmt % args)

    # ----------------------------------------------------------------- plumbing
    def _send(self, payload, status=HTTPStatus.OK, content_type="application/json"):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        else:
            body = payload
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message, status=HTTPStatus.BAD_REQUEST):
        self._send({"error": str(message)}, status)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    # ---------------------------------------------------------------------- GET
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        try:
            if path in {"/", "/index.html"}:
                return self._send((STATIC / "index.html").read_bytes(),
                                  content_type="text/html")
            if path.startswith("/api/"):
                return self._api_get(path[len("/api"):], query)
            return self._error("未找到", HTTPStatus.NOT_FOUND)
        except (DomainError, BundleError) as exc:
            return self._error(exc, HTTPStatus.CONFLICT if isinstance(exc, DomainError) else 400)
        except Exception as exc:  # pragma: no cover - keep server alive
            return self._error(f"内部错误：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def _api_get(self, path, query):
        w = self.workspace
        as_of = query.get("as_of") or None
        parts = [p for p in path.split("/") if p]
        if parts == ["overview"]:
            return self._send({
                "source": w.bundle.receipt(),
                "authorities": w.authority_register(),
                "as_of": w.default_as_of(),
                "events": w.events,
                "schools": w.bundle.schools,
                "rubrics": w.bundle.rubrics,
                "works": w.bundle.works,
                "teachers": w.bundle.teachers,
                "publications": w.publications,
                "assignments": w.assignments,
            })
        if parts[:1] == ["works"] and len(parts) >= 2:
            work_id = parts[1]
            if len(parts) == 2:
                return self._send(analysis.work_states(w, work_id, as_of))
            if parts[2] == "dimensions":
                return self._send(analysis.work_states(w, work_id, as_of))
            if parts[2] == "plan":
                return self._send(analysis.plan_assignments(
                    w, work_id, query.get("dimension", "evidence"), as_of))
        if parts == ["triage"]:
            return self._send(analysis.review_priorities(
                w, query.get("dimension") or None, as_of,
                int(query["limit"]) if query.get("limit") else None))
        if parts == ["compare"]:
            schools = [s for s in query.get("schools", "").split(",") if s]
            return self._send(analysis.compare_schools(
                w, schools, query["dimension"], query.get("rubric") or None, as_of))
        if parts == ["trace"]:
            schools = [s for s in query.get("schools", "").split(",") if s]
            return self._send(reporting.trace_comparison(
                w, schools[0], schools[1], query["dimension"],
                query.get("rubric") or None, as_of))
        if parts == ["scale"]:
            return self._send(analysis.scale_fit(
                w, query["dimension"], query.get("rubric", "R1"), as_of))
        if parts == ["variance"]:
            return self._send(analysis.variance_explanation(
                w, query["dimension"], as_of))
        if parts == ["equating"]:
            return self._send(analysis.equating(w, query["dimension"], as_of))
        if parts == ["withdrawals"]:
            return self._send({
                "approvals": [w.approval_status(a["id"], as_of or w.default_as_of())
                              for a in w.approvals],
                "qualification_withdrawals": w.withdrawal_events,
                "teachers": w.bundle.teachers,
            })
        if parts[:1] == ["publications"] and len(parts) == 3 and parts[2] == "audit":
            return self._send(analysis.audit_publication(w, parts[1], as_of))
        if parts == ["impact"]:
            schools = [s for s in query.get("schools", "").split(",") if s]
            return self._send(reporting.action_impact(
                w, schools[0], schools[1], query["dimension"],
                query.get("rubric") or None, as_of))
        return self._error("未知接口", HTTPStatus.NOT_FOUND)

    # --------------------------------------------------------------------- POST
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            with _LOCK:
                result = self._api_post(path[len("/api"):], body)
            return self._send(result)
        except (DomainError, BundleError) as exc:
            return self._error(exc, HTTPStatus.CONFLICT)
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            return self._error(f"请求无效：{exc}", HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover
            return self._error(f"内部错误：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def _api_post(self, path, body):
        w = self.workspace
        parts = [p for p in path.split("/") if p]
        actor = body.get("actor", "review-lead")
        if parts == ["assignments"]:
            return w.offer_assignment(body["work_id"], body["teacher_id"],
                                      body.get("on"), actor=actor,
                                      reason=body.get("reason", ""))
        if parts == ["seen"]:
            return w.add_seen(body["teacher_id"], body["work_id"], body["on"], actor=actor)
        if parts == ["scores"]:
            return w.add_score(body["work_id"], body["teacher_id"], body["rubric_id"],
                               body["dimension"], body["score"], body["on"],
                               body.get("kind", "initial"), actor=actor)
        if parts == ["approvals"]:
            return w.add_calibration_approval(
                body["from_rubric"], body["to_rubric"], body["dimension"],
                body["teacher_ids"], body["anchor_ids"], body["approved_on"],
                body.get("expires_on"), actor=actor)
        if parts == ["withdrawals"]:
            return analysis.withdrawal_impact(
                w, body["approval_id"], body["teacher_id"], body["on"],
                body.get("reason", ""), apply=True)
        if parts == ["qualifications"]:
            return w.withdraw_teacher_qualification(
                body["approval_id"], body["teacher_id"], body["on"],
                body.get("reason", ""), actor=actor)
        if parts[:1] == ["assignments"] and len(parts) == 3:
            assignment_id, action = parts[1], parts[2]
            if action == "accept":
                return w.accept_assignment(assignment_id, body.get("on"), actor=actor)
            if action == "return":
                return w.return_assignment(assignment_id, body.get("on"),
                                           body.get("reason", ""), actor=actor)
            if action == "complete":
                return w.complete_assignment(assignment_id, body["decisions"],
                                             body.get("on"), actor=actor)
        if parts[:1] == ["adjudications"] and len(parts) == 3 and parts[2] == "supersede":
            return w.supersede_adjudication(
                parts[1], body["decision"], body.get("score"), body["on"],
                body["rationale"], actor=actor,
                calibration_approval_ids=body.get("calibration_approval_ids"))
        if parts == ["publications"]:
            return w.record_publication(
                body["school_ids"], body["dimension"], body["score_ids"],
                body["approval_ids"], body["on"], actor=actor)
        raise DomainError("未知操作")


def serve(workspace: Workspace, host: str = "127.0.0.1", port: int = 8765) -> None:
    handler = type("BoundHandler", (Handler,), {"workspace": workspace})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"阅读作品复核控制台：http://{host}:{port}/")
    print(f"原始资料指纹：{workspace.bundle.sha256}")
    if workspace.state_path:
        print(f"状态文件：{workspace.state_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
