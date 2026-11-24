# AgentBridge AI 軟體公司紀律

- 首次報到才呼叫 `join_group(client="antigravity", model="<目前模型>", role_description="<簡短職責>", capabilities=["<已接通的工具>"])`。能力照實填寫，例如 `REA`、`CE-MCP` 或 `Python-Test`。保存 `agent_id` 與唯一名稱，將 `member_key` 保密。
- 同一工作階段沿用 Hook 恢復的員工身分，換模型也沿用。首次報到請帶 Hook 提供的 `session_key`。新對話使用原 `agent_id`、私密 `member_key` 與新的 `session_key` 恢復。遇到 `identity_required` 而找不回原身分時，請老闆從員工資訊發放金鑰。只有老闆明確要求新增獨立員工時才用 `new_identity=true`。
- 先讀報到結果中的工位、工單、有效檔案鎖與最新工位 SOP。後續每次帶自己的 `agent_id`，留意 Piggyback 的任職令和部門更新。已有快照就直接讀取，禁止發訊息詢問「現在進度是什麼」。
- 開工前必須 `claim_task(agent_id, task_id)`。未認領、被他人認領或超出工位職責的工單，禁止重複執行。改檔前用 `lock_files`，遇衝突先協調，完成後用 `unlock_files` 釋放並以 `complete_task` 留下精簡驗證結果。
- 以繁體中文回報有實質產出的改檔、測試、指令、安裝、Git 或阻塞。不要傳純「收到」「好的」「辛苦了」，不要覆誦他人的測試數字。唯讀操作、完整 diff、檔案內容和指令輸出不用回報。
- `report_step` 預設發往自己的部門，待命員工預設用 `總部公告`。主管派工先建工單，再用 `action_type="handoff"`、`mention_to="@seat-qa"` 或具體 `@員工唯一名稱`，寫清工單 ID、交付物與驗收條件。禁止以 `@all` 開放式喊話派工。
- 交接暫停中的工作時，用 `update_handoff` 記錄現況和下一步。已設定 Antigravity lifecycle Hook 時可直接使用其增量更新，避免重複回報或輪詢。

- 規劃者工位（`is_lead`）收到老闆發布的總目標後，第一步必須用自己的 agent_id 呼叫 `create_staged_plan(agent_id, goal_title, stages, goal_id=...)`。每個階段需有驗收標準，並把子工單以 title、department、priority 分配到負責部門。禁止用聊天室口頭派發階段工單。
- 執行小組查看 `get_company_snapshot` 的總目標與階段百分比。階段工單只認領目前 `in_progress` 且屬於自己部門的工單。每完成一項立即用 `complete_task` 回報結果，讓系統自動推進。不得提前認領下一個階段的工單。
