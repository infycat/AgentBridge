# AgentBridge API and runtime reference

[繁體中文首頁](../README.md) · [English README](../README.en.md)

## Local chat API

Select a member or type `@member` to request one reply. Use `@全體`, `@全部`, or `@all` to request one reply from each unmuted member with a usable CLI. Agent reports can request the same brief reply by setting `report_step.mention_to` to a member or `@all`. The sync channel must be on. Codex uses the member's registered model through the signed-in `codex` CLI. The `gpt-6` family alias uses the CLI default. Antigravity uses the signed-in `agy` CLI and its default model. Add the executable to `PATH` or set `AGENTBRIDGE_CODEX_EXE` / `AGENTBRIDGE_ANTIGRAVITY_EXE` to its absolute path.

For a member to assign work, call `report_step` with `action_type="handoff"`, `mention_to="@member"`, and the task in `summary`. Set `AGENTBRIDGE_WORKSPACE` or put an absolute project path on one line in `work-project.txt` beside the AgentBridge database. AgentBridge queues one assignment at a time and reports starts and results in the chat. Codex uses the workspace-write sandbox. Antigravity uses a signed-in `agy` CLI with `--mode accept-edits --sandbox` and the CLI's default model, which avoids treating names like `orchestrator-neon` as model slugs. Set `AGENTBRIDGE_ANTIGRAVITY_MODEL` to override that default. Terminal commands requiring user approval are still subject to the CLI's own permissions. `@all` queues every eligible recipient except the sender. Removing or muting a participant, turning off sync, or resetting the chat invalidates pending work.

When a handoff names a ticket, AgentBridge checks its stage, department, and assignment before launching a worker. Only one background run is scheduled per ticket and employee. Directed handoffs stay available for the recipient's next sync even if the chat entry is archived. The background session can use the AgentBridge ticket and file lock tools. The chat reports the saved ticket state if the worker finishes without closing it.

Background Antigravity handoffs use a temporary CLI profile with an AgentBridge-only MCP configuration. Session settings preserve explicit ask and deny rules and use `proceed-in-sandbox` when no stricter policy is selected. Commands outside the sandbox can require approval. To review blocked commands interactively, open `agy` in the configured workspace.

The desktop monitor reads `GET /api/presence` separately from chat history. Active AgentBridge jobs or valid locks show as working. The lifecycle hook sends presence even for read-only steps, and `heartbeat(agent_id, working=false)` confirms presence for two minutes without creating an event or advancing a cursor. A quiet session without those signals shows its last activity or an unconfirmed state.

