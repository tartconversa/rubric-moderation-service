"""Command-line service for reading-review comparability and moderation.

Examples
--------
    python3 -m reading_review inspect
    python3 -m reading_review withdrawals
    python3 -m reading_review withdraw --approval L1 --teacher T4 --on 2026-07-20 \
        --reason "校准资格撤回" --state var/state.json
    python3 -m reading_review triage --dimension evidence --limit 10
    python3 -m reading_review compare --schools C1 C2 --dimension evidence
    python3 -m reading_review trace --schools C1 C2 --dimension evidence --rubric R1
    python3 -m reading_review audit --publication P1

Commands that change operational state append to ``--state`` (default
``var/state.json`` under the current directory); the source bundle is never
modified.  Everything is deterministic and uses only the standard library.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import analysis, reporting
from .bundle import BundleError, load_bundle
from .domain import DomainError, Workspace


def _bundle(args) -> Workspace:
    bundle = load_bundle(args.materials)
    if getattr(args, "state", None):
        return Workspace.load(bundle, args.state)
    return Workspace(bundle)


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def cmd_inspect(args) -> None:
    workspace = _bundle(args)
    _print({
        "source": workspace.bundle.receipt(),
        "authorities": workspace.authority_register(),
        "as_of": workspace.default_as_of(),
    })


def cmd_dimensions(args) -> None:
    workspace = _bundle(args)
    _print(analysis.work_states(workspace, args.work, args.as_of))


def cmd_scale(args) -> None:
    workspace = _bundle(args)
    if args.dimension:
        payload = analysis.scale_fit(workspace, args.dimension, args.rubric, args.as_of)
    else:
        payload = analysis.variance_explanation(workspace, args.dimension or "evidence",
                                                args.as_of)
    _print(payload)


def cmd_variance(args) -> None:
    workspace = _bundle(args)
    _print(analysis.variance_explanation(workspace, args.dimension, args.as_of))


def cmd_compare(args) -> None:
    workspace = _bundle(args)
    _print(analysis.compare_schools(workspace, args.schools, args.dimension,
                                    args.rubric, args.as_of))


def cmd_trace(args) -> None:
    workspace = _bundle(args)
    _print(reporting.trace_comparison(workspace, args.schools[0], args.schools[1],
                                      args.dimension, args.rubric, args.as_of))


def cmd_triage(args) -> None:
    workspace = _bundle(args)
    _print(analysis.review_priorities(workspace, args.dimension, args.as_of, args.limit))


def cmd_plan(args) -> None:
    workspace = _bundle(args)
    _print(analysis.plan_assignments(workspace, args.work, args.dimension, args.as_of))


def cmd_offer(args) -> None:
    workspace = _bundle(args)
    _print(workspace.offer_assignment(args.work, args.teacher, args.on,
                                      reason=args.reason))


def cmd_accept(args) -> None:
    workspace = _bundle(args)
    _print(workspace.accept_assignment(args.assignment, args.on))


def cmd_return(args) -> None:
    workspace = _bundle(args)
    _print(workspace.return_assignment(args.assignment, args.on, args.reason))


def cmd_complete(args) -> None:
    workspace = _bundle(args)
    decisions = json.loads(Path(args.decisions).read_text(encoding="utf-8"))
    _print(workspace.complete_assignment(args.assignment, decisions, args.on))


def cmd_withdraw(args) -> None:
    workspace = _bundle(args)
    _print(analysis.withdrawal_impact(workspace, args.approval, args.teacher,
                                      args.on, args.reason))


def cmd_withdrawals(args) -> None:
    workspace = _bundle(args)
    _print({
        "approvals": [
            workspace.approval_status(a["id"], workspace.default_as_of())
            for a in workspace.approvals
        ],
        "qualification_withdrawals": workspace.withdrawal_events,
    })


def cmd_audit(args) -> None:
    workspace = _bundle(args)
    _print(analysis.audit_publication(workspace, args.publication, args.as_of))


def cmd_impact(args) -> None:
    workspace = _bundle(args)
    _print(reporting.action_impact(workspace, args.schools[0], args.schools[1],
                                   args.dimension, args.rubric, args.as_of))


def cmd_serve(args) -> None:
    from .server import serve
    serve(_bundle(args), host=args.host, port=args.port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m reading_review",
                                     description="阅读作品评分的可比性与复核分派")
    parser.add_argument("--materials", default="materials", help="原始资料目录")
    parser.add_argument("--state", default=None, help="运行状态文件（默认只读，不落盘）")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=func)
        p.add_argument("--as-of", default=None)
        return p

    add("inspect", cmd_inspect, "显示原始资料指纹和三种权威来源")
    p = add("dimensions", cmd_dimensions, "显示作品的逐维状态")
    p.add_argument("--work", required=True)

    p = add("scale", cmd_scale, "显示教师尺度与共同作品网络")
    p.add_argument("--dimension", default="evidence")
    p.add_argument("--rubric", default="R1")

    p = add("variance", cmd_variance, "说明作品难易、教师尺度与规则变化的可区分程度")
    p.add_argument("--dimension", default="evidence")

    p = add("compare", cmd_compare, "按维度、按规则的学校比较")
    p.add_argument("--schools", nargs="+", required=True)
    p.add_argument("--dimension", required=True)
    p.add_argument("--rubric", default=None)

    p = add("trace", cmd_trace, "从学校差异追到作品、裁决与联系依据")
    p.add_argument("--schools", nargs=2, required=True)
    p.add_argument("--dimension", required=True)
    p.add_argument("--rubric", default=None)

    p = add("triage", cmd_triage, "按维度优先级安排有限复核人力")
    p.add_argument("--dimension", default=None)
    p.add_argument("--limit", type=int, default=10)

    p = add("plan", cmd_plan, "列出某作品某维度的合格复核教师")
    p.add_argument("--work", required=True)
    p.add_argument("--dimension", required=True)

    p = add("offer", cmd_offer, "创建复核分派（待接受）")
    p.add_argument("--work", required=True)
    p.add_argument("--teacher", required=True)
    p.add_argument("--on", default=None)
    p.add_argument("--reason", default="")
    p.set_defaults(need_state=True)

    p = add("accept", cmd_accept, "教师接受复核")
    p.add_argument("--assignment", required=True)
    p.add_argument("--on", default=None)
    p.set_defaults(need_state=True)

    p = add("return", cmd_return, "教师退回复核")
    p.add_argument("--assignment", required=True)
    p.add_argument("--on", default=None)
    p.add_argument("--reason", default="")
    p.set_defaults(need_state=True)

    p = add("complete", cmd_complete, "按逐维决定完成复核")
    p.add_argument("--assignment", required=True)
    p.add_argument("--decisions", required=True, help="逐维决定 JSON 文件")
    p.add_argument("--on", default=None)
    p.set_defaults(need_state=True)

    p = add("withdraw", cmd_withdraw, "撤回教师校准资格并分类处理后续影响")
    p.add_argument("--approval", required=True)
    p.add_argument("--teacher", required=True)
    p.add_argument("--on", required=True)
    p.add_argument("--reason", default="")
    p.set_defaults(need_state=True)

    add("withdrawals", cmd_withdrawals, "列出校准联系与撤回状态")

    p = add("audit", cmd_audit, "审计已发布比较的引用范围")
    p.add_argument("--publication", required=True)

    p = add("impact", cmd_impact, "逐步显示复核行动对比较的影响")
    p.add_argument("--schools", nargs=2, required=True)
    p.add_argument("--dimension", required=True)
    p.add_argument("--rubric", default=None)

    p = add("serve", cmd_serve, "启动本地服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "need_state", False) and not args.state:
        parser.error("该操作会改变状态，必须提供 --state 写入位置")
    try:
        args.func(args)
    except (BundleError, DomainError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
