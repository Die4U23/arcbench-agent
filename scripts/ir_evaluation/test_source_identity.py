import unittest

from scripts.ir_evaluation.source_identity import bind_check_source, replay


def source(pointer, owner, case, kind, text):
    return dict(source_id=pointer, owner_id=owner, scenario_id=case, kind=kind, text=text)


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.sources = [
            source("/parent", "root", None, "description", "Apply to all queues."),
            source("/left/description", "left", None, "description", "Queue stays visible."),
            source("/left/first", "left", "stage-1", "When", "先选择项目。"),
            source("/left/result", "left", "stage-1", "Then", "The queue is paused."),
            source("/left/next", "left", "stage-2", "When", "Restart the queue."),
            source("/right/first", "right", "stage-1", "When", "先选择项目。"),
        ]
        self.leaf = dict(leaf_id="left", source_ids=[s["source_id"] for s in self.sources[:-1]])
        self.claim = dict(id="expected-state", owner_id="left", source_id="/left/result", quote="is paused")
        self.case = dict(name="演示", requirement_id="left", requirement_quote="选择项目", steps=[{}])
        self.plan = dict(cases=[self.case])
        self.link = dict(leaf_id="left", case="演示", step=0)

    def bind(self, **changes):
        return bind_check_source(self.sources, self.leaf, self.claim, self.plan, dict(self.link, **changes))

    def test_alias_uses_both_unique_public_addresses(self):
        result = self.bind()
        self.assertEqual(result["scope"], "same_scenario")
        self.assertEqual(result["case_source"], dict(source_id="/left/first", owner_id="left",
                         scenario_id="stage-1", kind="When", span=[1, 5]))
        self.assertEqual(result["claim_source"]["source_id"], "/left/result")
        self.assertEqual(result["claim_source"]["span"], [10, 19])
        proposal = dict(leaves=[dict(leaf_id="left", plan=self.plan)], obligations=[dict(self.claim, checks=[self.link])])
        self.assertEqual(replay(dict(sources=self.sources, leaves=[self.leaf]), proposal)[0]["binding"], result)

    def test_right_name_cannot_override_wrong_scenario_or_forged_quote(self):
        self.case.update(name="stage-1", requirement_quote="Restart")
        result = self.bind(case="stage-1")
        self.assertEqual(result["error"], "different_public_scenario")
        self.assertEqual(result["case_source"]["scenario_id"], "stage-2")
        self.case["requirement_quote"] = "restart"
        self.assertEqual(self.bind(case="stage-1")["error"], "case_quote_not_found")

    def test_description_and_ancestor_remain_requirement_context(self):
        self.case["requirement_quote"] = "Queue stays"
        result = self.bind()
        self.assertEqual(result["error"], "case_quote_has_no_scenario")
        self.assertIsNone(result["case_source"]["scenario_id"])
        self.claim.update(owner_id="root", source_id="/parent", quote="all queues")
        self.assertEqual(self.bind()["scope"], "requirement_context")
        self.case["requirement_quote"] = "all queues"
        self.assertEqual(self.bind()["error"], "case_quote_not_found")

    def test_repeated_occurrences_inside_one_record_or_across_records_are_ambiguous(self):
        self.sources[2]["text"] = "aaaa"
        self.case["requirement_quote"] = "aaa"
        self.assertEqual(self.bind()["error"], "case_ambiguous_quote")
        self.case["requirement_quote"] = "The queue"
        self.sources[2]["text"] = "The queue"
        self.assertEqual(self.bind()["error"], "case_ambiguous_quote")
        self.case["requirement_quote"] = "Restart"
        self.sources[3]["text"] *= 2
        self.assertEqual(self.bind()["error"], "claim_ambiguous_quote")

    def test_foreign_sources_wrong_owner_and_forged_claim_are_rejected(self):
        self.claim.update(owner_id="right", source_id="/right/first", quote="选择项目")
        self.assertEqual(self.bind()["error"], "claim_outside_leaf")
        self.claim["source_id"] = "/left/first"
        self.assertEqual(self.bind()["error"], "claim_quote_not_found")
        self.claim.update(owner_id="left", quote="never said")
        self.assertEqual(self.bind()["error"], "claim_quote_not_found")
        self.claim["quote"] = "选择项目"
        self.case["requirement_id"] = "right"
        self.assertEqual(self.bind()["error"], "case_owner_mismatch")

    def test_malformed_or_ambiguous_plan_link(self):
        self.assertEqual(self.bind(step=True)["error"], "step_not_found")
        self.assertEqual(self.bind(step=-1)["error"], "step_not_found")
        self.assertEqual(self.bind(case="elsewhere")["error"], "case_not_found")
        self.plan["cases"].append(dict(self.case))
        self.assertEqual(self.bind()["error"], "ambiguous_case_name")


if __name__ == "__main__":
    unittest.main()
