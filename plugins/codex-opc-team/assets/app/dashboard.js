(() => {
  "use strict";

  const REFRESH_MS = 30_000;
  const VIEWS = {
    overview: "总览",
    projects: "项目",
    runs: "运行",
    knowledge: "知识",
    lineage: "证据链",
    health: "系统健康",
    adapters: "Adapters",
    settings: "设置",
  };

  const state = {
    snapshot: null,
    context: null,
    csrf: "",
    queue: new Map(),
    adapters: null,
    adapterPlan: null,
    refreshTimer: null,
  };

  const byId = (id) => document.getElementById(id);
  const asObject = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const asArray = (value) => Array.isArray(value) ? value : [];
  const finiteNumber = (value) => Number.isFinite(Number(value)) ? Number(value) : 0;

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#icon-${name}`);
    svg.append(use);
    return svg;
  }

  function setText(id, value) {
    const node = byId(id);
    if (node) node.textContent = String(value ?? "");
  }

  function relativeTime(value) {
    const timestamp = Date.parse(String(value || ""));
    if (!Number.isFinite(timestamp)) return "时间未记录";
    const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
    if (seconds < 60) return `${Math.max(1, seconds)} 秒前`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} 分钟前`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} 小时前`;
    return `${Math.floor(hours / 24)} 天前`;
  }

  function statusLabel(value) {
    const labels = {
      aligning: "对齐中",
      planned: "已规划",
      implementing: "实现中",
      validating: "验收中",
      ready_for_manager: "待经理体验",
      completed: "已完成",
      paused: "已暂停",
      failed: "失败",
      available: "可用",
      unavailable: "不可用",
      degraded: "降级",
      healthy: "健康",
      disabled: "已禁用",
      invalid: "无效",
      absent: "未配置",
      high: "高优先级",
      medium: "中优先级",
      low: "低优先级",
    };
    return labels[String(value || "").toLowerCase()] || String(value || "未知");
  }

  function tone(value) {
    const normalized = String(value || "").toLowerCase();
    if (["completed", "ready_for_manager", "pass", "available", "healthy", "ready", "published", "disabled"].includes(normalized)) return "healthy";
    if (["failed", "blocked", "invalid", "error"].includes(normalized)) return "blocked";
    if (["planned", "implementing", "validating", "aligning", "paused", "degraded", "warning", "inconclusive"].includes(normalized)) return "warning";
    return "neutral";
  }

  function emptyState(title, description) {
    const node = document.createElement("div");
    node.className = "empty-state";
    node.append(icon("project"));
    const heading = document.createElement("b");
    heading.textContent = title;
    const copy = document.createElement("span");
    copy.textContent = description;
    node.append(heading, copy);
    return node;
  }

  function selectedRegistryProject() {
    const context = asObject(state.context);
    const selected = context.selected_project_id;
    return asArray(context.projects).find((project) => project.id === selected) || asArray(context.projects)[0] || null;
  }

  function selectedSnapshotProject() {
    const selected = selectedRegistryProject();
    const projects = asArray(asObject(state.snapshot).projects);
    if (!selected) return projects[0] || null;
    return projects.find((project) => project.id === selected.project_id) || projects[0] || null;
  }

  function renderContext() {
    const context = asObject(state.context);
    const projects = asArray(context.projects);
    const select = byId("project-select");
    select.replaceChildren();
    if (!projects.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = context.settings_state === "invalid" ? "接入清单不可用" : "尚未接入项目";
      select.append(option);
      select.disabled = true;
    } else {
      projects.forEach((project) => {
        const option = document.createElement("option");
        option.value = project.id;
        option.textContent = project.name || project.root_label || "未命名项目";
        option.selected = project.id === context.selected_project_id;
        select.append(option);
      });
      select.disabled = context.settings_state === "demo";
    }
    if (context.warning && context.settings_state === "invalid") {
      showBanner(context.warning.message || "App 接入清单不可用。");
    }
    byId("open-project-drawer").disabled = context.settings_state === "invalid";
  }

  function renderSummary() {
    const snapshot = asObject(state.snapshot);
    const summary = asObject(snapshot.summary);
    setText("stat-projects", finiteNumber(summary.active_projects));
    setText("stat-acceptance", finiteNumber(summary.pending_acceptance));
    setText("stat-candidates", finiteNumber(summary.candidates));
    setText("stat-published", finiteNumber(summary.published));
    setText("mode-label", snapshot.mode === "demo" ? "演示数据 · 只读" : "本地只读");
    setText("last-updated", `最后刷新：${relativeTime(snapshot.generated_at)}`);
    byId("synthetic-notice").hidden = snapshot.mode !== "demo";
  }

  function queueSource(item) {
    const mapping = {
      acceptance: ["run", "运行"],
      knowledge_publish: ["lineage", "证据链"],
      knowledge_review: ["knowledge", "知识"],
      system_health: ["health", "系统健康"],
      evaluation: ["run", "评测"],
    };
    return mapping[item.type] || ["overview", "总览"];
  }

  function renderQueue() {
    const queue = asArray(asObject(state.snapshot).manager_queue);
    const target = byId("queue-list");
    target.replaceChildren();
    state.queue.clear();
    setText("queue-count", `${queue.length} 项`);
    if (!queue.length) {
      target.append(emptyState("没有待处理项", "当前快照没有需要经理介入的验收、知识或评测事项。"));
      return;
    }
    queue.forEach((item, index) => {
      const itemId = String(item.id || `queue-${index + 1}`);
      state.queue.set(itemId, item);
      const row = document.createElement("button");
      row.type = "button";
      row.className = "queue-row filterable";
      row.dataset.queueId = itemId;
      row.setAttribute("aria-label", `查看 ${item.title || "待处理事项"} 的下一步`);

      const priority = document.createElement("span");
      const severity = String(item.severity || "low").toLowerCase();
      priority.className = `priority is-${severity}`;
      priority.textContent = severity === "high" ? "P1" : severity === "medium" ? "P2" : "P3";

      const copy = document.createElement("span");
      copy.className = "queue-copy";
      const title = document.createElement("b");
      title.textContent = item.title || "待处理事项";
      const description = document.createElement("small");
      description.textContent = item.description || "查看现有证据并决定下一步。";
      copy.append(title, description);

      const [iconName, sourceName] = queueSource(item);
      const source = document.createElement("span");
      source.className = "queue-source";
      source.append(icon(iconName));
      const sourceCopy = document.createElement("span");
      sourceCopy.textContent = sourceName;
      source.append(sourceCopy);

      const time = document.createElement("span");
      time.className = "queue-time";
      time.textContent = relativeTime(asObject(state.snapshot).generated_at);

      row.append(priority, copy, source, time, icon("chevron"));
      row.addEventListener("click", () => openQueue(itemId));
      target.append(row);
    });
  }

  function progressStages(status) {
    const order = ["planned", "implementing", "validating", "ready_for_manager", "completed"];
    const index = order.indexOf(String(status || ""));
    return [
      ["计划", index >= 0],
      ["执行", index >= 1],
      ["验证", index >= 2],
      ["记录", index >= 3],
    ];
  }

  function renderCurrentRun() {
    const target = byId("current-run");
    target.replaceChildren();
    const project = selectedSnapshotProject();
    if (!project) {
      target.append(emptyState("没有当前运行", "接入项目后，这里会显示其当前运行状态。"));
      return;
    }
    const run = asObject(project.run);
    const wrapper = document.createElement("div");
    wrapper.className = "run-summary";
    const top = document.createElement("div");
    top.className = "run-summary-top";
    const names = document.createElement("span");
    const label = document.createElement("small");
    label.textContent = "最新运行";
    const title = document.createElement("b");
    title.textContent = run.title || "暂无运行";
    names.append(label, title);
    const status = document.createElement("span");
    status.className = `run-status-text is-${tone(run.status)}`;
    const dot = document.createElement("i");
    dot.className = "status-dot";
    const statusCopy = document.createElement("b");
    statusCopy.textContent = statusLabel(run.status);
    status.append(dot, statusCopy);
    top.append(names, status);

    const steps = document.createElement("div");
    steps.className = "progress-steps";
    const stages = progressStages(run.status);
    stages.forEach(([name, complete], index) => {
      const step = document.createElement("span");
      step.className = `progress-step${complete ? " is-complete" : index === stages.findIndex((entry) => !entry[1]) ? " is-current" : ""}`;
      const marker = document.createElement("i");
      marker.textContent = complete ? "✓" : "";
      const stepName = document.createElement("b");
      stepName.textContent = name;
      const stepState = document.createElement("small");
      stepState.textContent = complete ? "完成" : index === stages.findIndex((entry) => !entry[1]) ? "进行中" : "待开始";
      step.append(marker, stepName, stepState);
      steps.append(step);
    });

    const next = document.createElement("div");
    next.className = "run-next";
    const nextTitle = document.createElement("strong");
    nextTitle.textContent = "下一步建议";
    const nextCopy = document.createElement("small");
    nextCopy.textContent = run.status === "ready_for_manager" ? "由经理体验已通过独立验收的结果。" : "继续按项目契约收集证据并完成独立验收。";
    next.append(nextTitle, nextCopy);
    wrapper.append(top, steps, next);
    target.append(wrapper);
  }

  function healthEntries() {
    const health = asObject(asObject(state.snapshot).health);
    return [
      ["file_git", "File/Git 权威"],
      ["mem0", "Mem0（可选）"],
      ["projects", "项目数据"],
    ].map(([key, fallback]) => [key, fallback, asObject(health[key])]);
  }

  function renderOverviewHealth() {
    const target = byId("overview-health");
    target.replaceChildren();
    healthEntries().slice(0, 2).forEach(([, fallback, item]) => {
      const row = document.createElement("div");
      row.className = `health-row is-${tone(item.state)}`;
      const dot = document.createElement("i");
      dot.className = "status-dot";
      const name = document.createElement("b");
      name.textContent = item.label || fallback;
      const detail = document.createElement("span");
      detail.textContent = item.detail || statusLabel(item.state);
      row.append(dot, name, detail);
      target.append(row);
    });
  }

  function snapshotProjectForRegistry(registryProject) {
    return asArray(asObject(state.snapshot).projects).find((project) => project.id === registryProject.project_id) || null;
  }

  function renderProjects() {
    const target = byId("project-list");
    target.replaceChildren();
    const projects = asArray(asObject(state.context).projects);
    setText("project-count", `共 ${projects.length} 个项目`);
    if (!projects.length) {
      target.append(emptyState("尚未接入项目", "使用“接入项目”明确添加一个 OPC 项目目录。App 不会扫描磁盘。"));
      renderInspector(null);
      return;
    }
    projects.forEach((registryProject) => {
      const project = snapshotProjectForRegistry(registryProject);
      const run = asObject(asObject(project).run);
      const acceptance = asObject(asObject(project).acceptance);
      const total = finiteNumber(acceptance.total);
      const passed = finiteNumber(acceptance.passed);

      const row = document.createElement("button");
      row.type = "button";
      row.className = "project-row filterable";
      row.dataset.registryId = registryProject.id;
      if (registryProject.id === asObject(state.context).selected_project_id) row.setAttribute("aria-current", "true");

      const name = document.createElement("span");
      name.className = "project-name";
      name.append(icon("project"));
      const nameCopy = document.createElement("span");
      const nameStrong = document.createElement("b");
      nameStrong.textContent = registryProject.name || asObject(project).name || "未命名项目";
      const rootLabel = document.createElement("small");
      rootLabel.textContent = registryProject.root_label || "显式目录";
      nameCopy.append(nameStrong, rootLabel);
      name.append(nameCopy);

      const runState = stateText(statusLabel(run.status), tone(run.status));
      const acceptanceText = document.createElement("span");
      acceptanceText.className = "text-state";
      acceptanceText.textContent = total ? `${passed} / ${total}` : "未记录";
      const updated = document.createElement("span");
      updated.className = "text-state";
      updated.textContent = relativeTime(run.updated_at || asObject(state.snapshot).generated_at);
      const sourceState = stateText(
        registryProject.state === "available" && asObject(project).source_state !== "invalid" ? "正常" : "不可用",
        registryProject.state === "available" && asObject(project).source_state !== "invalid" ? "healthy" : "blocked",
      );
      row.append(name, runState, acceptanceText, updated, sourceState);
      row.addEventListener("click", async () => {
        await selectProject(registryProject.id);
      });
      target.append(row);
    });
    renderInspector(selectedRegistryProject());
    applySearch();
  }

  function stateText(label, stateTone) {
    const node = document.createElement("span");
    node.className = `text-state is-${stateTone}`;
    const dot = document.createElement("i");
    dot.className = "status-dot";
    const copy = document.createElement("span");
    copy.textContent = label;
    node.append(dot, copy);
    return node;
  }

  function renderInspector(registryProject) {
    const target = byId("project-inspector");
    target.replaceChildren();
    if (!registryProject) {
      target.append(emptyState("未选择项目", "接入并选择项目后，这里会显示脱敏状态。"));
      return;
    }
    const project = snapshotProjectForRegistry(registryProject);
    const run = asObject(asObject(project).run);
    const acceptance = asObject(asObject(project).acceptance);

    const header = document.createElement("header");
    header.className = "inspector-header";
    header.append(icon("project"));
    const heading = document.createElement("span");
    const title = document.createElement("b");
    title.textContent = registryProject.name || "未命名项目";
    const status = document.createElement("small");
    status.textContent = registryProject.state === "available" ? "显式接入 · 只读" : "项目不可读 · 已降级";
    heading.append(title, status);
    header.append(heading);

    const projectState = document.createElement("section");
    projectState.className = "inspector-section";
    const projectStateTitle = document.createElement("h3");
    projectStateTitle.textContent = "项目状态";
    const facts = document.createElement("dl");
    [
      ["当前运行", statusLabel(run.status)],
      ["独立验收", acceptance.total ? `${finiteNumber(acceptance.passed)} / ${finiteNumber(acceptance.total)}` : "未记录"],
      ["最近更新", relativeTime(run.updated_at || asObject(state.snapshot).generated_at)],
    ].forEach(([term, description]) => {
      const row = document.createElement("div");
      const dt = document.createElement("dt");
      dt.textContent = term;
      const dd = document.createElement("dd");
      dd.textContent = description;
      row.append(dt, dd);
      facts.append(row);
    });
    projectState.append(projectStateTitle, facts);

    const boundary = document.createElement("section");
    boundary.className = "inspector-section";
    const boundaryTitle = document.createElement("h3");
    boundaryTitle.textContent = "数据边界";
    const boundaryCopy = document.createElement("p");
    boundaryCopy.textContent = `${registryProject.root_label || "显式目录"} · 仅从此项目读取固定字段，不访问其他位置。`;
    boundary.append(boundaryTitle, boundaryCopy);

    const actions = document.createElement("div");
    actions.className = "inspector-actions";
    const switchButton = document.createElement("button");
    switchButton.type = "button";
    switchButton.className = "secondary-button";
    switchButton.textContent = "切换到此项目";
    switchButton.disabled = registryProject.id === asObject(state.context).selected_project_id;
    switchButton.addEventListener("click", () => selectProject(registryProject.id));
    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "danger-button";
    removeButton.append(icon("trash"));
    const removeCopy = document.createElement("span");
    removeCopy.textContent = "移除接入";
    removeButton.append(removeCopy);
    removeButton.disabled = asObject(state.context).settings_state !== "ready";
    removeButton.addEventListener("click", () => removeProject(registryProject.id));
    actions.append(switchButton, removeButton);

    const note = document.createElement("p");
    note.className = "preservation-note";
    note.textContent = "移除接入不会删除项目 .opc、File/Git 知识、Git 历史、用户配置或 Mem0 数据；只会从 App 清单移除此目录。";
    target.append(header, projectState, boundary, actions, note);
  }

  function renderRuns() {
    const target = byId("run-list");
    target.replaceChildren();
    const projects = asArray(asObject(state.snapshot).projects);
    setText("run-count", `${projects.length} 个`);
    if (!projects.length) {
      target.append(emptyState("没有当前运行", "接入项目后，这里会显示其当前运行状态。"));
      return;
    }
    projects.forEach((project) => {
      const run = asObject(project.run);
      const acceptance = asObject(project.acceptance);
      const card = document.createElement("article");
      card.className = "run-card filterable";
      [
        [run.title || "当前运行", project.name || project.id || "未命名项目"],
        [statusLabel(run.status), run.active === false ? "非活动" : "活动"],
        [acceptance.total ? `${finiteNumber(acceptance.passed)} / ${finiteNumber(acceptance.total)}` : "未记录", "独立验收"],
        [relativeTime(run.updated_at || asObject(state.snapshot).generated_at), "最近更新"],
      ].forEach(([primary, secondary]) => {
        const item = document.createElement("span");
        const strong = document.createElement("b");
        strong.textContent = primary;
        const small = document.createElement("small");
        small.textContent = secondary;
        item.append(strong, small);
        card.append(item);
      });
      target.append(card);
    });
  }

  function renderKnowledge() {
    const target = byId("knowledge-flow");
    target.replaceChildren();
    const knowledge = asObject(asObject(state.snapshot).knowledge);
    const stages = [
      ["Candidate", finiteNumber(knowledge.candidate), "等待经理验证"],
      ["Approved", finiteNumber(knowledge.approved_uncommitted), "尚无 current HEAD provenance"],
      ["Published", finiteNumber(knowledge.published), "current HEAD 可验证"],
      ["Obsolete", finiteNumber(knowledge.obsolete), "已失效但保留历史"],
    ];
    stages.forEach(([label, count, detail]) => {
      const stage = document.createElement("article");
      stage.className = "knowledge-stage filterable";
      const value = document.createElement("strong");
      value.textContent = count;
      const title = document.createElement("b");
      title.textContent = label;
      const description = document.createElement("small");
      description.textContent = detail;
      stage.append(value, title, description);
      target.append(stage);
    });
    setText("knowledge-candidate", `${finiteNumber(knowledge.candidate)} 条等待治理`);
    setText("knowledge-approved", `${finiteNumber(knowledge.approved_uncommitted)} 条尚未闭环`);
    setText("knowledge-published", `${finiteNumber(knowledge.published)} 条可验证知识`);
  }

  function adapterStateLabel(value) {
    return {
      available: "可安装",
      installed: "已安装",
      drifted: "检测到漂移",
      blocked: "已阻止",
      unavailable: "宿主不可用",
    }[String(value || "")] || "未知";
  }

  function adapterReason(value) {
    return {
      HOST_NOT_FOUND: "未在 PATH 中发现宿主 CLI",
      HOST_VERSION_INCOMPATIBLE: "宿主版本不在已验证范围",
      HOST_VERSION_INCOMPATIBLE_UNINSTALL_SAFETY: "当前版本存在官方已知卸载安全缺陷",
      HOST_DISCOVERY_FAILED: "官方发现命令执行失败",
      HOST_DISCOVERY_INVALID: "官方发现结果无法验证",
      HOST_CONFIG_INVALID: "宿主配置损坏或无法通过官方诊断",
      DEMO_OR_ADAPTER_SERVICE_UNAVAILABLE: "演示模式不探测或修改真实宿主",
    }[String(value || "")] || String(value || "");
  }

  function adapterAction(host, operation, label, style = "secondary-button") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = style;
    button.textContent = label;
    button.dataset.adapterHost = host.host_id;
    button.dataset.adapterOperation = operation;
    button.disabled = !["available", "installed", "drifted"].includes(host.state);
    return button;
  }

  function renderAdapters() {
    const target = byId("adapter-list");
    target.replaceChildren();
    const hosts = asArray(asObject(state.adapters).hosts);
    if (!hosts.length) {
      target.append(emptyState("尚无 Adapter 状态", "刷新后重新执行本地只读探测。"));
      return;
    }
    hosts.forEach((host) => {
      const card = document.createElement("article");
      card.className = `adapter-card filterable is-${tone(host.state)}`;
      const header = document.createElement("header");
      const identity = document.createElement("div");
      const mark = document.createElement("span");
      mark.className = "adapter-mark";
      mark.textContent = String(host.display_name || host.host_id).slice(0, 1);
      const title = document.createElement("span");
      const strong = document.createElement("strong");
      strong.textContent = host.display_name || host.host_id;
      const unit = document.createElement("small");
      unit.textContent = host.integration_unit || "能力未发现";
      title.append(strong, unit);
      identity.append(mark, title);
      const badge = document.createElement("em");
      badge.className = `status-chip is-${tone(host.state)}`;
      badge.textContent = adapterStateLabel(host.state);
      header.append(identity, badge);

      const facts = document.createElement("dl");
      [
        ["宿主版本", host.host_version || "未发现"],
        ["已安装", host.installed_version || "—"],
        ["目标版本", host.target_version || "—"],
        ["验证契约", host.verified_contract || "未钉住"],
      ].forEach(([label, value]) => {
        const row = document.createElement("div");
        const dt = document.createElement("dt");
        const dd = document.createElement("dd");
        dt.textContent = label;
        dd.textContent = value;
        row.append(dt, dd);
        facts.append(row);
      });
      card.append(header, facts);

      if (host.reason || host.plugin_management) {
        const warning = document.createElement("p");
        warning.className = "adapter-warning";
        warning.textContent = host.reason
          ? adapterReason(host.reason)
          : "Kimi Plugin 后台安装不受支持；当前仅管理官方 Skill 投影。";
        card.append(warning);
      }
      const actions = document.createElement("div");
      actions.className = "adapter-actions";
      if (host.state === "available") {
        actions.append(adapterAction(host, "install", "预览安装", "primary-button"));
      } else if (host.state === "installed" || (host.state === "drifted" && host.ownership)) {
        actions.append(adapterAction(host, "update", host.state === "drifted" ? "查看修复计划" : "检查更新", "primary-button"));
        actions.append(adapterAction(host, "uninstall", "预览卸载"));
      } else {
        const unavailable = adapterAction(host, "inspect", "当前不可执行");
        unavailable.disabled = true;
        actions.append(unavailable);
      }
      card.append(actions);
      target.append(card);
    });
  }

  function renderLineage() {
    const project = selectedSnapshotProject();
    const stages = [...document.querySelectorAll("#lineage-timeline li")];
    stages.forEach((stage) => {
      stage.classList.remove("is-recorded", "is-warning");
      stage.querySelector("em").textContent = "未记录";
    });
    if (!project) {
      setText("lineage-project-label", "选择项目查看证据状态");
      return;
    }
    const run = asObject(project.run);
    const status = String(run.status || "");
    const feedback = String(project.feedback_status || "unavailable");
    const recorded = {
      context: Boolean(run.title),
      developer: ["implementing", "validating", "ready_for_manager", "completed"].includes(status),
      qa: ["validating", "ready_for_manager", "completed"].includes(status),
      feedback: !["unavailable", "absent", ""].includes(feedback),
      outcome: ["ready_for_manager", "completed"].includes(status),
    };
    stages.forEach((stage) => {
      const key = stage.dataset.stage;
      if (recorded[key]) {
        stage.classList.add("is-recorded");
        stage.querySelector("em").textContent = "已记录";
      } else if (key === "feedback" && feedback === "degraded") {
        stage.classList.add("is-warning");
        stage.querySelector("em").textContent = "已降级";
      }
    });
    setText("lineage-project-label", `${project.name || project.id || "当前项目"} · ${statusLabel(project.lineage_status)}`);
  }

  function renderHealth() {
    const target = byId("health-list");
    target.replaceChildren();
    healthEntries().forEach(([, fallback, item]) => {
      const card = document.createElement("article");
      card.className = `health-card is-${tone(item.state)} filterable`;
      const heading = document.createElement("header");
      heading.append(icon("health"));
      const title = document.createElement("b");
      title.textContent = item.label || fallback;
      heading.append(title);
      const status = document.createElement("p");
      status.textContent = `状态：${statusLabel(item.state)}`;
      const detail = document.createElement("p");
      detail.textContent = item.detail || "未提供状态详情。";
      card.append(heading, status, detail);
      target.append(card);
    });
  }

  function renderWarnings() {
    const warnings = asArray(asObject(state.snapshot).warnings)
      .map((item) => typeof item === "string" ? item : asObject(item).message)
      .filter(Boolean);
    if (warnings.length && asObject(state.snapshot).mode !== "demo") showBanner(warnings.slice(0, 3).join("；"));
    else if (asObject(state.context).settings_state !== "invalid") hideBanner();
  }

  function renderAll() {
    renderContext();
    renderSummary();
    renderQueue();
    renderCurrentRun();
    renderOverviewHealth();
    renderProjects();
    renderRuns();
    renderKnowledge();
    renderLineage();
    renderHealth();
    renderAdapters();
    renderWarnings();
    applySearch();
    setText("live-status", "OPC App 本地状态已刷新");
  }

  async function fetchJSON(path, options = {}) {
    const response = await fetch(path, {
      cache: "no-store",
      credentials: "same-origin",
      ...options,
      headers: {
        Accept: "application/json",
        ...(options.headers || {}),
      },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.error || `HTTP_${response.status}`);
      error.code = payload.error || `HTTP_${response.status}`;
      throw error;
    }
    return payload;
  }

  async function refresh() {
    byId("refresh-button").disabled = true;
    try {
      const [context, snapshot, adapters] = await Promise.all([
        fetchJSON("/api/app-context"),
        fetchJSON("/api/snapshot"),
        fetchJSON("/api/adapters"),
      ]);
      state.context = context;
      state.snapshot = snapshot;
      state.adapters = adapters;
      state.csrf = String(context.csrf_token || "");
      renderAll();
    } catch (error) {
      showBanner("无法读取本地 App 状态。请查看启动终端中的安全诊断信息。");
      setText("last-updated", "读取失败");
      setText("live-status", "OPC App 本地状态读取失败");
    } finally {
      byId("refresh-button").disabled = false;
    }
  }

  async function mutate(path, method, body) {
    const options = {
      method,
      headers: {
        "X-OPC-CSRF": state.csrf,
      },
    };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const context = await fetchJSON(path, options);
    state.context = context;
    state.csrf = String(context.csrf_token || state.csrf);
    await refresh();
  }

  async function adapterRequest(path, body) {
    return fetchJSON(path, {
      method: "POST",
      headers: {
        "X-OPC-CSRF": state.csrf,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
  }

  function adapterErrorMessage(code) {
    return {
      HOST_BLOCKED: "宿主缺失、版本不兼容或官方发现不可用；未执行任何写入。",
      NOT_OPC_OWNED: "没有可证明的 OPC 所有权记录，已拒绝修改。",
      USER_MODIFIED_CONFLICT: "受管内容已被用户修改；已保留现场并转为人工处理。",
      UNKNOWN_TARGET_CONFLICT: "目标位置已有未知内容；已保留且未覆盖。",
      CONFIRMATION_REQUIRED: "必须重新核对计划并明确确认。",
      PLAN_NOT_FOUND_OR_USED: "计划已过期或已经执行，请重新生成。",
      PLAN_EXPIRED: "计划已超过有效期且已作废，请重新探测并生成。",
      PLAN_STATE_CHANGED: "来源、宿主、发现结果、所有权或目标状态已变化；未执行任何写入。",
      VERIFY_FAILED_ROLLED_BACK: "宿主回读验证失败，已尝试恢复操作前状态。",
      APPLY_FAILED: "宿主写入失败；其他 Adapter 和私人知识未被修改。",
    }[String(code || "")] || "操作未完成；请刷新后查看结构化状态。";
  }

  async function openAdapterPlan(hostId, operation) {
    try {
      const plan = await adapterRequest("/api/adapters/plan", {
        host_id: hostId,
        operation,
      });
      state.adapterPlan = plan;
      setText("adapter-dialog-host", `${hostId} · ${operation}`);
      setText("adapter-plan-version", plan.source_version);
      setText("adapter-plan-ref", plan.source_ref);
      setText("adapter-plan-hash", String(plan.content_hash || "").slice(0, 16));
      setText(
        "adapter-plan-expiry",
        `${Number(plan.expires_in_seconds || 0)} 秒（单次使用）`
      );
      setText("adapter-plan-rollback", plan.rollback);
      const diff = byId("adapter-plan-diff");
      diff.replaceChildren();
      asArray(plan.changes).forEach((change) => {
        const row = document.createElement("div");
        const action = document.createElement("strong");
        action.className = `diff-${change.action}`;
        action.textContent = String(change.action || "").toUpperCase();
        const target = document.createElement("code");
        target.textContent = change.target || "";
        row.append(action, target);
        diff.append(row);
      });
      setText("adapter-plan-preserves", `明确保留：${asArray(plan.preserves).join("、")}`);
      byId("adapter-confirm").checked = false;
      byId("apply-adapter-plan").disabled = true;
      byId("adapter-plan-error").hidden = true;
      byId("adapter-dialog").showModal();
    } catch (error) {
      showBanner(adapterErrorMessage(error.code));
    }
  }

  async function applyAdapterPlan() {
    const plan = asObject(state.adapterPlan);
    if (!byId("adapter-confirm").checked || !plan.plan_id) return;
    const button = byId("apply-adapter-plan");
    const errorNode = byId("adapter-plan-error");
    button.disabled = true;
    errorNode.hidden = true;
    try {
      const result = await adapterRequest("/api/adapters/apply", {
        plan_id: plan.plan_id,
        confirmation_token: plan.confirmation_token,
      });
      byId("adapter-dialog").close();
      state.adapterPlan = null;
      await refresh();
      showBanner(
        result.state === "verification_required"
          ? "宿主写入已完成，但 Kimi 新进程发现仍需在已配置模型的环境中人工验收。"
          : "Adapter 已执行并通过宿主官方发现机制回读。"
      );
    } catch (error) {
      errorNode.textContent = adapterErrorMessage(error.code);
      errorNode.hidden = false;
    } finally {
      button.disabled = !byId("adapter-confirm").checked;
    }
  }

  function errorMessage(code) {
    const mapping = {
      ABSOLUTE_PROJECT_ROOT_REQUIRED: "请输入项目的绝对目录。",
      PROJECT_UNAVAILABLE: "目录不可读取或不存在。",
      UNSAFE_PROJECT_ROOT: "目录是链接或不安全的来源，已拒绝接入。",
      INVALID_OPC_PROJECT: "目录中没有有效的 .opc/project.json。",
      PROJECT_ALREADY_REGISTERED: "此项目已经接入。",
      PROJECT_LIMIT_REACHED: "显式项目数量已达到上限。",
      INVALID_SETTINGS: "App 接入清单损坏；为避免覆盖，已拒绝写入。",
      CSRF_FORBIDDEN: "本地会话已变化，请刷新后重试。",
      DEMO_SETTINGS_READ_ONLY: "演示模式不写入 App 设置。",
    };
    return mapping[code] || "操作未完成；本地设置保持不变。";
  }

  async function selectProject(itemId) {
    if (!itemId || asObject(state.context).settings_state !== "ready") return;
    try {
      await mutate("/api/selection", "POST", { project_id: itemId });
    } catch (error) {
      showBanner(errorMessage(error.code));
    }
  }

  async function removeProject(itemId) {
    if (!itemId) return;
    try {
      await mutate(`/api/projects/${encodeURIComponent(itemId)}`, "DELETE");
      setText("live-status", "项目已从 App 接入清单移除；项目与知识数据均未删除");
    } catch (error) {
      showBanner(errorMessage(error.code));
    }
  }

  function openProjectDrawer() {
    if (asObject(state.context).settings_state === "invalid") return;
    const dialog = byId("project-drawer");
    byId("project-path").value = "";
    byId("project-form-error").hidden = true;
    dialog.showModal();
    byId("project-path").focus();
  }

  function closeProjectDrawer() {
    byId("project-drawer").close();
  }

  async function saveProject() {
    const path = byId("project-path").value.trim();
    const errorNode = byId("project-form-error");
    errorNode.hidden = true;
    byId("save-project").disabled = true;
    try {
      await mutate("/api/projects", "POST", { path });
      closeProjectDrawer();
      window.location.hash = "#projects";
      setText("live-status", "项目已显式接入 OPC App");
    } catch (error) {
      errorNode.textContent = errorMessage(error.code);
      errorNode.hidden = false;
    } finally {
      byId("save-project").disabled = false;
    }
  }

  function openQueue(itemId) {
    const item = state.queue.get(itemId);
    if (!item) return;
    setText("queue-dialog-severity", statusLabel(item.severity || "medium"));
    setText("queue-dialog-title", item.title || "下一步");
    setText("queue-dialog-description", item.description || "请查看现有证据后决定下一步。");
    setText("queue-dialog-next-step", item.next_step || "使用对应 OPC Skill 查看详情。");
    byId("queue-dialog").showModal();
  }

  function resolveView(value) {
    const candidate = String(value || "").replace(/^#/, "");
    return Object.prototype.hasOwnProperty.call(VIEWS, candidate) ? candidate : "overview";
  }

  function activateView(value) {
    const viewName = resolveView(value);
    document.querySelectorAll(".app-view").forEach((view) => {
      const selected = view.dataset.view === viewName;
      view.hidden = !selected;
      view.classList.toggle("is-active", selected);
    });
    document.querySelectorAll(".nav-item[data-nav]").forEach((item) => {
      const selected = item.dataset.nav === viewName;
      item.classList.toggle("is-active", selected);
      if (selected) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    setText("view-title", VIEWS[viewName]);
    document.title = `${VIEWS[viewName]} · OPC`;
    closeMobileMenu();
    applySearch();
    window.scrollTo({ top: 0, behavior: "auto" });
  }

  function applySearch() {
    const query = byId("global-search").value.trim().toLocaleLowerCase("zh-CN");
    const active = document.querySelector(".app-view:not([hidden])");
    if (!active) return;
    active.querySelectorAll(".filterable").forEach((node) => {
      node.hidden = Boolean(query) && !node.textContent.toLocaleLowerCase("zh-CN").includes(query);
    });
  }

  function openMobileMenu() {
    byId("sidebar").classList.add("is-open");
    byId("sidebar-scrim").hidden = false;
    byId("mobile-menu").setAttribute("aria-expanded", "true");
  }

  function closeMobileMenu() {
    byId("sidebar").classList.remove("is-open");
    byId("sidebar-scrim").hidden = true;
    byId("mobile-menu").setAttribute("aria-expanded", "false");
  }

  function showBanner(message) {
    const banner = byId("error-banner");
    banner.textContent = message;
    banner.hidden = false;
  }

  function hideBanner() {
    const banner = byId("error-banner");
    banner.hidden = true;
    banner.textContent = "";
  }

  function setupEvents() {
    document.querySelectorAll(".nav-item[data-nav]").forEach((item) => {
      item.addEventListener("click", (event) => {
        event.preventDefault();
        const hash = `#${item.dataset.nav}`;
        if (window.location.hash === hash) activateView(hash);
        else window.location.hash = hash;
      });
    });
    window.addEventListener("hashchange", () => activateView(window.location.hash));
    byId("global-search").addEventListener("input", applySearch);
    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") {
        event.preventDefault();
        byId("global-search").focus();
      }
      if (event.key === "Escape") closeMobileMenu();
    });
    byId("project-select").addEventListener("change", (event) => selectProject(event.target.value));
    byId("refresh-button").addEventListener("click", refresh);
    byId("mobile-menu").addEventListener("click", () => {
      if (byId("sidebar").classList.contains("is-open")) closeMobileMenu();
      else openMobileMenu();
    });
    byId("sidebar-scrim").addEventListener("click", closeMobileMenu);
    byId("open-project-drawer").addEventListener("click", openProjectDrawer);
    byId("manage-projects-from-settings").addEventListener("click", () => {
      window.location.hash = "#projects";
      openProjectDrawer();
    });
    byId("close-project-drawer").addEventListener("click", closeProjectDrawer);
    byId("cancel-project").addEventListener("click", closeProjectDrawer);
    byId("save-project").addEventListener("click", saveProject);
    byId("project-path").addEventListener("keydown", (event) => {
      if (event.key === "Enter") saveProject();
    });
    byId("close-queue-dialog").addEventListener("click", () => byId("queue-dialog").close());
    byId("copy-next-step").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(byId("queue-dialog-next-step").textContent);
        setText("live-status", "下一步建议已复制");
      } catch (error) {
        setText("live-status", "浏览器未允许复制；请手动选择文本");
      }
    });
    byId("adapter-list").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-adapter-host]");
      if (!button) return;
      openAdapterPlan(button.dataset.adapterHost, button.dataset.adapterOperation);
    });
    byId("close-adapter-dialog").addEventListener("click", () => byId("adapter-dialog").close());
    byId("cancel-adapter-plan").addEventListener("click", () => byId("adapter-dialog").close());
    byId("adapter-confirm").addEventListener("change", (event) => {
      byId("apply-adapter-plan").disabled = !event.target.checked;
    });
    byId("apply-adapter-plan").addEventListener("click", applyAdapterPlan);
  }

  function scheduleRefresh() {
    window.clearInterval(state.refreshTimer);
    state.refreshTimer = window.setInterval(() => {
      if (document.visibilityState === "visible") refresh();
    }, REFRESH_MS);
  }

  setupEvents();
  const initialView = resolveView(window.location.hash);
  if (window.location.hash !== `#${initialView}`) window.history.replaceState(null, "", `#${initialView}`);
  activateView(initialView);
  refresh();
  scheduleRefresh();
})();
