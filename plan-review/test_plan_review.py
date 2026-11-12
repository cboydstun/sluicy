import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import plan_review  # noqa: E402

REVIEWS = "## Reviewer A\n" + "claim confirmed at api/src/x.ts:12. " * 5 + "\n## Reviewer B\n" + "back-compat ok for installed clients. " * 5


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        env = {"PLAN_REVIEW_HOME": str(self.dir / "state"), "PLAN_REVIEW": ""}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.plan = self.dir / "plan.md"
        self.plan.write_text("# Plan\n\nDo the thing.\n")
        self.reviews = self.dir / "reviews.md"
        self.reviews.write_text(REVIEWS)
        self.repo = self.dir / "repo"
        (self.repo / "sub").mkdir(parents=True)
        (self.repo / ".sluicy.toml").write_text('project = "p"\n[plan_review]\nenabled = true\n')

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_hook(self, payload):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(out):
            code = plan_review.main(["hook"])
        self.assertEqual(code, 0)
        text = out.getvalue().strip()
        return json.loads(text) if text else None

    def exit_payload(self, **tool_input):
        tool_input.setdefault("planFilePath", str(self.plan))
        return {"session_id": "s1", "tool_name": "ExitPlanMode", "cwd": str(self.repo / "sub"), "tool_input": tool_input}

    def record(self, **over):
        args = ["record", "--session", "s1", "--plan-file", str(self.plan), "--reviews", str(self.reviews)]
        for k, v in over.items():
            args[args.index("--" + k.replace("_", "-")) + 1] = v
        with redirect_stdout(io.StringIO()):
            return plan_review.main(args)

    def assertDenied(self, out):
        self.assertIsNotNone(out)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_repo_without_sluicy_config_is_not_gated(self):
        payload = self.exit_payload()
        payload["cwd"] = str(self.dir)
        self.assertIsNone(self.run_hook(payload))

    def test_repo_that_does_not_enable_it_is_not_gated(self):
        (self.repo / ".sluicy.toml").write_text('project = "p"\n')
        self.assertIsNone(self.run_hook(self.exit_payload()))

    def test_project_focus_reaches_reviewer_b(self):
        (self.repo / ".sluicy.toml").write_text('project = "p"\n[plan_review]\nenabled = true\nfocus = "the mobile builds in the stores"\n')
        reason = self.assertDenied(self.run_hook(self.exit_payload()))
        self.assertIn("the mobile builds in the stores", reason)
        self.assertIn(str(Path(plan_review.__file__).resolve()), reason)

    def test_unreviewed_plan_is_denied_with_instructions(self):
        reason = self.assertDenied(self.run_hook(self.exit_payload()))
        self.assertIn("Reviewer A", reason)
        self.assertIn("--session s1", reason)
        self.assertIn(str(self.plan), reason)

    def test_other_tools_pass(self):
        self.assertIsNone(self.run_hook({"session_id": "s1", "tool_name": "Bash", "tool_input": {}}))

    def test_recorded_plan_passes_by_file(self):
        self.assertEqual(self.record(), 0)
        self.assertIsNone(self.run_hook(self.exit_payload()))

    def test_recorded_plan_passes_by_inline_text_and_empty_input(self):
        self.record()
        self.assertIsNone(self.run_hook(self.exit_payload(plan="# Plan\n\nDo the thing.", planFilePath=None)))
        self.assertIsNone(self.run_hook({"session_id": "s1", "tool_name": "ExitPlanMode", "tool_input": {}}))

    def test_edited_plan_is_denied_again(self):
        self.record()
        self.plan.write_text("# Plan\n\nDo a different thing.\n")
        reason = self.assertDenied(self.run_hook(self.exit_payload()))
        self.assertIn("changed since", reason)

    def test_review_is_per_session(self):
        self.record()
        payload = self.exit_payload()
        payload["session_id"] = "s2"
        self.assertDenied(self.run_hook(payload))

    def test_record_refuses_thin_reviews(self):
        self.reviews.write_text("## Reviewer A\nlgtm\n")
        self.assertEqual(self.record(), 1)
        self.reviews.write_text("## Reviewer A\n" + "x " * 400)
        self.assertEqual(self.record(), 1)
        self.assertEqual(self.record(reviews=str(self.dir / "missing.md")), 1)

    def test_off_switches(self):
        with mock.patch.dict(os.environ, {"PLAN_REVIEW": "off"}):
            self.assertIsNone(self.run_hook(self.exit_payload()))
        (plan_review.home() / "off").touch()
        self.assertIsNone(self.run_hook(self.exit_payload()))

    def test_bad_stdin_and_internal_error_allow(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO("not json")), redirect_stdout(out):
            self.assertEqual(plan_review.main(["hook"]), 0)
        with mock.patch.object(plan_review, "load_state", side_effect=RuntimeError("boom")):
            out = self.run_hook(self.exit_payload())
        self.assertIn("gate error", out["systemMessage"])


if __name__ == "__main__":
    unittest.main()
