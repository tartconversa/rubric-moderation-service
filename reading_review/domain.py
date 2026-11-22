"""Business objects, authority rules and the append-only action log.

The source bundle is never edited.  All operational changes (approvals,
assignments, adjudications, new scores, withdrawals) are events appended to a
separate JSON state file and replayed deterministically.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
from pathlib import Path
from typing import Any, Iterable

from .bundle import Bundle


class DomainError(ValueError):
    """A request violates the business rules."""


def _date(value: str, field: str) -> str:
    if not value:
        raise DomainError(f"{field} 必须提供日期")
    try:
        return _dt.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise DomainError(f"{field} 日期格式无效：{value}") from exc


def _next_id(prefix: str, records: Iterable[dict[str, Any]]) -> str:
    highest = 0
    for record in records:
        rid = record.get("id", "")
        if rid.startswith(prefix) and rid[len(prefix):].isdigit():
            highest = max(highest, int(rid[len(prefix):]))
    return f"{prefix}{highest + 1}"


class Workspace:
    """Bundle plus replayed operational state."""

    def __init__(self, bundle: Bundle, events: list[dict[str, Any]] | None = None,
                 state_path: str | Path | None = None):
        self.bundle = bundle
        self.state_path = Path(state_path) if state_path else None
        self.events: list[dict[str, Any]] = []
        self._reset()
        for event in events or []:
            self._append(event, persist=False)

    # ------------------------------------------------------------------ setup
    def _reset(self) -> None:
        b = self.bundle
        self.approvals = copy.deepcopy(b.calibration_approvals)
        self.assignments = copy.deepcopy(b.assignments)
        self.adjudications = copy.deepcopy(b.adjudications)
        self.publications = copy.deepcopy(b.publications)
        self.scores = copy.deepcopy(b.scores)
        self.seen = copy.deepcopy(b.seen)
        self.withdrawal_events: list[dict[str, Any]] = []
        for record in self.adjudications:
            record.setdefault("status", "active")
            record.setdefault("superseded_by", None)
            record.setdefault("superseded_on", None)
            record.setdefault("basis", self._default_basis(record))
            record.setdefault("raw_score_ids", self._raw_score_ids_for(record))
            record.setdefault("reviewer_id", self._reviewer_for(record))
        for record in self.assignments:
            record.setdefault("offered_on", record.get("accepted_on"))
            record.setdefault("history", [])

    def _default_basis(self, adjudication: dict[str, Any]) -> dict[str, Any]:
        assignment = next((a for a in self.assignments
                           if a["id"] == adjudication["assignment_id"]), None)
        work_id = assignment["work_id"] if assignment else None
        raw_ids = self._raw_score_ids_for(adjudication)
        return {
            "kind": "source_record",
            "rationale": "原始复核记录未附理由；保留原记录、任务和原始评分引用",
            "raw_score_ids": raw_ids,
            "work_id": work_id,
            "dimension": adjudication["dimension"],
        }

    def _raw_score_ids_for(self, adjudication: dict[str, Any]) -> list[str]:
        assignment = next((a for a in self.assignments
                           if a["id"] == adjudication["assignment_id"]), None)
        if not assignment:
            return []
        return sorted(
            s["id"] for s in self.scores
            if s["work_id"] == assignment["work_id"]
            and s["dimension"] == adjudication["dimension"]
            and s["kind"] == "initial"
        )

    def _reviewer_for(self, adjudication: dict[str, Any]) -> str | None:
        assignment = next((a for a in self.assignments
                           if a["id"] == adjudication["assignment_id"]), None)
        return assignment["teacher_id"] if assignment else None

    # ------------------------------------------------------------------ event
    def _append(self, event: dict[str, Any], persist: bool = True) -> None:
        event = copy.deepcopy(event)
        event["seq"] = len(self.events) + 1
        event.setdefault("actor", "unknown")
        event.setdefault("on", self.latest_bundle_date())
        self.events.append(event)
        self._apply(event)
        if persist and self.state_path:
            self.save()

    def _apply(self, event: dict[str, Any]) -> None:
        kind = event["type"]
        payload = event["payload"]
        if kind == "approval.added":
            record = copy.deepcopy(payload)
            record.setdefault("withdrawn_on", None)
            record.setdefault("expires_on", None)
            record.setdefault("approval_source", "runtime")
            self.approvals.append(record)
        elif kind == "approval.withdrawn":
            record = self._approval(payload["approval_id"])
            existing = record.get("withdrawn_on")
            on = payload["on"]
            if existing is None or on < existing:
                record["withdrawn_on"] = on
            record["withdrawal_reason"] = payload.get("reason", "")
        elif kind == "qualification.withdrawn":
            self.withdrawal_events.append(copy.deepcopy(payload))
        elif kind == "assignment.offered":
            self.assignments.append({
                "id": payload["assignment_id"],
                "work_id": payload["work_id"],
                "teacher_id": payload["teacher_id"],
                "accepted_on": None,
                "offered_on": payload["on"],
                "status": "offered",
                "reason": payload.get("reason", ""),
                "history": [{"on": payload["on"], "status": "offered",
                             "actor": event["actor"]}],
            })
        elif kind == "assignment.accepted":
            record = self._assignment(payload["assignment_id"])
            record["status"] = "accepted"
            record["accepted_on"] = payload["on"]
            record.setdefault("history", []).append(
                {"on": payload["on"], "status": "accepted", "actor": event["actor"]})
        elif kind == "assignment.returned":
            record = self._assignment(payload["assignment_id"])
            record["status"] = "returned"
            record.setdefault("history", []).append(
                {"on": payload["on"], "status": "returned", "actor": event["actor"],
                 "reason": payload.get("reason", "")})
            record["returned_on"] = payload["on"]
            record["return_reason"] = payload.get("reason", "")
        elif kind == "assignment.completed":
            record = self._assignment(payload["assignment_id"])
            record["status"] = "completed"
            record["completed_on"] = payload["on"]
            record.setdefault("history", []).append(
                {"on": payload["on"], "status": "completed", "actor": event["actor"]})
        elif kind == "assignment.revoked":
            record = self._assignment(payload["assignment_id"])
            record["status"] = "revoked"
            record["revoked_on"] = payload["on"]
            record["revoked_reason"] = payload.get("reason", "")
            record["revoked_by"] = event["actor"]
            record.setdefault("history", []).append(
                {"on": payload["on"], "status": "revoked", "actor": event["actor"],
                 "reason": payload.get("reason", "")})
        elif kind == "adjudication.recorded":
            record = copy.deepcopy(payload)
            record.setdefault("status", "active")
            record.setdefault("superseded_by", None)
            record.setdefault("superseded_on", None)
            self.adjudications.append(record)
        elif kind == "adjudication.superseded":
            old = self._adjudication(payload["superseded_id"])
            old["status"] = "superseded"
            old["superseded_by"] = payload["new_id"]
            old["superseded_on"] = payload["on"]
            old["supersede_reason"] = payload.get("reason", "")
            new = copy.deepcopy(payload["new_record"])
            new.setdefault("status", "active")
            new.setdefault("superseded_by", None)
            new.setdefault("superseded_on", None)
            self.adjudications.append(new)
        elif kind == "score.added":
            self.scores.append(copy.deepcopy(payload))
        elif kind == "seen.added":
            self.seen.append(copy.deepcopy(payload))
        elif kind == "publication.recorded":
            self.publications.append(copy.deepcopy(payload))
        else:  # pragma: no cover - defensive
            raise DomainError(f"未知事件类型：{kind}")

    def save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"source_sha256": self.bundle.sha256, "events": self.events}
        self.state_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, bundle: Bundle, state_path: str | Path) -> "Workspace":
        path = Path(state_path)
        events: list[dict[str, Any]] = []
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("source_sha256") not in (None, bundle.sha256):
                raise DomainError("状态文件与当前原始资料的指纹不一致")
            events = raw.get("events", [])
        return cls(bundle, events, state_path=path)

    # ------------------------------------------------------------------ dates
    def latest_bundle_date(self) -> str:
        dates = ["2026-01-01"]
        for table in (self.bundle.scores, self.bundle.adjudications,
                      self.bundle.assignments, self.bundle.publications):
            for row in table:
                for key in ("on", "accepted_on"):
                    if row.get(key):
                        dates.append(row[key])
        for row in self.bundle.calibration_approvals:
            for key in ("approved_on", "withdrawn_on"):
                if row.get(key):
                    dates.append(row[key])
        for row in self.bundle.works:
            if row.get("on"):
                dates.append(row["on"])
        for row in self.events:
            if row.get("on"):
                dates.append(row["on"])
        return max(dates)

    def default_as_of(self) -> str:
        dates = [self.latest_bundle_date()]
        dates.extend(e["on"] for e in self.events if e.get("on"))
        return max(dates)

    # ----------------------------------------------------------------- lookup
    def _approval(self, approval_id: str) -> dict[str, Any]:
        try:
            return next(a for a in self.approvals if a["id"] == approval_id)
        except StopIteration as exc:
            raise DomainError(f"未知校准联系：{approval_id}") from exc

    def _assignment(self, assignment_id: str) -> dict[str, Any]:
        try:
            return next(a for a in self.assignments if a["id"] == assignment_id)
        except StopIteration as exc:
            raise DomainError(f"未知复核任务：{assignment_id}") from exc

    def _adjudication(self, adjudication_id: str) -> dict[str, Any]:
        try:
            return next(a for a in self.adjudications if a["id"] == adjudication_id)
        except StopIteration as exc:
            raise DomainError(f"未知裁决：{adjudication_id}") from exc

    def work(self, work_id: str) -> dict[str, Any]:
        return self.bundle.get("works", work_id)

    @property
    def scores_by_id(self) -> dict[str, dict[str, Any]]:
        return {s["id"]: s for s in self.scores}

    def teacher(self, teacher_id: str) -> dict[str, Any]:
        return self.bundle.get("teachers", teacher_id)

    def person(self, person_id: str) -> dict[str, Any]:
        return self.bundle.get("people", person_id)

    def school_of_work(self, work_id: str) -> str:
        return self.person(self.work(work_id)["person_id"])["school_id"]

    def rubric(self, rubric_id: str) -> dict[str, Any]:
        return self.bundle.get("rubrics", rubric_id)

    # --------------------------------------------------------------- authority
    def initial_scores(self, work_id: str | None = None,
                       dimension: str | None = None) -> list[dict[str, Any]]:
        return [s for s in self.scores if s["kind"] == "initial"
                and (work_id is None or s["work_id"] == work_id)
                and (dimension is None or s["dimension"] == dimension)]

    def raw_scores_for(self, work_id: str, dimension: str) -> list[dict[str, Any]]:
        return self.initial_scores(work_id, dimension)

    def active_adjudications(self, work_id: str) -> dict[str, dict[str, Any]]:
        assignment_ids = {a["id"] for a in self.assignments if a["work_id"] == work_id}
        result: dict[str, dict[str, Any]] = {}
        for record in self.adjudications:
            if record["assignment_id"] not in assignment_ids:
                continue
            if record.get("status", "active") != "active":
                continue
            current = result.get(record["dimension"])
            if current is None or (record["on"], record["id"]) >= (current["on"], current["id"]):
                result[record["dimension"]] = record
        return result

    def adjudication_history(self, work_id: str) -> list[dict[str, Any]]:
        assignment_ids = {a["id"] for a in self.assignments if a["work_id"] == work_id}
        return [r for r in self.adjudications if r["assignment_id"] in assignment_ids]

    def assignments_for_work(self, work_id: str) -> list[dict[str, Any]]:
        return [a for a in self.assignments if a["work_id"] == work_id]

    def assignment_load(self, teacher_id: str) -> int:
        """Accepted and completed reviews consume a teacher's weekly capacity.

        Completed reviews are deliberately not released by returning another
        assignment; only the returned assignment's own reservation is lifted.
        """
        return sum(1 for a in self.assignments if a["teacher_id"] == teacher_id
                   and a["status"] in {"accepted", "completed"})

    def teacher_capacity(self, teacher_id: str) -> int:
        return int(self.teacher(teacher_id).get("capacity", 0))

    def teacher_has_seen(self, teacher_id: str, work_id: str) -> bool:
        return any(v["teacher_id"] == teacher_id and v["work_id"] == work_id
                   for v in self.seen)

    def teacher_initial_scorer(self, teacher_id: str, work_id: str) -> bool:
        return any(s["teacher_id"] == teacher_id and s["work_id"] == work_id
                   and s["kind"] == "initial" for s in self.scores)

    def teacher_restrictions(self, as_of: str | None = None) -> dict[str, set[str]]:
        as_of = as_of or self.default_as_of()
        restricted: dict[str, set[str]] = {}
        for event in self.withdrawal_events:
            if event.get("on", "9999-12-31") <= as_of:
                restricted.setdefault(event["teacher_id"], set()).add(event["dimension"])
        return restricted

    def teacher_blocked(self, teacher_id: str, as_of: str | None = None,
                        dimension: str | None = None) -> bool:
        restrictions = self.teacher_restrictions(as_of).get(teacher_id, set())
        if dimension is not None:
            return dimension in restrictions
        # A withdrawn qualification stops that teacher from taking new blind
        # reviews at all: the blind protocol relies on the teacher's calibration.
        return bool(restrictions)

    def approval_status(self, approval_id: str, as_of: str) -> dict[str, Any]:
        approval = self._approval(approval_id)
        approved = approval["approved_on"]
        ends = [x for x in (approval.get("withdrawn_on"), approval.get("expires_on")) if x]
        active = approved <= as_of and all(as_of < end for end in ends)
        return {
            "approval": approval,
            "active": active,
            "effective_end": min(ends) if ends else None,
            "reason": None if active else (
                "尚未生效" if as_of < approved else "已撤回或失效"
            ),
        }

    # ----------------------------------------------------------------- actions
    def offer_assignment(self, work_id: str, teacher_id: str, on: str | None = None,
                         actor: str = "review-lead", reason: str = "") -> dict[str, Any]:
        self.work(work_id)
        self.teacher(teacher_id)
        on = _date(on or self.default_as_of(), "分派日期")
        as_of = on
        if self.teacher_initial_scorer(teacher_id, work_id):
            raise DomainError("教师不能复核自己首评的作品")
        if self.school_of_work(work_id) == self.teacher(teacher_id)["school_id"]:
            raise DomainError("教师不能复核任教学校学生的作品")
        if self.teacher_has_seen(teacher_id, work_id):
            raise DomainError("教师已看过该作品，不能作为盲评任务")
        if self.teacher_blocked(teacher_id, as_of):
            raise DomainError("教师校准资格已撤回，不能继续盲评")
        if any(a["work_id"] == work_id and a["teacher_id"] == teacher_id
               and a["status"] in {"offered", "accepted", "completed"}
               for a in self.assignments):
            raise DomainError("同一作品不能重复确认给同一教师")
        assignment_id = _next_id("A", self.assignments)
        event = {
            "type": "assignment.offered",
            "on": on,
            "actor": actor,
            "payload": {"assignment_id": assignment_id, "work_id": work_id,
                        "teacher_id": teacher_id, "on": on, "reason": reason},
        }
        self._append(event)
        return self._assignment(assignment_id)

    def accept_assignment(self, assignment_id: str, on: str | None = None,
                          actor: str = "review-lead") -> dict[str, Any]:
        record = self._assignment(assignment_id)
        on = _date(on or self.default_as_of(), "接受日期")
        if record["status"] != "offered":
            raise DomainError("只有待接受的复核任务可以接受")
        if self.teacher_blocked(record["teacher_id"], on):
            raise DomainError("教师校准资格已撤回，不能继续盲评")
        if self.teacher_has_seen(record["teacher_id"], record["work_id"]):
            raise DomainError("教师已看过该作品，不能作为盲评任务")
        if self.assignment_load(record["teacher_id"]) >= self.teacher_capacity(record["teacher_id"]):
            raise DomainError("教师本周复核容量已满")
        self._append({"type": "assignment.accepted", "on": on, "actor": actor,
                      "payload": {"assignment_id": assignment_id, "on": on}})
        return self._assignment(assignment_id)

    def return_assignment(self, assignment_id: str, on: str | None = None,
                          reason: str = "", actor: str = "review-lead") -> dict[str, Any]:
        record = self._assignment(assignment_id)
        on = _date(on or self.default_as_of(), "退回日期")
        if record["status"] not in {"offered", "accepted"}:
            raise DomainError("只有未完成的复核任务可以退回")
        self._append({"type": "assignment.returned", "on": on, "actor": actor,
                      "payload": {"assignment_id": assignment_id, "on": on,
                                  "reason": reason}})
        return self._assignment(assignment_id)

    def complete_assignment(self, assignment_id: str, decisions: list[dict[str, Any]],
                            on: str | None = None, actor: str = "review-lead") -> dict[str, Any]:
        record = self._assignment(assignment_id)
        on = _date(on or self.default_as_of(), "完成日期")
        if record["status"] != "accepted":
            raise DomainError("只有已接受的复核任务可以完成")
        if self.teacher_blocked(record["teacher_id"], on):
            raise DomainError("教师校准资格已撤回，不能继续盲评")
        if not decisions:
            raise DomainError("完成复核至少需要一个逐维决定")
        work = self.work(record["work_id"])
        dimensions = set(self.rubric(work["rubric_id"])["dimensions"])
        restricted = self.teacher_restrictions(on).get(record["teacher_id"], set())
        for decision in decisions:
            if decision["dimension"] in restricted:
                raise DomainError(
                    "该维度的校准资格已撤回，不能继续由原复核人完成盲评")
        seen_dims: set[str] = set()
        prepared: list[dict[str, Any]] = []
        for decision in decisions:
            dimension = decision["dimension"]
            if dimension not in dimensions:
                raise DomainError(f"维度不属于作品规则：{dimension}")
            if dimension in seen_dims:
                raise DomainError(f"同一维度重复裁决：{dimension}")
            seen_dims.add(dimension)
            verdict = decision["decision"]
            if verdict not in {"revise", "keep", "insufficient"}:
                raise DomainError(f"未知复核决定：{verdict}")
            score = decision.get("score")
            if verdict == "revise":
                if score is None:
                    raise DomainError("修分必须提供正式分")
                max_score = int(self.rubric(work["rubric_id"])["max_score"])
                if not isinstance(score, (int, float)) or not 0 <= score <= max_score:
                    raise DomainError("正式分超出规则范围")
            elif score is not None:
                raise DomainError("维持或材料不足时不应提供正式分")
            raw_ids = [s["id"] for s in self.raw_scores_for(record["work_id"], dimension)]
            prepared.append({
                "id": _next_id("D", self.adjudications),
                "assignment_id": assignment_id,
                "dimension": dimension,
                "decision": verdict,
                "score": score,
                "on": on,
                "reviewer_id": record["teacher_id"],
                "raw_score_ids": raw_ids,
                "basis": {
                    "kind": "formal_review",
                    "rationale": decision.get("rationale", ""),
                    "raw_score_ids": raw_ids,
                    "work_id": record["work_id"],
                    "dimension": dimension,
                    "calibration_approval_ids": decision.get("calibration_approval_ids", []),
                },
                "status": "active",
                "superseded_by": None,
                "superseded_on": None,
            })
        self._append({"type": "assignment.completed", "on": on, "actor": actor,
                      "payload": {"assignment_id": assignment_id, "on": on}})
        for record in prepared:
            self._append({"type": "adjudication.recorded", "on": on, "actor": actor,
                          "payload": record})
        return self._assignment(assignment_id)

    def supersede_adjudication(self, old_id: str, decision: str,
                               score: float | None, on: str, rationale: str,
                               actor: str = "research-lead",
                               calibration_approval_ids: list[str] | None = None) -> dict[str, Any]:
        old = self._adjudication(old_id)
        if old.get("status", "active") != "active":
            raise DomainError("只能替代仍有效的裁决")
        if decision not in {"revise", "keep", "insufficient"}:
            raise DomainError(f"未知复核决定：{decision}")
        if not rationale.strip():
            raise DomainError("替代既有裁决必须说明依据，不能静默推翻")
        if decision == "revise":
            if score is None:
                raise DomainError("修分必须提供正式分")
            work_id = self._work_of_adjudication(old)
            max_score = int(self.rubric(self.work(work_id)["rubric_id"])["max_score"])
            if not 0 <= score <= max_score:
                raise DomainError("正式分超出规则范围")
        elif score is not None:
            raise DomainError("维持或材料不足时不应提供正式分")
        work_id = self._work_of_adjudication(old)
        new_id = _next_id("D", self.adjudications)
        new_record = {
            "id": new_id,
            "assignment_id": old["assignment_id"],
            "dimension": old["dimension"],
            "decision": decision,
            "score": score,
            "on": _date(on, "裁决日期"),
            "reviewer_id": actor,
            "raw_score_ids": old.get("raw_score_ids", []),
            "basis": {
                "kind": "explicit_supersede",
                "rationale": rationale,
                "raw_score_ids": old.get("raw_score_ids", []),
                "work_id": work_id,
                "dimension": old["dimension"],
                "calibration_approval_ids": calibration_approval_ids or [],
                "superseded_id": old_id,
            },
            "status": "active",
            "superseded_by": None,
            "superseded_on": None,
        }
        self._append({
            "type": "adjudication.superseded",
            "on": new_record["on"],
            "actor": actor,
            "payload": {
                "superseded_id": old_id,
                "new_id": new_id,
                "on": new_record["on"],
                "reason": rationale,
                "new_record": new_record,
            },
        })
        return new_record

    def _work_of_adjudication(self, adjudication: dict[str, Any]) -> str:
        assignment = self._assignment(adjudication["assignment_id"])
        return assignment["work_id"]

    def add_calibration_approval(self, from_rubric: str, to_rubric: str, dimension: str,
                                 teacher_ids: list[str], anchor_ids: list[str],
                                 approved_on: str, expires_on: str | None = None,
                                 actor: str = "research-lead") -> dict[str, Any]:
        approved_on = _date(approved_on, "批准日期")
        if expires_on:
            expires_on = _date(expires_on, "失效日期")
            if expires_on <= approved_on:
                raise DomainError("失效日期必须晚于批准日期")
        for rid in (from_rubric, to_rubric):
            self.rubric(rid)
        if dimension not in self.rubric(from_rubric)["dimensions"]:
            raise DomainError("维度不属于原规则")
        if dimension not in self.rubric(to_rubric)["dimensions"]:
            raise DomainError("维度不属于目标规则")
        if not teacher_ids:
            raise DomainError("校准联系必须列出适用教师")
        for tid in teacher_ids:
            self.teacher(tid)
        if not anchor_ids:
            raise DomainError("校准联系必须列出共同作品")
        for wid in anchor_ids:
            if not self.work(wid).get("anchor"):
                raise DomainError(f"共同作品必须是校准作品：{wid}")
        record = {
            "id": _next_id("L", self.approvals),
            "from_rubric": from_rubric,
            "to_rubric": to_rubric,
            "dimension": dimension,
            "approved_on": approved_on,
            "withdrawn_on": None,
            "expires_on": expires_on,
            "teacher_ids": list(dict.fromkeys(teacher_ids)),
            "anchor_ids": list(dict.fromkeys(anchor_ids)),
            "approval_source": "runtime",
        }
        self._append({"type": "approval.added", "on": approved_on, "actor": actor,
                      "payload": record})
        return record

    def add_score(self, work_id: str, teacher_id: str, rubric_id: str, dimension: str,
                  score: float, on: str, kind: str = "initial",
                  actor: str = "review-lead") -> dict[str, Any]:
        self.work(work_id)
        self.teacher(teacher_id)
        rubric = self.rubric(rubric_id)
        if dimension not in rubric["dimensions"]:
            raise DomainError("维度不属于评分规则")
        if not 0 <= score <= int(rubric["max_score"]):
            raise DomainError("评分超出规则范围")
        if kind not in {"initial", "calibration"}:
            raise DomainError("评分类型必须是 initial 或 calibration")
        record = {
            "id": _next_id("S", self.scores),
            "work_id": work_id,
            "teacher_id": teacher_id,
            "rubric_id": rubric_id,
            "dimension": dimension,
            "score": score,
            "on": _date(on, "评分日期"),
            "kind": kind,
            "source": "runtime",
        }
        self._append({"type": "score.added", "on": record["on"], "actor": actor,
                      "payload": record})
        return record

    def add_seen(self, teacher_id: str, work_id: str, on: str,
                 actor: str = "review-lead") -> dict[str, Any]:
        self.teacher(teacher_id)
        self.work(work_id)
        record = {"id": _next_id("V", self.seen), "teacher_id": teacher_id,
                  "work_id": work_id, "on": _date(on, "阅读日期")}
        self._append({"type": "seen.added", "on": record["on"], "actor": actor,
                      "payload": record})
        return record

    def withdraw_approval(self, approval_id: str, on: str, reason: str,
                          actor: str = "research-lead") -> dict[str, Any]:
        approval = self._approval(approval_id)
        on = _date(on, "撤回日期")
        self._append({"type": "approval.withdrawn", "on": on, "actor": actor,
                      "payload": {"approval_id": approval_id, "on": on,
                                  "reason": reason}})
        return approval

    def withdraw_teacher_qualification(self, approval_id: str, teacher_id: str,
                                       on: str, reason: str,
                                       actor: str = "research-lead") -> dict[str, Any]:
        approval = self._approval(approval_id)
        if teacher_id not in approval["teacher_ids"]:
            raise DomainError("该教师不在此校准联系的适用范围内")
        on = _date(on, "撤回日期")
        if any(e["approval_id"] == approval_id and e["teacher_id"] == teacher_id
               for e in self.withdrawal_events):
            raise DomainError("该教师的校准资格已经撤回")
        self._append({
            "type": "qualification.withdrawn",
            "on": on,
            "actor": actor,
            "payload": {
                "approval_id": approval_id,
                "teacher_id": teacher_id,
                "dimension": approval["dimension"],
                "from_rubric": approval["from_rubric"],
                "to_rubric": approval["to_rubric"],
                "on": on,
                "reason": reason,
            },
        })
        # Unfinished assignments must not be left with an unqualified reviewer.
        revoked = []
        for assignment in list(self.assignments):
            if (assignment["teacher_id"] == teacher_id
                    and assignment["status"] in {"offered", "accepted"}):
                self._append({
                    "type": "assignment.revoked",
                    "on": on,
                    "actor": actor,
                    "payload": {
                        "assignment_id": assignment["id"],
                        "on": on,
                        "reason": f"校准资格撤回（{approval_id}/{teacher_id}）：{reason}",
                    },
                })
                revoked.append(assignment["id"])
        return {"withdrawal_event": self.withdrawal_events[-1], "revoked_assignments": revoked}

    def record_publication(self, school_ids: list[str], dimension: str,
                           score_ids: list[str], approval_ids: list[str],
                           on: str, actor: str = "research-lead") -> dict[str, Any]:
        for sid in school_ids:
            self.bundle.get("schools", sid)
        for approval_id in approval_ids:
            self._approval(approval_id)
        valid_scores = {s["id"] for s in self.scores}
        missing = [sid for sid in score_ids if sid not in valid_scores]
        if missing:
            raise DomainError("发布引用了不存在的评分：" + ",".join(missing))
        record = {
            "id": _next_id("P", self.publications),
            "on": _date(on, "发布日期"),
            "school_ids": list(dict.fromkeys(school_ids)),
            "dimension": dimension,
            "score_ids": list(dict.fromkeys(score_ids)),
            "approval_ids": list(dict.fromkeys(approval_ids)),
        }
        self._append({"type": "publication.recorded", "on": record["on"],
                      "actor": actor, "payload": record})
        return record

    # --------------------------------------------------------------- reporting
    def authority_register(self) -> dict[str, Any]:
        """Show that the three sources are separate and non-destructive."""
        return {
            "raw_scores": {
                "count": len(self.scores),
                "source": "教师原始评分，只读保留",
                "mutable": False,
            },
            "scale_adjustments": {
                "kind": "派生分析层",
                "rewrites_raw": False,
                "replaces_formal_review": False,
            },
            "formal_reviews": {
                "adjudication_count": len(self.adjudications),
                "active": sum(1 for a in self.adjudications if a.get("status") == "active"),
                "retained_after_supersede": sum(
                    1 for a in self.adjudications if a.get("status") == "superseded"),
            },
            "events": len(self.events),
        }
