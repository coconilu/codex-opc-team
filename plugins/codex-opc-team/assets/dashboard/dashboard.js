(() => {
  "use strict";

  const REFRESH_MS = 30_000;
  const VIEWS = {
    overview: {
      title: "总览",
      description: "关键状态与待经理处理事项",
    },
    projects: {
      title: "项目",
      description: "查看显式接入项目与独立验收进度",
    },
    runs: {
      title: "运行",
      description: "查看各项目当前运行，不推断完整历史",
    },
    knowledge: {
      title: "知识",
      description: "查看候选、批准与 Git 发布状态",
    },
    lineage: {
      title: "证据链",
      description: "查看当前项目的上下文、实现与验收证据",
    },
    health: {
      title: "系统健康",
      description: "查看本地数据源与可选组件状态",
    },
  };
  const state = {
    snapshot: null,
    queue: new Map(),
    refreshTimer: null,
    selectedProjectId: null,
  };

  const byId = (id) => document.getElementById(id);
  const text = (id, value) => {
    const node = byId(id);
    if (node) node.textContent = value == null ? "—" : String(value);
  };

  const icon = (name, className = "icon") => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", className);
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#icon-${name}`);
    svg.append(use);
    return svg;
  };

  const asArray = (value) => (Array.isArray(value) ? value : []);
  const asObject = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});
  const finiteNumber = (value) => (Number.isFinite(Number(value)) ? Number(value) : 0);

  function relativeTime(value) {
    const timestamp = Date.parse(value);
    if (!Number.isFinite(timestamp)) return "时间未知";
    const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
    if (seconds < 60) return `${seconds || 1} 秒前`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} 分钟前`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} 小时前`;
    return `${Math.floor(hours / 24)} 天前`;
  }

  function statusTone(status) {
    const normalized = String(status || "").toLowerCase();
    if (["completed", "ready_for_manager", "pass", "available", "healthy", "ready", "published"].includes(normalized)) {
      return "healthy";
    }
    if (["failed", "blocked", "invalid", "error"].includes(normalized)) return "blocked";
    if (["planned", "implementing", "validating", "aligning", "paused", "degraded", "warning", "inconclusive"].includes(normalized)) {
      return "warning";
    }
    return "neutral";
  }

  function statusLabel(status) {
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
    return labels[String(status || "").toLowerCase()] || String(status || "未知");
  }

  function emptyState(title, description) {
    const node = document.createElement("div");
    node.className = "empty-state";
    node.append(icon("folder"));
    const strong = document.createElement("strong");
    strong.textContent = title;
    const paragraph = document.createElement("p");
    paragraph.textContent = description;
    node.append(strong, paragraph);
    return node;
  }

  function renderSummary(snapshot) {
    const summary = asObject(snapshot.summary);
    text("stat-projects", finiteNumber(summary.active_projects));
    text("stat-acceptance", finiteNumber(summary.pending_acceptance));
    text("stat-candidates", finiteNumber(summary.candidates));
    text("stat-published", finiteNumber(summary.published));
    text("mode-label", snapshot.mode === "demo" ? "演示数据 · 只读" : "本地只读");
    text("last-updated", `最后更新：${relativeTime(snapshot.generated_at)}`);
  }

  function renderProjects(snapshot) {
    const projects = asArray(snapshot.projects);
    const target = byId("project-list");
    target.replaceChildren();
    text("project-count", `${projects.length} 个`);
    if (!projects.length) {
      state.selectedProjectId = null;
      target.append(emptyState("尚未接入项目", "启动时使用 --project-root 明确指定项目目录。Dashboard 不会扫描用户目录。"));
      renderTimeline(null);
      return;
    }

    projects.forEach((project, index) => {
      const run = asObject(project.run);
      const acceptance = asObject(project.acceptance);
      const total = Math.max(0, finiteNumber(acceptance.total));
      const passed = Math.min(total, Math.max(0, finiteNumber(acceptance.passed)));
      const percent = total > 0 ? Math.round((passed / total) * 100) : 0;
      const tone = statusTone(run.status);

      const row = document.createElement("article");
      row.className = "project-row";
      row.dataset.projectId = String(project.id || "");

      const symbol = document.createElement("span");
      symbol.className = "project-symbol";
      symbol.append(icon(index % 2 === 0 ? "run" : "lineage"));

      const main = document.createElement("div");
      main.className = "project-main";
      const name = document.createElement("strong");
      name.textContent = project.name || project.id || "未命名项目";
      const status = document.createElement("div");
      status.className = "project-status";
      const dot = document.createElement("span");
      dot.className = `status-dot is-${tone}`;
      const label = document.createElement("span");
      label.textContent = `${statusLabel(run.status)}${run.active === false ? " · 非活动" : " · 活动"}`;
      status.append(dot, label);
      main.append(name, status);

      const progress = document.createElement("div");
      progress.className = "project-progress";
      const progressHeading = document.createElement("div");
      progressHeading.className = "progress-heading";
      const progressLabel = document.createElement("span");
      progressLabel.textContent = "独立验收进度";
      const progressPercent = document.createElement("strong");
      progressPercent.textContent = total ? `${percent}%` : "未记录";
      progressHeading.append(progressLabel, progressPercent);
      const track = document.createElement("div");
      track.className = "progress-track";
      const fill = document.createElement("div");
      fill.className = "progress-fill";
      fill.style.width = `${percent}%`;
      track.append(fill);
      const count = document.createElement("span");
      count.className = "progress-count";
      count.textContent = total ? `${passed} / ${total}` : "0 / 0";
      progress.append(progressHeading, track, count);

      const time = document.createElement("div");
      time.className = "project-time";
      const timeLabel = document.createElement("small");
      timeLabel.textContent = "最近运行";
      const timeValue = document.createElement("strong");
      timeValue.textContent = relativeTime(run.updated_at || snapshot.generated_at);
      time.append(timeLabel, timeValue);

      row.append(symbol, main, progress, time);
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      row.setAttribute("aria-label", `查看 ${name.textContent} 的证据链`);
      const select = (openLineage = false) => {
        target.querySelectorAll(".project-row").forEach((item) => item.removeAttribute("aria-current"));
        row.setAttribute("aria-current", "true");
        state.selectedProjectId = String(project.id || "");
        renderTimeline(project);
        if (openLineage) navigateToView("lineage");
      };
      row.addEventListener("click", () => select(true));
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          select(true);
        }
      });
      target.append(row);
    });

    const selectedProject = projects.find(
      (project) => String(project.id || "") === state.selectedProjectId,
    ) || projects[0];
    state.selectedProjectId = String(selectedProject.id || "");
    const selectedRow = [...target.querySelectorAll(".project-row")].find(
      (row) => row.dataset.projectId === state.selectedProjectId,
    );
    selectedRow?.setAttribute("aria-current", "true");
    renderTimeline(selectedProject);
  }

  function renderRuns(snapshot) {
    const projects = asArray(snapshot.projects);
    const target = byId("run-list");
    target.replaceChildren();
    text("run-count", `${projects.length} 个`);
    if (!projects.length) {
      target.append(emptyState("没有当前运行", "显式接入项目后，这里会显示其当前运行状态。"));
      return;
    }

    projects.forEach((project) => {
      const run = asObject(project.run);
      const acceptance = asObject(project.acceptance);
      const total = Math.max(0, finiteNumber(acceptance.total));
      const passed = Math.min(total, Math.max(0, finiteNumber(acceptance.passed)));
      const tone = statusTone(run.status);

      const card = document.createElement("article");
      card.className = "run-card";

      const heading = document.createElement("div");
      heading.className = "run-heading";
      const symbol = document.createElement("span");
      symbol.className = "run-symbol";
      symbol.append(icon("run"));
      const names = document.createElement("span");
      const title = document.createElement("strong");
      title.textContent = run.title || "当前运行";
      const projectName = document.createElement("small");
      projectName.textContent = project.name || project.id || "未命名项目";
      names.append(title, projectName);
      heading.append(symbol, names);

      const status = document.createElement("div");
      status.className = "run-status";
      const dot = document.createElement("span");
      dot.className = `status-dot is-${tone}`;
      const statusText = document.createElement("strong");
      statusText.textContent = statusLabel(run.status);
      const activity = document.createElement("small");
      activity.textContent = run.active === false ? "非活动" : "活动";
      status.append(dot, statusText, activity);

      const evidence = document.createElement("div");
      evidence.className = "run-evidence";
      const evidenceLabel = document.createElement("span");
      evidenceLabel.textContent = "独立验收";
      const evidenceValue = document.createElement("strong");
      evidenceValue.textContent = total ? `${passed} / ${total}` : "未记录";
      evidence.append(evidenceLabel, evidenceValue);

      const updated = document.createElement("div");
      updated.className = "run-updated";
      const updatedLabel = document.createElement("span");
      updatedLabel.textContent = "最近更新";
      const updatedValue = document.createElement("strong");
      updatedValue.textContent = relativeTime(run.updated_at || snapshot.generated_at);
      updated.append(updatedLabel, updatedValue);

      card.append(heading, status, evidence, updated);
      target.append(card);
    });
  }

  function renderKnowledge(snapshot) {
    const knowledge = asObject(snapshot.knowledge);
    const nestedCounts = asObject(knowledge.counts);
    const counts = Object.keys(nestedCounts).length ? nestedCounts : knowledge;
    text("knowledge-candidate", finiteNumber(counts.candidate));
    text("knowledge-approved", finiteNumber(counts.approved_uncommitted));
    text("knowledge-published", finiteNumber(counts.published));
    text("knowledge-obsolete", finiteNumber(counts.obsolete));

    const health = asObject(snapshot.health);
    const fileGit = asObject(health.file_git);
    const mem0 = asObject(health.mem0);
    text("authority-detail", fileGit.detail || statusLabel(fileGit.state));
    text("provider-title", mem0.label || "Mem0");
    text("provider-detail", mem0.detail || statusLabel(mem0.state));
  }

  function severityClass(value) {
    const severity = String(value || "info").toLowerCase();
    if (["blocked", "critical", "error", "high"].includes(severity)) return "blocked";
    if (["warning", "attention", "medium"].includes(severity)) return "warning";
    return "info";
  }

  function renderQueue(snapshot) {
    const queue = asArray(snapshot.manager_queue);
    const target = byId("queue-list");
    target.replaceChildren();
    state.queue.clear();
    text("queue-count", `${queue.length} 项`);
    if (!queue.length) {
      target.append(emptyState("没有待处理项", "当前快照没有需要经理介入的验收、知识或评测事项。"));
      return;
    }

    queue.slice(0, 8).forEach((item, index) => {
      const id = String(item.id || `queue-${index + 1}`);
      state.queue.set(id, item);
      const card = document.createElement("article");
      card.className = `queue-card is-${severityClass(item.severity)}`;
      const title = document.createElement("div");
      title.className = "queue-title";
      const dot = document.createElement("span");
      dot.className = "status-dot";
      const titleText = document.createElement("span");
      titleText.textContent = item.title || "待处理事项";
      title.append(dot, titleText);
      const description = document.createElement("p");
      description.textContent = item.description || "需要经理查看现有证据并决定下一步。";
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "查看下一步";
      button.dataset.queueId = id;
      button.addEventListener("click", () => openQueueDialog(id));
      card.append(title, description, button);
      target.append(card);
    });
  }

  function healthTone(value) {
    const stateValue = String(value || "").toLowerCase();
    if (["healthy", "ready", "available", "disabled"].includes(stateValue)) return "healthy";
    if (["invalid", "blocked", "error", "failed"].includes(stateValue)) return "blocked";
    if (["absent", "unavailable"].includes(stateValue)) return "neutral";
    return "warning";
  }

  function renderHealth(snapshot) {
    const target = byId("health-list");
    target.replaceChildren();
    const health = asObject(snapshot.health);
    const entries = [
      ["file_git", "document", "File/Git 权威源"],
      ["mem0", "database", "Mem0"],
      ["projects", "folder", "项目数据"],
    ];

    entries.forEach(([key, iconName, fallbackLabel]) => {
      const item = asObject(health[key]);
      const stateValue = item.state || "absent";
      const row = document.createElement("div");
      row.className = `health-row is-${healthTone(stateValue)}`;
      const dot = document.createElement("span");
      dot.className = "health-dot";
      const content = document.createElement("span");
      const title = document.createElement("strong");
      title.textContent = item.label || fallbackLabel;
      const detail = document.createElement("small");
      detail.textContent = item.detail || "未提供状态详情";
      content.append(title, detail);
      const badge = document.createElement("span");
      badge.className = "health-state";
      badge.textContent = statusLabel(stateValue);
      row.append(dot, icon(iconName), content, badge);
      target.append(row);
    });
  }

  function renderTimeline(project) {
    const stages = [...document.querySelectorAll("#timeline li")];
    stages.forEach((stage) => {
      stage.classList.remove("is-recorded", "is-warning");
      stage.querySelector("em").textContent = "未记录";
    });
    if (!project) {
      text("lineage-project-label", "选择当前项目查看证据状态");
      return;
    }

    const run = asObject(project.run);
    const status = String(run.status || "");
    const hasRun = Boolean(run.title || run.id);
    const lineage = String(project.lineage_status || "unavailable");
    const feedback = String(project.feedback_status || "unavailable");
    const recorded = {
      context: hasRun,
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
    if (lineage === "degraded") {
      stages[0]?.classList.add("is-warning");
      const firstStageLabel = stages[0]?.querySelector("em");
      if (firstStageLabel) {
        firstStageLabel.textContent = "链路降级";
      }
    }
    text("lineage-project-label", `${project.name || project.id || "当前项目"} · ${statusLabel(lineage)}`);
  }

  function renderWarnings(snapshot) {
    const warnings = asArray(snapshot.warnings)
      .map((value) => {
        if (typeof value === "string") return value.trim();
        const warning = asObject(value);
        return String(warning.message || warning.code || "").trim();
      })
      .filter(Boolean);
    const banner = byId("error-banner");
    if (!warnings.length) {
      banner.hidden = true;
      banner.textContent = "";
      return;
    }
    banner.hidden = false;
    banner.textContent = `本地状态提示：${warnings.slice(0, 3).join("；")}`;
  }

  function openQueueDialog(id) {
    const item = state.queue.get(id);
    if (!item) return;
    text("dialog-severity", statusLabel(item.severity || "warning"));
    text("dialog-title", item.title || "下一步");
    text("dialog-description", item.description || "请查看现有证据后决定下一步。");
    text("dialog-next-step", item.next_step || "使用对应 OPC Skill 查看详情");
    const dialog = byId("queue-dialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
  }

  function render(snapshot) {
    state.snapshot = snapshot;
    renderSummary(snapshot);
    renderProjects(snapshot);
    renderRuns(snapshot);
    renderKnowledge(snapshot);
    renderQueue(snapshot);
    renderHealth(snapshot);
    renderWarnings(snapshot);
    text("live-status", "OPC Dashboard 本地数据已刷新");
  }

  async function refresh() {
    try {
      const response = await fetch("/api/snapshot", {
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const snapshot = await response.json();
      if (!snapshot || typeof snapshot !== "object") throw new Error("snapshot 格式无效");
      render(snapshot);
    } catch (error) {
      const banner = byId("error-banner");
      banner.hidden = false;
      banner.textContent = "无法读取本地快照。请查看启动终端中的安全诊断信息。";
      text("last-updated", "读取失败");
      text("live-status", "OPC Dashboard 本地数据读取失败");
    }
  }

  function scheduleRefresh() {
    window.clearInterval(state.refreshTimer);
    state.refreshTimer = window.setInterval(() => {
      if (document.visibilityState === "visible") refresh();
    }, REFRESH_MS);
  }

  function resolveView(value) {
    const candidate = String(value || "").replace(/^#/, "");
    return Object.hasOwn(VIEWS, candidate) ? candidate : "overview";
  }

  function activateView(name) {
    const activeView = resolveView(name);
    document.querySelectorAll(".dashboard-view").forEach((view) => {
      const selected = view.dataset.view === activeView;
      view.hidden = !selected;
      view.classList.toggle("is-active", selected);
    });
    document.querySelectorAll(".nav-item").forEach((item) => {
      const selected = item.dataset.nav === activeView;
      item.classList.toggle("is-active", selected);
      if (selected) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    const metadata = VIEWS[activeView];
    text("view-title", metadata.title);
    text("view-description", metadata.description);
    document.title = `${metadata.title} · OPC Control Room`;
    window.scrollTo({ top: 0, behavior: "auto" });
  }

  function navigateToView(name) {
    const nextView = resolveView(name);
    const nextHash = `#${nextView}`;
    if (window.location.hash === nextHash) activateView(nextView);
    else window.location.hash = nextHash;
  }

  function setupNavigation() {
    document.querySelectorAll(".nav-item").forEach((item) => {
      item.addEventListener("click", (event) => {
        event.preventDefault();
        navigateToView(item.dataset.nav);
      });
    });
    window.addEventListener("hashchange", () => activateView(window.location.hash));
    const initialView = resolveView(window.location.hash);
    if (window.location.hash !== `#${initialView}`) {
      window.history.replaceState(null, "", `#${initialView}`);
    }
    activateView(initialView);
  }

  setupNavigation();
  refresh();
  scheduleRefresh();
})();
