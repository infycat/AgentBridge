from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agentbridge.hook import classify_tool_event
from agentbridge.server import BroadcastHub
from agentbridge.store import BridgeStore


class BridgeStoreTests(unittest.TestCase):

    def test_member_withdrawal_checks_authority_and_replaces_the_visible_message(self) -> None:
        sender = self.store.join_group("codex", "sender", "worker")
        reader = self.store.join_group("antigravity", "reader", "review")
        actor = sender["agent_id"], sender["member_key"]
        peer = reader["agent_id"], reader["member_key"]
        self.store.lock_files(actor[0], ["example.txt"])
        posted = self.store.report_step(actor[0], "edit", "discard this message", ["example.txt"], "@all")
        origin = self.store.get_message(posted["event_id"])
        self.assertTrue(self.store.can_reply_to_mention(origin, peer[0]))
        with self.assertRaises(PermissionError):
            self.store.remove_message(origin["id"], actor=peer, own_only=True)
        with self.assertRaises(PermissionError):
            self.store.remove_message(origin["id"], actor=(actor[0], peer[1]))
        self.assertEqual(self.store.member_messages(*actor)[0]["id"], origin["id"])
        removed = self.store.remove_message(origin["id"], actor=actor, own_only=True)
        self.assertEqual(removed["removal_kind"], "withdrawn")
        self.assertEqual(removed["files_touched"], [])
        self.assertEqual(self.store.remove_message(origin["id"], actor=actor, own_only=True), removed)
        self.assertFalse(self.store.can_reply_to_mention(origin, peer[0]))
        views = [self.store.get_group_state(), self.store.dashboard_snapshot(), self.store.get_sync_status(peer[0])]
        self.assertNotIn("discard this message", str(views))
        self.assertNotIn(actor[1], str(views))
        self.assertEqual(self.store.list_locks()[0]["display_path"], "example.txt")

    def test_admin_access_can_be_granted_revoked_and_rotated(self) -> None:
        admin = self.store.join_group("codex", "moderator", "review")
        member = self.store.join_group("antigravity", "worker", "worker")
        actor = admin["agent_id"], admin["member_key"]
        target = member["agent_id"]
        event = self.store.report_step(target, "test", "finished")["event_id"]
        owner = self.store.owner_message("Owner announcement")["id"]
        with self.assertRaises(PermissionError):
            self.store.remove_message(event, actor=actor)
        self.assertTrue(self.store.set_member_admin(actor[0], True)["is_admin"])
        with self.assertRaises(PermissionError):
            self.store.remove_message(owner, actor=actor)
        self.assertEqual(self.store.remove_message(event, actor=actor)["removal_kind"], "deleted")
        self.assertTrue(self.store.set_member_muted(target, True, actor=actor)["muted"])
        self.store.set_member_admin(target, True)
        with self.assertRaises(PermissionError):
            self.store.remove_member(target, actor=actor)
        self.store.set_member_admin(target, False)
        current_key = self.store.issue_member_key(actor[0])
        with self.assertRaises(PermissionError):
            self.store.remove_member(target, actor=actor)
        actor = actor[0], current_key
        self.store.set_member_admin(actor[0], False)
        with self.assertRaises(PermissionError):
            self.store.remove_member(target, actor=actor)
        self.store.set_member_admin(actor[0], True)
        self.assertIsNotNone(self.store.remove_member(target, actor=actor))
        with self.assertRaises(PermissionError):
            self.store.report_step(target, "test", "removed membership")

    def test_presence_separates_activity_heartbeat_and_active_locks(self) -> None:
        with patch("agentbridge.store.time.time", return_value=1000) as now:
            identity = self.store.join_group("codex", "presence-test", "validator")["agent_id"]
            event_count = len(self.store.get_group_state()["messages"])
            self.assertEqual(self.store.get_presence()[0]["state"], "recent")
            now.return_value = 1600
            self.assertEqual(self.store.get_presence()[0]["state"], "unknown")
            self.store.heartbeat(identity)
            self.assertEqual(self.store.get_presence()[0]["state"], "online")
            self.store.heartbeat(identity, working=True)
            self.assertEqual(self.store.get_presence()[0]["state"], "working")
            self.assertEqual(len(self.store.get_group_state()["messages"]), event_count)
            now.return_value = 1721
            self.assertEqual(self.store.get_presence()[0]["state"], "recent")
            self.store.lock_files(identity, ["example.txt"])
            self.assertEqual(self.store.get_presence()[0]["state"], "working")
            now.return_value = 1722 + self.store.lock_ttl
            self.assertEqual(self.store.get_presence()[0]["state"], "recent")
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "bridge.sqlite3"
        self.store = BridgeStore(self.db_path, lock_ttl=60)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_piggyback_delta_and_empty_result(self) -> None:
        first = self.store.report_step("Codex", "edit", "added endpoint", ["src/api.py"])
        self.assertEqual(first["messages"], [])

        second = self.store.report_step("Antigravity", "test", "tests passed")
        self.assertEqual(len(second["messages"]), 1)
        self.assertIn("Codex edit src/api.py added endpoint", second["messages"][0])
        self.assertLessEqual(len(second["messages"][0]), 150)

        no_delta = self.store.report_step("Antigravity", "command", "lint completed")
        self.assertEqual(no_delta["messages"], [])
        # Read-only actions are filtered at the storage boundary and add no output.
        readonly = self.store.report_step("Antigravity", "read_file", "opened README")
        self.assertFalse(readonly["recorded"])
        self.assertEqual(readonly["messages"], [])

    def test_lock_conflict_shows_latest_owner_progress(self) -> None:
        self.store.lock_files("Codex", ["src/shared.py"])
        self.store.report_step("Codex", "edit", "refactoring parser", ["src/shared.py"])
        result = self.store.lock_files("Antigravity", ["src/shared.py"])
        self.assertEqual(result["acquired"], [])
        self.assertEqual(result["conflicts"][0]["agent_id"], "Codex")
        self.assertEqual(result["conflicts"][0]["progress"], "refactoring parser")

    def test_lock_paths_keep_long_names_and_whitespace_distinct(self) -> None:
        common = "src/" + "x" * 130
        original = [common + "one.py", "src/a  b.py", *(f"src/file-{i}.txt" for i in range(6))]
        self.assertEqual(self.store.lock_files("Codex", original)["acquired"], original)
        result = self.store.lock_files("Antigravity", [common + "two.py", "src/a b.py"])
        self.assertEqual(result["acquired"], [common + "two.py", "src/a b.py"])
        self.assertEqual(result["conflicts"], [])
        conflicts = self.store.lock_files("Antigravity", [f"./src/subdir/../{common.removeprefix('src/')}one.py"])
        self.assertEqual(conflicts["conflicts"][0]["file"], original[0])
        self.assertIn(original[0], {lock["display_path"] for lock in self.store.list_locks()})

    def test_existing_alias_lock_still_conflicts_and_unlocks(self) -> None:
        now = time.time()
        with self.store._connection() as conn:
            conn.execute("INSERT INTO file_locks(path_key,display_path,agent_id,locked_at,expires_at,progress_summary) "
                         "VALUES(?,?,?,?,?,?)", ("src/sub/../same.py", "src/sub/../same.py", "Codex", now, now + 60, "legacy"))
        blocked = self.store.lock_files("Antigravity", ["src/same.py"])
        self.assertEqual(blocked["conflicts"][0]["progress"], "legacy")
        self.assertEqual(self.store.unlock_files("Codex", ["src/same.py"])["released"], ["src/same.py"])
        self.assertEqual(self.store.lock_files("Antigravity", ["src/same.py"])["acquired"], ["src/same.py"])

    def test_project_relative_and_absolute_paths_use_the_same_lock(self) -> None:
        workspace = Path(self.temp.name)
        store = BridgeStore(workspace / "scoped.sqlite3", workspace=workspace)
        absolute = str(workspace / "src" / "file.py")
        store.lock_files("Codex", ["src/file.py"])
        result = store.lock_files("Antigravity", [absolute])
        self.assertEqual(result["conflicts"][0]["agent_id"], "Codex")
        self.assertEqual(store.unlock_files("Codex", [absolute])["released"], [absolute])
        self.store.lock_files("Codex", ["src/preexisting.py"])
        reopened = BridgeStore(self.db_path, workspace=workspace)
        prior_lock = str(workspace / "src" / "preexisting.py")
        self.assertEqual(reopened.lock_files("Antigravity", [prior_lock])["conflicts"][0]["agent_id"], "Codex")

    def test_muted_channel_records_but_does_not_broadcast(self) -> None:
        self.store.set_channel_enabled(False)
        a = self.store.report_step("Codex", "edit", "changed module", ["module.py"])
        b = self.store.report_step("Antigravity", "test", "test run completed")
        self.assertEqual(a["messages"], [])
        self.assertEqual(b["messages"], [])
        self.assertEqual(len(self.store.dashboard_snapshot()["events"]), 2)

        self.store.set_channel_enabled(True)
        resumed = self.store.get_sync_status("Antigravity")
        self.assertEqual(len(resumed["unread"]), 1)
        self.assertIn("Codex edit module.py", resumed["unread"][0])

    def test_summary_is_compact_and_never_contains_a_diff(self) -> None:
        patch = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new"
        self.store.report_step("Codex", "git", patch)
        event = self.store.dashboard_snapshot()["events"][0]
        self.assertLessEqual(len(event["line"]), 150)
        self.assertNotIn("diff --git", event["line"])
        self.assertIn("diff omitted", event["line"])

        long_paths = ["C:/workspace/" + ("nested/" * 20) + f"file{index}.py" for index in range(6)]
        self.store.report_step("Antigravity", "edit", "completed", long_paths)
        compact = self.store.dashboard_snapshot()["events"][0]["line"]
        self.assertLessEqual(len(compact), 150)
        self.assertIn("completed", compact)

    def test_wal_and_concurrent_writes(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(str(mode).lower(), "wal")

        def write(index: int) -> None:
            agent = "Codex" if index % 2 == 0 else "Antigravity"
            self.store.report_step(agent, "command", f"command {index}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(24)))
        snapshot = self.store.dashboard_snapshot(limit=100)
        self.assertEqual(len(snapshot["events"]), 24)
        ids = [event["id"] for event in snapshot["events"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_hooks_only_emit_write_side_events_and_strip_output(self) -> None:
        readonly = classify_tool_event({
            "tool_name": "Bash", "tool_input": {"command": "git status --short"},
            "tool_response": {"exit_code": 0},
        })
        self.assertIsNone(readonly)

        event = classify_tool_event({
            "tool_name": "Bash", "tool_input": {"command": "pytest -q tests/test_sync.py"},
            "tool_response": {"exit_code": 0, "stdout": "large output must not be forwarded"},
        })
        self.assertIsNotNone(event)
        self.assertEqual(event["action_type"], "test")
        self.assertEqual(event["summary"], "pytest completed")
        self.assertNotIn("large output", json.dumps(event))

        antigravity = classify_tool_event({
            "toolCall": {"name": "write_file", "args": {"FilePath": "src/bridge.py"}},
            "error": "",
        })
        self.assertEqual(antigravity["action_type"], "edit")
        self.assertEqual(antigravity["files_touched"], ["src/bridge.py"])

    def test_reset_clears_session_state_but_preserves_channel(self) -> None:
        self.store.set_channel_enabled(False)
        self.store.lock_files("Codex", ["src/temporary.py"])
        self.store.report_step("Codex", "edit", "temporary change", ["src/temporary.py"])
        self.store.reset_session()
        snapshot = self.store.dashboard_snapshot()
        self.assertEqual(snapshot["events"], [])
        self.assertEqual(snapshot["locks"], [])
        self.assertEqual(snapshot["status"]["status_summary"], "Session reset")
        self.assertFalse(snapshot["status"]["channel_enabled"])

    def test_join_group_normalizes_and_numbers_duplicate_members(self) -> None:
        first = self.store.join_group("Co Dex", "GPT 6 Luna", "backend engineer")
        second = self.store.join_group("codex", "gpt6luna", "frontend engineer", new_identity=True)
        third = self.store.join_group("Anti Gravity", "Opus 5.5", "reviewer")

        self.assertEqual(first["agent_name"], "codex-gpt6luna")
        self.assertEqual(second["agent_name"], "codex-gpt6luna-2")
        self.assertEqual(third["agent_name"], "antigravity-opus5.5")
        self.assertNotEqual(first["agent_id"], second["agent_id"])
        self.assertRegex(first["color"], r"^#[0-9A-Fa-f]{6}$")
        self.assertEqual(first["system_message"]["action_type"], "system")
        self.assertIn("已到職", first["system_message"]["summary"])
        state = self.store.get_group_state()
        self.assertEqual(len(state["members"]), 3)
        self.assertTrue({"agent_id", "agent_name", "client_name", "model_name", "role_description", "color", "muted", "last_seen_at"}.issubset(state["members"][0]))

    def test_owner_mentions_are_private_and_priority_and_muted_speech_is_hidden(self) -> None:
        codex = self.store.join_group("codex", "gpt6luna", "builder")
        antigravity = self.store.join_group("antigravity", "opus5.5", "reviewer")
        for index in range(8):
            self.store.report_step(codex["agent_id"], "edit", f"normal update {index}")

        owner = self.store.owner_message("Please review the new parser", "@antigravity-opus5.5")
        self.assertEqual(owner["sender_id"], "owner")
        self.assertEqual(owner["sender_name"], "老闆")
        self.assertTrue(owner["is_owner"])
        private_to_codex = self.store.report_step(codex["agent_id"], "test", "Codex checkpoint")
        self.assertFalse(any("Please review the new parser" in line for line in private_to_codex["messages"]))
        to_target = self.store.report_step(antigravity["agent_id"], "test", "review started")
        self.assertTrue(any("Please review the new parser" in line for line in to_target["messages"]))

        self.store.set_member_muted(antigravity["agent_id"], True)
        self.store.report_step(antigravity["agent_id"], "edit", "muted private progress")
        peer = self.store.report_step(codex["agent_id"], "command", "peer check")
        self.assertFalse(any("muted private progress" in line for line in peer["messages"]))
        visible_to_owner = self.store.get_group_state()
        self.assertTrue(any("muted private progress" in item["summary"] for item in visible_to_owner["messages"]))

    def test_hook_session_binding_and_member_removal_release_locks(self) -> None:
        member = self.store.join_group("codex", "gpt6luna", "builder")
        self.assertTrue(self.store.bind_hook_session("codex-session-1", member["agent_id"]))
        self.assertEqual(
            self.store.resolve_hook_identity("Codex", "codex-session-1"), member["agent_id"]
        )
        self.store.lock_files(member["agent_id"], ["src/shared.py"])
        removed = self.store.remove_member(member["agent_id"])
        self.assertEqual(removed["member"]["agent_name"], "codex-gpt6luna")
        self.assertEqual(self.store.list_locks(), [])
        self.assertIsNone(self.store.resolve_hook_identity("Codex", "codex-session-1"))

    def test_legacy_database_migrates_in_place(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        conn = sqlite3.connect(legacy_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,created_at REAL NOT NULL,"
            "agent_id TEXT NOT NULL,action_type TEXT NOT NULL,summary TEXT NOT NULL,files_json TEXT NOT NULL,"
            "line TEXT NOT NULL,archived_at REAL)"
        )
        conn.execute(
            "INSERT INTO events(created_at,agent_id,action_type,summary,files_json,line) "
            "VALUES(1,'Codex','edit','legacy event','[]','[00:00:01] Codex edit legacy event')"
        )
        conn.commit()
        conn.close()

        upgraded = BridgeStore(legacy_path)
        state = upgraded.get_group_state()
        self.assertEqual(state["messages"][0]["summary"], "legacy event")
        self.assertEqual(state["messages"][0]["sender_name"], "Codex")
        check = sqlite3.connect(legacy_path)
        try:
            member_columns = {row[1] for row in check.execute("PRAGMA table_info(members)")}
            event_columns = {row[1] for row in check.execute("PRAGMA table_info(events)")}
        finally:
            check.close()
        self.assertIn("agent_name", member_columns)
        self.assertIn("mention_to", event_columns)
        self.assertIn("priority", event_columns)


class BroadcastHubTests(unittest.TestCase):
    def test_concurrent_sends_still_work_after_loop_restart(self) -> None:
        hub = BroadcastHub()

        async def overlapping_sends():
            hub.bind_loop()
            started = asyncio.Event()
            release = asyncio.Event()
            received = []

            class Client:
                async def accept(self):
                    pass

                async def send_json(self, payload):
                    received.append(payload)
                    started.set()
                    await release.wait()

            client = Client()
            await hub.connect(client)
            first = asyncio.create_task(hub._broadcast({"type": "first"}))
            await asyncio.wait_for(started.wait(), timeout=1)
            second = asyncio.create_task(hub._broadcast({"type": "second"}))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)
            hub.disconnect(client)
            return received

        for _ in range(2):
            self.assertEqual(asyncio.run(overlapping_sends()), [{"type": "first"}, {"type": "second"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
