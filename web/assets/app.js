/* AgentBridge — 公司聊天室 (vanilla JS, no build step) */
(() => {
  "use strict";

  // ---------- Endpoint resolution ----------
  const isHttp = location.protocol === "http:" || location.protocol === "https:";
  const API = isHttp ? "" : "http://127.0.0.1:8765";
  const WS_URL = isHttp
    ? `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`
    : "ws://127.0.0.1:8765/ws";

  // ---------- State ----------
  const S = {
    channel: true, members: [], locks: [], messages: [],
    workstations: [], tasks: [], stages: [], channels: ["總部公告"], activeChannel: "", showOlder: false,
    presence: new Map(),
    online: false, demo: false, filter: "all", query: "",
    target: null,            // manually chosen recipient when the message has no leading @
    autoScroll: true, unseen: 0, fresh: new Set(),
  };
  let ws = null, wsRetry = 0, wsTimer = null;
  let roadmapEvents = 0;

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------- Theme ----------
  const savedTheme = localStorage.getItem("ab-theme") ||
    (matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.dataset.theme = savedTheme;
  $("themeBtn").onclick = () => {
    const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = t;
    localStorage.setItem("ab-theme", t);
  };

  // ---------- Helpers ----------
  const toDate = (v) => {
    if (v == null || v === "") return null;
    if (typeof v === "number") return new Date(v < 1e12 ? v * 1000 : v);
    const d = new Date(v); return isNaN(d) ? null : d;
  };
  const pad = (n) => String(n).padStart(2, "0");
  const fmtTime = (v) => { const d = toDate(v); return d ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : ""; };
  const fmtFull = (v) => { const d = toDate(v); return d ? `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` : ""; };
  const fmtDay = (d) => {
    const t = new Date(); const y = new Date(t); y.setDate(t.getDate() - 1);
    if (d.toDateString() === t.toDateString()) return "今天";
    if (d.toDateString() === y.toDateString()) return "昨天";
    return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`;
  };
  const ago = (v) => {
    const d = toDate(v); if (!d) return "—";
    const s = (Date.now() - d) / 1000;
    if (s < 60) return "剛剛";
    if (s < 3600) return `${Math.floor(s / 60)} 分鐘前`;
    if (s < 86400) return `${Math.floor(s / 3600)} 小時前`;
    return fmtTime(v) && `${d.getMonth() + 1}/${d.getDate()}`;
  };
  const presenceLabels = { working: "工作中", online: "在線", recent: "近期有活動", unknown: "在線待確認" };
  const seenAt = (m) => S.presence.get(m.agent_id)?.last_seen_at || m.last_seen_at;
  function memberPresence(m) {
    if (!S.online && !S.demo) return "unknown";
    const status = Date.now() - presenceLoadedAt < 24000 ? S.presence.get(m.agent_id)?.state : null;
    if (Object.hasOwn(presenceLabels, status)) return status;
    const d = toDate(m.last_seen_at);
    return d && Date.now() - d < 5 * 60 * 1000 ? "recent" : "unknown";
  }
  function presenceSummary() {
    const count = { working: 0, online: 0, recent: 0, unknown: 0 };
    for (const m of S.members) count[memberPresence(m)]++;
    return [count.working && `${count.working} 位工作中`, count.online && `${count.online} 位在線`,
      count.recent && `${count.recent} 位近期有活動`, count.unknown && `${count.unknown} 位在線待確認`]
      .filter(Boolean).join(" · ") || "尚無員工";
  }
  const memberByName = (n) => S.members.find((m) => m.agent_name === n);
  const memberById = (id) => S.members.find((m) => m.agent_id === id);
  const seatByEmployee = (id) => S.workstations.find((seat) => seat.assigned_agent_id === id);
  function olderEmployee(member) {
    const id = member.agent_id;
    const lastSeen = toDate(seenAt(member));
    return memberPresence(member) === "unknown" && lastSeen && Date.now() - lastSeen > 30 * 60_000 &&
      !member.is_admin && !seatByEmployee(id) && !S.locks.some((lock) => lock.agent_id === id) &&
      !S.tasks.some((task) => task.assignee_agent_id === id && task.status !== "done");
  }
  const mentionTargets = () => [
    ...S.members,
    ...S.workstations.filter((seat) => memberById(seat.assigned_agent_id)).map((seat) => ({
      agent_name: seat.seat_id, role_description: seat.title, color: memberById(seat.assigned_agent_id).color,
    })),
  ];

  /** Avatar initials: same base model opened multiple times → C1/C2; otherwise first letters → AO */
  function initials(name) {
    const member = memberByName(name);
    const base = member && `${member.client_name}-${member.model_name}`;
    if (base) {
      const suffix = name.startsWith(base + "-") ? name.slice(base.length + 1) : "";
      const hasSibling = S.members.some((item) => item.agent_name !== name &&
        item.client_name === member.client_name && item.model_name === member.model_name);
      if (/^\d+$/.test(suffix) || name === base && hasSibling) {
        return { text: (member.client_name[0] || "?").toUpperCase() + (suffix || "1"), dup: true };
      }
    }
    const parts = name.split(/[-_\s.]+/).filter(Boolean);
    const t = ((parts[0] || "?")[0] + ((parts[1] || "")[0] || "")).toUpperCase();
    return { text: t, dup: false };
  }
  function avatarHTML(name, color, { cls = "", presence = null } = {}) {
    const ini = initials(name);
    return `<div class="avatar ${cls}" style="background:${esc(color || "#94a3b8")}" title="${esc(name)}">${esc(ini.text)}${
      presence === null ? "" : `<span class="presence ${presence}" aria-hidden="true"></span>`}</div>`;
  }
  const TYPE_LABEL = { edit: "改檔", test: "測試", handoff: "交接", command: "指令", install: "安裝", git: "Git", lock: "鎖定", unlock: "解鎖" };
  const isSystem = (m) => m.sender_id === "system" || ["system", "unlock"].includes(m.action_type) || m.sender_role === "system";

  const messageBody = (text) => globalThis.renderChatMarkdown?.(text, S.query) ?? esc(text).replace(/\r?\n/g, "<br>");

  // ---------- Toast / modal ----------
  let toastT;
  function toast(msg) { const t = $("toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(toastT); toastT = setTimeout(() => t.classList.add("hidden"), 2600); }
  function confirmBox(title, body, okText = "確定") {
    return new Promise((res) => {
      const previousFocus = document.activeElement;
      $("modalTitle").textContent = title; $("modalBody").innerHTML = body; $("modalOk").textContent = okText;
      $("modal").classList.remove("hidden");
      const done = (v) => {
        $("modal").classList.add("hidden"); $("modalBody").textContent = "";
        $("modalOk").onclick = $("modalCancel").onclick = $("modal").onkeydown = null;
        previousFocus?.focus(); res(v);
      };
      $("modalOk").onclick = () => done(true); $("modalCancel").onclick = () => done(false);
      $("modal").onkeydown = (e) => { if (e.key === "Escape") { e.preventDefault(); done(false); } };
      ($("modalBody").querySelector("input") || $("modalCancel")).focus();
    });
  }

  // ---------- API ----------
  async function api(method, path, body) {
    const r = await fetch(API + path, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || data.detail || `HTTP ${r.status}`);
    return data;
  }

  async function loadState() {
    try {
      const startingRoadmapEvents = roadmapEvents;
      const st = await api("GET", "/api/state");
      S.demo = false;
      if (startingRoadmapEvents === roadmapEvents) S.stages = st.stages || [];
      applyState(st);
      if (startingRoadmapEvents === roadmapEvents) roadmapPanel.render(st);
      setOnline(true);
      refreshPresence();
      return true;
    } catch { setOnline(false); return false; }
  }
  const companyPanel = globalThis.initCompanyPanel({
    $, state: S, api, esc, refresh: loadState, memberPresence, presenceLabels,
  });
  const roadmapPanel = globalThis.initRoadmap({ $, api, esc, refresh: loadState, toast });
  let presenceReading = false, lastPresence = "", presenceLoadedAt = 0;
  async function refreshPresence() {
    if (!S.online || S.demo || document.hidden || presenceReading) return;
    presenceReading = true;
    try {
      const data = await api("GET", "/api/presence");
      if (!S.online || S.demo || !Array.isArray(data.members)) return;
      const snapshot = JSON.stringify(data.members);
      const stale = Date.now() - presenceLoadedAt >= 24000;
      presenceLoadedAt = Date.now();
      if (!stale && snapshot === lastPresence) return;
      lastPresence = snapshot;
      S.presence = new Map(data.members.map((member) => [member.agent_id, member]));
      renderMembers(); renderTargets(); renderInfo(); companyPanel.refresh();
    } catch { /* Keep the chat connection independent of the status request. */ }
    finally { presenceReading = false; }
  }
  function applyState(st) {
    S.channel = st.channel_enabled !== false;
    S.members = st.members || [];
    S.locks = st.locks || [];
    S.workstations = st.workstations || [];
    S.tasks = st.tasks || [];
    S.channels = st.channels || ["總部公告"];
    const removed = new Map(S.messages.filter((m) => m.removal_kind).map((m) => [m.id, m]));
    S.messages = (st.messages || []).map((m) => removed.get(m.id) || m).sort((a, b) => a.id - b.id);
    S.fresh.clear();
    renderAll(true);
  }

  function connectWS() {
    if (S.demo) return;
    clearTimeout(wsTimer);
    try { ws = new WebSocket(WS_URL); } catch { return scheduleWS(); }
    $("connDot").className = "dot wait";
    ws.onopen = () => { wsRetry = 0; loadState(); };
    ws.onmessage = (e) => { let p; try { p = JSON.parse(e.data); } catch { return; } handleEvent(p.type || p.event, p.data ?? p.payload); };
    ws.onclose = () => { if (!S.demo) { setOnline(false); scheduleWS(); } };
    ws.onerror = () => { try { ws.close(); } catch {} };
  }
  function scheduleWS() {
    clearTimeout(wsTimer);
    const delay = Math.min(15000, 1000 * 2 ** wsRetry++);
    wsTimer = setTimeout(connectWS, delay);
  }

  function handleEvent(type, data) {
    switch (type) {
      case "stages_updated":
        roadmapEvents++;
        S.stages = data.stages || [];
        roadmapPanel.render(data);
        companyPanel.refresh();
        break;
      case "new_message": addMessage(data); break;
      case "message_updated": updateMessage(data); break;
      case "member_updated":
        if (Array.isArray(data)) S.members = data;
        else if (data && data.agent_id) {
          const i = S.members.findIndex((m) => m.agent_id === data.agent_id);
          if (data.removed || data.deleted) { if (i >= 0) S.members.splice(i, 1); }
          else if (i >= 0) S.members[i] = { ...S.members[i], ...data }; else S.members.push(data);
        }
        renderMembers(); renderTargets(); renderMessages(false); renderInfo(); companyPanel.refresh(); break;
      case "workstations_updated":
        S.workstations = data || [];
        renderDepartments(); renderMembers(); renderTargets(); renderMessages(false); renderInfo(); companyPanel.refresh(); break;
      case "tasks_updated":
        S.tasks = data || [];
        renderDepartments(); renderMembers(); renderTargets(); companyPanel.refresh(); break;
      case "locks_updated": S.locks = Array.isArray(data) ? data : (data?.locks || []); renderLocks(); renderMembers(); renderTargets(); renderInfo(); break;
      case "channel_status": S.channel = !!(data && (data.enabled ?? data.channel_enabled)); renderChannel(); break;
      case "session_reset": S.messages = []; S.locks = []; S.fresh.clear(); loadState(); renderAll(true); toast("會話已重置"); break;
    }
  }

  function updateMessage(message) {
    if (!message) return;
    const i = S.messages.findIndex((m) => m.id === message.id);
    if (i === -1) return addMessage(message);
    S.messages[i] = message;
    renderMessages(false); renderInfo();
  }

  function addMessage(m) {
    if (!m || S.messages.some((x) => x.id === m.id)) return;
    S.messages.push(m);
    if (m.channel && !S.channels.includes(m.channel)) renderDepartments();
    if (!passFilter(m)) return;
    S.fresh.add(m.id);
    const box = $("messages");
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    renderMessages(false);
    if (nearBottom || m.is_owner) scrollBottom(true);
    else { S.unseen++; showNewBtn(); }
    renderInfo();
  }

  // ---------- Connection UI ----------
  function setOnline(on) {
    const changed = S.online !== on;
    S.online = on;
    roadmapPanel.setOnline(on);
    if (!on) { S.presence.clear(); lastPresence = ""; presenceLoadedAt = 0; }
    $("connDot").className = "dot " + (on ? "on" : S.demo ? "wait" : "off");
    $("offlinePill").classList.toggle("hidden", on || S.demo);
    if (changed) { renderMembers(); renderInfo(); companyPanel.refresh(); }
    else renderHeaderSub();
  }
  function renderHeaderSub() {
    const summary = presenceSummary();
    const label = S.demo ? `展示模式 · ${summary}` : S.online ? summary : "未連線 · 自動重連中…";
    if ($("onlineCount").textContent !== label) $("onlineCount").textContent = label;
    $("chatSub").textContent = S.members.length
      ? S.members.map((m) => m.agent_name).join("、") + "、老闆"
      : "公司聊天室";
  }

  // ---------- Render ----------
  function renderAll(jump) { renderChannel(); renderDepartments(); renderMembers(); renderLocks(); renderTargets(); renderMessages(jump); renderInfo(); companyPanel.refresh(); }

  function renderDepartments() {
    S.channels = [...new Set(["總部公告", ...S.workstations.map((seat) => seat.department),
      ...S.tasks.map((task) => task.department), ...S.messages.map((message) => message.channel).filter(Boolean)])];
    if (S.activeChannel && !S.channels.includes(S.activeChannel)) S.activeChannel = "";
    $("departmentBar").innerHTML = ["", ...S.channels].map((dept) =>
      `<button type="button" data-department="${esc(dept)}" aria-pressed="${dept === S.activeChannel}">${dept ? "#" + esc(dept) : "全部頻道"}</button>`).join("");
    $("composerChannel").textContent = "發送至 #" + (S.activeChannel || "總部公告");
  }

  $("departmentBar").onclick = (event) => {
    const button = event.target.closest("[data-department]");
    if (!button) return;
    S.activeChannel = button.dataset.department;
    S.unseen = 0;
    $("newMsgBtn").classList.add("hidden");
    renderDepartments();
    [...$("departmentBar").children].find((item) => item.dataset.department === S.activeChannel)?.focus();
    renderMessages(true);
  };

  function renderChannel() {
    const b = $("channelBtn");
    b.classList.toggle("on", S.channel);
    b.querySelector(".lbl").textContent = S.channel ? "同步通道 ON" : "同步通道 OFF";
    $("muteBanner").classList.toggle("hidden", S.channel);
  }

  function renderMembers() {
    $("memberCount").textContent = S.members.length;
    const inactive = S.members.filter(olderEmployee);
    const toggle = $("olderMembers");
    toggle.hidden = !inactive.length;
    toggle.textContent = S.showOlder ? `收起 ${inactive.length} 位久未活動員工` : `顯示 ${inactive.length} 位久未活動員工`;
    toggle.setAttribute("aria-expanded", String(S.showOlder));
    const displayed = S.showOlder ? S.members : S.members.filter((member) => !olderEmployee(member));
    $("memberList").innerHTML = displayed.length ? displayed.map((m) => {
      const presence = memberPresence(m);
      const seat = seatByEmployee(m.agent_id);
      return `
      <li class="member ${m.muted ? "muted" : ""}" data-id="${esc(m.agent_id)}">
        ${avatarHTML(m.agent_name, m.color, { presence })}
        <div class="m-main">
          <div class="m-row"><span class="m-name">${esc(m.agent_name)}</span>${m.muted ? '<span class="m-badge">已靜音</span>' : ""}<span class="m-time" title="最後活動">${ago(seenAt(m))}</span></div>
          <div class="m-role">${m.is_admin ? '<span class="admin-label">管理員</span> ' : ""}<span class="seat-badge" title="${esc(seat?.department || "")}">${esc(seat?.title || "待命區")}</span>${m.role_description ? " · " + esc(m.role_description) : ""}</div>
          <div class="m-client"><span class="m-presence">${presenceLabels[presence]}</span> · ${esc([m.client_name, m.model_name].filter(Boolean).join(" · "))}</div>
        </div>
        <div class="m-actions">
          <button data-act="mute">${m.muted ? "解除靜音" : "靜音"}</button>
          <button data-act="kick" class="kick">移出公司</button>
        </div>
      </li>`;
    }).join("") : `<li class="lock-empty">${S.members.length ? "目前沒有近期活動或入座的員工" : "尚無 Agent 加入"}</li>`;
    renderHeaderSub();
  }

  function renderLocks() {
    $("lockCount").textContent = S.locks.length;
    $("lockList").innerHTML = S.locks.length ? S.locks.map((l) => {
      const name = l.agent_name || memberById(l.agent_id)?.agent_name || l.agent_id;
      const color = l.color || memberById(l.agent_id)?.color || "#94a3b8";
      return `<li class="lock" title="鎖定於 ${esc(fmtFull(l.locked_at))}">
        <div class="lock-path">🔒 ${esc(l.display_path || l.path || l.file_path)}</div>
        <div class="lock-foot"><span class="owner-badge" style="background:${esc(color)}"><i></i>${esc(name)}</span>
        <span class="lock-sum">${esc(l.progress_summary || "")}</span></div></li>`;
    }).join("") : '<li class="lock-empty">目前沒有檔案被鎖定 ✨</li>';
  }

  function renderTargets() {
    const allTargets = mentionTargets();
    if (S.target && S.target !== "@all" && !allTargets.some((target) => target.agent_name === S.target)) S.target = null;
    const mention = draftMention($("input").value, allTargets);
    const selected = mention === "@all" ? mention : mention?.slice(1);
    const targets = allTargets.filter((target) => S.showOlder || target.agent_name === selected ||
      !target.agent_id || !olderEmployee(target));
    $("targetBar").innerHTML = `<button class="tgt all ${selected === "@all" ? "active" : ""}" data-name="@all" aria-pressed="${selected === "@all"}">@全體</button>` +
      targets.map((m) => `<button class="tgt ${selected === m.agent_name ? "active" : ""}" data-name="${esc(m.agent_name)}" aria-pressed="${selected === m.agent_name}" title="${esc(m.role_description)}">
        ${avatarHTML(m.agent_name, m.color)}@${esc(m.agent_name)}</button>`).join("");
  }

  $("olderMembers").onclick = () => {
    S.showOlder = !S.showOlder;
    renderMembers();
    renderTargets();
  };

  function passFilter(m) {
    if (S.activeChannel && (m.channel || "總部公告") !== S.activeChannel) return false;
    if (S.filter === "edit" && !(m.action_type === "edit" || (m.files_touched || []).length)) return false;
    if (S.filter === "owner" && !m.is_owner) return false;
    if (S.query) {
      const hay = `${m.summary} ${m.sender_name} ${(m.files_touched || []).join(" ")} ${m.mention_to || ""}`.toLowerCase();
      if (!hay.includes(S.query.toLowerCase())) return false;
    }
    return true;
  }

  function renderMessages(jump) {
    const box = $("messages");
    const list = S.messages.filter(passFilter);
    if (!list.length) {
      box.innerHTML = `<div class="empty"><div class="empty-logo">${S.messages.length ? "🔍" : "💬"}</div><div>${S.messages.length ? "沒有符合條件的訊息" : "尚無訊息，等待 Agent 到職…"}</div></div>`;
      return;
    }
    let html = "", lastDay = "", prev = null;
    for (const m of list) {
      const d = toDate(m.created_at) || new Date();
      const day = d.toDateString();
      if (day !== lastDay) { html += `<div class="day-sep">${fmtDay(d)}</div>`; lastDay = day; prev = null; }
      const isNew = S.fresh.has(m.id) ? " new" : "";
      if (isSystem(m)) {
        const unlock = m.action_type === "unlock" || m.action_type === "system" && m.summary === "釋放檔案鎖";
        const label = unlock ? `${m.sender_name} 釋放檔案鎖${m.files_touched?.length ? " · " + m.files_touched.length + " 檔" : ""}` : m.summary;
        html += `<div class="sys-msg${isNew}${unlock ? " unlock-notice" : ""}" data-mid="${m.id}" title="${esc(unlock ? (m.files_touched || []).join("\n") : "")}">${highlightSys(label)}</div>`; prev = null; continue;
      }
      const cont = prev && prev.sender_id === m.sender_id && !!prev.is_owner === !!m.is_owner &&
        (d - (toDate(prev.created_at) || d)) < 3 * 60 * 1000 ? " cont" : "";
      html += m.is_owner ? ownerHTML(m, cont + isNew) : agentHTML(m, cont + isNew);
      prev = m;
    }
    const keep = box.scrollHeight - box.scrollTop;
    box.innerHTML = html;
    if (jump) scrollBottom(false);
    else if (!S.autoScroll) box.scrollTop = box.scrollHeight - keep;
    else scrollBottom(false);
    setTimeout(() => S.fresh.clear(), 400);
  }
  function highlightSys(t) {
    let h = esc(t);
    S.members.slice().sort((a, b) => b.agent_name.length - a.agent_name.length).forEach((m) => {
      const n = esc(m.agent_name);
      h = h.split(n).join(`\u0000${S.members.indexOf(m)}\u0000`);
    });
    return h.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${esc(S.members[i].agent_name)}</code>`);
  }
  const chipsHTML = (files) => files && files.length ? `<div class="chips">${files.map((f) =>
    `<button class="chip" data-file="${esc(f)}"><svg viewBox="0 0 24 24"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/></svg>${esc(f)}</button>`).join("")}</div>` : "";

  function bubbleBody(message) {
    if (message.removal_kind) return `<div class="b-removed">${esc(message.summary)}</div>`;
    return `<div class="b-text">${messageBody(message.summary)}</div>`;
  }

  function messageMeta(message) {
    const action = message.is_owner ? "撤回訊息" : "刪除訊息";
    const button = message.removal_kind ? "" : `<button type="button" class="message-remove" aria-label="${action}" title="${action}" data-remove="${message.id}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5"/></svg></button>`;
    return `<div class="b-meta" title="${esc(fmtFull(message.created_at))}"><span>${fmtTime(message.created_at)}</span>${message.is_owner ? '<span class="ticks">✓✓</span>' : ""}${button}</div>`;
  }

  function agentHTML(m, cls) {
    const mem = memberById(m.sender_id);
    const seat = seatByEmployee(m.sender_id);
    const name = mem?.agent_name || m.sender_name || m.sender_id;
    const color = mem?.color || m.color;
    const role = mem?.role_description || (m.sender_role && m.sender_role !== "agent" ? m.sender_role : "");
    const t = m.action_type;
    const badge = TYPE_LABEL[t] ? `<span class="type-badge tb-${t}">${TYPE_LABEL[t]}</span>` : "";
    const mention = m.mention_to && m.mention_to !== "@all" ? `<div class="b-target">→ ${esc(m.mention_to)}</div>` : "";
    return `<div class="msg agent${cls}" data-mid="${m.id}">
      ${avatarHTML(name, color, { cls: "sm" })}
      <div class="bubble-wrap"><div class="bubble">
        <div class="b-head"><span class="b-name" style="color:${esc(color)}">${esc(name)}</span>${
          seat || mem ? `<span class="b-seat seat-badge" title="${esc(seat ? '#' + seat.department + ' · ' + seat.seat_id : '尚未指派工位')}">${esc(seat?.title || "待命區")}</span>` : ""}${
          role ? `<span class="b-role" title="${esc(role)}">${esc(role)}</span>` : ""}${badge}</div>
        ${mention}${bubbleBody(m)}
        ${messageMeta(m)}
        ${chipsHTML(m.files_touched)}
      </div></div></div>`;
  }
  function ownerHTML(m, cls) {
    const tgt = m.mention_to ? `<div class="b-target">${m.mention_to === "@all" ? "@全體員工" : esc(m.mention_to)}</div>` : "";
    return `<div class="msg owner${cls}" data-mid="${m.id}">
      <div class="bubble-wrap"><div class="bubble">
        ${cls.includes("cont") ? "" : '<div class="b-head"><span class="owner-crown">👑 老闆</span></div>'}
        ${tgt}${bubbleBody(m)}
        ${messageMeta(m)}
      </div></div></div>`;
  }

  // ---------- Scrolling ----------
  const box = $("messages");
  function scrollBottom(smooth) {
    box.scrollTo({ top: box.scrollHeight, behavior: smooth ? "smooth" : "auto" });
    S.unseen = 0; $("newMsgBtn").classList.add("hidden");
  }
  function showNewBtn() { const b = $("newMsgBtn"); b.textContent = `${S.unseen > 1 ? S.unseen + " 則" : "有"}新訊息 ↓`; b.classList.remove("hidden"); }
  box.addEventListener("scroll", () => {
    S.autoScroll = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    if (S.autoScroll) { S.unseen = 0; $("newMsgBtn").classList.add("hidden"); }
  });
  $("newMsgBtn").onclick = () => scrollBottom(true);

  // ---------- Info panel ----------
  let infoView = { kind: "group" };
  function openInfo(view) { infoView = view; $("infoPanel").classList.remove("collapsed"); renderInfo(); }
  $("infoBtn").onclick = () => { const p = $("infoPanel"); if (!p.classList.contains("collapsed") && infoView.kind === "group") p.classList.add("collapsed"); else openInfo({ kind: "group" }); };
  $("infoClose").onclick = () => $("infoPanel").classList.add("collapsed");
  function renderInfo() {
    if ($("infoPanel").classList.contains("collapsed")) return;
    const body = $("infoBody");
    if (infoView.kind === "member") {
      const m = memberById(infoView.id);
      if (!m) { infoView = { kind: "group" }; return renderInfo(); }
      const mine = S.messages.filter((x) => x.sender_id === m.agent_id);
      const files = [...new Set(mine.flatMap((x) => x.files_touched || []))];
      $("infoTitle").textContent = "員工資訊";
      const presence = memberPresence(m);
      const seat = seatByEmployee(m.agent_id);
      body.innerHTML = `<div class="info-hero">${avatarHTML(m.agent_name, m.color, { presence })}<div class="nm">${esc(m.agent_name)}</div><div class="m-role">${esc(m.role_description || "")}</div></div>
        <div class="info-card"><h4>基本資料</h4>
          <div class="kv"><span>Agent ID</span><span>${esc(m.agent_id)}</span></div>
          <div class="kv"><span>客戶端</span><span>${esc(m.client_name || "—")}</span></div>
          <div class="kv"><span>模型</span><span>${esc(m.model_name || "—")}</span></div>
          <div class="kv"><span>公司權限</span><span>${m.is_admin ? "管理員" : "員工"}</span></div>
          <div class="kv"><span>目前工位</span><span>${esc(seat?.title || "待命區")}</span></div>
          ${seat ? `<div class="kv"><span>工位 SOP</span><span>${esc(seat.sop_prompt || "未設定")}</span></div>` : ""}
          <div class="kv"><span>狀態</span><span>${presenceLabels[presence]}${m.muted ? " · 已靜音" : ""}</span></div>
          <div class="kv"><span>最後活躍</span><span>${esc(fmtFull(seenAt(m)) || "—")}</span></div>
          <div class="kv"><span>訊息數</span><span>${mine.length}</span></div></div>
        <div class="info-card"><h4>公司管理</h4><div class="member-controls">
          <button type="button" class="btn ghost" data-member-control="admin">${m.is_admin ? "取消管理員" : "設為管理員"}</button>
          <button type="button" class="btn ghost" data-member-control="key">發放員工金鑰</button>
        </div></div>
        <div class="info-card"><h4>碰過的檔案 (${files.length})</h4>${chipsHTML(files) || '<div class="lock-empty" style="padding:0">尚無</div>'}</div>`;
    } else if (infoView.kind === "file") {
      const f = infoView.file;
      const related = S.messages.filter((x) => (x.files_touched || []).includes(f));
      const lock = S.locks.find((l) => (l.display_path || l.path) === f);
      $("infoTitle").textContent = "檔案動態";
      body.innerHTML = `<div class="info-card"><h4>檔案</h4><div class="lock-path">${esc(f)}</div>
        ${lock ? `<div class="lock-foot" style="margin-top:8px"><span class="owner-badge" style="background:${esc(lock.color || "#94a3b8")}"><i></i>${esc(lock.agent_name)}</span><span class="lock-sum">鎖定中</span></div>` : ""}</div>
        <div class="info-card"><h4>相關訊息 (${related.length})</h4><ul class="info-list">${related.map((x) =>
          `<li data-jump="${x.id}"><b>${esc(x.sender_name)}</b> · ${fmtTime(x.created_at)}<div class="b-text">${messageBody(x.summary)}</div></li>`).join("")}</ul></div>`;
    } else {
      $("infoTitle").textContent = "公司資訊";
      const files = [...new Set(S.messages.flatMap((x) => x.files_touched || []))];
      body.innerHTML = `<div class="info-hero"><div class="group-avatar" style="width:72px;height:72px;font-size:24px;border-radius:20px">AB</div><div class="nm">AgentBridge AI 軟體公司</div></div>
        <div class="info-card"><h4>概況</h4>
          <div class="kv"><span>連線</span><span>${S.demo ? "展示模式" : S.online ? "已連線" : "離線"}</span></div>
          <div class="kv"><span>同步通道</span><span>${S.channel ? "ON" : "OFF"}</span></div>
          <div class="kv"><span>員工狀態</span><span>${presenceSummary()}</span></div>
          <div class="kv"><span>訊息</span><span>${S.messages.length}</span></div>
          <div class="kv"><span>檔案鎖</span><span>${S.locks.length}</span></div></div>
        <div class="info-card"><h4>本次會話改動檔案 (${files.length})</h4>${chipsHTML(files) || '<div class="lock-empty" style="padding:0">尚無</div>'}</div>`;
    }
  }
  function jumpTo(id) {
    const el = box.querySelector(`[data-mid="${id}"]`);
    if (!el) { S.filter = "all"; S.query = ""; $("searchInput").value = ""; syncSeg(); renderMessages(false); return jumpTo2(id); }
    jumpTo2(id);
  }
  function jumpTo2(id) {
    const el = box.querySelector(`[data-mid="${id}"]`); if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
  }

  // ---------- Events: sidebar ----------
  $("memberList").addEventListener("click", async (e) => {
    const li = e.target.closest(".member"); if (!li) return;
    const m = memberById(li.dataset.id); if (!m) return;
    const act = e.target.closest("button")?.dataset.act;
    if (act === "mute") return doMute(m);
    if (act === "kick") return doKick(m);
    openInfo({ kind: "member", id: m.agent_id });
  });
  async function doMute(m) {
    const muted = !m.muted;
    if (S.demo) { m.muted = muted; renderMembers(); renderInfo(); return toast(`${m.agent_name} 已${muted ? "靜音" : "解除靜音"}`); }
    try { await api("POST", `/api/members/${encodeURIComponent(m.agent_id)}/mute`, { muted }); m.muted = muted; renderMembers(); toast(`${m.agent_name} 已${muted ? "靜音" : "解除靜音"}`); }
    catch (err) { toast("操作失敗：" + err.message); }
  }
  async function doKick(m) {
    if (!(await confirmBox("移出公司", `確定要將 <b>${esc(m.agent_name)}</b> 移出公司嗎？`, "移出"))) return;
    if (S.demo) {
      S.members = S.members.filter((x) => x !== m);
      S.locks = S.locks.filter((l) => l.agent_id !== m.agent_id);
      demoPush({ sender_id: "system", sender_name: "系統", action_type: "system", summary: `${m.agent_name} 已被老闆移出公司` });
      return renderAll(false);
    }
    try { await api("DELETE", `/api/members/${encodeURIComponent(m.agent_id)}`); S.members = S.members.filter((x) => x.agent_id !== m.agent_id); renderAll(false); }
    catch (err) { toast("移出失敗：" + err.message); }
  }
  async function changeAdmin(member) {
    const is_admin = !member.is_admin;
    const action = is_admin ? "設為管理員" : "取消管理員";
    if (!(await confirmBox(action, `<b>${esc(member.agent_name)}</b> 將${is_admin ? "獲得" : "失去"}刪除一般員工訊息、靜音和移出員工的權限。`, "確認"))) return;
    try {
      const data = S.demo ? { member: { ...member, is_admin } }
        : await api("POST", `/api/members/${encodeURIComponent(member.agent_id)}/admin`, { is_admin });
      handleEvent("member_updated", data.member);
      toast(is_admin ? "已設為管理員" : "已取消管理員");
    } catch (err) { toast("操作失敗：" + err.message); }
  }
  async function issueMemberKey(member) {
    if (S.demo) return toast("展示模式的員工無法取得金鑰");
    if (!(await confirmBox("發放員工金鑰", `為 <b>${esc(member.agent_name)}</b> 產生新金鑰，既有金鑰會立即失效。`, "發放"))) return;
    try {
      const data = await api("POST", `/api/members/${encodeURIComponent(member.agent_id)}/key`);
      await confirmBox("員工金鑰", `<label for="memberKey">請將金鑰私下交給 ${esc(member.agent_name)}，供恢復原身分、撤回訊息或執行已授權的管理操作。</label>
        <input class="member-key" id="memberKey" value="${esc(data.member_key)}" type="text" autocomplete="off" readonly>`, "關閉");
    } catch (err) { toast("發放失敗：" + err.message); }
  }
  $("lockList").addEventListener("click", (e) => { const p = e.target.closest(".lock")?.querySelector(".lock-path"); if (p) openInfo({ kind: "file", file: p.textContent.replace(/^🔒\s*/, "") }); });

  // ---------- Events: header ----------
  $("channelBtn").onclick = async () => {
    const enabled = !S.channel;
    if (S.demo) { S.channel = enabled; renderChannel(); renderInfo(); return; }
    try { await api("POST", "/api/channel/toggle", { enabled }); S.channel = enabled; renderChannel(); renderInfo(); }
    catch (err) { toast("切換失敗：" + err.message); }
  };
  function syncSeg() { $("filterSeg").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.f === S.filter)); }
  $("filterSeg").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; S.filter = b.dataset.f; syncSeg(); renderMessages(true); };
  let qT; $("searchInput").oninput = (e) => { clearTimeout(qT); qT = setTimeout(() => { S.query = e.target.value.trim(); renderMessages(true); }, 150); };
  $("resetBtn").onclick = async () => {
    if (!(await confirmBox("重置會話", "這會封存目前所有訊息並清除檔案鎖，<b>無法復原</b>。確定要重置嗎？", "重置"))) return;
    if (!(await confirmBox("再次確認", "真的要重置 AgentBridge 會話嗎？", "確定重置"))) return;
    if (S.demo) { S.messages = []; S.locks = []; renderAll(true); return toast("（展示）會話已重置"); }
    try { await api("POST", "/api/session/reset"); handleEvent("session_reset", {}); }
    catch (err) { toast("重置失敗：" + err.message); }
  };
  $("exportBtn").onclick = () => {
    const lines = [`# AgentBridge AI 軟體公司對話紀錄`, ``, `匯出時間：${fmtFull(new Date())}`, ``,
      `## 員工`, ...S.members.map((m) => `- **${m.agent_name}** — ${m.role_description || ""} (${[m.client_name, m.model_name].filter(Boolean).join(" / ")})`), ``, `## 訊息`, ``];
    for (const m of S.messages.filter(passFilter)) {
      if (isSystem(m)) { lines.push(`> _${fmtFull(m.created_at)} · ${m.summary}_`, ``); continue; }
      const who = m.is_owner ? "👑 老闆" : m.sender_name;
      const tag = TYPE_LABEL[m.action_type] ? ` \`${TYPE_LABEL[m.action_type]}\`` : "";
      lines.push(`**${who}**${tag} · ${fmtFull(m.created_at)}${m.mention_to ? ` → ${m.mention_to}` : ""}`, ``, m.summary, ``);
      if ((m.files_touched || []).length) lines.push(m.files_touched.map((f) => `- \`${f}\``).join("\n"), ``);
    }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" }));
    const d = new Date(); a.download = `agentbridge-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}.md`;
    a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  box.addEventListener("click", async (e) => {
    const remove = e.target.closest("[data-remove]");
    if (remove) {
      const id = Number(remove.dataset.remove);
      const message = S.messages.find((m) => m.id === id);
      if (!message || message.removal_kind) return;
      const action = message.is_owner ? "撤回" : "刪除";
      if (!(await confirmBox(`${action}訊息`, `確定要${action}這則訊息嗎？聊天室會顯示${action}提示。`, action))) return;
      try {
        const data = S.demo ? { message: {
          ...message, summary: message.is_owner ? "訊息已撤回" : "訊息已由管理員刪除",
          removal_kind: message.is_owner ? "withdrawn" : "deleted", mention_to: null, files_touched: [],
        } } : await api("DELETE", `/api/messages/${id}`);
        updateMessage(data.message);
        toast(`已${action}訊息`);
      } catch (err) { toast("操作失敗：" + err.message); }
      return;
    }
    const c = e.target.closest(".chip"); if (c) return openInfo({ kind: "file", file: c.dataset.file });
    const av = e.target.closest(".msg.agent .avatar"); if (av) { const id = memberById(av.closest(".msg") && S.messages.find((x) => x.id == av.closest(".msg").dataset.mid)?.sender_id); if (id) openInfo({ kind: "member", id: id.agent_id }); }
  });
  $("infoBody").addEventListener("click", (e) => {
    const action = e.target.closest("[data-member-control]")?.dataset.memberControl;
    if (action && infoView.kind === "member") {
      const member = memberById(infoView.id);
      if (!member) return;
      if (action === "admin") return changeAdmin(member);
      if (action === "key") return issueMemberKey(member);
    }
    const c = e.target.closest(".chip"); if (c) return openInfo({ kind: "file", file: c.dataset.file });
    const j = e.target.closest("[data-jump]"); if (j) jumpTo(+j.dataset.jump);
  });

  // ---------- Composer ----------
  const input = $("input");
  const leadingMention = /^\s*@([\w.\-]+|全體員工|全體成員|全體|全部)/i;
  function draftMention(content, targets = mentionTargets()) {
    const names = new Map(targets.map((target) => [target.agent_name.toLowerCase(), target.agent_name]));
    function matchName(token) {
      const name = token.toLowerCase();
      if (["all", "全體員工", "全體成員", "全體", "全部"].includes(name)) return "@all";
      const canonical = names.get(name) || names.get(name.replace(/\.+$/, ""));
      return canonical ? "@" + canonical : null;
    }
    const leading = content.match(leadingMention);
    if (leading) return matchName(leading[1]) || "@" + leading[1];
    if (S.target) return S.target === "@all" ? "@all" : "@" + S.target;
    for (const match of content.matchAll(/(?<![\w.\-])@([\w.\-]+|全體員工|全體成員|全體|全部)/g)) {
      const mention = matchName(match[1]);
      if (mention) return mention;
    }
    return null;
  }
  $("targetBar").onclick = (e) => {
    const button = e.target.closest(".tgt");
    if (!button) return;
    const name = button.dataset.name;
    const leading = input.value.match(leadingMention);
    if (leading) {
      const recipient = name === "@all" ? name : "@" + name;
      input.value = input.value.replace(leadingMention, recipient);
      S.target = null;
      autosize();
    } else {
      S.target = S.target === name ? null : name;
    }
    closeMention(); renderTargets(); input.focus();
  };
  const autosize = () => { input.style.height = "auto"; input.style.height = Math.min(160, input.scrollHeight) + "px"; };

  let mp = { open: false, items: [], sel: 0, start: 0 };
  function mentionCandidates(q) {
    const all = [{ agent_name: "全體", role_description: "通知所有員工", color: "#00a884", _all: true }, ...mentionTargets()];
    return all.filter((m) => m.agent_name.toLowerCase().includes(q.toLowerCase()));
  }
  function updateMention() {
    const pos = input.selectionStart, before = input.value.slice(0, pos);
    const m = before.match(/(^|\s)@([\w.\-\u4e00-\u9fa5]*)$/);
    if (!m) return closeMention();
    mp.start = pos - m[2].length - 1;
    mp.items = mentionCandidates(m[2]);
    if (!mp.items.length) return closeMention();
    mp.sel = Math.min(mp.sel, mp.items.length - 1); mp.open = true;
    $("mentionPop").innerHTML = mp.items.map((x, i) => `<div class="mp-item ${i === mp.sel ? "sel" : ""}" data-i="${i}">
      ${x._all ? '<div class="avatar sm" style="background:#00a884">@</div>' : avatarHTML(x.agent_name, x.color, { cls: "sm" })}
      <div><div class="mp-name">@${esc(x.agent_name)}</div><div class="mp-role">${esc(x.role_description || "")}</div></div></div>`).join("");
    $("mentionPop").classList.remove("hidden");
  }
  function closeMention() { mp.open = false; mp.sel = 0; $("mentionPop").classList.add("hidden"); }
  function pickMention(i) {
    const x = mp.items[i]; if (!x) return;
    const pos = input.selectionStart;
    const ins = `@${x.agent_name} `;
    input.value = input.value.slice(0, mp.start) + ins + input.value.slice(pos);
    input.selectionStart = input.selectionEnd = mp.start + ins.length;
    closeMention(); autosize(); renderTargets(); input.focus();
  }
  $("mentionPop").addEventListener("mousedown", (e) => { const it = e.target.closest(".mp-item"); if (it) { e.preventDefault(); pickMention(+it.dataset.i); } });
  input.addEventListener("input", () => { autosize(); updateMention(); renderTargets(); });
  input.addEventListener("click", updateMention);
  input.addEventListener("blur", () => setTimeout(closeMention, 120));
  input.addEventListener("keydown", (e) => {
    if (mp.open) {
      if (e.key === "ArrowDown") { e.preventDefault(); mp.sel = (mp.sel + 1) % mp.items.length; return updateMention(); }
      if (e.key === "ArrowUp") { e.preventDefault(); mp.sel = (mp.sel - 1 + mp.items.length) % mp.items.length; return updateMention(); }
      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); return pickMention(mp.sel); }
      if (e.key === "Escape") return closeMention();
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  $("sendBtn").onclick = send;

  async function send() {
    const content = input.value.trim(); if (!content) return;
    const mention = draftMention(content);
    const previousTarget = S.target;
    if (leadingMention.test(content)) S.target = null;
    input.value = ""; autosize(); renderTargets();
    if (S.demo) { demoOwner(content, mention); return; }
    try {
      const res = await api("POST", "/api/owner/message", { content, mention_to: mention, channel: S.activeChannel || "總部公告" });
      const msg = res.message || (res.id ? res : null);
      if (msg) addMessage(msg);
      if (msg?.mention_to && res.auto_reply) {
        const { replying = [], unavailable = [], busy = [], reason } = res.auto_reply;
        const retained = msg.mention_to !== "@all" ? "，指令已保留至下次同步" : "";
        const notices = [];
        if (reason === "channel_off") notices.push("同步通道已關閉" + retained);
        if (busy.length) notices.push(`${busy.join("、")} 正在工作${retained}`);
        if (unavailable.length) notices.push(`無法啟動 ${unavailable.join("、")}${retained}`);
        if (replying.length) notices.push(`${replying.length} 位 Agent 正在回覆`);
        toast(notices.join("。") || "目前沒有可回覆的員工");
      }
      scrollBottom(true);
    } catch (err) {
      if (!input.value) { input.value = content; S.target = previousTarget; }
      autosize(); renderTargets(); toast("發送失敗：" + err.message);
    }
  }

  // ---------- Demo mock mode ----------
  let demoId = 1000;
  function demoPush(p) {
    addMessage({ id: ++demoId, sender_role: "", color: "#94a3b8", is_owner: false, files_touched: [], mention_to: null, created_at: new Date().toISOString(), ...p });
  }
  function loadDemo() {
    S.demo = true; clearTimeout(wsTimer); try { ws && ws.close(); } catch {}
    const now = Date.now(), iso = (min) => new Date(now - min * 60000).toISOString();
    const members = [
      { agent_id: "codex-gpt6luna", agent_name: "codex-gpt6luna", client_name: "Codex CLI", model_name: "gpt-6-luna", role_description: "後端 API 與資料庫", color: "#38BDF8", muted: false, last_seen_at: iso(0.3) },
      { agent_id: "codex-gpt6luna-2", agent_name: "codex-gpt6luna-2", client_name: "Codex CLI", model_name: "gpt-6-luna", role_description: "測試撰寫與 CI 驗證", color: "#A78BFA", muted: false, last_seen_at: iso(1) },
      { agent_id: "antigravity-opus5.5", agent_name: "antigravity-opus5.5", client_name: "Antigravity", model_name: "claude-opus-5.5", role_description: "前端介面與整體架構", color: "#34D399", muted: false, last_seen_at: iso(2) },
    ];
    const M = (id, min, sender, type, text, files = [], mention = null) => {
      const mem = members.find((m) => m.agent_id === sender);
      return sender === "owner"
        ? { id, sender_id: "owner", sender_name: "老闆", sender_role: "owner", color: "#F59E0B", is_owner: true, action_type: "message", summary: text, files_touched: [], mention_to: mention, created_at: iso(min) }
        : sender === "system"
          ? { id, sender_id: "system", sender_name: "系統", sender_role: "system", color: "#94A3B8", is_owner: false, action_type: "system", summary: text, files_touched: [], mention_to: "@all", created_at: iso(min) }
          : { id, sender_id: sender, sender_name: mem.agent_name, sender_role: mem.role_description, color: mem.color, is_owner: false, action_type: type, summary: text, files_touched: files, mention_to: mention, created_at: iso(min) };
    };
    applyState({
      channel_enabled: true, members,
      locks: [
        { display_path: "agentbridge/store.py", agent_id: "codex-gpt6luna", agent_name: "codex-gpt6luna", color: "#38BDF8", locked_at: iso(6), progress_summary: "新增 mute 欄位遷移" },
        { display_path: "web/assets/app.css", agent_id: "antigravity-opus5.5", agent_name: "antigravity-opus5.5", color: "#34D399", locked_at: iso(3), progress_summary: "夜間模式配色" },
      ],
      messages: [
        M(1, 42, "system", "", "codex-gpt6luna 已到職"),
        M(2, 41, "system", "", "antigravity-opus5.5 已到職"),
        M(3, 40, "owner", "", "大家好，今天目標：完成老闆聊天室前端 + 員工靜音 API。@all 請先認領工作。", [], "@all"),
        M(4, 38, "antigravity-opus5.5", "handoff", "我負責 web/ 前端，會做日夜間雙主題與 WebSocket 重連。後端欄位請以 /api/state 為準。"),
        M(5, 37, "codex-gpt6luna", "edit", "已新增 members.muted 欄位與 POST /api/members/{id}/mute 路由。", ["agentbridge/store.py", "agentbridge/server.py"]),
        M(6, 30, "system", "", "codex-gpt6luna-2 已到職"),
        M(7, 29, "owner", "", "@codex-gpt6luna-2 你跟 codex-gpt6luna 同模型，請專心寫測試，不要動 store.py。", [], "@codex-gpt6luna-2"),
        M(8, 28, "codex-gpt6luna-2", "test", "收到。已補上 mute / kick / reset 三組整合測試，全部 23 項通過 ✅", ["tests/test_sync.py"]),
        M(9, 20, "antigravity-opus5.5", "edit", "完成側邊欄員工列表與即時檔案鎖區塊；同模型多開會自動顯示 C1 / C2 頭像。", ["web/index.html", "web/assets/app.css", "web/assets/app.js"]),
        M(10, 12, "codex-gpt6luna", "handoff", "store.py 遷移完成，等 @codex-gpt6luna-2 跑一次完整回歸再釋放鎖。", [], "@codex-gpt6luna-2"),
        M(11, 4, "codex-gpt6luna-2", "test", "回歸測試通過，可以釋放 agentbridge/store.py 的鎖了。", ["tests/run_verification.py"]),
      ],
    });
    setOnline(false); renderHeaderSub();
    toast("已載入展示資料（Demo Mock Mode）");
  }
  function demoOwner(content, mention) {
    addMessage({ id: ++demoId, sender_id: "owner", sender_name: "老闆", sender_role: "owner", color: "#F59E0B", is_owner: true, action_type: "message", summary: content, files_touched: [], mention_to: mention, created_at: new Date().toISOString() });
    const target = mention && mention !== "@all" ? memberByName(mention.slice(1)) : S.members.filter((m) => !m.muted)[Math.floor(Math.random() * S.members.length)];
    if (!target || target.muted || !S.channel && !mention) return;
    setTimeout(() => {
      target.last_seen_at = new Date().toISOString();
      demoPush({ sender_id: target.agent_id, sender_name: target.agent_name, sender_role: target.role_description, color: target.color, action_type: "handoff", summary: `收到老闆指令，${target.agent_name} 開始處理：「${content.slice(0, 40)}」` });
      renderMembers();
    }, 900 + Math.random() * 900);
  }
  $("demoBtn").onclick = loadDemo;

  // ---------- Boot ----------
  setInterval(refreshPresence, 8000);
  document.addEventListener("visibilitychange", refreshPresence);
  setInterval(() => { renderMembers(); }, 30000);
  loadState().then((ok) => { connectWS(); if (!ok && !isHttp) toast("找不到本地後端，可點右上角「載入展示資料」預覽"); });
  // Allow ?demo in URL for instant preview
  if (/[?&]demo\b/.test(location.search)) loadDemo();
})();
