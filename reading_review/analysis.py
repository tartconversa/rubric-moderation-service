"""Dimension-level adjudication state, scale evidence, school comparisons,
review triage and traceable publication audits.

All results are derived from ``Workspace``; nothing here writes to the source
bundle or back-dates a judgement.
"""
from __future__ import annotations

import datetime as _dt
from collections import defaultdict
from typing import Any, Iterable

from .domain import DomainError, Workspace
from .statlib import (
    cluster_contrast,
    linear_fit,
    pearson,
    two_way_fit,
    weighted_mean_cluster,
)

RESOLVED = "resolved"
CONTESTED = "contested"
UNREVIEWED = "unreviewed"


# --------------------------------------------------------------------- helpers
def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return sum(values) / len(values) if values else None


def _max_score(workspace: Workspace, rubric_id: str) -> int:
    return int(workspace.rubric(rubric_id)["max_score"])


def _as_of(workspace: Workspace, as_of: str | None) -> str:
    return _as_of_date(as_of) if as_of else workspace.default_as_of()


def _as_of_date(value: str) -> str:
    try:
        return _dt.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise DomainError(f"as_of 日期格式无效：{value}") from exc


def _event_date(workspace: Workspace, event: dict[str, Any]) -> str:
    return event.get("on") or workspace.latest_bundle_date()


def _withdrawn_dimensions(workspace: Workspace, teacher_id: str,
                          as_of: str) -> set[str]:
    return workspace.teacher_restrictions(as_of).get(teacher_id, set())


# ------------------------------------------------------- dimension-level state
def dimension_state(workspace: Workspace, work_id: str, dimension: str,
                    as_of: str | None = None) -> dict[str, Any]:
    """Resolve one dimension while keeping the decision as a dated artefact.

    A formal decision is never overruled by later calibration material.  If a
    reviewer later loses the qualification that supported the *link*, the
    decision is retained but its connection support is reported separately.
    """
    as_of = _as_of(workspace, as_of)
    work = workspace.work(work_id)
    rubric = workspace.rubric(work["rubric_id"])
    if dimension not in rubric["dimensions"]:
        raise DomainError(f"维度不属于作品规则：{dimension}")
    raw = workspace.raw_scores_for(work_id, dimension)
    raw_ids = [s["id"] for s in raw if s["on"] <= as_of]
    raw_values = [s["score"] for s in raw if s["on"] <= as_of]
    history = [
        r for r in workspace.adjudication_history(work_id)
        if r["dimension"] == dimension
    ]
    active = [
        r for r in history
        if r.get("status", "active") == "active" and r["on"] <= as_of
    ]
    active.sort(key=lambda r: (r["on"], r["id"]))
    current = active[-1] if active else None
    base = {
        "work_id": work_id,
        "dimension": dimension,
        "rubric_id": work["rubric_id"],
        "as_of": as_of,
        "raw_score_ids": raw_ids,
        "raw_values": raw_values,
        "raw_mean": _mean(raw_values),
        "history": history,
        "adjudication": current,
    }
    if current is None:
        base.update({
            "status": UNREVIEWED,
            "value": base["raw_mean"],
            "source": "raw_scores" if raw_values else "none",
            "connection_supported": None,
            "notes": ["尚无正式逐维裁决，显示为原始评分争议"] if raw_values else ["无可用证据"],
        })
        return base
    reviewer = current.get("reviewer_id")
    withdrawn = set()
    if reviewer:
        withdrawn = _withdrawn_dimensions(workspace, reviewer, as_of)
    # The decision itself is never silently overturned.  What can fail later is
    # the calibration link that supported it; that is reported separately.
    connection_supported = dimension not in withdrawn
    decision = current["decision"]
    if decision == "insufficient":
        base.update({
            "status": CONTESTED,
            "value": None,
            "source": "formal_review",
            "connection_supported": connection_supported,
            "notes": ["正式复核认定材料不足；该维度继续显示争议"],
        })
    else:
        value = current["score"] if decision == "revise" else base["raw_mean"]
        base.update({
            "status": RESOLVED,
            "value": value,
            "source": "formal_review",
            "connection_supported": connection_supported,
            "notes": [
                ("正式复核修分" if decision == "revise" else "正式复核维持原分")
                + f"（{current['on']}，{current['id']}）"
            ] + (["复核人校准资格已撤回，结论保留但联系依据不再受支持"]
                 if not connection_supported else []),
        })
    return base


def work_states(workspace: Workspace, work_id: str,
                as_of: str | None = None) -> dict[str, Any]:
    work = workspace.work(work_id)
    dims = workspace.rubric(work["rubric_id"])["dimensions"]
    states = {d: dimension_state(workspace, work_id, d, as_of) for d in dims}
    resolved = [d for d, s in states.items() if s["status"] == RESOLVED]
    contested = [d for d, s in states.items() if s["status"] == CONTESTED]
    unreviewed = [d for d, s in states.items() if s["status"] == UNREVIEWED]
    return {
        "work_id": work_id,
        "rubric_id": work["rubric_id"],
        "states": states,
        "resolved_dimensions": resolved,
        "contested_dimensions": contested,
        "unreviewed_dimensions": unreviewed,
        # A work is never a single validity flag: each dimension keeps its own
        # usability verdict.  Only formally resolved dimensions are "reliable";
        # unreviewed ones remain displayable but carry raw-score uncertainty.
        "reliable_dimensions": resolved,
        "raw_only_dimensions": unreviewed,
        "usable_dimensions": resolved + unreviewed,
    }


