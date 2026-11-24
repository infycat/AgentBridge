/* Workstation and ticket management in the existing company chat. */
globalThis.initCompanyPanel = ({ $, state, api, esc, refresh, memberPresence, presenceLabels }) => {
  const panel = $("companyPanel");
  const body = $("companyBody");
  const feedback = $("companyFeedback");
  const statuses = { todo: "待認領", in_progress: "處理中", review: "待驗收", done: "已完成" };
  let view = "seats", editor = null, pending = false, taskDepartment = "";
  const seatPath = (seatId) => "/api/workstations/" + encodeURIComponent(seatId);
  const employee = (id) => state.members.find((member) => member.agent_id === id);
  const tags = (items) => items.map((item) => `<span>${esc(item)}</span>`).join("");
  const option = (value, label, selected) =>
    `<option value="${esc(value)}"${value === selected ? " selected" : ""}>${esc(label)}</option>`;
  const button = (action, label, id = "", style = "ghost") =>
    `<button type="button" class="btn ${style}" data-company-action="${action}" data-id="${esc(id)}">${label}</button>`;
  const chip = (name) => `<span>${esc(name)}</span>`;
  const choice = (name, id, items, value, placeholder) =>
    `<label>${name}<select name="${id}">${placeholder ? option("", placeholder, value) : ""}${
      items.map(([key, label]) => option(key, label, value)).join("")}</select></label>`;

  function setFeedback(text, error = false) {
    feedback.textContent = text;
    feedback.classList.toggle("error", error);
  }

  function seats() {
    const cards = state.workstations.map((seat) => {
      const occupant = employee(seat.assigned_agent_id);
      const presence = occupant ? memberPresence(occupant) : "vacant";
      const tools = [...new Set([...(seat.capabilities || []), ...(occupant?.capabilities || [])])];
      return `<article class="company-card">
        <h3>${esc(seat.title)}</h3>
        <div class="company-tags">${chip("#" + seat.department)}${seat.is_lead ? chip("主管") : ""}${tags(tools)}</div>
        <p><span class="dot ${presence}" aria-hidden="true"></span>${
          occupant ? esc(occupant.agent_name + " · " + presenceLabels[presence]) : "空缺"}</p>
        ${seat.sop_prompt ? `<details><summary>工位 SOP</summary><p>${esc(seat.sop_prompt)}</p></details>` : ""}
        <div class="company-actions">${button("assign", "指派／換崗", seat.seat_id)}
          ${button("edit-seat", "編輯工位", seat.seat_id)}</div>
      </article>`;
    }).join("");
    const sitting = new Set(state.workstations.map((seat) => seat.assigned_agent_id));
    const lobby = state.members.filter((member) => !sitting.has(member.agent_id));
    return `<div class="company-toolbar">${button("add-seat", "＋ 新增工位", "", "primary")}</div>
      ${cards ? `<div class="company-grid">${cards}</div>` : '<p class="company-empty">尚無工位</p>'}
      <div class="company-card"><h3>待命區</h3><p>${lobby.length ? lobby.map((member) => esc(member.agent_name)).join("、") : "目前無待命員工"}</p></div>`;
  }

  function board() {
    const seatsById = new Map(state.workstations.map((seat) => [seat.seat_id, seat]));
    const openStages = new Set(state.stages.filter((stage) => stage.status === "in_progress").map((stage) => stage.stage_id));
    if (taskDepartment && !state.channels.includes(taskDepartment)) taskDepartment = "";
    const filtered = state.tasks.filter((task) => !taskDepartment || task.department === taskDepartment);
    const groups = Object.entries(statuses).map(([status, label]) => {
      const matches = filtered.filter((task) => task.status === status);
      const cards = matches.map((task) => {
        const assigned = seatsById.get(task.assignee_seat_id);
        const assignee = employee(task.assignee_agent_id);
        const occupant = employee(assigned?.assigned_agent_id);
        const canDispatch = occupant && !occupant.muted && ["todo", "in_progress"].includes(status) &&
          (!task.stage_id || openStages.has(task.stage_id));
        return `<article class="company-card"><h3>${esc(task.title)}</h3>
          <div class="company-tags">${chip("#" + task.department)}${chip(({ normal: "普通", high: "高", urgent: "緊急", low: "低" })[task.priority] || task.priority)}</div>
          <p>${esc([assigned?.title, (assignee || occupant)?.agent_name].filter(Boolean).join(" · ") || "未指派")}</p>
          ${task.result_summary ? `<p>${esc(task.result_summary)}</p>` : ""}
          <div class="company-actions">${button("edit-task", "管理工單", task.task_id)}
            ${canDispatch ? button("dispatch-task", "派工", task.task_id, "primary") : ""}</div></article>`;
      }).join("");
      return `<section class="company-lane"><h3>${label} · ${matches.length}</h3>
        ${cards || '<p class="company-empty">沒有工單</p>'}</section>`;
    }).join("");
    return `<div class="company-toolbar">${button("add-task", "＋ 開工單", "", "primary")}
      <select id="taskDepartment" aria-label="篩選工單部門">${option("", "全部部門", taskDepartment)}
        ${state.channels.map((dept) => option(dept, dept, taskDepartment)).join("")}</select></div>
      <div class="company-grid">${groups}</div>`;
  }

  function seatForm() {
    const seat = state.workstations.find((item) => item.seat_id === editor.id);
    const editing = editor.action !== "add-seat";
    if (editing && !seat) return '<p class="company-empty">此工位已被移除。</p>';
    if (editor.action === "delete-seat") {
      return `<form class="company-form"><h3>刪除「${esc(seat.title)}」？</h3>
        <p>在座員工會移到待命區。此工位的工單會解除工位指派。</p>
        <div class="company-actions"><button class="btn danger" type="submit">確認刪除</button>${button("back", "返回")}</div></form>`;
    }
    if (editor.action === "assign") {
      const assignments = new Map(state.workstations.map((item) => [item.assigned_agent_id, item.title]));
      const candidates = state.members.map((member) => [member.agent_id,
        member.agent_name + " · " + (assignments.get(member.agent_id) || "待命區") + " · " + presenceLabels[memberPresence(member)]]);
      return `<form class="company-form"><h3>${esc(seat.title)}</h3>
        ${choice("入座員工", "agent_id", candidates, seat.assigned_agent_id, "空缺／調離")}
        <div class="company-actions"><button class="btn primary" type="submit">確認指派</button>${button("back", "返回")}</div></form>`;
    }
    return `<form class="company-form"><h3>${editing ? esc(seat.title) : "新增工位"}</h3>
      ${!editing ? '<label>工位 ID（選填）<input name="seat_id" maxlength="60" placeholder="seat-example" pattern="seat-[A-Za-z0-9][A-Za-z0-9-]*"></label>' : `<p>${esc(seat.seat_id)}</p>`}
      <label>工位名稱<input name="title" maxlength="100" required value="${esc(seat?.title)}"></label>
      <label>部門<input name="department" list="workstationDepartments" maxlength="60" required value="${esc(seat?.department)}"></label>
      <datalist id="workstationDepartments">${state.channels.map((dept) => option(dept, dept, "")).join("")}</datalist>
      <label>能力標籤（用逗號分隔）<input name="capabilities" value="${esc(seat?.capabilities.join(", "))}"></label>
      <label>工位 SOP（最多 300 字）<textarea name="sop_prompt" maxlength="300" rows="5">${esc(seat?.sop_prompt)}</textarea></label>
      <label class="checkbox"><input name="is_lead" type="checkbox"${seat?.is_lead ? " checked" : ""}>主管工位</label>
      <div class="company-actions"><button class="btn primary" type="submit">儲存工位</button>
        ${button("back", "返回")}${editing ? button("delete-seat", "刪除工位", seat.seat_id, "danger") : ""}</div></form>`;
  }

  function taskForm() {
    const task = state.tasks.find((item) => item.task_id === editor.id);
    if (editor.id && !task) return '<p class="company-empty">找不到此工單。</p>';
    if (editor.action === "dispatch-task") {
      const seat = state.workstations.find((item) => item.seat_id === task.assignee_seat_id);
      const recipient = employee(seat?.assigned_agent_id);
      if (!recipient) return `<div class="company-card"><p>請先為工單指派已有員工入座的工位。</p>${button("back", "返回")}</div>`;
      return `<form class="company-form"><h3>${esc(task.title)}</h3>
        <p>${esc(seat.title)} · ${esc(recipient.agent_name)}</p>
        <label>派工備註<textarea name="instructions" rows="3" maxlength="2000"></textarea></label>
        <div class="company-actions"><button class="btn primary" type="submit">確認派工</button>${button("back", "返回")}</div></form>`;
    }
    const priority = { low: "低", normal: "普通", high: "高", urgent: "緊急" };
    const current = task?.priority ?? "normal";
    if (!Object.hasOwn(priority, current)) priority[current] = current;
    return `<form class="company-form"><h3>${task ? "管理工單" : "新增工單"}</h3>
      <label>工單標題<input name="title" maxlength="160" required value="${esc(task?.title)}"></label>
      <label>部門<input name="department" list="taskDepartments" maxlength="60" required value="${esc(task?.department || taskDepartment || state.activeChannel || "總部公告")}"></label>
      <datalist id="taskDepartments">${state.channels.map((dept) => option(dept, dept, "")).join("")}</datalist>
      ${choice("優先級", "priority", Object.entries(priority), current)}
      ${choice("指派工位", "assignee_seat_id", state.workstations.map((seat) => [seat.seat_id, seat.title]), task?.assignee_seat_id, "不指定")}
      ${task ? choice("工單狀態", "status", Object.entries(statuses), task.status) : ""}
      ${task ? `<label>完成結果<textarea name="result_summary" rows="3" maxlength="300">${esc(task.result_summary)}</textarea></label>` : ""}
      <div class="company-actions"><button class="btn primary" type="submit">儲存工單</button>${button("back", "返回")}</div></form>`;
  }

  function render() {
    if (!panel.open) return;
    body.innerHTML = view === "seats" ? (editor ? seatForm() : seats()) : (editor ? taskForm() : board());
  }

  function open(kind) {
    view = kind;
    editor = null;
    $("companyTitle").textContent = kind === "seats" ? "公司工位" : "工單看板";
    setFeedback("");
    if (!panel.open) panel.showModal();
    render();
  }

  $("companyClose").onclick = () => panel.close();
  $("workstationsBtn").onclick = () => open("seats");
  $("tasksBtn").onclick = () => open("tasks");
  body.addEventListener("click", (event) => {
    const target = event.target.closest("[data-company-action]");
    if (!target || pending) return;
    const { companyAction: action, id } = target.dataset;
    editor = action === "back" ? null : { action, id };
    setFeedback("");
    render();
    body.querySelector("form input, form select, form button")?.focus();
  });
  body.addEventListener("change", (event) => {
    if (event.target.id !== "taskDepartment") return;
    taskDepartment = event.target.value;
    render();
    $("taskDepartment")?.focus();
  });
  body.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (pending || !editor) return;
    if (!state.online || state.demo) return setFeedback("請先連線到本機 AgentBridge。", true);
    const form = event.target;
    const values = Object.fromEntries(new FormData(form));
    const { action, id } = editor;
    let method, path, payload;
    if (action === "assign") {
      [method, path, payload] = ["POST", seatPath(id) + "/assign", { agent_id: values.agent_id || null }];
    } else if (action === "delete-seat") {
      [method, path] = ["DELETE", seatPath(id)];
    } else if (view === "seats") {
      [method, path] = id ? ["PUT", seatPath(id)] : ["POST", "/api/workstations"];
      payload = { ...values, is_lead: form.elements.is_lead.checked,
        capabilities: values.capabilities.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean) };
    } else if (action === "dispatch-task") {
      [method, path, payload] = ["POST", "/api/tasks/" + encodeURIComponent(id) + "/dispatch", values];
    } else {
      [method, path] = id ? ["PATCH", "/api/tasks/" + encodeURIComponent(id)] : ["POST", "/api/tasks"];
      payload = { ...values, assignee_seat_id: values.assignee_seat_id || null };
    }
    pending = true;
    const submit = event.submitter;
    if (submit) submit.disabled = true;
    setFeedback("處理中…");
    try {
      const result = await api(method, path, payload);
      editor = null;
      await refresh();
      render();
      if (action === "dispatch-task") setFeedback(result.status_message);
      else setFeedback(action === "assign" ? "指派已更新，任職令將隨員工的下一次 MCP 呼叫送達。" : "變更已儲存。");
    } catch (error) {
      setFeedback(error.message, true);
    } finally {
      pending = false;
      if (submit) submit.disabled = false;
    }
  });
  return { refresh: () => { if (!editor) render(); } };
};
