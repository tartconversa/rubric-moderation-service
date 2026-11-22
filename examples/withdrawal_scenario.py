"""End-to-end business scenario: withdrawal combined with partial adjudication.

The source bundle stays untouched; every action is appended to a state file.

Scenario
--------
1. Review W75 evidence: the reviewer revises one dimension, keeps another and
   declares the third insufficient — so one work carries a usable dimension
   and a still-contested dimension at the same time.
2. A second review of W63 evidence is offered and accepted but not completed.
3. Teacher T2's calibration qualification (evidence, L1) is withdrawn while
   that assignment is still pending.
4. Assertions:
   - the pending assignment is revoked, not silently left assignable;
   - the completed per-dimension verdicts are retained with their original
     dates and bases, but their connection support is flagged;
   - raw scores are untouched (no global reset to "pending");
   - T2 can no longer receive blind reviews;
   - the school comparison does not use T2's calibration scores for the
     scale link, and reports the reduced link support;
   - the withdrawal is visible in the publication audit.

Run: python3 -m examples.withdrawal_scenario --state var/scenario.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from reading_review import analysis, reporting
from reading_review.bundle import load_bundle
from reading_review.domain import Workspace


def run(materials: Path, state_path: Path) -> dict:
    bundle = load_bundle(materials)
    workspace = Workspace.load(bundle, state_path)
    if workspace.events:
        raise SystemExit("演练状态文件已有事件；请用新的 --state 路径。")
    source_scores = {s["id"]: s["score"] for s in workspace.scores}

    # 1. Partially adjudicated work: one decided dimension, one contested one.
    first = workspace.offer_assignment("W75", "T2", on="2026-07-21",
                                       reason="分歧较大，优先复核证据维度")
    workspace.accept_assignment(first["id"], on="2026-07-21")
    workspace.complete_assignment(first["id"], [
        {"dimension": "evidence", "decision": "revise", "score": 4,
         "rationale": "复核人重读后按规则修分至 4"},
        {"dimension": "interpretation", "decision": "keep",
         "rationale": "原始评分与复核一致，维持"},
        {"dimension": "reflection", "decision": "insufficient",
         "rationale": "反思段落缺失，材料不足，保留争议"},
    ], on="2026-07-22")

    # 2. A pending assignment for the same teacher on another work.
    pending = workspace.offer_assignment("W63", "T2", on="2026-07-22",
                                         reason="补足证据维度复核")
    workspace.accept_assignment(pending["id"], on="2026-07-22")

    # 3. Withdraw T2's evidence qualification from L1.
    withdrawal = analysis.withdrawal_impact(
        workspace, "L1", "T2", "2026-07-24", "校准材料被判定不适用于该教师")

    # 4. Evidence: dimension state, comparison, publication audit.
    states = analysis.work_states(workspace, "W75", "2026-07-24")
    comparison = analysis.compare_schools(workspace, ["C1", "C2"], "evidence",
                                          "R1", "2026-07-24")
    audit = analysis.audit_publication(workspace, "P1", "2026-07-24")
    impact = reporting.action_impact(workspace, "C1", "C2", "evidence", "R1",
                                     "2026-07-24")
    plan = analysis.plan_assignments(workspace, "W63", "evidence", "2026-07-24")

    # ------------------------------------------------------------ assertions
    assert states["states"]["evidence"]["status"] == "resolved"
    assert states["states"]["evidence"]["value"] == 4
    assert states["states"]["reflection"]["status"] == "contested"
    assert states["states"]["reflection"]["value"] is None
    assert states["states"]["evidence"]["connection_supported"] is False, \
        "资格撤回后应标注联系依据失效，但不改写结论"
    assert not withdrawal["after_pending"], "未完成分派必须撤销"
    assert withdrawal["source_scores_unchanged"], "原始评分不得被改写"
    assert all(d["raw_retained"] for d in withdrawal["score_dispositions"])
    assert any(not d["usable_for_scale_link"] for d in withdrawal["score_dispositions"])
    assert {s["id"]: s["score"] for s in workspace.scores} == source_scores
    assert not any(t["teacher_id"] == "T2" for t in plan["eligible"]), \
        "失去资格的教师不得再获分派"
    assert audit["verdict"] in {"invalid", "stale"}
    assert any(p["kind"] == "approval_now_inactive" or
               p["kind"] == "scores_from_withdrawn_qualification"
               for p in audit["problems"])
    evidence_block = comparison["blocks"][0]
    assert evidence_block["scale"]["link_withdrawn_teachers"] == ["T2"]
    assert all(t != "T2" for t in evidence_block["scale"].get("teacher_offsets", {}))

    return {
        "withdrawal": withdrawal,
        "w75_states": states,
        "comparison": comparison,
        "audit": audit,
        "impact": impact,
        "plan_for_W63": plan,
        "state_file": str(state_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--materials", default="materials")
    parser.add_argument("--state", default="var/scenario.json")
    args = parser.parse_args()
    result = run(Path(args.materials), Path(args.state))
    print(json.dumps({
        "state_file": result["state_file"],
        "w75": {d: (s["status"], s["value"], s["connection_supported"])
                for d, s in result["w75_states"]["states"].items()},
        "revoked": result["withdrawal"]["applied"]["revoked_assignments"],
        "retained_adjudications": result["withdrawal"]["retained_adjudications"],
        "publication_verdict": result["audit"]["verdict"],
        "publication_problems": [p["kind"] for p in result["audit"]["problems"]],
        "link_withdrawn_teachers": result["comparison"]["blocks"][0]["scale"]["link_withdrawn_teachers"],
        "impact": [
            {"seq": t["seq"], "type": t["type"], "helped": t["helped"]}
            for t in result["impact"]
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
