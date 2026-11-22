"""Small deterministic statistics used by the review analyses.

No randomness is used, so every report is reproducible from the same inputs.
All functions deliberately return their own diagnostics; callers must decide
whether the evidence is strong enough for a comparison.
"""
from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean


def _cell_means(rows):
    cells: dict[tuple[str, str], list[float]] = defaultdict(list)
    for work, teacher, value in rows:
        cells[(work, teacher)].append(value)
    return {key: mean(values) for key, values in cells.items()}


def two_way_fit(rows, iterations: int = 300, tolerance: float = 1e-10):
    """Additive work-difficulty / teacher-severity fit by alternating means.

    ``rows`` is an iterable of ``(work_id, teacher_id, normalized_score)``.
    Returns teacher offsets (summing to zero), work effects, residual standard
    deviation and approximate standard errors.
    """
    cells = _cell_means(rows)
    if not cells:
        return {
            "teachers": {}, "works": {}, "residual_sd": None, "observations": 0,
            "teacher_se": {}, "work_se": {}, "identifiable": False,
            "reason": "没有可用于两向估计的评分",
        }
    works = sorted({w for w, _ in cells})
    teachers = sorted({t for _, t in cells})
    work_effect = {w: 0.0 for w in works}
    teacher_offset = {t: 0.0 for t in teachers}
    teacher_n = defaultdict(int)
    work_n = defaultdict(int)
    for (w, t) in cells:
        teacher_n[t] += 1
        work_n[w] += 1
    for _ in range(iterations):
        previous = dict(teacher_offset)
        for w in works:
            values = [cells[(w, t)] - teacher_offset[t] for t in teachers if (w, t) in cells]
            if values:
                work_effect[w] = mean(values)
        for t in teachers:
            values = [cells[(w, t)] - work_effect[w] for w in works if (w, t) in cells]
            if values:
                teacher_offset[t] = mean(values)
        shift = mean(teacher_offset.values())
        for t in teachers:
            teacher_offset[t] -= shift
        for w in works:
            work_effect[w] += shift
        if max(abs(teacher_offset[t] - previous[t]) for t in teachers) < tolerance:
            break
    residuals = [cells[(w, t)] - work_effect[w] - teacher_offset[t] for (w, t) in cells]
    n = len(residuals)
    # Residual variance with the fitted work/teacher effects removed.  The
    # naive divisor is intentionally conservative for small anchor sets.
    residual_var = sum(r * r for r in residuals) / max(n - 1, 1)
    residual_sd = math.sqrt(residual_var)
    teacher_var = sum(v * v for v in teacher_offset.values()) / max(len(teachers), 1)
    work_var = sum(v * v for v in work_effect.values()) / max(len(works), 1)
    total = teacher_var + work_var + residual_var
    shares = {
        "teacher": teacher_var / total if total else None,
        "work": work_var / total if total else None,
        "residual": residual_var / total if total else None,
    }
    teacher_se = {t: residual_sd / math.sqrt(teacher_n[t]) for t in teachers}
    work_se = {w: residual_sd / math.sqrt(work_n[w]) for w in works}
    spread = (max(teacher_offset.values()) - min(teacher_offset.values())) if teachers else 0.0
    # With a single observation per cell, work difficulty and teacher severity
    # are algebraically confounded.  We only label the fit identifiable when
    # there is a connected, multiply-scored design.
    identifiable = len(teachers) > 1 and len(works) > 1 and min(teacher_n.values()) > 0
    return {
        "teachers": teacher_offset,
        "works": work_effect,
        "residual_sd": residual_sd,
        "teacher_se": teacher_se,
        "work_se": work_se,
        "teacher_ci95": {t: 1.96 * teacher_se[t] for t in teachers},
        "work_ci95": {w: 1.96 * work_se[w] for w in works},
        "shares": shares,
        "teacher_spread": spread,
        "observations": n,
        "cells": n,
        "teacher_count": len(teachers),
        "work_count": len(works),
        "identifiable": identifiable,
        "separability": (
            "teacher_scale_vs_residual"
            if residual_sd and spread / residual_sd >= 1.0
            else "weak"
        ),
    }


def weighted_mean_cluster(values, weights, clusters):
    """Weighted mean with a cluster-robust standard error.

    Returns estimate, standard error, 95% interval and the number of clusters.
    A household spanning two schools is one cluster, which keeps the interval
    from pretending those observations are independent.
    """
    if not values:
        return {"estimate": None, "se": None, "ci95": None, "n": 0, "clusters": 0}
    total_w = sum(weights)
    if total_w <= 0:
        total_w = float(len(values))
        weights = [1.0] * len(values)
    estimate = sum(w * v for w, v in zip(weights, values)) / total_w
    residual = [v - estimate for v in values]
    cluster_totals: dict[str, float] = defaultdict(float)
    for w, r, c in zip(weights, residual, clusters):
        cluster_totals[str(c)] += w * r
    g = len(cluster_totals)
    if g > 1:
        factor = g / (g - 1)
        variance = factor * sum(t * t for t in cluster_totals.values()) / (total_w * total_w)
        se = math.sqrt(variance)
    else:
        se = None
    return {
        "estimate": estimate,
        "se": se,
        "ci95": (estimate - 1.96 * se, estimate + 1.96 * se) if se is not None else None,
        "n": len(values),
        "clusters": g,
    }


def cluster_contrast(group_a, group_b):
    """Difference of weighted means with a cluster-robust standard error.

    ``group_a`` / ``group_b`` are sequences of ``(value, weight, cluster)``.
    A cluster (a synthetic household) may appear in both groups; its
    contributions are combined before squaring, so siblings across schools are
    not treated as independent observations.
    """
    def _mean(group):
        total = sum(w for _, w, _ in group)
        if total <= 0:
            total = float(len(group))
            group = [(v, 1.0, c) for v, _, c in group]
        estimate = sum(w * v for v, w, _ in group) / total
        return estimate, total, group

    if not group_a or not group_b:
        return {"difference": None, "se": None, "ci95": None, "clusters": 0}
    mean_a, total_a, group_a = _mean(group_a)
    mean_b, total_b, group_b = _mean(group_b)
    influence: dict[str, float] = defaultdict(float)
    for value, weight, cluster in group_a:
        influence[str(cluster)] += weight * (value - mean_a) / total_a
    for value, weight, cluster in group_b:
        influence[str(cluster)] -= weight * (value - mean_b) / total_b
    g = len(influence)
    difference = mean_a - mean_b
    if g > 1:
        variance = (g / (g - 1)) * sum(t * t for t in influence.values())
        se = math.sqrt(variance)
    else:
        se = None
    return {
        "difference": difference,
        "se": se,
        "ci95": (difference - 1.96 * se, difference + 1.96 * se) if se is not None else None,
        "clusters": g,
        "mean_a": mean_a,
        "mean_b": mean_b,
    }


def pearson(xs, ys):
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mx, my = mean(xs), mean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(vx * vy)


def linear_fit(xs, ys):
    """Ordinary least squares slope/intercept/RMSE, or None when degenerate."""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mx, my = mean(xs), mean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    if vx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx
    intercept = my - slope * mx
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    return {
        "slope": slope,
        "intercept": intercept,
        "rmse": math.sqrt(sum(r * r for r in residuals) / len(residuals)),
        "n": len(xs),
    }
