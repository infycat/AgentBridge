globalThis.initRoadmap = ({ $, api, esc, refresh, toast }) => {
  const list = $("stageStepper"), details = $("stageDetails");
  const publish = $("publishGoal"), dialog = $("goalDialog"), form = $("goalForm");
  const retry = $("retryGoal");
  const formFeedback = $("goalFeedback");
  const stageText = { pending: "待解鎖", in_progress: "進行中", completed: "已完成" };
  const taskText = { todo: "待認領", in_progress: "處理中", review: "待驗收", done: "已完成" };
  let roadmap = {}, selected = null, online = false, stageLayout = "", retrying = false;

  function select(stageId) {
    selected = stageId;
    for (const button of list.querySelectorAll("button")) {
      button.setAttribute("aria-expanded", String(button.dataset.stage === selected));
    }
    const stage = (roadmap.stages || []).find((item) => item.stage_id === selected);
    details.hidden = !stage;
    if (!stage) {
      details.replaceChildren();
      return;
    }
    details.innerHTML = `<h3>${esc(stage.title)}</h3><p>${esc(stage.description)}</p>
      <div class="stage-departments">${(stage.departments || []).map((group) =>
        `<section><h4>#${esc(group.department)} <small>${group.done} / ${group.total} 完成</small></h4>
          <ul>${group.tasks.map((task) =>
            `<li><span>${esc(task.title)}</span><small>${esc(taskText[task.status])} · ${
              task.assignee_name ? esc(task.assignee_name) : task.assignee_seat_title ?
                "指派給 " + esc(task.assignee_seat_title) : "未認領"}</small></li>`
          ).join("")}</ul></section>`
      ).join("")}</div>`;
  }

  function syncPublish() {
    publish.disabled = !online || Boolean(roadmap.active_goal);
    publish.title = roadmap.active_goal ? "目前的總目標完成後可再發布" : "";
    const awaitingPlan = Boolean(roadmap.active_goal) && !(roadmap.stages || []).length;
    retry.hidden = !awaitingPlan;
    publish.hidden = awaitingPlan;
    retry.disabled = !online || retrying || roadmap.planning?.status === "running";
  }

  function render(data) {
    roadmap = data;
    const stages = roadmap.stages || [];
    const goal = roadmap.active_goal || roadmap.completed_goal;
    const current = stages.find((stage) => stage.status === "in_progress");
    const message = current ? `階段 ${current.stage_order} / ${stages.length} 正在進行 · ${current.progress_percent}%`
      : roadmap.completed_goal ? `全部 ${stages.length} 個階段已完成`
      : goal ? roadmap.planning?.detail || "等待規劃者分解階段" : "";
    $("roadmapTitle").textContent = goal?.title || "尚無總目標";
    $("roadmapTitle").title = goal?.description || "";
    if ($("roadmapStatus").textContent !== message) $("roadmapStatus").textContent = message;
    syncPublish();
    list.hidden = !stages.length;
    const layout = JSON.stringify(stages.map((stage) => [stage.stage_id, stage.status, stage.title]));
    if (layout !== stageLayout) {
      stageLayout = layout;
      const focused = list.contains(document.activeElement) && document.activeElement.dataset.stage;
      const scroll = list.scrollLeft;
      list.innerHTML = stages.map((stage) => `<li>
      <button type="button" class="stage-button ${esc(stage.status)}" data-stage="${esc(stage.stage_id)}"
        aria-controls="stageDetails"${stage.status === "in_progress" ? ' aria-current="step"' : ""}>
        <span class="stage-top"><span class="stage-marker" aria-hidden="true">${stage.status === "completed" ? "✓" : stage.stage_order}</span>
          <span>${esc(stageText[stage.status])}</span></span>
        <span class="stage-title">${esc(stage.title)}</span>
        ${stage.status !== "pending" ?
          `<span class="stage-meter"><progress max="100" value="${stage.progress_percent}" aria-hidden="true"></progress>
            <strong>${stage.progress_percent}%</strong></span>` : ""}
      </button></li>`).join("");
      list.scrollLeft = scroll;
      if (focused) [...list.querySelectorAll("button")].find((item) => item.dataset.stage === focused)?.focus({ preventScroll: true });
    } else {
      for (const [index, button] of [...list.querySelectorAll(".stage-button")].entries()) {
        const progress = button.querySelector("progress");
        if (progress) {
          progress.value = stages[index].progress_percent;
          button.querySelector("strong").textContent = `${progress.value}%`;
        }
      }
    }
    select(stages.some((stage) => stage.stage_id === selected) ? selected : null);
  }

  list.addEventListener("click", (event) => {
    const button = event.target.closest("[data-stage]");
    if (button) select(button.dataset.stage === selected ? null : button.dataset.stage);
  });
  publish.addEventListener("click", () => {
    form.reset();
    formFeedback.textContent = "";
    dialog.showModal();
    form.elements.title.focus();
  });
  $("goalClose").addEventListener("click", () => dialog.close());
  retry.addEventListener("click", async () => {
    if (retry.disabled || !roadmap.active_goal) return;
    retrying = true;
    syncPublish();
    try {
      const path = "/api/goals/" + encodeURIComponent(roadmap.active_goal.goal_id) + "/retry";
      const { planning } = await api("POST", path, {});
      toast(planning.queued ? "已重新派送規劃" : planning.reason === "channel_off" ? "請先開啟同步通道" : "目前沒有可啟動的規劃者");
      await refresh();
    } catch (error) {
      toast(error.message);
    } finally {
      retrying = false;
      syncPublish();
    }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter || form.querySelector("button[type=submit]");
    if (button.disabled) return;
    button.disabled = true;
    formFeedback.textContent = "發布中…";
    try {
      const result = await api("POST", "/api/goals", Object.fromEntries(new FormData(form)));
      dialog.close();
      toast(result.planning?.queued ? "目標已發布，規劃者正在接收指令" : "目標已發布，等待規劃者接收");
      await refresh();
    } catch (error) {
      formFeedback.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });

  render({});
  return { render, setOnline(value) { online = value; syncPublish(); } };
};