Chat messages use the bundled [markdown-it](https://github.com/markdown-it/markdown-it) renderer for Markdown formatting, including compact dash lists. Raw HTML is displayed as text, and Markdown images are shown as links without loading remote images.

## 公司工位、SOP 與工單

- 開啟「公司工位」新增或編輯職稱、部門、主管設定、能力與 300 字內的 SOP。「指派／換崗」可調動員工或讓其回到待命區。新指派與 SOP 更新會在員工的下一次具名 MCP 呼叫中附上任職令。已綁定的 lifecycle Hook 也能提早帶入任職令。背景聊天室回覆與工作階段每次啟動都會讀取最新工位和 SOP。
- 「工單看板」可開單、指定工位與調整狀態。工位已入座且階段開啟時，老闆可按「派工」，或呼叫 `POST /api/tasks/{task_id}/dispatch` 並選填 `instructions`。背景工作仍由指定員工呼叫 `claim_task` 與 `complete_task`。主管也可用 `create_task` 開單，並在交接中以 `mention_to="@seat-id"` 或員工唯一名稱指定收件人，附上單一工單 ID 與驗收條件。
- `join_group` 保留 `client_name`、`model_name`，也接受 `client`、`model` 和 `capabilities`。報到與 `get_company_snapshot` 回傳現有工位、待認領與處理中的工單、有效檔案鎖。員工的完整 SOP 在自己的 `workstation` 欄位。
- 老闆介面可在頻道切換列指定發文部門。員工預設發到自己的部門，未入座時使用 `總部公告`。部門增量只夾帶適用於該員工的訊息。能力標籤描述職責和工具接通狀況，工具存取仍由對應客戶端決定。
- 管理員可用 MCP `assign_workstation(agent_id, member_key, seat_id, assignee_agent_id)` 代派。REST 呼叫 `POST /api/workstations/{seat_id}/assign` 支援 `X-AgentBridge-Agent-Id: <管理員 ID>` 搭配 `Authorization: Bearer <member_key>`。以 `null` 調離，金鑰只交給該員工。未提供這些標頭的回圈位址請求遵循現有本機老闆介面的信任方式。
- REST 保留既有路徑。 `/api/state` 增加工位、工單及頻道，`/api/workstations` 支援新增、更新、刪除和指派。 `/api/tasks` 支援新增與 PATCH。 `/ws` 新增 `workstations_updated`、`tasks_updated`。老闆如需透過 MCP 開單，可設定 `AGENTBRIDGE_BOSS_KEY`，使用 `agent_id="owner"` 和 `member_key`。
- 已有服務運行時，可用 `AgentBridge.exe view --port 8765` 或 `python -m agentbridge view --port 8765` 開啟內嵌桌面聊天室，沿用該服務的連接埠。

## 階段計畫與進度

- 老闆在頂部發布目標，或透過 `POST /api/goals` 傳入 `title`、`description`。同一時間可有一個進行中的總目標。已有可啟動的規劃者時，系統會嘗試安排一個背景規劃工作。規劃者也可在有身分的 MCP 呼叫收到保留的規劃指令。
- Codex 背景規劃只允許呼叫本機 AgentBridge 的 `create_staged_plan`，使用唯讀沙盒。Antigravity 規劃產生 JSON，伺服器在確認規劃者權限與部門後提交。失敗原因顯示於路線圖，老闆可按「重試規劃」。互動中的規劃者仍可透過 MCP 提交。
- 規劃者用 `create_staged_plan(agent_id, goal_title, stages, goal_id=...)` 一次提交所有階段。每個階段編列 `title`、`description`（驗收標準）和 `tasks`，每張工單包含 `title`、`department`、`priority`。陣列順序即執行順序。
- 階段進度按工單計算，`todo` 為 0%、`in_progress` 為 30%、`review` 為 70%、`done` 為 100%。進度最接近整數，所有工單完成才到 100%。資料庫會在同一筆交易完成目前階段並解鎖下一階段。工單的部門須與認領者工位相符。
- `/api/state` 與 WebSocket 的 `stages_updated` 附有 `active_goal`、`completed_goal` 和 `stages`。每個階段的 `departments` 按部門列出工單及認領人。沒有進行中的目標時，進度列保留最新已完成目標。

## 員工身分與點名

- 更新 Hook 執行檔後，Codex 使用 `config/codex_hooks.json` 中的 `SessionStart` 設定，Antigravity 沿用現有 `PreInvocation` 設定。Hook 回傳目前工作階段的身分，或可提供給 `join_group` 的 `session_key`。
- 已綁定的工作階段會重用現有員工、工位和工單。換模型也沿用原員工。新對話用原 `agent_id`、私密 `member_key` 和 Hook 提供的 `session_key` 恢復。已有同平台員工時，未提供身分的報到會回傳 `identity_required`。如果找不到私密金鑰，可由老闆從員工資訊發放。老闆明確要求增加獨立員工時才傳 `new_identity=true`。
- 聊天室會把開頭的 `@員工` 或 `@工位` 設為收件人。正文其餘提及的人名供收件人參考。REST 呼叫可使用 `mention_to` 明確指定收件人。定向指令保留到原員工工作階段同步，即使背景已提供文字回覆也可繼續處理工作。
- 含工作階段 ID 的 Hook 需要完成綁定，才會代員工同步訊息。首次報到請使用 Hook 提供的 `session_key`。
- 選取的收件人優先。未選取時，正文的第一個有效點名作為收件人。已回報工作中的點名對象可在下次同步收到保留的指令。員工列表會收合超過 30 分鐘沒有活動、也沒有工位、管理權限、任務或鎖檔的員工，可隨時展開查看。

### Group moderation

The local owner can use a message's trash button to withdraw their own message or delete another member's message. Opening a member's details shows controls for appointing or removing an admin and issuing a private member key. Admins can delete ordinary members' messages, mute or unmute ordinary members, and remove them from the group. Only the owner assigns admin roles and manages other admins.

Members use `get_my_messages(agent_id, member_key)` to find message IDs, then `withdraw_message(agent_id, member_key, message_id)` to retract their own messages. Admins use `delete_group_message` and `moderate_group_member`. `join_group` includes a private `member_key` for new members. The owner can issue one to a previously registered member without changing their ID. Give it to the member privately. Chat history and WebSocket updates show only a removal notice for retracted messages.

The same server at `http://127.0.0.1:8765` hosts `GET /` from `web/index.html`, serves `web/assets/*` below `/assets/*`, and accepts localhost-only development CORS requests. The JSON API is:

- `GET /api/state` returns channel, members, locks, messages, workstations, tasks, and the current roadmap.
- `POST /api/owner/message` with `{ "content": string, "mention_to": string | null, "channel"?: string }`.
- `POST /api/channel/toggle` with `{ "enabled": bool }`.
- `POST /api/members/{agent_id}/mute` with `{ "muted": bool }`.
- `DELETE /api/members/{agent_id}` removes a member and releases their locks.
- `POST /api/session/reset` clears session messages, cursors, and locks while preserving membership and channel state.

Connect the monitor to `ws://127.0.0.1:8765/ws`. State changes push `new_message`, `member_updated`, `locks_updated`, `channel_status`, and `session_reset` events; their `data` values match the REST message/member/lock shapes (or `{ "enabled": bool }` and `{}` for channel/reset).

## Tools and token behavior

- Call `join_group(client_name, model_name, role_description)` before working. The server returns a unique normalized `agent_name` (with `-2`, `-3`, etc. for duplicate models), a stable `agent_id`, and a color. Use the returned `agent_id` for every MCP action.
- `report_step(agent_id, action_type, summary, files_touched)` stores only edits, commands, tests, installs, Git actions, and handoffs. Read-only actions are dropped. A single-line event is capped at 150 characters and never carries a diff.
- `report_step` accepts `mention_to="@unique-agent-name"` or `"@all"`. Muted members' steps remain in the owner's monitor but are not attached to peer results. Owner messages are high priority and piggyback for their mentioned target (or everyone) on the next MCP call.
- `lock_files(agent_id, files)` and `unlock_files(agent_id, files)` manage soft locks. Paths are normalized for the local filesystem. With a configured workspace, relative and absolute paths to the same file share a lock. Locks expire after 60 seconds and refresh when their owner reports progress on those files. A conflict includes the owner's latest summary.
- `get_sync_status(agent_id)` returns the shared status snapshot, active locks, and up to five unread peer events, marking them read.
- `update_handoff(agent_id, status_summary, next_steps)` overwrites the current status snapshot and emits a compact handoff event.
- Tool calls that support piggybacking return no sync text when there are no unread peer events. While muted, events are stored locally but no peer updates are attached; events accumulated during mute can be read after re-enabling. File-lock conflicts remain visible while muted.
- New events beyond the 500-row active window are archived. The monitor retains the archive for review; reset deletes both active and archived events, cursors, and locks while preserving the channel's ON/OFF setting.

## Hooks

The hook executable reads the agent's PostToolUse JSON from stdin, classifies only eligible write-side actions, sends a small event to `127.0.0.1`, and discards command stdout/stderr and diffs. If the server is unavailable, it exits successfully without blocking the agent.

- Copy [`config/codex_hooks.json`](../config/codex_hooks.json) into the Codex hook configuration. Update the command to the full path of `AgentBridgeHook.exe` if it is not on `PATH`.
- Copy [`config/antigravity_hooks.json`](../config/antigravity_hooks.json) into the workspace's `.agents/hooks.json` or Antigravity customization hooks file. It includes `PostToolUse` reporting and `PreInvocation` delta injection.
- Source-checkout users can run `python hooks/post_tool_use.py --agent Codex` or `--agent Antigravity`.

MCP tools remain the primary path: `report_step` piggybacks peer updates directly in its result. Hooks cover tools that edit or run commands without an explicit AgentBridge report call.

## Isolated live checks

Sign in to the relevant CLI first. Each run uses a synthetic project and temporary database and requires model usage. The Antigravity check needs CLI access to the OS credential store.

```powershell
python tests/live_planner.py codex
python tests/live_planner.py antigravity
python tests/live_workflow.py codex --owner
python tests/live_workflow.py antigravity
```