# --------------------------------------------------------------- scale network
def _anchor_rows(workspace: Workspace, dimension: str, rubric_id: str,
                 as_of: str) -> list[tuple[str, str, float]]:
    rows = []
    max_score = _max_score(workspace, rubric_id)
    restrictions = workspace.teacher_restrictions(as_of)
    for score in workspace.scores:
        if score["rubric_id"] != rubric_id or score["dimension"] != dimension:
            continue
        if score["on"] > as_of:
            continue
        work = workspace.work(score["work_id"])
        # The score row carries the rule actually used; an anchor may be
        # scored under several rules, so compare against the score's rule.
        if not work["anchor"]:
            continue
        # A score remains a raw fact, but a score whose calibration
        # qualification was withdrawn may no longer support the scale link.
        if dimension in restrictions.get(score["teacher_id"], set()):
            continue
        rows.append((score["work_id"], score["teacher_id"], score["score"] / max_score))
    return rows


def calibration_network(workspace: Workspace, dimension: str, rubric_id: str,
                        as_of: str | None = None) -> dict[str, Any]:
    as_of = _as_of(workspace, as_of)
    rows = _anchor_rows(workspace, dimension, rubric_id, as_of)
    # An edge exists when two teachers scored the same anchor work.  Only
    # anchor evidence can make two teachers' severity comparable.
    by_work: dict[str, set[str]] = defaultdict(set)
    for work_id, teacher_id, _ in rows:
        by_work[work_id].add(teacher_id)
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    teachers = {t["id"] for t in workspace.bundle.teachers}
    for teacher in teachers:
        find(teacher)
    for work_teachers in by_work.values():
        ordered = sorted(work_teachers)
        for other in ordered[1:]:
            union(ordered[0], other)
    components: dict[str, list[str]] = defaultdict(list)
    for teacher in sorted(teachers):
        components[find(teacher)].append(teacher)
    connected_groups = [sorted(v) for v in components.values()]
    connected_groups.sort(key=lambda group: (-len(group), group[0]))
    main = connected_groups[0] if connected_groups else []
    isolated = sorted(t for group in connected_groups for t in group if len(group) == 1)
    # Approval evidence is separate from the score graph; both are reported.
    approvals = []
    for approval in workspace.approvals:
        if approval["dimension"] != dimension:
            continue
        if approval["from_rubric"] != rubric_id and approval["to_rubric"] != rubric_id:
            continue
        status = workspace.approval_status(approval["id"], as_of)
        approvals.append({
            "approval_id": approval["id"],
            "dimensions": approval["dimension"],
            "teachers": approval["teacher_ids"],
            "anchors": approval["anchor_ids"],
            "active": status["active"],
            "effective_end": status["effective_end"],
            "reason": status["reason"],
        })
    return {
        "dimension": dimension,
        "rubric_id": rubric_id,
        "as_of": as_of,
        "teachers_with_anchor_scores": sorted({t for _, t, _ in rows}),
        "components": connected_groups,
        "main_component": main,
        "isolated": isolated,
        "unified_scale_claim": (
            "只有同一连通分量内的教师可视为已建立尺度联系；孤立教师不能当作已统一尺度"
        ),
        "approvals": approvals,
        "note": "共同作品网络按实际共同评分建立；名单本身不构成可靠性结论。",
    }


def scale_fit(workspace: Workspace, dimension: str, rubric_id: str,
              as_of: str | None = None) -> dict[str, Any]:
    as_of = _as_of(workspace, as_of)
    fit = two_way_fit(_anchor_rows(workspace, dimension, rubric_id, as_of))
    network = calibration_network(workspace, dimension, rubric_id, as_of)
    fit["dimension"] = dimension
    fit["rubric_id"] = rubric_id
    fit["network"] = network
    if fit["observations"]:
        main = set(network["main_component"])
        # Every teacher outside the connected anchor component lacks a scale
        # link, whether or not they have any anchor score at all.
        fit["unconnected_teachers"] = sorted(
            t for t in {x["id"] for x in workspace.bundle.teachers} if t not in main)
        fit["link_withdrawn_teachers"] = sorted(
            t for t in {x["id"] for x in workspace.bundle.teachers}
            if dimension in workspace.teacher_restrictions(as_of).get(t, set())
        )
        fit["distinguishable_teachers"] = sorted(
            t for t, offset in fit["teachers"].items()
            if abs(offset) > fit["teacher_ci95"][t]
        )
        fit["distinguishable_works"] = sorted(
            w for w, effect in fit["works"].items()
            if abs(effect) > fit["work_ci95"][w]
        )
        ratio = (fit["teacher_spread"] / fit["residual_sd"]) if fit["residual_sd"] else 0.0
        fit["separation_ratio"] = ratio
        fit["verdict"] = (
            "教师尺度差异可与残差区分"
            if ratio >= 1.5
            else "教师尺度差异只能部分区分，个体调整仍不稳定"
            if ratio >= 1.0
            else "教师尺度差异不足以与随机差异区分，不应逐师调整"
        )
    else:
        fit["unconnected_teachers"] = sorted(t["id"] for t in workspace.bundle.teachers)
        fit["link_withdrawn_teachers"] = sorted(
            t for t in {x["id"] for x in workspace.bundle.teachers}
            if dimension in workspace.teacher_restrictions(as_of).get(t, set())
        )
        fit["distinguishable_teachers"] = []
        fit["distinguishable_works"] = []
        fit["separation_ratio"] = None
        fit["verdict"] = "无共同作品评分，无法估计教师尺度"
    return fit


