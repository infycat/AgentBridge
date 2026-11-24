import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agentbridge.mention_replies import MentionReplies
from agentbridge.store import BridgeStore


class SessionIdentityTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.store = BridgeStore(Path(scratch.name) / "test.db")

    def test_repeated_join_is_atomic_and_preserves_the_workstation_and_key(self):
        def join(_):
            return self.store.join_group("codexdesktop", "gpt-6", "員工", session_key="chat-a")

        with ThreadPoolExecutor(max_workers=4) as pool:
            registrations = list(pool.map(join, range(8)))
        first, = [entry for entry in registrations if "member_key" in entry]
        self.assertEqual({entry["agent_id"] for entry in registrations}, {first["agent_id"]})
        self.assertEqual(len(self.store.list_members()), 1)
        self.store.company.assign_workstation("seat-cto", first["agent_id"])
        resume = self.store.join_group("codex", "gpt-6.2-sol", "", session_key="chat-a")
        self.assertEqual(resume["agent_id"], first["agent_id"])
        self.assertEqual(resume["workstation"]["seat_id"], "seat-cto")
        self.assertNotIn("member_key", resume)
        with self.assertRaises(PermissionError):
            self.store.join_group("codex", "gpt-6", "", agent_id=first["agent_id"], member_key="wrong")
        recovered = self.store.join_group(
            "codex", "gpt-6", "", session_key="chat-b",
            agent_id=first["agent_id"], member_key=first["member_key"],
        )
        self.assertEqual(recovered["agent_id"], first["agent_id"])
        self.assertEqual(self.store.get_session_identity("chat-b", "Codex")["agent_id"], first["agent_id"])

    def test_unbound_duplicate_requires_explicit_new_employee(self):
        first = self.store.join_group("codex", "gpt-6", "", session_key="first")
        duplicate = self.store.join_group("codexcli", "gpt-6", "", session_key="second")
        self.assertTrue(duplicate["identity_required"])
        self.assertEqual(duplicate["existing_names"], [first["agent_name"]])
        self.assertEqual(len(self.store.list_members()), 1)
        second = self.store.join_group("codex", "gpt-6", "", session_key="second", new_identity=True)
        self.assertEqual(second["agent_name"], "codex-gpt-6-2")
        self.assertFalse(self.store.bind_hook_session("first", second["agent_id"]))
        self.assertEqual(self.store.get_session_identity("first", "Codex")["agent_id"], first["agent_id"])

    def test_unbound_hook_does_not_consume_the_only_employee_inbox(self):
        first = self.store.join_group("antigravity", "gemini", "", session_key="original")
        self.assertIsNone(self.store.resolve_hook_identity("Antigravity", "fresh"))
        self.assertIsNone(self.store.resolve_hook_identity("Codex", "original"))
        self.assertEqual(self.store.resolve_hook_identity("Antigravity", "original"), first["agent_id"])
        self.assertEqual(self.store.resolve_hook_identity("Antigravity"), first["agent_id"])

    def test_explicit_hook_identity_supports_full_ids_and_long_model_names(self):
        model = "model-" + "x" * 58
        agent = self.store.join_group("codex", model, "")
        second = self.store.join_group("codex", "another-model", "", new_identity=True)
        for identity in (agent["agent_id"], agent["agent_name"]):
            self.assertEqual(self.store.resolve_hook_identity(identity), agent["agent_id"])
        self.assertNotEqual(agent["agent_id"], second["agent_id"])

    def test_busy_direct_recipient_gets_one_durable_delivery(self):
        gemini = self.store.join_group("antigravity", "gemini", "")
        second = self.store.join_group("antigravity", "gemini", "", new_identity=True)
        self.store.heartbeat(gemini["agent_id"], working=True)
        self.store.tool_sync(gemini["agent_id"], unread=True)
        self.store.tool_sync(second["agent_id"], unread=True)
        message = self.store.owner_message("@antigravity-gemini 請分配 @antigravity-gemini-2 的介面測試工單")
        self.assertEqual(message["mention_to"], "@antigravity-gemini")
        dispatcher = MentionReplies(self.store, resolve=lambda name: self.fail("Busy receiver launched a second process"))
        self.addCleanup(dispatcher.close)
        self.assertEqual(dispatcher.submit(message)["busy"], ["antigravity-gemini"])
        with self.store._connection() as conn:
            conn.execute("UPDATE events SET archived_at=created_at WHERE id=?", (message["id"],))
        self.assertEqual(self.store.tool_sync(second["agent_id"]), [])
        self.assertIn("介面測試工單", self.store.tool_sync(gemini["agent_id"])[0])
        self.assertEqual(self.store.tool_sync(gemini["agent_id"], unread=True), [])

    def test_model_and_old_client_label_cannot_accidentally_create_a_new_employee(self):
        first = self.store.join_group("codex", "gpt-6", "舊員工")
        self.store.company.assign_workstation("seat-engine", first["agent_id"])
        with self.store._connection() as conn:
            conn.execute("UPDATE members SET client_name=? WHERE agent_id=?", ("codexdesktop-gpt-6-2", first["agent_id"]))
        for label in ("Codex", "codex-cli", "codexdesktop-gpt-6"):
            rejected = self.store.join_group(label, "gpt-6.2-sol", "新模型", session_key="lost")
            self.assertTrue(rejected["identity_required"])
            self.assertEqual(rejected["existing_names"], [first["agent_name"]])
        self.assertFalse(self.store.bind_hook_session("wrong-client", first["agent_id"], "Antigravity"))
        self.assertTrue(self.store.has_client_members("CodexDesktop"))
        self.assertIsNone(self.store.get_session_identity("lost", "Codex"))
        restored = self.store.join_group(
            "codex", "gpt-6.2-sol", "", session_key="lost",
            agent_id=first["agent_id"], member_key=first["member_key"],
        )
        self.assertEqual(restored["workstation"]["seat_id"], "seat-engine")
        self.assertEqual(restored["agent_name"], first["agent_name"])
        self.assertEqual(self.store.get_session_identity("lost", "Codex")["agent_id"], first["agent_id"])
        self.assertEqual(len(self.store.list_members()), 1)

    def test_background_reply_preserves_the_instruction_for_the_original_session(self):
        member = self.store.join_group("antigravity", "gemini", "")
        self.store.tool_sync(member["agent_id"], unread=True)
        origin = self.store.owner_message("請提供本次驗收結果", "@antigravity-gemini")
        self.store.publish_mention_reply(origin, member["agent_id"], "驗收完成")
        self.assertIn("請提供本次驗收結果", self.store.tool_sync(member["agent_id"], unread=True)[0])
        self.assertEqual(self.store.tool_sync(member["agent_id"], unread=True), [])

    def test_directed_employee_handoff_survives_archival_and_respects_department_visibility(self):
        sender = self.store.join_group("codex", "gpt-6", "")
        receiver = self.store.join_group("antigravity", "gemini", "")
        self.store.company.assign_workstation("seat-cto", sender["agent_id"])
        self.store.tool_sync(receiver["agent_id"], unread=True)
        directed = self.store.report_step(sender["agent_id"], "handoff", "驗收交接 #123", [],
                                          "@" + receiver["agent_name"])
        with self.store._connection() as conn:
            conn.execute("UPDATE events SET archived_at=created_at WHERE id=?", (directed["event_id"],))
        self.assertEqual(self.store.tool_sync(receiver["agent_id"], unread=True), [])
        self.store.company.assign_workstation("seat-qa", receiver["agent_id"])
        self.assertEqual(sum("驗收交接 #123" in item for item in
                             self.store.tool_sync(receiver["agent_id"], unread=True)), 1)
        self.assertFalse(any("驗收交接 #123" in item for item in self.store.tool_sync(receiver["agent_id"], unread=True)))


if __name__ == "__main__":
    unittest.main()
