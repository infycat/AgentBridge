import json
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from agentbridge.mention_replies import AgentRunError, MentionReplies, _agy_work_settings, _antigravity_result
from agentbridge.store import BridgeStore


class MentionReplyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = BridgeStore(Path(directory.name) / "state.db")
        self.codex = self.store.join_group("codex", "test", "test responder")
        self.antigravity = self.store.join_group("antigravity", "test", "test responder")

    def test_antigravity_reports_a_blocked_background_tool(self):
        log = Path(self.store.db_path).parent / "agy.log"
        log.write_text('Startup: You are not logged into Antigravity.\nSilent auth succeeded.\n'
                       'Print mode: soft-denying tool confirmation "Bash" at step 2\n')
        with self.assertRaises(AgentRunError) as result:
            _antigravity_result(b'{"status":"CANCELED","response":""}', b'', 1, log)
        self.assertEqual(result.exception.reason, "permission")

        text = _antigravity_result(b'{"status":"SUCCESS","response":"Edited the file."}', b'', 0, log)
        self.assertEqual(text, "Edited the file.\n有工具因需要核准而未執行。")

    def test_antigravity_distinguishes_cli_failures(self):
        log = Path(self.store.db_path).parent / "missing-agy.log"
        for phrase, reason in ((b"authentication required", "sign_in"), (b"workspace is not trusted", "workspace"),
                               (b"print timeout", "timeout"), (b"invalid model selection", "model")):
            with self.subTest(reason=reason), self.assertRaises(AgentRunError) as result:
                _antigravity_result(b'{"status":"ERROR"}', phrase, 1, log)
            self.assertEqual(result.exception.reason, reason)
        self.assertEqual(_antigravity_result(b'{"status":"SUCCESS","response":"ok"}', b'', 0, log), "ok")

    def test_antigravity_work_uses_only_a_session_sandbox_policy(self):
        home = Path(self.store.db_path).parent
        settings = home / ".gemini" / "antigravity-cli" / "settings.json"
        with patch("agentbridge.mention_replies.Path.home", return_value=home), \
             patch("agentbridge.mention_replies._agy_supports_session_settings", return_value=True) as supported:
            self.assertEqual(_agy_work_settings("agy.exe"), ["--settings", '{"toolPermission":"proceed-in-sandbox"}'])
            self.assertFalse(settings.exists())
            supported.return_value = False
            self.assertEqual(_agy_work_settings("agy.exe"), [])
            supported.return_value = True
            settings.parent.mkdir(parents=True)
            explicit = '{"toolPermission":"strict"}'
            settings.write_text(explicit, encoding="utf-8")
            self.assertEqual(_agy_work_settings("agy.exe"), [])
            self.assertEqual(settings.read_text(encoding="utf-8"), explicit)
            project = home / "project"
            overrides = json.loads(_agy_work_settings("agy.exe", project)[1])
            self.assertEqual(overrides["toolPermission"], "strict")
            self.assertFalse(overrides["allowNonWorkspaceAccess"])
            self.assertIn("mcp(AgentBridge/claim_task)", overrides["permissions"]["allow"])
            self.assertNotIn("mcp(AgentBridge/*)", overrides["permissions"]["allow"])

    def dispatcher(self, run=None):
        reply = run or (lambda exe, member, text, cancel: f"{member['agent_name']}: {text}")
        dispatcher = MentionReplies(self.store, resolve=lambda client: f"{client}.exe", run=reply)
        self.addCleanup(dispatcher.close)
        return dispatcher

    def assert_replies(self, text, expected):
        deadline = time.monotonic() + 3
        while True:
            messages = self.store.get_group_state()["messages"]
            found = {
                m["sender_name"] for m in messages
                if not m["is_owner"] and m["action_type"] == "message" and m["summary"].endswith(text)
            }
            if found == expected:
                return
            if time.monotonic() >= deadline:
                self.assertEqual(found, expected)
            time.sleep(.03)

    def test_target_and_all(self):
        dispatch = self.dispatcher()
        target = self.store.owner_message("target", "@" + self.codex["agent_name"])
        self.assertEqual(dispatch.submit(target)["replying"], [self.codex["agent_name"]])
        self.assert_replies("target", {self.codex["agent_name"]})
        everyone = self.store.owner_message("everyone", "@all")
        expected = {self.codex["agent_name"], self.antigravity["agent_name"]}
        self.assertEqual(set(dispatch.submit(everyone)["replying"]), expected)
        self.assert_replies("everyone", expected)

    def test_only_explicit_mentions_and_unmuted_members(self):
        dispatch = self.dispatcher()
        self.assertEqual(dispatch.submit(self.store.owner_message("ordinary")), {})
        self.store.set_member_muted(self.antigravity["agent_id"], True)
        self.assertEqual(dispatch.submit(self.store.owner_message("muted", "@all"))["replying"], [self.codex["agent_name"]])
        self.assert_replies("muted", {self.codex["agent_name"]})
        self.store.set_channel_enabled(False)
        self.assertEqual(dispatch.submit(self.store.owner_message("disabled", "@all")), {"reason": "channel_off"})

    def test_member_report_mentions_reply_without_dispatching_work(self):
        def run(exe, member, text, cancel, *, sender_name, still_allowed):
            self.assertTrue(still_allowed())
            return f"{sender_name}: {text}"

        dispatch = self.dispatcher(run)
        sender = self.antigravity["agent_id"]
        target = "@" + self.codex["agent_name"]
        inline = self.store.report_step(sender, "edit", f"Thanks {target}")
        self.assertEqual(dispatch.submit(self.store.get_message(inline["event_id"])), {})
        for mention in (target, "@all"):
            report = self.store.report_step(sender, "edit", f"Progress for {mention}", mention_to=mention)
            self.assertEqual(dispatch.submit(self.store.get_message(report["event_id"]))["replying"], [self.codex["agent_name"]])
            self.assert_replies(f"{self.antigravity['agent_name']}: Progress for {mention}", {self.codex["agent_name"]})
        self.store.set_member_muted(sender, True)
        report = self.store.report_step(sender, "edit", "muted", mention_to=target)
        self.assertEqual(dispatch.submit(self.store.get_message(report["event_id"]))["replying"], [])

    def test_reset_discards_pending_reply(self):
        started, release = Event(), Event()

        def run(exe, member, text, cancel):
            started.set()
            release.wait(3)
            return "late reply"

        dispatch = self.dispatcher(run)
        dispatch.submit(self.store.owner_message("old", "@" + self.codex["agent_name"]))
        self.assertTrue(started.wait(2))
        presence = {member["agent_id"]: member["state"] for member in self.store.get_presence(dispatch.active_members())}
        self.assertEqual(presence[self.codex["agent_id"]], "working")
        self.store.reset_session()
        self.store.owner_message("new", "@" + self.codex["agent_name"])
        release.set()
        dispatch.pool.shutdown(wait=True)
        self.assertEqual(dispatch.active_members(), set())
        self.assertEqual([m["summary"] for m in self.store.get_group_state()["messages"]], ["new"])

    def test_named_handoff_starts_work_and_returns_results(self):
        saved = []
        project = Path(self.store.db_path).parent

        def work(exe, member, text, cancel, **settings):
            saved.append((member["agent_id"], text, settings["project"], settings["work_context"]))
            return "implementation report"

        dispatch = MentionReplies(self.store, resolve=lambda client: f"{client}.exe", run=work, project=project)
        self.addCleanup(dispatch.close)
        self.store.report_step(self.codex["agent_id"], "edit", "target progress", ["draft.txt"])
        self.store.lock_files(self.codex["agent_id"], ["draft.txt"])
        self.store.report_step(self.antigravity["agent_id"], "edit", "other member progress", ["other.txt"])
        detail = "Implement the request.\n" + "x" * 300
        result = self.store.report_step(self.antigravity["agent_id"], "handoff", detail, [], "@" + self.codex["agent_name"])
        origin = self.store.get_message(result["event_id"])
        self.assertEqual(dispatch.submit(origin, work=True)["replying"], [self.codex["agent_name"]])
        dispatch.work_pool.shutdown(wait=True)
        self.assertEqual(len(saved), 1)
        agent_id, prompt, cwd, context = saved[0]
        self.assertEqual((agent_id, prompt, cwd), (self.codex["agent_id"], detail, project))
        self.assertEqual(context["active_locks"], ["draft.txt"])
        self.assertEqual(len(context["recent_reports"]), 1)
        self.assertIn("target progress", context["recent_reports"][0])
        self.assert_replies("implementation report", {self.codex["agent_name"]})

    def test_sender_cannot_assign_to_self_or_while_muted(self):
        dispatch = MentionReplies(self.store, resolve=lambda client: f"{client}.exe", project=Path(self.store.db_path).parent)
        self.addCleanup(dispatch.close)
        other_codex = self.store.join_group("codex", "second-test", "test responder", new_identity=True)
        self_assignment = self.store.report_step(self.codex["agent_id"], "handoff", "self", [], "@" + self.codex["agent_name"])
        self.assertEqual(dispatch.submit(self.store.get_message(self_assignment["event_id"]), work=True)["replying"], [])
        self.store.set_member_muted(self.codex["agent_id"], True)
        muted = self.store.report_step(self.codex["agent_id"], "handoff", "muted", [], "@" + other_codex["agent_name"])
        self.assertEqual(dispatch.submit(self.store.get_message(muted["event_id"]), work=True)["replying"], [])

    def test_ticket_dispatch_checks_stage_and_distinguishes_unfinished_work(self):
        self.store.company.assign_workstation("seat-engine", self.codex["agent_id"])
        stages = self.store.company.roadmap.create_plan(None, "交付驗收", [
            {"title": title, "description": "驗收狀態檔", "tasks": [
                {"title": title, "department": "引擎研發", "priority": "normal"},
            ]} for title in ("第一階段", "第二階段")
        ])["stages"]
        ticket_ids = [stage["departments"][0]["tasks"][0]["task_id"] for stage in stages]
        called = Event()
        release = Event()

        def work(_exe, _member, _text, _cancel, *, project, still_allowed, work_context):
            self.assertTrue(still_allowed())
            self.assertEqual(work_context["ticket"]["task_id"], ticket_ids[0])
            called.set()
            self.assertTrue(release.wait(3))
            return "CLI 宣稱完成"

        dispatcher = MentionReplies(self.store, resolve=lambda client: f"{client}.exe", run=work,
                                    project=Path(self.store.db_path).parent)
        self.addCleanup(dispatcher.close)
        target = "@" + self.codex["agent_name"]
        closed_phase = self.store.owner_message(f"驗收{ticket_ids[1]}", target, action_type="handoff")
        rejected = dispatcher.submit(closed_phase, work=True)
        self.assertIn("尚未解鎖", rejected["blocked"][self.codex["agent_name"]])
        self.assertFalse(called.is_set())
        origin = self.store.owner_message(f"執行{ticket_ids[0]}", target, action_type="handoff")
        self.assertEqual(dispatcher.submit(origin, work=True)["replying"], [self.codex["agent_name"]])
        self.assertTrue(called.wait(3))
        repeated = self.store.owner_message(f"重送{ticket_ids[0]}", target, action_type="handoff")
        try:
            self.assertEqual(dispatcher.submit(repeated, work=True)["busy"], [self.codex["agent_name"]])
        finally:
            release.set()
        dispatcher.work_pool.shutdown(wait=True)
        self.assertEqual(self.store.company.get_task(ticket_ids[0])["status"], "todo")
        self.assertTrue(any("工單未完成（todo）" in msg["summary"] for msg in self.store.get_group_state()["messages"]))