def equating(workspace: Workspace, dimension: str,
             as_of: str | None = None) -> dict[str, Any]:
    """Compare two rules on the shared anchors without merging raw scores."""
    as_of = _as_of(workspace, as_of)
    rubrics = [r["id"] for r in workspace.bundle.rubrics]
    if len(rubrics) < 2:
        return {"dimension": dimension, "usable": False, "reason": "只有一个规则版本"}
    first, second = rubrics[0], rubrics[1]
    means: dict[str, dict[str, list[float]]] = {first: defaultdict(list),
                                               second: defaultdict(list)}
    restrictions = workspace.teacher_restrictions(as_of)
    for score in workspace.scores:
        if score["dimension"] != dimension or score["on"] > as_of:
            continue
        work = workspace.work(score["work_id"])
        if not work["anchor"] or score["rubric_id"] not in means:
            continue
        # A withdrawn qualification invalidates the score as link evidence.
        if dimension in restrictions.get(score["teacher_id"], set()):
            continue
        means[score["rubric_id"]][score["work_id"]].append(
            score["score"] / _max_score(workspace, score["rubric_id"]))
    first_means = {w: _mean(v) for w, v in means[first].items()}
    second_means = {w: _mean(v) for w, v in means[second].items()}
    shared = sorted(set(first_means) & set(second_means))
    xs = [first_means[w] for w in shared]
    ys = [second_means[w] for w in shared]
    fit = linear_fit(xs, ys)
    corr = pearson(xs, ys)
    approvals = []
    for approval in workspace.approvals:
        if approval["dimension"] != dimension:
            continue
        if {approval["from_rubric"], approval["to_rubric"]} != {first, second}:
            continue
        status = workspace.approval_status(approval["id"], as_of)
        covered = sorted(set(shared) & set(approval["anchor_ids"]))
        approvals.append({
            "approval_id": approval["id"],
            "dimension": approval["dimension"],
            "active": status["active"],
            "approved_on": approval["approved_on"],
            "effective_end": status["effective_end"],
            "reason": status["reason"],
            "teacher_count": len(approval["teacher_ids"]),
            "covered_shared_anchors": covered,
            "covers_all_shared": bool(shared) and set(shared) <= set(approval["anchor_ids"]),
            "scope_source": approval.get("approval_source", "source"),
        })
    # An approval only authorises a link it actually covers; an approval list
    # without the shared anchors is not a blank cheque.
    approved = any(
        item["active"] and item["teacher_count"] > 0 and item["covered_shared_anchors"]
        for item in approvals
    )
    adequate = bool(fit and corr is not None and corr >= 0.60 and len(shared) >= 6)
    return {
        "dimension": dimension,
        "as_of": as_of,
        "rubrics": [first, second],
        "shared_anchor_count": len(shared),
        "shared_anchor_ids": shared,
        "slope": fit["slope"] if fit else None,
        "intercept": fit["intercept"] if fit else None,
        "rmse": fit["rmse"] if fit else None,
        "correlation": corr,
        "minimum_correlation": 0.60,
        "approved_link": approved,
        "link_fully_covered": any(item["active"] and item["covers_all_shared"]
                                  for item in approvals),
        "approvals": approvals,
        "statistically_adequate": adequate,
        "usable": approved and adequate,
        "rule": "换算仅限已批准维度与已覆盖的共同作品；未覆盖的作品不换算。",
        "verdict": (
            "可借助已批准联系换算，但只限该维度与已覆盖共同作品"
            if approved and adequate
            else "统计上不足以支持跨规则换算"
            if not adequate
            else "统计关系尚可，但没有有效且覆盖共同作品的批准联系，不能换算"
        ),
    }


# ------------------------------------------------------- score availability
def score_dispositions(workspace: Workspace, teacher_id: str,
                       as_of: str | None = None) -> list[dict[str, Any]]:
    """Per-score usability after a qualification withdrawal.

    Raw scores are retained.  What changes is whether a score may still feed a
    scale link or a comparison that depends on it.
    """
    as_of = _as_of(workspace, as_of)
    restrictions = workspace.teacher_restrictions(as_of).get(teacher_id, set())
    rows = []
    for score in workspace.scores:
        if score["teacher_id"] != teacher_id:
            continue
        restricted = score["dimension"] in restrictions
        rows.append({
            "score_id": score["id"],
            "work_id": score["work_id"],
            "dimension": score["dimension"],
            "kind": score["kind"],
            "raw_retained": True,
            "usable_for_within_rule_raw": True,
            "usable_for_scale_link": not restricted,
            "flag": "联系依据失效" if restricted else "可用",
        })
    return rows


# --------------------------------------------------------- school comparisons
def _eligible_rubrics(workspace: Workspace, school_ids: list[str],
                      dimension: str, as_of: str) -> list[str]:
    result = []
    for rubric in workspace.bundle.rubrics:
        rid = rubric["id"]
        if dimension not in rubric["dimensions"]:
            continue
        if any(_school_works(workspace, sid, rid, as_of) for sid in school_ids):
            result.append(rid)
    return result


