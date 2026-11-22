"""Traceable views across school comparisons, dimensions and scale links."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .analysis import (
    _as_of,
    audit_publication,
    calibration_network,
    compare_schools,
    dimension_state,
    equating,
    scale_fit,
)
from .domain import DomainError, Workspace


def trace_comparison(workspace: Workspace, school_a: str, school_b: str,
                     dimension: str, rubric_id: str | None = None,
                     as_of: str | None = None) -> dict[str, Any]:
    """Walk from a school difference to work, dimension, basis and disputes."""
    comparison = compare_schools(workspace, [school_a, school_b], dimension,
                                 rubric_id, as_of)
    as_of = comparison["as_of"]
    traced_blocks = []
    for block in comparison["blocks"]:
        contrasts = []
        for contrast in block["contrasts"]:
            provenance = contrast["provenance"]
            works = []
            for school_key, school_id in (("works_a", school_a), ("works_b", school_b)):
                for item in provenance[school_key]:
                    state = dimension_state(workspace, item["work_id"], dimension, as_of)
                    works.append({
                        "school_id": school_id,
                        "work_id": item["work_id"],
                        "value": item["value"],
                        "raw_score_ids": item["raw_score_ids"],
                        "raw_values": state["raw_values"],
                        "decision": (state["adjudication"] or {}).get("decision", "unreviewed"),
                        "decision_id": (state["adjudication"] or {}).get("id"),
                        "decision_on": (state["adjudication"] or {}).get("on"),
                        "rationale": item["rationale"],
                        "connection_supported": item["connection_supported"],
                        "status": item["status"],
                        "calibration_evidence": _calibration_evidence(
                            workspace, item["work_id"], dimension, as_of),
                    })
            unresolved = [w for w in works if w["status"] != "resolved"]
            contrasts.append({
                "school_a": school_a,
                "school_b": school_b,
                "difference": contrast["difference"],
                "ci95": contrast["ci95"],
                "reliable": contrast["reliable"],
                "adjusted_difference": contrast["adjusted"].get("difference"),
                "adjusted_ci95": contrast["adjusted"].get("ci95"),
                "scale_verdict": contrast["scale_verdict"],
                "works": works,
                "works_not_formally_resolved": [
                    {"work_id": w["work_id"], "status": w["status"],
                     "raw_score_ids": w["raw_score_ids"]}
                    for w in unresolved
                ],
                "link_basis": provenance["scale"],
                "rules": {
                    "rubric_id": block["rubric_id"],
                    "max_score": block["max_score"],
                    "mixed_across_rules": False,
                },
            })
        traced_blocks.append({
            "rubric_id": block["rubric_id"],
            "school_summaries": block["schools"],
            "adjusted_summaries": block["adjusted_schools"],
            "excluded_works": block["excluded_works"],
            "contrasts": contrasts,
        })
    return {
        "dimension": dimension,
        "as_of": as_of,
        "schools": [school_a, school_b],
        "blocks": traced_blocks,
        "equating": equating(workspace, dimension, as_of),
        "network": calibration_network(
            workspace, dimension,
            rubric_id or workspace.bundle.rubrics[0]["id"], as_of),
        "still_contested": sorted({
            item["work_id"]
            for block in traced_blocks for contrast in block["contrasts"]
            for item in contrast["works_not_formally_resolved"]
        }),
        "publications_relying_on_scope": [
            {"publication_id": p["id"], "on": p["on"],
             "audit": audit_publication(workspace, p["id"], as_of)["verdict"]}
            for p in workspace.publications
            if school_a in p["school_ids"] or school_b in p["school_ids"]
        ],
    }


def _calibration_evidence(workspace: Workspace, work_id: str, dimension: str,
                          as_of: str) -> list[dict[str, Any]]:
    evidences = []
    for approval in workspace.approvals:
        if approval["dimension"] != dimension or work_id not in approval["anchor_ids"]:
            continue
        status = workspace.approval_status(approval["id"], as_of)
        evidences.append({
            "approval_id": approval["id"],
            "active": status["active"],
            "effective_end": status["effective_end"],
            "reason": status["reason"],
            "teacher_ids": approval["teacher_ids"],
        })
    return evidences


def comparison_delta(workspace: Workspace, school_a: str, school_b: str,
                     dimension: str, rubric_id: str, prefix_events: list[dict[str, Any]],
                     as_of: str | None = None) -> dict[str, Any]:
    """Replay a subset of events to measure whether an action changed a result."""
    probe = Workspace(workspace.bundle, prefix_events)
    comparison = compare_schools(probe, [school_a, school_b], dimension,
                                 rubric_id, as_of)
    block = comparison["blocks"][0]
    contrast = block["contrasts"][0]
    return {
        "difference": contrast["difference"],
        "ci95": contrast["ci95"],
        "reliable": contrast["reliable"],
        "adjusted_difference": contrast["adjusted"].get("difference"),
        "residual_sd": block["scale"]["residual_sd"],
        "resolved_works": sum(block["evidence"][s]["formally_resolved"]
                              for s in (school_a, school_b)),
        "excluded_adjusted": sum(len(v) for v in
                                 contrast["provenance"]["scale"]["excluded_from_adjusted"].values()),
    }


def action_impact(workspace: Workspace, school_a: str, school_b: str,
                  dimension: str, rubric_id: str | None = None,
                  as_of: str | None = None) -> list[dict[str, Any]]:
    """Show, event by event, whether a later action helped the comparison."""
    rubric_id = rubric_id or workspace.bundle.rubrics[0]["id"]
    trials = []
    running: list[dict[str, Any]] = []
    previous = comparison_delta(workspace, school_a, school_b, dimension,
                                rubric_id, running, as_of)
    for event in workspace.events:
        running = running + [event]
        current = comparison_delta(workspace, school_a, school_b, dimension,
                                   rubric_id, running, as_of)
        changed = (
            current["difference"] != previous["difference"]
            or current["adjusted_difference"] != previous["adjusted_difference"]
            or current["resolved_works"] != previous["resolved_works"]
            or current["excluded_adjusted"] != previous["excluded_adjusted"]
            or current["residual_sd"] != previous["residual_sd"]
        )
        trials.append({
            "seq": event["seq"],
            "type": event["type"],
            "on": event.get("on"),
            "actor": event.get("actor"),
            "changed_comparison": changed,
            "before": previous,
            "after": current,
            "helped": _helped(previous, current),
        })
        previous = current
    return trials


def _helped(before: dict[str, Any], after: dict[str, Any]) -> str:
    def width(result):
        ci = result.get("ci95")
        return (ci[1] - ci[0]) if ci else None

    bw, aw = width(before), width(after)
    if bw is not None and aw is not None and abs(bw - aw) > 1e-9:
        return "区间收窄，比较更精确" if aw < bw else "区间变宽，比较更不确定"
    if after["resolved_works"] > before["resolved_works"]:
        return "新增正式裁决，证据更完整"
    if after["excluded_adjusted"] < before["excluded_adjusted"]:
        return "减少无法调整的评分"
    if before["difference"] != after["difference"]:
        return "点估计变化，需要重新解释"
    if before.get("residual_sd") != after.get("residual_sd"):
        return "尺度联系改变，调整后的不确定性随之变化"
    return "对这次比较没有可测影响"
