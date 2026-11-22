"""Business-rule tests for dimension state, scale, comparison and withdrawal."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from reading_review import analysis, reporting
from reading_review.bundle import BundleError, load_bundle
from reading_review.domain import DomainError, Workspace

MATERIALS = Path(__file__).resolve().parents[1] / "materials"


def new_workspace(state: str | None = None) -> Workspace:
    bundle = load_bundle(MATERIALS)
    return Workspace(bundle, state_path=state)


class BundleTests(unittest.TestCase):
    def test_source_bundle_untouched(self):
        bundle = load_bundle(MATERIALS)
        before = (MATERIALS / "data.json").read_bytes()
        workspace = Workspace(bundle)
        workspace.offer_assignment("W63", "T2", on="2026-07-21")
        self.assertEqual(before, (MATERIALS / "data.json").read_bytes())

    def test_invalid_reference_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            data = json.loads((MATERIALS / "data.json").read_text(encoding="utf-8"))
            meta = json.loads((MATERIALS / "contract.json").read_text(encoding="utf-8"))
            data["scores"][0]["work_id"] = "NO-SUCH-WORK"
            (dest / "data.json").write_text(json.dumps(data), encoding="utf-8")
            (dest / "contract.json").write_text(json.dumps(meta), encoding="utf-8")
            with self.assertRaisesRegex(BundleError, "关联资料不存在"):
                load_bundle(dest)


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.ws = new_workspace()

    def test_scale_adjustment_does_not_rewrite_raw(self):
        before = {s["id"]: s["score"] for s in self.ws.scores}
        fit = analysis.scale_fit(self.ws, "evidence", "R1")
        self.assertTrue(fit["observations"] > 0)
        self.assertEqual(before, {s["id"]: s["score"] for s in self.ws.scores})
        self.assertFalse(self.ws.authority_register()["raw_scores"]["mutable"])

    def test_formal_review_outranks_adjustment(self):
        state = analysis.dimension_state(self.ws, "W72", "evidence")
        self.assertEqual(state["status"], "resolved")
        self.assertEqual(state["source"], "formal_review")
        self.assertEqual(state["value"], 4)
        self.assertEqual(state["adjudication"]["id"], "D1")

    def test_insufficient_dimension_stays_contested(self):
        state = analysis.dimension_state(self.ws, "W72", "reflection")
        self.assertEqual(state["status"], "contested")
        self.assertIsNone(state["value"])
        self.assertEqual(state["adjudication"]["decision"], "insufficient")

    def test_later_approval_cannot_silently_overturn(self):
        before = analysis.dimension_state(self.ws, "W72", "reflection")
        self.ws.add_calibration_approval(
            "R1", "R2", "reflection", ["T1"], ["W1"],
            approved_on="2026-07-25", actor="research-lead")
        after = analysis.dimension_state(self.ws, "W72", "reflection")
        self.assertEqual(before["adjudication"]["id"], after["adjudication"]["id"])
        self.assertEqual(after["status"], "contested")
        self.assertEqual(after["adjudication"]["decision"], "insufficient")

    def test_explicit_supersede_keeps_history(self):
        new = self.ws.supersede_adjudication(
            "D2", "keep", None, "2026-07-26",
            "补交反思段落，材料充分，改为维持原分")
        state = analysis.dimension_state(self.ws, "W72", "reflection")
        self.assertEqual(state["status"], "resolved")
        self.assertEqual(state["adjudication"]["id"], new["id"])
        old = next(a for a in self.ws.adjudications if a["id"] == "D2")
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["superseded_by"], new["id"])
        self.assertIn("补交反思段落", new["basis"]["rationale"])
        with self.assertRaises(DomainError):
            self.ws.supersede_adjudication("D2", "keep", None, "2026-07-27", "再次")


class DimensionIndependenceTests(unittest.TestCase):
    """One usable dimension must not make every dimension usable (or invalid)."""

    def setUp(self):
        self.ws = new_workspace()

    def test_partial_adjudication_keeps_per_dimension_use(self):
        assignment = self.ws.offer_assignment("W75", "T2", on="2026-07-21")
        self.ws.accept_assignment(assignment["id"], on="2026-07-21")
        self.ws.complete_assignment(assignment["id"], [
            {"dimension": "evidence", "decision": "keep", "rationale": "一致"},
            {"dimension": "reflection", "decision": "insufficient",
             "rationale": "缺反思段落"},
        ], on="2026-07-22")
        states = analysis.work_states(self.ws, "W75")
        self.assertEqual(states["resolved_dimensions"], ["evidence"])
        self.assertEqual(states["contested_dimensions"], ["reflection"])
        self.assertIn("interpretation", states["unreviewed_dimensions"])
        # The comparison uses the usable evidence dimension even while another
        # dimension is contested (W75 is in the R2 block).
        comparison = analysis.compare_schools(self.ws, ["C3", "C1"], "evidence")
        used = {w["work_id"] for block in comparison["blocks"]
                for group in ("works_a", "works_b")
                for w in block["contrasts"][0]["provenance"][group]}
        self.assertIn("W75", used)
        # The contested dimension is excluded rather than voiding the work.
        reflection = analysis.compare_schools(self.ws, ["C3", "C1"], "reflection")
        excluded = {e["work_id"] for block in reflection["blocks"]
                    for e in block["excluded_works"]}
        self.assertIn("W75", excluded)
        self.assertNotIn("W75", {
            w["work_id"] for block in reflection["blocks"]
            for group in ("works_a", "works_b")
            for w in block["contrasts"][0]["provenance"][group]
        })


class ScaleNetworkTests(unittest.TestCase):
    def setUp(self):
        self.ws = new_workspace()

    def test_unconnected_teachers_not_claimed_unified(self):
        network = analysis.calibration_network(self.ws, "evidence", "R1")
        self.assertIn("T11", network["isolated"])
        self.assertIn("T12", network["isolated"])
        self.assertEqual(len(network["main_component"]), 10)
        fit = analysis.scale_fit(self.ws, "evidence", "R1")
        self.assertIn("T11", fit["unconnected_teachers"])
        self.assertIn("T12", fit["unconnected_teachers"])

    def test_scale_difference_disclosed_not_overclaimed(self):
        fit = analysis.scale_fit(self.ws, "evidence", "R1")
        self.assertLess(fit["separation_ratio"], 1.5)
        self.assertIn("区分", fit["verdict"])

    def test_rules_never_mixed(self):
        comparison = analysis.compare_schools(self.ws, ["C1", "C2"], "evidence")
        self.assertEqual(comparison["rubrics"], ["R1", "R2"])
        self.assertFalse(comparison["mixing_across_rules"])
        for block in comparison["blocks"]:
            self.assertIsInstance(block["rubric_id"], str)
            used_rubrics = {prov["source"] for prov in
                            block["contrasts"][0]["provenance"]["works_a"]}
            self.assertTrue(used_rubrics)
        # Raw comparison is by rule; R1 and R2 appear as separate blocks.
        self.assertEqual([b["rubric_id"] for b in comparison["blocks"]], ["R1", "R2"])

    def test_equating_requires_valid_covered_approval(self):
        eq = analysis.equating(self.ws, "evidence")
        # L1 is withdrawn by default; correlation is near zero.
        self.assertFalse(eq["approved_link"])
        self.assertFalse(eq["usable"])
        self.assertLess(eq["correlation"], 0.6)
        reflection = analysis.equating(self.ws, "reflection")
        self.assertTrue(reflection["approved_link"])  # L2 still active
        self.assertFalse(reflection["usable"])         # but statistically inadequate
        self.assertIn("统计上不足", reflection["verdict"])

    def test_uncertainty_disclosed(self):
        comparison = analysis.compare_schools(self.ws, ["C1", "C2"], "evidence")
        contrast = comparison["blocks"][0]["contrasts"][0]
        self.assertIsNotNone(contrast["ci95"])
        self.assertFalse(contrast["reliable"])
        variance = analysis.variance_explanation(self.ws, "evidence")
        self.assertIn("disclosure", variance)
        self.assertIsNotNone(variance["rules"]["R1"]["residual_sd"])


class AssignmentRuleTests(unittest.TestCase):
    def setUp(self):
        self.ws = new_workspace()

    def test_cannot_review_own_initial(self):
        with self.assertRaisesRegex(DomainError, "自己首评"):
            self.ws.offer_assignment("W72", "T1", on="2026-07-21")

    def test_cannot_review_own_school(self):
        with self.assertRaisesRegex(DomainError, "任教学校"):
            self.ws.offer_assignment("W72", "T6", on="2026-07-21")
        plan = analysis.plan_assignments(self.ws, "W72", "evidence")
        self.assertIn("T6", {x["teacher_id"] for x in plan["excluded"]})

    def test_seen_anchor_cannot_be_blind_review(self):
        # A calibration work a teacher has seen can never become a blind task,
        # even if the teacher never scored it.
        self.ws.add_seen("T11", "W3", "2026-07-21")
        with self.assertRaisesRegex(DomainError, "看过"):
            self.ws.offer_assignment("W3", "T11", on="2026-07-21")
        plan = analysis.plan_assignments(self.ws, "W3", "evidence")
        excluded = {x["teacher_id"] for x in plan["excluded"]}
        self.assertIn("T11", excluded)
        self.assertTrue(any("看过" in "；".join(x["reasons"])
                            for x in plan["excluded"] if x["teacher_id"] == "T11"))

    def test_capacity_and_return_semantics(self):
        assignment = self.ws.offer_assignment("W63", "T2", on="2026-07-21")
        self.ws.accept_assignment(assignment["id"], on="2026-07-21")
        self.assertEqual(self.ws.assignment_load("T2"), 2)  # A1 + new
        # Returning another assignment does not release completed work.
        other = next(a for a in self.ws.assignments if a["id"] == "A1")
        self.ws.return_assignment(other["id"], on="2026-07-22", reason="冲突")
        self.assertEqual(self.ws.assignment_load("T2"), 1)
        # The completed review counted before and after.
        completed = next(a for a in self.ws.assignments if a["id"] == "A3")
        self.assertEqual(completed["status"], "completed")

    def test_duplicate_confirmation_rejected(self):
        self.ws.offer_assignment("W63", "T2", on="2026-07-21")
        with self.assertRaisesRegex(DomainError, "重复确认"):
            self.ws.offer_assignment("W63", "T2", on="2026-07-22")

    def test_capacity_enforced_on_accept(self):
        ws = Workspace(load_bundle(MATERIALS))
        # Fill T6 (school C6) to its capacity of 6 with works outside C6.
        offers = ["W61", "W62", "W63", "W64", "W67", "W68"]
        for work in offers:
            record = ws.offer_assignment(work, "T6", on="2026-07-21")
            ws.accept_assignment(record["id"], on="2026-07-21")
        extra = ws.offer_assignment("W69", "T6", on="2026-07-21")
        with self.assertRaisesRegex(DomainError, "容量已满"):
            ws.accept_assignment(extra["id"], on="2026-07-21")

    def test_priority_considers_disagreement_and_impact(self):
        ranked = analysis.review_priorities(self.ws, "evidence", limit=5)
        self.assertTrue(ranked)
        self.assertTrue(any("已发布比较" in "；".join(item["reasons"])
                            for item in ranked))
        for item in ranked:
            self.assertIn(item["dimension"], {"evidence", "interpretation", "reflection"})
            self.assertNotEqual(item["status"], "resolved")


class WithdrawalTests(unittest.TestCase):
    def setUp(self):
        self.ws = new_workspace()

    def test_withdrawal_handles_three_categories(self):
        # Completed review by T2 (partial adjudication) + pending assignment.
        done = self.ws.offer_assignment("W75", "T2", on="2026-07-21")
        self.ws.accept_assignment(done["id"], on="2026-07-21")
        self.ws.complete_assignment(done["id"], [
            {"dimension": "evidence", "decision": "revise", "score": 4,
             "rationale": "按规则修分"},
            {"dimension": "reflection", "decision": "insufficient",
             "rationale": "材料不足"},
        ], on="2026-07-22")
        pending = self.ws.offer_assignment("W63", "T2", on="2026-07-22")
        self.ws.accept_assignment(pending["id"], on="2026-07-22")
        before_scores = {s["id"]: s["score"] for s in self.ws.scores}

        result = analysis.withdrawal_impact(
            self.ws, "L1", "T2", "2026-07-24", "校准材料不适用")

        self.assertTrue(result["unfinished_assignments"])
        self.assertFalse(result["after_pending"], "未完成分派必须撤销")
        self.assertTrue(len(result["retained_calibration_scores"]) > 0)
        self.assertTrue(result["retained_adjudications"])
        self.assertTrue(result["publications_affected"])
        self.assertTrue(result["source_scores_unchanged"])
        self.assertEqual(before_scores, {s["id"]: s["score"] for s in self.ws.scores})
        # Nothing was reset to pending: scores keep their ids and values.
        self.assertFalse(any(d["usable_for_scale_link"]
                             for d in result["score_dispositions"]
                             if d["dimension"] == "evidence"))
        # Completed conclusions remain, flagged for connection support.
        evidence = analysis.dimension_state(self.ws, "W75", "evidence", "2026-07-24")
        self.assertEqual(evidence["decision"] if "decision" in evidence else
                         evidence["adjudication"]["decision"], "revise")
        self.assertFalse(evidence["connection_supported"])
        self.assertEqual(evidence["adjudication"]["on"], "2026-07-22")

    def test_withdrawn_teacher_cannot_be_assigned(self):
        analysis.withdrawal_impact(self.ws, "L1", "T2", "2026-07-24", "撤回")
        plan = analysis.plan_assignments(self.ws, "W63", "evidence", "2026-07-24")
        self.assertNotIn("T2", {x["teacher_id"] for x in plan["eligible"]})
        self.assertIn("T2", {x["teacher_id"] for x in plan["excluded"]})
        with self.assertRaisesRegex(DomainError, "资格已撤回"):
            self.ws.offer_assignment("W63", "T2", on="2026-07-25")

    def test_scale_link_drops_withdrawn_scores(self):
        before = analysis.scale_fit(self.ws, "evidence", "R2")
        analysis.withdrawal_impact(self.ws, "L1", "T2", "2026-07-24", "撤回")
        after = analysis.scale_fit(self.ws, "evidence", "R2")
        self.assertLess(after["observations"], before["observations"])
        self.assertIn("T2", after["link_withdrawn_teachers"])
        # Raw scores still physically present.
        self.assertTrue(any(s["teacher_id"] == "T2" for s in self.ws.scores))

    def test_publication_marked_stale(self):
        analysis.withdrawal_impact(self.ws, "L1", "T2", "2026-07-24", "撤回")
        audit = analysis.audit_publication(self.ws, "P1", "2026-07-24")
        self.assertIn(audit["verdict"], {"invalid", "stale"})
        kinds = {p["kind"] for p in audit["problems"]}
        self.assertTrue({"approval_now_inactive",
                         "scores_from_withdrawn_qualification"} & kinds)


class PublicationAndTraceTests(unittest.TestCase):
    def setUp(self):
        self.ws = new_workspace()

    def test_publication_conflicts_are_detected(self):
        audit = analysis.audit_publication(self.ws, "P1")
        kinds = {p["kind"] for p in audit["problems"]}
        self.assertIn("scope_leak_schools", kinds)
        self.assertIn("dimension_mismatch", kinds)
        self.assertEqual(audit["verdict"], "invalid")

    def test_trace_reaches_works_decisions_and_disputes(self):
        trace = reporting.trace_comparison(self.ws, "C1", "C2", "evidence", "R1")
        contrast = trace["blocks"][0]["contrasts"][0]
        self.assertTrue(contrast["works"])
        for item in contrast["works"]:
            self.assertIn("raw_score_ids", item)
            self.assertIn("decision", item)
            self.assertIn("calibration_evidence", item)
        self.assertIn("P1", {p["publication_id"] for p in
                             trace["publications_relying_on_scope"]})

    def test_actions_report_whether_comparison_helped(self):
        assignment = self.ws.offer_assignment("W75", "T2", on="2026-07-21")
        self.ws.accept_assignment(assignment["id"], on="2026-07-21")
        self.ws.complete_assignment(assignment["id"], [
            {"dimension": "evidence", "decision": "keep", "rationale": "一致"},
        ], on="2026-07-22")
        impact = reporting.action_impact(self.ws, "C1", "C2", "evidence", "R1")
        self.assertTrue(impact)
        for step in impact:
            self.assertIn("helped", step)
            self.assertIn("changed_comparison", step)


class PersistenceTests(unittest.TestCase):
    def test_state_roundtrip_and_hash_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            ws = new_workspace(str(path))
            ws.offer_assignment("W63", "T2", on="2026-07-21")
            self.assertTrue(path.exists())
            reloaded = Workspace.load(load_bundle(MATERIALS), path)
            self.assertEqual(len(reloaded.events), 1)
            self.assertEqual(reloaded.assignments[-1]["work_id"], "W63")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["source_sha256"] = "0" * 64
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(DomainError, "指纹"):
                Workspace.load(load_bundle(MATERIALS), path)


if __name__ == "__main__":
    unittest.main()