def _school_works(workspace: Workspace, school_id: str, rubric_id: str,
                  as_of: str) -> list[str]:
    works = []
    for work in workspace.bundle.works:
        if work["rubric_id"] != rubric_id or work["on"] > as_of:
            continue
        if workspace.school_of_work(work["id"]) == school_id:
            works.append(work["id"])
    return works


def _work_value(workspace: Workspace, work_id: str, dimension: str,
                as_of: str, adjusted: bool, fits: dict[str, Any]) -> dict[str, Any]:
    state = dimension_state(workspace, work_id, dimension, as_of)
    # The comparison is built for one dimension; works whose rule lacks that
    # dimension are out of scope rather than "no evidence".
    if dimension not in workspace.rubric(state["rubric_id"])["dimensions"]:
        return {"work_id": work_id, "included": False, "reason": "规则不含该维度"}
    rubric_id = state["rubric_id"]
    if state["status"] == CONTESTED:
        return {"work_id": work_id, "included": False, "reason": "正式复核认定材料不足"}
    if state["value"] is None:
        return {"work_id": work_id, "included": False, "reason": "无可用证据"}
    fit = fits.get(rubric_id)
    max_score = _max_score(workspace, rubric_id)
    if not adjusted:
        return {
            "work_id": work_id,
            "included": True,
            "value": state["value"],
            "state": state,
            "reason": "原始分或正式分",
        }
    applied = []
    excluded = []
    for score in workspace.raw_scores_for(work_id, dimension):
        if score["on"] > as_of:
            continue
        offset = fit["teachers"].get(score["teacher_id"]) if fit else None
        if offset is None or score["teacher_id"] in (fit or {}).get("unconnected_teachers", []):
            excluded.append(score["teacher_id"])
            continue
        applied.append(score["score"] - offset * max_score)
    # A formal decision is authoritative; scale adjustment never overwrites it,
    # whether it revised or kept the original score.
    if (state["source"] == "formal_review"
            and (state["adjudication"] or {}).get("decision") in {"revise", "keep"}):
        return {
            "work_id": work_id,
            "included": True,
            "value": state["value"],
            "state": state,
            "reason": "正式复核结论不作为自动调整对象",
            "adjustment": "formal_score",
        }
    if excluded:
        return {
            "work_id": work_id,
            "included": False,
            "reason": "评分教师不在共同作品连通分量内，无法做尺度调整",
            "excluded_teachers": sorted(set(excluded)),
            "state": state,
        }
    if not applied:
        return {"work_id": work_id, "included": False, "reason": "无可用评分"}
    return {
        "work_id": work_id,
        "included": True,
        "value": _mean(applied),
        "state": state,
        "reason": "派生尺度调整（原始分未被改写）",
        "adjustment": "derived",
    }


def compare_schools(workspace: Workspace, school_ids: list[str], dimension: str,
                    rubric_id: str | None = None, as_of: str | None = None) -> dict[str, Any]:
    """Dimension-specific, rule-specific school comparison with provenance.

    Never mixes rubric versions.  Each dimension is decided independently, so
    one usable dimension does not make the rest of the work usable.
    """
    as_of = _as_of(workspace, as_of)
    for sid in school_ids:
        workspace.bundle.get("schools", sid)
    eligible = _eligible_rubrics(workspace, school_ids, dimension, as_of)
    if rubric_id:
        if rubric_id not in eligible:
            raise DomainError("所选规则在该维度没有可用作品")
        rubrics = [rubric_id]
    else:
        rubrics = eligible
    fits = {rid: scale_fit(workspace, dimension, rid, as_of) for rid in rubrics}
    blocks = []
    for rid in rubrics:
        blocks.append(_compare_within_rubric(
            workspace, school_ids, dimension, rid, as_of, fits[rid]))
    return {
        "dimension": dimension,
        "as_of": as_of,
        "school_ids": school_ids,
        "rubrics": rubrics,
        "blocks": blocks,
        "mixing_across_rules": False,
        "mixing_note": "规则不同的总分不能直接混合；每个规则单独比较，跨规则换算需另行批准。",
        "disclosure": (
            "均值按资料中的抽样基础权重加权，该权重不是无回应校正权重；"
            "标准误按 household 聚类，但未包含抽样设计之外的额外不确定性。"
            "区间含 0 时只能报告差异不可区分。"
        ),
        "equating": equating(workspace, dimension, as_of),
    }


def _compare_within_rubric(workspace: Workspace, school_ids: list[str],
                           dimension: str, rubric_id: str, as_of: str,
                           fit: dict[str, Any]) -> dict[str, Any]:
    max_score = _max_score(workspace, rubric_id)
    school_rows: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    provenance: dict[str, list[dict[str, Any]]] = defaultdict(list)
    excluded: list[dict[str, Any]] = []
    for school in school_ids:
        for work_id in _school_works(workspace, school, rubric_id, as_of):
            value = _work_value(workspace, work_id, dimension, as_of, False, {rubric_id: fit})
            if not value["included"]:
                excluded.append({"school_id": school, **value})
                continue
            person = workspace.person(workspace.work(work_id)["person_id"])
            weight = float(person.get("sample_weight") or 1.0)
            school_rows[school].append((value["value"], weight, person["household"]))
            provenance[school].append({
                "work_id": work_id,
                "person_id": person["id"],
                "household": person["household"],
                "value": value["value"],
                "source": value["state"]["source"],
                "status": value["state"]["status"],
                "raw_score_ids": value["state"]["raw_score_ids"],
                "adjudication_id": (value["state"]["adjudication"] or {}).get("id"),
                "rationale": ((value["state"]["adjudication"] or {}).get("basis") or {}).get("rationale"),
                "connection_supported": value["state"]["connection_supported"],
            })
    summaries = {}
    adjusted_summaries = {}
    adjusted_provenance = {}
    excluded_from_adjusted: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for school in school_ids:
        summaries[school] = weighted_mean_cluster(
            [v for v, _, _ in school_rows[school]],
            [w for _, w, _ in school_rows[school]],
            [c for _, _, c in school_rows[school]],
        )
        rows = []
        for work_id in _school_works(workspace, school, rubric_id, as_of):
            value = _work_value(workspace, work_id, dimension, as_of, True, {rubric_id: fit})
            if not value["included"]:
                excluded_from_adjusted[school].append({"work_id": work_id,
                                                       "reason": value.get("reason")})
                continue
            person = workspace.person(workspace.work(work_id)["person_id"])
            rows.append((value["value"], float(person.get("sample_weight") or 1.0),
                         person["household"]))
        adjusted_summaries[school] = weighted_mean_cluster(
            [v for v, _, _ in rows], [w for _, w, _ in rows],
            [c for _, _, c in rows])
        adjusted_provenance[school] = rows
    contrasts = []
    for i, left in enumerate(school_ids):
        for right in school_ids[i + 1:]:
            raw = cluster_contrast(school_rows[left], school_rows[right])
            adj = cluster_contrast(adjusted_provenance[left], adjusted_provenance[right])
            raw["school_a"], raw["school_b"] = left, right
            raw["adjusted"] = adj
            # Whether the *adjusted* contrast is usable depends on both raw and
            # adjusted evidence; dropping the unconnected teachers can move it.
            adj["reliable"] = bool(
                adj["ci95"] is not None and (adj["ci95"][0] > 0 or adj["ci95"][1] < 0))
            adj["coverage_a"] = adjusted_summaries[left]["n"]
            adj["coverage_b"] = adjusted_summaries[right]["n"]
            raw["scale_sd"] = fit.get("residual_sd")
            raw["reliable"] = bool(
                raw["ci95"] is not None
                and (raw["ci95"][0] > 0 or raw["ci95"][1] < 0)
            )
            raw["scale_verdict"] = fit.get("verdict")
            contrast_provenance = {
                "school_a": left,
                "school_b": right,
                "dimension": dimension,
                "rubric_id": rubric_id,
                "works_a": provenance[left],
                "works_b": provenance[right],
                "scale": {
                    "teacher_offsets": fit.get("teachers"),
                    "teacher_ci95": fit.get("teacher_ci95"),
                    "unconnected_teachers": fit.get("unconnected_teachers"),
                    "link_withdrawn_teachers": fit.get("link_withdrawn_teachers"),
                    "excluded_from_adjusted": {
                        "a": excluded_from_adjusted[left],
                        "b": excluded_from_adjusted[right],
                    },
                },
            }
            raw["provenance"] = contrast_provenance
            contrasts.append(raw)
    return {
        "rubric_id": rubric_id,
        "max_score": max_score,
        "weighting": "sample_weight；按 household 聚类",
        "schools": summaries,
        "adjusted_schools": adjusted_summaries,
        "contrasts": contrasts,
        "excluded_works": excluded,
        "evidence": {
            school: {
                "works": len(provenance[school]),
                "formally_resolved": sum(
                    1 for item in provenance[school] if item["status"] == RESOLVED),
                "raw_only": sum(
                    1 for item in provenance[school] if item["status"] == UNREVIEWED),
            }
            for school in school_ids
        },
        "scale": {
            "residual_sd": fit.get("residual_sd"),
            "separation_ratio": fit.get("separation_ratio"),
            "verdict": fit.get("verdict"),
            "distinguishable_teachers": fit.get("distinguishable_teachers", []),
            "unconnected_teachers": fit.get("unconnected_teachers", []),
            "link_withdrawn_teachers": fit.get("link_withdrawn_teachers", []),
            "shares": fit.get("shares"),
        },
    }


def variance_explanation(workspace: Workspace, dimension: str,
                         as_of: str | None = None) -> dict[str, Any]:
    as_of = _as_of(workspace, as_of)
    per_rule = {}
    for rubric in workspace.bundle.rubrics:
        fit = scale_fit(workspace, dimension, rubric["id"], as_of)
        per_rule[rubric["id"]] = {
            "work_share": (fit.get("shares") or {}).get("work"),
            "teacher_share": (fit.get("shares") or {}).get("teacher"),
            "residual_share": (fit.get("shares") or {}).get("residual"),
            "teacher_spread": fit.get("teacher_spread"),
            "work_range": (
                max(fit["works"].values()) - min(fit["works"].values())
                if fit.get("works") else None
            ),
            "residual_sd": fit.get("residual_sd"),
            "separation_ratio": fit.get("separation_ratio"),
            "distinguishable_teachers": len(fit.get("distinguishable_teachers", [])),
            "teacher_count": fit.get("teacher_count"),
            "verdict": fit.get("verdict"),
        }
    eq = equating(workspace, dimension, as_of)
    return {
        "dimension": dimension,
        "rules": per_rule,
        "rule_change": {
            "shared_anchor_count": eq["shared_anchor_count"],
            "correlation": eq["correlation"],
            "rmse": eq["rmse"],
            "approved_link": eq["approved_link"],
            "usable": eq["usable"],
            "verdict": eq["verdict"],
        },
        "disclosure": (
            "作品难易、教师尺度与余差在同一模型中估计；教师尺度只在共同作品连通分量内可辨。"
            "规则间换算另需统计充分且仍有效的批准联系，未满足时不能混合总分。"
        ),
    }


# --------------------------------------------------------------- publication
def audit_publication(workspace: Workspace, publication_id: str,
                      as_of: str | None = None) -> dict[str, Any]:
    as_of = _as_of(workspace, as_of)
    try:
        publication = next(p for p in workspace.publications if p["id"] == publication_id)
    except StopIteration as exc:
        raise DomainError(f"未知发布：{publication_id}") from exc
    scores = {s["id"]: s for s in workspace.scores}
    problems = []
    used = [scores[sid] for sid in publication["score_ids"] if sid in scores]
    missing = [sid for sid in publication["score_ids"] if sid not in scores]
    if missing:
        problems.append({"kind": "missing_scores", "score_ids": missing})
    outside_schools = [s for s in used
                       if workspace.school_of_work(s["work_id"]) not in publication["school_ids"]]
    if outside_schools:
        problems.append({
            "kind": "scope_leak_schools",
            "count": len(outside_schools),
            "schools": sorted({workspace.school_of_work(s["work_id"]) for s in outside_schools}),
            "score_ids": [s["id"] for s in outside_schools][:20],
        })
    other_dimensions = [s for s in used if s["dimension"] != publication["dimension"]]
    if other_dimensions:
        problems.append({
            "kind": "dimension_mismatch",
            "count": len(other_dimensions),
            "dimensions": sorted({s["dimension"] for s in other_dimensions}),
            "score_ids": [s["id"] for s in other_dimensions][:20],
        })
    rubric_ids = sorted({s["rubric_id"] for s in used})
    if len(rubric_ids) > 1:
        problems.append({"kind": "mixed_rules", "rubrics": rubric_ids})
    approval_reports = []
    for approval_id in publication["approval_ids"]:
        try:
            approval = next(a for a in workspace.approvals if a["id"] == approval_id)
        except StopIteration:
            problems.append({"kind": "missing_approval", "approval_id": approval_id})
            continue
        status_at_publication = workspace.approval_status(approval_id, publication["on"])
        status_now = workspace.approval_status(approval_id, as_of)
        approval_reports.append({
            "approval_id": approval_id,
            "dimension": approval["dimension"],
            "active_at_publication": status_at_publication["active"],
            "active_now": status_now["active"],
            "withdrawn_on": approval.get("withdrawn_on"),
            "expires_on": approval.get("expires_on"),
        })
        if not status_at_publication["active"]:
            problems.append({"kind": "approval_inactive_at_publication",
                             "approval_id": approval_id})
        if not status_now["active"]:
            problems.append({"kind": "approval_now_inactive",
                             "approval_id": approval_id,
                             "withdrawn_on": approval.get("withdrawn_on"),
                             "hint": "已发出的比较需重跑，不能静默沿用"})
        if approval["dimension"] != publication["dimension"]:
            problems.append({"kind": "approval_dimension_mismatch",
                             "approval_id": approval_id,
                             "approval_dimension": approval["dimension"]})
    withdrawn_teachers = sorted({
        s["teacher_id"] for s in used
        if workspace.teacher_blocked(s["teacher_id"], as_of)
    })
    if withdrawn_teachers:
        problems.append({"kind": "scores_from_withdrawn_qualification",
                         "teachers": withdrawn_teachers,
                         "hint": "原始分保留，但依赖该资格的联系不能继续使用"})
    unresolved_in_scope = []
    for work_id in sorted({s["work_id"] for s in used}):
        state = dimension_state(workspace, work_id, publication["dimension"], as_of)
        if state["status"] != RESOLVED:
            unresolved_in_scope.append({
                "work_id": work_id,
                "status": state["status"],
                "raw_score_ids": state["raw_score_ids"],
            })
    severity = "invalid" if any(
        p["kind"] in {"scope_leak_schools", "dimension_mismatch", "mixed_rules",
                      "missing_scores", "approval_inactive_at_publication"}
        for p in problems) else "stale" if any(
        p["kind"] in {"approval_now_inactive",
                      "scores_from_withdrawn_qualification"} for p in problems
    ) else "usable_with_caveats"
    return {
        "publication_id": publication_id,
        "on": publication["on"],
        "as_of": as_of,
        "dimension": publication["dimension"],
        "school_ids": publication["school_ids"],
        "rubric_ids": rubric_ids,
        "score_count": len(publication["score_ids"]),
        "used_count": len(used),
        "problems": problems,
        "approvals": approval_reports,
        "unresolved_dimensions_in_scope": unresolved_in_scope,
        "verdict": severity,
        "action": (
            "撤回或重发：引用范围与声明不符" if severity == "invalid"
            else "标记待复核：联系已失效，重跑前不得继续引用"
            if severity == "stale"
            else "可继续引用但需披露不确定性"
        ),
    }


# ------------------------------------------------------------------- triage
def _publication_impact(workspace: Workspace, work_id: str) -> dict[str, Any]:
    direct = []
    indirect = []
    school = workspace.school_of_work(work_id)
    for publication in workspace.publications:
        if work_id in {workspace.scores_by_id[sid]["work_id"]
                       for sid in publication["score_ids"]
                       if sid in workspace.scores_by_id}:
            direct.append(publication["id"])
        elif school in publication["school_ids"]:
            indirect.append(publication["id"])
    return {"direct": sorted(set(direct)), "indirect": sorted(set(indirect))}


def _raw_disagreement(workspace: Workspace, work_id: str, dimension: str,
                      as_of: str) -> dict[str, Any]:
    values = [s["score"] for s in workspace.raw_scores_for(work_id, dimension)
              if s["on"] <= as_of]
    max_score = _max_score(workspace, workspace.work(work_id)["rubric_id"])
    if len(values) < 2:
        return {"normalized_range": 0.0, "range": 0.0, "count": len(values),
                "max_score": max_score}
    return {
        "normalized_range": (max(values) - min(values)) / max_score,
        "range": max(values) - min(values),
        "count": len(values),
        "max_score": max_score,
    }


def review_priorities(workspace: Workspace, dimension: str | None = None,
                      as_of: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Rank dimension-level review targets, never whole works as one flag."""
    as_of = _as_of(workspace, as_of)
    dimensions = ([dimension] if dimension else
                  workspace.rubric(workspace.bundle.rubrics[0]["id"])["dimensions"])
    priorities = []
    for work in workspace.bundle.works:
        rubric = workspace.rubric(work["rubric_id"])
        for dim in dimensions:
            if dim not in rubric["dimensions"]:
                continue
            state = dimension_state(workspace, work["id"], dim, as_of)
            if state["status"] == RESOLVED:
                continue
            disagreement = _raw_disagreement(workspace, work["id"], dim, as_of)
            impact = _publication_impact(workspace, work["id"])
            network = calibration_network(workspace, dim, work["rubric_id"], as_of)
            gap = bool(network["isolated"]) and not network["approvals"]
            contested = state["status"] == CONTESTED
            score = (
                0.45 * disagreement["normalized_range"]
                + 0.30 * (1.0 if impact["direct"] else 0.6 if impact["indirect"] else 0.15)
                + 0.15 * (1.0 if contested else 0.4)
                + 0.10 * (1.0 if gap else 0.0)
            )
            candidates = plan_assignments(workspace, work["id"], dim, as_of, apply_limit=False)
            priorities.append({
                "work_id": work["id"],
                "dimension": dim,
                "rubric_id": work["rubric_id"],
                "status": state["status"],
                "priority": round(score, 4),
                "disagreement": disagreement,
                "published_impact": impact,
                "calibration_gap": gap,
                "eligible_reviewers": len(candidates["eligible"]),
                "reasons": _priority_reasons(disagreement, impact, contested, gap,
                                             candidates["eligible"]),
                "raw_score_ids": state["raw_score_ids"],
            })
    priorities.sort(key=lambda item: (-item["priority"], item["work_id"], item["dimension"]))
    if limit:
        priorities = priorities[:limit]
    return priorities


def _priority_reasons(disagreement, impact, contested, gap, eligible) -> list[str]:
    reasons = []
    if disagreement["normalized_range"] >= 0.25:
        reasons.append(
            f"教师分歧达到规则的 {disagreement['normalized_range']:.0%}")
    if impact["direct"]:
        reasons.append("评分直接进入已发布比较：" + "、".join(impact["direct"]))
    elif impact["indirect"]:
        reasons.append("所在学校出现在已发布比较：" + "、".join(impact["indirect"]))
    if contested:
        reasons.append("正式复核已认定材料不足，争议仍未解决")
    if gap:
        reasons.append("该维度校准网络存在孤立教师且无有效联系")
    if eligible == 0:
        reasons.append("当前没有合格且有余量的复核教师，需要先调整分派")
    return reasons


def plan_assignments(workspace: Workspace, work_id: str, dimension: str,
                     as_of: str | None = None, apply_limit: bool = True) -> dict[str, Any]:
    """Eligibility and ranking for a blind review of one work dimension."""
    as_of = _as_of(workspace, as_of)
    work = workspace.work(work_id)
    if dimension not in workspace.rubric(work["rubric_id"])["dimensions"]:
        raise DomainError("维度不属于作品规则")
    initial = {s["teacher_id"] for s in workspace.raw_scores_for(work_id, dimension)}
    school = workspace.school_of_work(work_id)
    network = calibration_network(workspace, dimension, work["rubric_id"], as_of)
    main = set(network["main_component"])
    candidates = []
    excluded = []
    for teacher in workspace.bundle.teachers:
        tid = teacher["id"]
        reasons = []
        if tid in initial:
            reasons.append("曾首评该作品")
        if teacher["school_id"] == school:
            reasons.append("任教学校即学生学校")
        if workspace.teacher_has_seen(tid, work_id):
            reasons.append("已看过该校准作品，不能盲评")
        if workspace.teacher_blocked(tid, as_of):
            reasons.append("校准资格已撤回")
        if any(a["work_id"] == work_id and a["teacher_id"] == tid
               and a["status"] in {"offered", "accepted", "completed"}
               for a in workspace.assignments):
            reasons.append("该作品已分派给此教师")
        remaining = workspace.teacher_capacity(tid) - workspace.assignment_load(tid)
        if remaining <= 0:
            reasons.append("本周复核容量已满")
        if reasons:
            excluded.append({"teacher_id": tid, "reasons": reasons,
                             "remaining_capacity": max(remaining, 0)})
            continue
        gap_role = tid not in main
        score = (
            0.5 * (1.0 if gap_role else 0.0)
            + 0.3 * (remaining / max(workspace.teacher_capacity(tid), 1))
            + 0.2 * (1.0 if work["anchor"] else 0.0)
        )
        candidates.append({
            "teacher_id": tid,
            "school_id": teacher["school_id"],
            "remaining_capacity": remaining,
            "closes_calibration_gap": gap_role,
            "rank_score": round(score, 4),
            "why": ("补充校准网络缺口" if gap_role else "尺度已在共同作品网络内")
                   + ("；校准作品复核" if work["anchor"] else ""),
        })
    candidates.sort(key=lambda item: (-item["rank_score"], item["teacher_id"]))
    if apply_limit:
        for item in candidates:
            item["proposed"] = True
        for item in excluded:
            item["proposed"] = False
    return {
        "work_id": work_id,
        "dimension": dimension,
        "as_of": as_of,
        "anchor": bool(work["anchor"]),
        "student_school": school,
        "eligible": candidates,
        "excluded": excluded,
        "blind_rule": "教师不得首评、不得任教同校、不得看过该作品",
    }


# ------------------------------------------------------- withdrawal handling
def withdrawal_impact(workspace: Workspace, approval_id: str, teacher_id: str,
                      on: str, reason: str, apply: bool = True) -> dict[str, Any]:
    approval = next((a for a in workspace.approvals if a["id"] == approval_id), None)
    if approval is None:
        raise DomainError(f"未知校准联系：{approval_id}")
    if teacher_id not in approval["teacher_ids"]:
        raise DomainError("该教师不在此校准联系的适用范围内")
    before_pending = [
        a for a in workspace.assignments
        if a["teacher_id"] == teacher_id and a["status"] in {"offered", "accepted"}
    ]
    score_ids = {s["id"] for s in workspace.scores}
    affected_scores = [s for s in workspace.scores
                       if s["teacher_id"] == teacher_id]
    affected_calibration = [s for s in affected_scores
                            if s["kind"] == "calibration"
                            and s["dimension"] == approval["dimension"]
                            and s["rubric_id"] in {approval["from_rubric"], approval["to_rubric"]}]
    affected_adjudications = []
    for record in workspace.adjudications:
        assignment = next((a for a in workspace.assignments
                           if a["id"] == record["assignment_id"]), None)
        if assignment and assignment["teacher_id"] == teacher_id:
            affected_adjudications.append(record)
    affected_publications = [
        p for p in workspace.publications if approval_id in p["approval_ids"]
    ]
    affected_comparisons = []
    for publication in affected_publications:
        affected_comparisons.append({
            "publication_id": publication["id"],
            "school_ids": publication["school_ids"],
            "dimension": publication["dimension"],
            "on": publication["on"],
            "action": "标记为待复核并重跑；历史发布内容保留，不能静默改写",
        })
    applied = None
    if apply:
        # Apply first so the per-score disposition reflects the withdrawal.
        applied = workspace.withdraw_teacher_qualification(
            approval_id, teacher_id, on, reason, actor="research-lead")
    result = {
        "approval_id": approval_id,
        "teacher_id": teacher_id,
        "dimension": approval["dimension"],
        "from_rubric": approval["from_rubric"],
        "to_rubric": approval["to_rubric"],
        "on": on,
        "reason": reason,
        "unfinished_assignments": [
            {"assignment_id": a["id"], "work_id": a["work_id"], "status": a["status"]}
            for a in before_pending
        ],
        "retained_calibration_scores": [
            {"score_id": s["id"], "work_id": s["work_id"],
             "usable_for_scale_link": False, "raw_retained": True}
            for s in affected_calibration
        ],
        "retained_adjudications": [
            {"adjudication_id": r["id"], "dimension": r["dimension"],
             "decision": r["decision"], "on": r["on"],
             "basis_retained": True,
             "connection_support": False}
            for r in affected_adjudications
        ],
        "publications_affected": affected_comparisons,
        "score_dispositions": score_dispositions(workspace, teacher_id, on),
        "policy": (
            "未完成分派撤销；已完成评分与正式结论保留原值并标注联系依据失效；"
            "已发布比较标记待复核。不把评分整体重置为待评。"
        ),
    }
    if apply:
        result["applied"] = applied
        result["after_pending"] = [
            {"assignment_id": a["id"], "work_id": a["work_id"], "status": a["status"]}
            for a in workspace.assignments
            if a["teacher_id"] == teacher_id and a["status"] in {"offered", "accepted"}
        ]
        result["source_scores_unchanged"] = score_ids == {s["id"] for s in workspace.scores}
    return result
