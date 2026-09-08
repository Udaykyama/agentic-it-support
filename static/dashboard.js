(() => {
  "use strict";

  const API = "/api/v1";
  const PAGE_SIZE = 25;
  const REQUEST_TIMEOUT = 20000;
  const $ = (id) => document.getElementById(id);
  const numberFormat = new Intl.NumberFormat();
  const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
  const dayFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });
  const categories = {
    access: { label: "Access", color: "#818cf8" },
    hardware: { label: "Hardware", color: "#f59e0b" },
    software: { label: "Software", color: "#34d399" },
    network: { label: "Network", color: "#38bdf8" },
    other: { label: "Other", color: "#94a3b8" },
  };
  const statuses = {
    pending: "Submitted",
    processing: "Processing",
    recommended: "Recommended",
    escalated: "Escalated",
    resolved: "Confirmed resolved",
    failed: "Failed",
    draft: "Draft",
    approving: "Indexing for approval",
    approved: "Approved",
    rejected: "Rejected / revoked",
    queued: "Queued",
    running: "Running",
    retrying: "Retrying",
    completed: "Completed",
  };
  const activeJobStatuses = new Set(["queued", "running", "retrying"]);
  const ticketExplanations = {
    pending: "Submitted and waiting for background processing. No recommendation has been produced yet.",
    processing: "Classification and approved-runbook lookup are in progress. This is not a completed outcome.",
    recommended: "Advice from a human-approved runbook. These instructions have not been executed or verified by NeuralDesk. An operator must confirm the actual outcome.",
    escalated: "Needs human triage. A routing target is an internal label or email, not confirmation that a notification was delivered.",
    failed: "Processing failed. Your ticket is saved and remains unresolved. Review the error and retry when appropriate.",
    resolved: "An operator explicitly confirmed resolution. Their recorded outcome is shown below.",
  };
  const state = {
    epoch: 0,
    readRevision: 0,
    user: null,
    tenant: null,
    identityKey: null,
    csrf: null,
    aiEnabled: false,
    controllers: new Set(),
    loads: new Map(),
    operations: new Set(),
    refresh: null,
    refreshTimer: null,
    expiryTimer: null,
    retryAt: 0,
    signingOut: false,
    tickets: [],
    runbooks: [],
    insights: null,
    ticketOffset: 0,
    runbookOffset: 0,
    ticketPagination: null,
    runbookPagination: null,
    selectedId: null,
    selectedTicket: null,
    ticketOpener: null,
    resolutionDrafts: new Map(),
    submission: null,
    jobs: new Map(),
    jobPollOffset: 0,
    runbookErrors: new Map(),
    runbookNotices: new Map(),
    generationErrors: new Map(),
    signatures: new Map(),
  };

  class ApiError extends Error {
    constructor(message, { status = 0, code = "", requestId = "", details = null } = {}) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.code = code;
      this.requestId = requestId;
      this.details = details;
    }
  }

  class ObsoleteResponse extends Error {}

  function node(tag, className = "", text = null) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== null && text !== undefined) result.textContent = String(text);
    return result;
  }

  function actionButton(label, className, action, focusKey = "") {
    const result = node("button", className, label);
    result.type = "button";
    result.addEventListener("click", action);
    if (focusKey) result.dataset.focusKey = focusKey;
    return result;
  }

  function message(target, text = "") {
    const element = typeof target === "string" ? $(target) : target;
    element.textContent = text;
    element.hidden = !text;
  }

  function announce(text) {
    $("app-status").textContent = text;
  }

  function humanize(value) {
    const text = String(value ?? "").replace(/[_-]+/g, " ").trim();
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : "Not classified";
  }

  function contentText(value) {
    if (value === null || value === undefined || value === "") return "Not provided.";
    return typeof value === "string" ? value : JSON.stringify(value, null, 2);
  }

  function numeric(value) {
    return typeof value === "number" && Number.isFinite(value) ? value : 0;
  }

  function count(value) {
    return numberFormat.format(Math.max(0, numeric(value)));
  }

  function percent(value) {
    return `${Math.round(Math.max(0, Math.min(1, numeric(value))) * 100)}%`;
  }

  function date(value, dayOnly = false) {
    if (!value) return "Not provided";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? "Not provided" : (dayOnly ? dayFormat : dateFormat).format(parsed);
  }

  function hasRole(...roles) {
    return Boolean(state.user && roles.some((role) => state.user.roles.includes(role)));
  }

  function isStaff() {
    return hasRole("agent", "admin");
  }

  function canWrite() {
    return Boolean(state.user && state.csrf && !state.signingOut);
  }

  function canSubmit() {
    return hasRole("requester", "agent", "admin");
  }

  function canSeeInsights() {
    return hasRole("viewer", "agent", "admin");
  }

  function categoryBadge(category) {
    const known = Object.hasOwn(categories, category) ? category : "other";
    return node("span", `category-tag cat-${known}`, category ? humanize(category) : "Not classified");
  }

  function statusBadge(status) {
    const known = Object.hasOwn(statuses, status) ? status : "pending";
    const badge = node("span", `status-badge status-${known}`);
    const dot = node("span", "status-dot");
    dot.setAttribute("aria-hidden", "true");
    badge.append(dot, node("span", "", Object.hasOwn(statuses, status) ? statuses[status] : humanize(status)));
    return badge;
  }

  function errorText(error) {
    const parts = [error.message || "The request could not be completed. Please try again."];
    if (error.code) parts.push(`Code: ${error.code}`);
    if (error.details) parts.push(`Details: ${contentText(error.details).slice(0, 1600)}`);
    if (error.requestId) parts.push(`Request reference: ${error.requestId}`);
    if (error.status === 429) {
      const seconds = Math.max(1, Math.ceil((state.retryAt - Date.now()) / 1000));
      parts.push(`Wait ${seconds} seconds before trying again.`);
    }
    return parts.join("\n");
  }

  function ignored(error) {
    return error instanceof ObsoleteResponse || error.status === 401;
  }

  function malformed() {
    return new ApiError("The server returned an unexpected response. Refresh to try again.", { code: "invalid_response" });
  }

  function captureFocus(container) {
    const focused = document.activeElement;
    return container.contains(focused) ? focused.dataset.focusKey || null : null;
  }

  function restoreFocus(container, key) {
    if (!key) return false;
    const replacement = Array.from(container.querySelectorAll("[data-focus-key]")).find((item) => item.dataset.focusKey === key);
    if (!replacement || replacement.disabled) return false;
    replacement.focus({ preventScroll: true });
    return true;
  }

  function changed(name, data) {
    const signature = JSON.stringify(data);
    if (state.signatures.get(name) === signature) return false;
    state.signatures.set(name, signature);
    return true;
  }

  function retryAfter(value) {
    if (!value) return Date.now() + 30000;
    const seconds = Number(value);
    if (Number.isFinite(seconds)) return Date.now() + Math.max(1, seconds) * 1000;
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? Date.now() + 30000 : Math.max(Date.now() + 1000, parsed);
  }

  async function request(path, { method = "GET", body, headers = {}, authCheck = false } = {}) {
    const epoch = state.epoch;
    if (!authCheck && !state.user) throw new ObsoleteResponse();
    if (method !== "GET" && !state.csrf) {
      throw new ApiError("Your session cannot make changes without a CSRF token. Sign in again.", { code: "csrf_unavailable" });
    }
    if (Date.now() < state.retryAt && path !== "/auth/logout") {
      throw new ApiError("Requests are temporarily rate limited.", { status: 429, code: "rate_limited" });
    }
    const controller = new AbortController();
    state.controllers.add(controller);
    const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT);
    try {
      const requestHeaders = { Accept: "application/json", ...headers };
      if (body !== undefined) requestHeaders["Content-Type"] = "application/json";
      if (method !== "GET") requestHeaders["X-CSRF-Token"] = state.csrf;
      const response = await fetch(path === "/auth/logout" ? path : `${API}${path}`, {
        method,
        headers: requestHeaders,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "same-origin",
        cache: "no-store",
        signal: controller.signal,
        redirect: "error",
      });
      let payload = null;
      if (response.status !== 204) {
        try {
          payload = await response.json();
        } catch {
          payload = null;
        }
      }
      if (epoch !== state.epoch) throw new ObsoleteResponse();
      if (!response.ok) {
        const serverError = payload && payload.error;
        const error = new ApiError(
          serverError && typeof serverError.message === "string" ? serverError.message : `Request failed (${response.status}). Try again or contact your administrator.`,
          {
            status: response.status,
            code: serverError?.code || "",
            requestId: serverError?.request_id || response.headers.get("X-Request-ID") || "",
            details: serverError?.details || null,
          },
        );
        if (response.status === 401) {
          endSession(state.user ? "Your session expired. Sign in again to continue. Company data has been cleared from this page." : "Use your company account to submit tickets and access human-approved support runbooks.");
        } else if (response.status === 429) {
          state.retryAt = Math.max(state.retryAt, retryAfter(response.headers.get("Retry-After")));
          $("connection-state").textContent = "Rate limited";
          $("connection-state").classList.add("is-paused");
        }
        throw error;
      }
      // A late read must not overwrite a write the server has already acknowledged.
      if (method !== "GET") state.readRevision += 1;
      if (response.status === 204 || path === "/auth/logout") return {};
      if (!payload || typeof payload !== "object") throw malformed();
      return payload;
    } catch (error) {
      if (epoch !== state.epoch) throw new ObsoleteResponse();
      if (error.name === "AbortError") {
        throw new ApiError("The request timed out. Your changes could not be confirmed; check the current state before retrying.", { code: "request_timeout" });
      }
      if (error instanceof TypeError) {
        throw new ApiError("The server could not be reached. Check your connection and try again.", { code: "network_error" });
      }
      throw error;
    } finally {
      window.clearTimeout(timer);
      state.controllers.delete(controller);
    }
  }

  function resetTenantData() {
    state.epoch += 1;
    state.readRevision += 1;
    window.clearTimeout(state.refreshTimer);
    window.clearTimeout(state.expiryTimer);
    state.controllers.forEach((controller) => controller.abort());
    state.controllers.clear();
    state.loads.clear();
    state.operations.clear();
    state.signatures.clear();
    state.resolutionDrafts.clear();
    state.jobs.clear();
    state.jobPollOffset = 0;
    state.runbookErrors.clear();
    state.runbookNotices.clear();
    state.generationErrors.clear();
    state.tickets = [];
    state.runbooks = [];
    state.insights = null;
    state.ticketOffset = 0;
    state.runbookOffset = 0;
    state.ticketPagination = null;
    state.runbookPagination = null;
    state.selectedId = null;
    state.selectedTicket = null;
    state.ticketOpener = null;
    state.submission = null;
    state.retryAt = 0;
    $("ticket-form").reset();
    $("f-email").readOnly = false;
    $("ticket-status").value = "";
    $("insight-days").value = "14";
    $("ticket-detail").hidden = true;
    $("ticket-detail-content").replaceChildren();
    $("ticket-detail-heading").textContent = "Ticket details";
    $("tenant-name").textContent = "";
    $("user-name").textContent = "";
    $("user-role").textContent = "";
    $("workspace-scope").textContent = "";
    $("runbook-stat-detail").textContent = "Available for future advice";
    $("runbook-policy").textContent = "Only human-approved, indexed runbooks can provide recommendations.";
    $("insight-window").textContent = "Comparing matching categories and subcategories with an equal preceding window.";
    $("form-policy").textContent = "";
    $("email-hint").textContent = "";
    $("ticket-list").replaceChildren(emptyTicketRow("Tickets will appear here after sign-in."));
    $("runbook-list").replaceChildren(node("p", "no-data", "Runbooks will appear here after sign-in."));
    $("insight-list").replaceChildren(node("p", "no-data", "Recurring issues will appear here after sign-in."));
    $("job-list").replaceChildren(node("p", "no-data", "No jobs tracked in this tab yet."));
    $("cat-breakdown").replaceChildren();
    $("ring-pct").textContent = "—";
    $("ring-fill").style.strokeDashoffset = "283";
    [
      "stat-total", "stat-resolved", "stat-escalated", "stat-runbooks", "stat-pending",
      "stat-processing", "stat-recommended", "stat-failed", "ticket-count", "runbook-count",
      "tickets-page", "runbooks-page",
    ].forEach((id) => { $(id).textContent = "—"; });
    [
      "app-error", "stats-error", "tickets-error", "runbooks-error", "insights-error",
      "ticket-detail-error", "submit-error", "submit-result", "submission-retry-note",
      "pipeline-notice", "permission-notice",
    ].forEach((id) => message(id));
    ["tickets-region", "runbook-list", "insight-list", "ticket-detail-content"].forEach((id) => $(id).setAttribute("aria-busy", "false"));
  }

  function endSession(explanation) {
    state.user = null;
    state.tenant = null;
    state.csrf = null;
    state.identityKey = null;
    state.aiEnabled = false;
    resetTenantData();
    $("workspace").hidden = true;
    $("auth-panel").hidden = false;
    $("auth-message").textContent = explanation;
    $("auth-retry-btn").hidden = false;
    ["identity", "refresh-btn", "logout-btn", "connection-state"].forEach((id) => { $(id).hidden = true; });
    announce("Not signed in.");
    updateControls();
  }

  function applyIdentity(payload) {
    if (!payload.user || !payload.tenant || !Array.isArray(payload.user.roles) ||
        !payload.user.subject || !payload.tenant.id ||
        String(payload.user.tenant_id) !== String(payload.tenant.id)) throw malformed();
    const key = JSON.stringify([payload.user.subject, payload.tenant.id, [...payload.user.roles].sort(), payload.user.email || ""]);
    const newIdentity = key !== state.identityKey;
    if (newIdentity) resetTenantData();
    state.user = payload.user;
    state.tenant = payload.tenant;
    state.csrf = typeof payload.csrf_token === "string" && payload.csrf_token ? payload.csrf_token : null;
    state.aiEnabled = payload.capabilities?.ai_enabled === true;
    state.identityKey = key;
    $("tenant-name").textContent = payload.tenant.name || "Company workspace";
    $("user-name").textContent = payload.user.email || "Signed-in company account";
    $("user-role").textContent = payload.user.roles.map(humanize).join(" · ");
    $("workspace-scope").textContent = canSeeInsights() ? "Company-wide tickets and knowledge in your authorized workspace." : "Your tickets and company-approved runbooks. Other employees’ tickets are not shown.";
    $("workspace").hidden = false;
    $("auth-panel").hidden = true;
    ["identity", "refresh-btn", "logout-btn", "connection-state"].forEach((id) => { $(id).hidden = false; });
    if (newIdentity) {
      $("f-email").value = payload.user.email || "";
      $("ticket-list").replaceChildren(emptyTicketRow("Loading tickets…"));
      $("runbook-list").replaceChildren(node("p", "no-data", "Loading runbooks…"));
    }
    window.clearTimeout(state.expiryTimer);
    const rawExpiry = payload.user.expires_at;
    const expiry = typeof rawExpiry === "number" ? (rawExpiry < 1e12 ? rawExpiry * 1000 : rawExpiry) : Date.parse(rawExpiry);
    if (Number.isFinite(expiry)) {
      if (expiry <= Date.now()) {
        endSession("Your session expired. Sign in again to continue.");
        return false;
      }
      const epoch = state.epoch;
      state.expiryTimer = window.setTimeout(() => {
        if (epoch === state.epoch) endSession("Your session expired. Sign in again to continue. Company data has been cleared from this page.");
      }, Math.min(expiry - Date.now(), 2147483647));
    }
    updateControls();
    return true;
  }

  function updateControls() {
    const submitting = state.operations.has("submit");
    const missingEmail = canSubmit() && !isStaff() && !state.user?.email;
    $("ticket-fields").disabled = !canWrite() || !canSubmit() || missingEmail || submitting;
    $("submit-ticket").disabled = !canWrite() || !canSubmit() || missingEmail || submitting;
    $("submit-ticket").textContent = submitting ? "Submitting…" : state.submission ? "Retry submission" : "Submit ticket";
    $("f-email").readOnly = !isStaff();
    $("refresh-btn").disabled = Boolean(state.refresh) || state.signingOut;
    $("refresh-btn").textContent = state.refresh ? "Refreshing…" : "↻ Refresh";
    $("logout-btn").disabled = state.signingOut;
    $("logout-btn").textContent = state.signingOut ? "Signing out…" : "Sign out";
    $("auth-retry-btn").disabled = Boolean(state.refresh);
    $("ticket-status").disabled = state.loads.has("tickets") || !state.user;
    $("insight-days").disabled = state.loads.has("insights") || !state.user;
    $("reload-ticket").disabled = state.loads.has("detail") || state.operations.has(`ticket:${state.selectedId}`);
    $("insights-panel").hidden = !canSeeInsights();
    if (state.user) {
      message("pipeline-notice", state.aiEnabled ? "" : "AI processing is disabled. Ticket submission and human triage remain available. Draft generation, indexing, and AI retries are unavailable.");
      message("permission-notice", state.csrf ? "" : "This session has no CSRF token and is read-only. Sign in again before making changes.");
      $("runbook-policy").textContent = canSeeInsights() ? "Drafts require administrator review. Only approved, indexed runbooks can provide future recommendations." : "Only human-approved, indexed runbooks are available to you.";
      $("form-policy").textContent = !canSubmit() ? "Your viewer role is read-only. An agent or requester can submit a ticket."
        : missingEmail ? "Your identity provider must supply an email claim before you can submit. Ask your company administrator to add it, then sign in again."
          : !state.csrf ? "Sign in again to obtain a session that can submit tickets."
            : isStaff() ? "Submit for yourself or on behalf of a colleague. An accepted ticket is not yet a resolved ticket."
              : "Your email comes from company SSO and cannot be changed here. An agent confirms the final outcome.";
      $("email-hint").textContent = isStaff() ? "Enter the email of the person requesting support." : missingEmail ? "An email claim was not provided by your identity provider." : "Verified by your company identity provider.";
    }
    updatePagination("tickets");
    updatePagination("runbooks");
  }

  function pagination(payload, length) {
    if (!payload || !Number.isInteger(payload.total) || !Number.isInteger(payload.offset) ||
        !Number.isInteger(payload.limit) || payload.limit < 1 || payload.offset < 0 ||
        payload.total < 0 || typeof payload.has_more !== "boolean") throw malformed();
    return { ...payload, length };
  }

  function updatePagination(name) {
    const page = name === "tickets" ? state.ticketPagination : state.runbookPagination;
    const busy = state.loads.has(name) || !state.user;
    $(`${name}-prev`).disabled = busy || !page || page.offset === 0;
    $(`${name}-next`).disabled = busy || !page || !page.has_more;
    if (page) {
      $(`${name}-page`).textContent = page.total === 0 ? "0 results"
        : page.length === 0 ? `No results on this page · ${count(page.total)} total`
          : `${count(page.offset + 1)}–${count(page.offset + page.length)} of ${count(page.total)}`;
    }
  }

  function loadResource(name, key, fetcher, accept, errorId, label, busyId) {
    const existing = state.loads.get(name);
    if (existing && existing.key === key && existing.epoch === state.epoch && existing.revision === state.readRevision) return existing.promise;
    const operation = { key, epoch: state.epoch, revision: state.readRevision };
    state.loads.set(name, operation);
    if (busyId) $(busyId).setAttribute("aria-busy", "true");
    updateControls();
    operation.promise = (async () => {
      try {
        const payload = await fetcher();
        if (state.loads.get(name) !== operation || operation.epoch !== state.epoch || operation.revision !== state.readRevision) return true;
        accept(payload);
        message(errorId);
        return true;
      } catch (error) {
        if (state.loads.get(name) === operation && operation.epoch === state.epoch && operation.revision === state.readRevision && !ignored(error)) {
          message(errorId, `${label} could not be loaded. Previous data, if any, may be out of date.\n${errorText(error)}`);
          if (name === "tickets" && !state.ticketPagination) $("ticket-list").replaceChildren(emptyTicketRow("Tickets are unavailable. Use Refresh to try again."));
          if (name === "runbooks" && !state.runbookPagination) $("runbook-list").replaceChildren(node("p", "no-data", "Runbooks are unavailable. Use Refresh to try again."));
          if (name === "insights" && !state.insights) $("insight-list").replaceChildren(node("p", "no-data", "Recurring issues are unavailable. Use Refresh to try again."));
          if (name === "detail" && !state.selectedTicket) {
            $("ticket-detail-heading").textContent = "Ticket details unavailable";
            $("ticket-detail-content").replaceChildren(node("p", "secondary", "Use Refresh ticket details to try again."));
          }
        }
        return false;
      } finally {
        if (state.loads.get(name) === operation) {
          state.loads.delete(name);
          if (busyId) $(busyId).setAttribute("aria-busy", "false");
          updateControls();
        }
      }
    })();
    return operation.promise;
  }

  function loadStats() {
    return loadResource("stats", "stats", () => request("/stats"), (payload) => {
      if (!payload.statuses || !payload.categories || !payload.runbooks || typeof payload.total !== "number") throw malformed();
      $("stat-total").textContent = count(payload.total);
      ["resolved", "escalated", "pending", "processing", "recommended", "failed"].forEach((status) => {
        $(`stat-${status}`).textContent = count(payload.statuses[status]);
      });
      $("stat-runbooks").textContent = count(payload.runbooks.approved);
      $("runbook-stat-detail").textContent = canSeeInsights()
        ? `${count(payload.runbooks.draft)} draft · ${count(payload.runbooks.approving)} indexing · ${count(payload.runbooks.rejected)} rejected`
        : "Available for future advice";
      const rate = Math.max(0, Math.min(1, numeric(payload.resolution_rate)));
      $("ring-pct").textContent = percent(rate);
      const circumference = 2 * Math.PI * 45;
      $("ring-fill").style.strokeDasharray = String(circumference);
      $("ring-fill").style.strokeDashoffset = String(circumference * (1 - rate));
      const fragment = document.createDocumentFragment();
      Object.entries(categories).forEach(([category, info]) => {
        const row = node("div", "cat-row");
        const track = node("div", "cat-track");
        const fill = node("div", "cat-fill");
        fill.style.width = `${payload.total > 0 ? Math.min(100, Math.max(0, numeric(payload.categories[category]) / payload.total * 100)) : 0}%`;
        fill.style.backgroundColor = info.color;
        track.setAttribute("aria-hidden", "true");
        track.append(fill);
        row.append(node("span", "cat-name", info.label), track, node("span", "cat-num", count(payload.categories[category])));
        fragment.append(row);
      });
      $("cat-breakdown").replaceChildren(fragment);
    }, "stats-error", "Statistics");
  }

  function emptyTicketRow(text) {
    const row = node("tr");
    const cell = node("td", "no-data", text);
    cell.colSpan = 4;
    row.append(cell);
    return row;
  }

  function renderTickets() {
    if (!changed("tickets", [state.tickets, state.selectedId])) return;
    const list = $("ticket-list");
    const focus = captureFocus(list);
    if (!state.tickets.length) {
      const emptyMessage = state.ticketPagination?.total > 0 && state.ticketOffset > 0
        ? "No tickets remain on this page. Select Previous."
        : $("ticket-status").value ? "No tickets match this status. Try All states."
          : canSubmit() ? "No tickets yet. Submit your first ticket below." : "No tickets are visible in this workspace yet.";
      list.replaceChildren(emptyTicketRow(emptyMessage));
      return;
    }
    const fragment = document.createDocumentFragment();
    state.tickets.forEach((ticket) => {
      const row = node("tr", "ticket-row");
      row.classList.toggle("is-selected", String(ticket.id) === state.selectedId);
      const titleCell = node("td");
      const title = actionButton(ticket.title || "Untitled ticket", "text-button ticket-title", () => openTicket(ticket.id, title), `ticket:${ticket.id}`);
      title.dataset.ticketId = String(ticket.id);
      title.setAttribute("aria-controls", "ticket-detail");
      title.setAttribute("aria-expanded", String(String(ticket.id) === state.selectedId));
      titleCell.append(title, node("div", "ticket-submitter", ticket.submitter || "Email not provided"));
      const categoryCell = node("td");
      categoryCell.append(categoryBadge(ticket.category));
      const statusCell = node("td");
      statusCell.append(statusBadge(ticket.status));
      const confidenceCell = node("td");
      if (ticket.confidence !== null && ticket.confidence !== undefined && Number.isFinite(ticket.confidence)) {
        confidenceCell.append(node("div", "conf-label", percent(ticket.confidence)));
        const track = node("div", "conf-bar");
        track.setAttribute("aria-hidden", "true");
        const fill = node("div", "conf-fill");
        fill.style.width = percent(ticket.confidence);
        track.append(fill);
        confidenceCell.append(track);
      } else {
        confidenceCell.append(node("div", "conf-label", "Not available"));
      }
      row.append(titleCell, categoryCell, statusCell, confidenceCell);
      fragment.append(row);
    });
    list.replaceChildren(fragment);
    restoreFocus(list, focus);
  }

  function loadTickets() {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(state.ticketOffset) });
    if ($("ticket-status").value) params.set("status", $("ticket-status").value);
    return loadResource("tickets", params.toString(), () => request(`/tickets?${params}`), (payload) => {
      if (!Array.isArray(payload.tickets)) throw malformed();
      state.ticketPagination = pagination(payload.pagination, payload.tickets.length);
      state.tickets = payload.tickets;
      $("ticket-count").textContent = count(payload.pagination.total);
      renderTickets();
    }, "tickets-error", "Tickets", "tickets-region");
  }

  function contentBlock(label, value, ordered = false) {
    const section = node("section", "content-block");
    section.append(node("h4", "", label));
    if (Array.isArray(value)) {
      const list = node(ordered ? "ol" : "ul");
      value.forEach((item) => list.append(node("li", "", contentText(item))));
      section.append(value.length ? list : node("p", "secondary", "Not provided."));
    } else {
      section.append(node("p", "prose", contentText(value)));
    }
    return section;
  }

  function metadata(entries) {
    const list = node("dl", "detail-meta");
    entries.forEach(([label, value]) => {
      const item = node("div");
      item.append(node("dt", "", label), node("dd", "", value));
      list.append(item);
    });
    return list;
  }

  function replaceTicket(ticket) {
    const index = state.tickets.findIndex((item) => String(item.id) === String(ticket.id));
    if (index !== -1) state.tickets[index] = ticket;
    if (state.selectedId === String(ticket.id)) {
      state.selectedTicket = ticket;
      renderTicketDetail();
    }
    renderTickets();
  }

  function openTicket(id, opener = null) {
    if (!state.user) return;
    state.selectedId = String(id);
    state.selectedTicket = null;
    state.ticketOpener = opener || document.activeElement;
    state.signatures.delete("detail");
    message("ticket-detail-error");
    $("ticket-detail").hidden = false;
    $("ticket-detail-heading").textContent = "Loading ticket details…";
    $("ticket-detail-content").replaceChildren(node("p", "secondary", "Loading the current ticket from the server…"));
    $("ticket-detail-heading").focus({ preventScroll: true });
    $("ticket-detail").scrollIntoView({ block: "nearest", behavior: "auto" });
    renderTickets();
    void loadSelectedTicket();
  }

  function closeTicket() {
    const opener = state.ticketOpener;
    state.selectedId = null;
    state.selectedTicket = null;
    state.loads.delete("detail");
    $("ticket-detail").hidden = true;
    $("ticket-detail-content").replaceChildren();
    renderTickets();
    if (opener?.isConnected) opener.focus({ preventScroll: true });
    else $("tickets-heading").focus({ preventScroll: true });
  }

  function loadSelectedTicket() {
    const id = state.selectedId;
    if (!id || state.operations.has(`ticket:${id}`)) return Promise.resolve(true);
    return loadResource("detail", id, () => request(`/tickets/${encodeURIComponent(id)}`), (payload) => {
      if (!payload.ticket || String(payload.ticket.id) !== id) throw malformed();
      state.selectedTicket = payload.ticket;
      renderTicketDetail();
    }, "ticket-detail-error", "Ticket details", "ticket-detail-content");
  }

  function renderTicketDetail() {
    const ticket = state.selectedTicket;
    if (!ticket || String(ticket.id) !== state.selectedId) return;
    const busy = state.operations.has(`ticket:${ticket.id}`);
    if (!changed("detail", [ticket, state.user?.roles, canWrite(), state.aiEnabled, busy])) return;
    const container = $("ticket-detail-content");
    const focus = captureFocus(container);
    const fragment = document.createDocumentFragment();
    $("ticket-detail-heading").textContent = ticket.title || "Ticket details";
    fragment.append(statusBadge(ticket.status), node("p", "notice", ticketExplanations[ticket.status] || "The ticket has an unrecognized state. Refresh or contact an administrator."));
    fragment.append(metadata([
      ["Ticket reference", ticket.id],
      ["Submitter", ticket.submitter || "Not provided"],
      ["Category / pattern", `${humanize(ticket.category)} / ${humanize(ticket.subcategory)}`],
      ["Classification confidence", ticket.confidence !== null && ticket.confidence !== undefined ? percent(ticket.confidence) : "Not available"],
      ["Submitted", date(ticket.created_at)],
      ["Last updated", date(ticket.updated_at)],
    ]));
    fragment.append(contentBlock("Reported issue", ticket.description));
    if (ticket.recommendation) fragment.append(contentBlock("Recommended instructions — not a verified fix", ticket.recommendation));
    if (ticket.runbook_id) fragment.append(node("p", "secondary", `Advice source runbook: ${ticket.runbook_id}. Approval may have changed since this advice was recorded.`));
    if (ticket.assigned_to) fragment.append(contentBlock("Internal routing target (notification not confirmed)", ticket.assigned_to));
    if (ticket.reason) fragment.append(contentBlock("Triage reason", ticket.reason));
    if (ticket.error_code) fragment.append(node("p", "error-message", `Processing error: ${ticket.error_code}`));
    if (ticket.status === "resolved") {
      fragment.append(contentBlock("Operator-confirmed resolution", ticket.resolution));
      fragment.append(node("p", "secondary", `Confirmed on ${date(ticket.resolved_at)}.`));
    }
    if (ticket.status === "failed" && ticket.can_retry === true) {
      const retry = actionButton(busy ? "Retrying…" : "Retry ticket processing", "gen-btn", () => retryTicket(ticket), `retry-ticket:${ticket.id}`);
      retry.disabled = busy || !canWrite() || !state.aiEnabled;
      fragment.append(retry);
      if (!state.aiEnabled) fragment.append(node("p", "form-hint", "AI is disabled. An agent can still triage this ticket manually."));
    }
    if (isStaff() && ["pending", "processing", "recommended", "escalated", "failed"].includes(ticket.status)) {
      const form = node("form", "resolve-form");
      form.append(node("h4", "", "Confirm the actual resolution"));
      form.append(node("p", "form-hint", "Record what was actually done and verified. This explicit confirmation, not the recommendation, marks the ticket resolved."));
      const label = node("label", "form-label", "Resolution and verification");
      label.htmlFor = "actual-resolution";
      const input = node("textarea", "form-textarea");
      input.id = "actual-resolution";
      input.name = "resolution";
      input.required = true;
      input.maxLength = 12000;
      input.rows = 5;
      input.value = state.resolutionDrafts.get(String(ticket.id)) || "";
      input.dataset.focusKey = `resolution:${ticket.id}`;
      input.disabled = busy || !canWrite();
      input.addEventListener("input", () => state.resolutionDrafts.set(String(ticket.id), input.value));
      const submit = node("button", "submit-btn", busy ? "Saving confirmation…" : "Confirm resolved");
      submit.type = "submit";
      submit.disabled = busy || !canWrite();
      submit.dataset.focusKey = `resolve:${ticket.id}`;
      form.append(label, input, node("p", "form-hint", "Required · up to 12,000 characters."), submit);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (form.reportValidity()) void resolveTicket(ticket, input.value);
      });
      fragment.append(form);
    } else if (ticket.status !== "resolved") {
      fragment.append(node("p", "form-hint", isStaff() ? "Resolution confirmation is available after processing finishes." : "An agent or administrator can record a verified outcome and confirm resolution."));
    }
    container.replaceChildren(fragment);
    restoreFocus(container, focus);
  }

  async function mutate(key, action, onError, pending = () => {}) {
    if (!state.user || state.operations.has(key)) return;
    const epoch = state.epoch;
    const focusKey = document.activeElement?.dataset.focusKey;
    state.operations.add(key);
    message("app-error");
    updateControls();
    pending();
    try {
      await action();
    } catch (error) {
      if (epoch === state.epoch && !ignored(error)) {
        onError(error);
        message("app-error", `The action could not be completed.\n${errorText(error)}`);
      }
    } finally {
      if (epoch === state.epoch) {
        state.operations.delete(key);
        updateControls();
        pending();
        if (focusKey && document.activeElement === document.body && !restoreFocus(document, focusKey)) {
          if (key.startsWith("ticket:") && !$("ticket-detail").hidden) $("ticket-detail-heading").focus({ preventScroll: true });
          else if (key.startsWith("runbook:")) restoreFocus(document, key);
          else if (key.startsWith("generate:")) $("insights-heading").focus({ preventScroll: true });
        }
        scheduleRefresh();
      }
    }
  }

  function uuid() {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0"));
    return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
  }

  function submissionPayload() {
    const payload = {
      title: $("f-title").value.trim(),
      description: $("f-desc").value.trim(),
      submitter: $("f-email").value.trim(),
    };
    if ($("f-category").value) payload.category_hint = $("f-category").value;
    return payload;
  }

  function submissionMessage(ticket, duplicate) {
    const prefix = duplicate ? "Original submission found. " : "Ticket saved. ";
    if (ticket.status === "pending" || ticket.status === "processing") return `${prefix}${statuses[ticket.status]}; wait for processing before expecting a recommendation or routing outcome.`;
    if (ticket.status === "recommended") return `${prefix}Approved-runbook advice is available, but no fix has been verified.`;
    if (ticket.status === "resolved") return `${prefix}This ticket already has an operator-confirmed resolution.`;
    if (ticket.status === "failed") return `${prefix}Processing failed; open the ticket to review and retry.`;
    if (ticket.status === "escalated") return `${prefix}Escalated for human triage. A routing target is not confirmation of a delivered notification.`;
    return `${prefix}Server status: ${humanize(ticket.status)}. Open the ticket for details.`;
  }

  async function submitTicket(event) {
    event.preventDefault();
    if (!canWrite() || !canSubmit() || state.operations.has("submit") || !$("ticket-form").reportValidity()) return;
    const payload = submissionPayload();
    if (!payload.title || !payload.description || !payload.submitter) {
      message("submit-error", "Title, description, and submitter email must contain more than spaces.");
      return;
    }
    const signature = JSON.stringify(payload);
    if (!state.submission || state.submission.signature !== signature) state.submission = { signature, key: uuid() };
    const idempotencyKey = state.submission.key;
    message("submit-error");
    message("submit-result");
    await mutate("submit", async () => {
      const result = await request("/tickets", {
        method: "POST",
        body: payload,
        headers: { "Idempotency-Key": idempotencyKey },
      });
      if (!result.ticket?.id) throw malformed();
      if (result.job) trackJob(result.job, { type: "ticket", title: payload.title, resourceId: result.ticket.id });
      state.submission = null;
      $("f-title").value = "";
      $("f-desc").value = "";
      $("f-category").value = "";
      message("submission-retry-note");
      const outcome = `${submissionMessage(result.ticket, result.duplicate)} Reference: ${result.ticket.id}`;
      message("submit-result", outcome);
      announce(outcome);
      state.ticketOffset = 0;
      $("ticket-status").value = "";
      openTicket(result.ticket.id);
      replaceTicket(result.ticket);
      await Promise.all([loadTickets(), loadStats()]);
    }, (error) => {
      message("submit-error", errorText(error));
      message("submission-retry-note", "Keep the same fields and retry to check the original submission without creating a duplicate. Editing the fields starts a new submission.");
    });
  }

  async function retryTicket(ticket) {
    if (!canWrite() || !state.aiEnabled || ticket.can_retry !== true) return;
    message("ticket-detail-error");
    await mutate(`ticket:${ticket.id}`, async () => {
      const result = await request(`/tickets/${encodeURIComponent(ticket.id)}/retry`, { method: "POST" });
      if (!result.ticket?.id || !result.job?.id) throw malformed();
      replaceTicket(result.ticket);
      trackJob(result.job, { type: "ticket", title: ticket.title, resourceId: ticket.id });
      announce("Retry accepted. The ticket is queued for processing, not resolved.");
      await Promise.all([loadStats(), loadTickets()]);
    }, (error) => {
      if (state.selectedId === String(ticket.id)) message("ticket-detail-error", errorText(error));
    }, renderTicketDetail);
  }

  async function resolveTicket(ticket, value) {
    const resolution = value.trim();
    if (!canWrite() || !isStaff()) return;
    if (!resolution) {
      message("ticket-detail-error", "Describe the actual resolution and verification; spaces alone are not enough.");
      return;
    }
    message("ticket-detail-error");
    await mutate(`ticket:${ticket.id}`, async () => {
      const result = await request(`/tickets/${encodeURIComponent(ticket.id)}/resolve`, { method: "POST", body: { resolution } });
      if (!result.ticket?.id || result.ticket.status !== "resolved") throw malformed();
      state.resolutionDrafts.delete(String(ticket.id));
      replaceTicket(result.ticket);
      announce("The operator-confirmed resolution has been saved.");
      await Promise.all([loadStats(), loadTickets(), canSeeInsights() ? loadInsights() : Promise.resolve()]);
    }, (error) => {
      if (state.selectedId === String(ticket.id)) message("ticket-detail-error", errorText(error));
    }, renderTicketDetail);
  }

  function sourceLinks(ids) {
    const container = node("div", "source-links");
    if (!Array.isArray(ids) || !ids.length) {
      container.append(node("p", "secondary", "No source ticket references provided."));
      return container;
    }
    ids.forEach((id) => {
      const link = actionButton(`Ticket ${String(id).slice(0, 8)}`, "text-button", () => openTicket(id, link), `source:${id}`);
      link.title = `Open ticket ${id}`;
      container.append(link);
    });
    return container;
  }

  function renderRunbooks() {
    const busyKeys = Array.from(state.operations).filter((key) => key.startsWith("runbook:"));
    if (!changed("runbooks", [state.runbooks, state.user?.roles, canWrite(), state.aiEnabled, busyKeys, Array.from(state.runbookErrors), Array.from(state.runbookNotices)])) return;
    const container = $("runbook-list");
    const focus = captureFocus(container);
    const previous = new Map(Array.from(container.children).filter((item) => item.tagName === "DETAILS").map((item) => [
      item.dataset.runbookId,
      {
        open: item.open,
        reviewed: item.querySelector('input[type="checkbox"]')?.checked,
        content: item.reviewContent,
        revoking: item.querySelector(".action-confirmation")?.open,
      },
    ]));
    if (!state.runbooks.length) {
      container.replaceChildren(node("p", "no-data", canSeeInsights() ? "No runbooks on this page. Eligible recurring issues below can become drafts for review." : "No approved runbooks are available yet. You can still submit a ticket for human triage."));
      return;
    }
    const fragment = document.createDocumentFragment();
    state.runbooks.forEach((runbook, index) => {
      const id = String(runbook.id);
      const busy = state.operations.has(`runbook:${id}`);
      const details = node("details", "runbook-item");
      details.dataset.runbookId = id;
      details.open = previous.get(id)?.open || false;
      details.reviewContent = JSON.stringify([
        runbook.title, runbook.status, runbook.category, runbook.subcategory, runbook.problem,
        runbook.root_cause, runbook.steps, runbook.prevention, runbook.source_ticket_ids,
      ]);
      const summary = node("summary");
      summary.dataset.focusKey = `runbook:${id}`;
      summary.append(node("span", "runbook-name", runbook.title || "Untitled runbook"));
      const meta = node("span", "runbook-meta");
      meta.append(categoryBadge(runbook.category), statusBadge(runbook.status));
      summary.append(meta);
      const body = node("div", "runbook-body");
      if (runbook.status === "draft") body.append(node("p", "notice", "Draft only. Review the full content and its sources before approval. It cannot provide recommendations yet."));
      if (runbook.status === "approving") body.append(node("p", "notice", "Approval is being indexed in the background. This runbook is not ready for recommendations until its status is Approved."));
      if (runbook.status === "rejected") body.append(node("p", "notice", "Rejected or revoked. Future recommendations are blocked; historical advice remains recorded on tickets."));
      body.append(metadata([
        ["Runbook reference", id],
        ["Pattern", humanize(runbook.subcategory)],
        ["Created", date(runbook.created_at)],
      ]));
      body.append(contentBlock("Problem", runbook.problem));
      body.append(contentBlock("Cause described in this runbook", runbook.root_cause));
      body.append(contentBlock("Steps to review", runbook.steps, true));
      body.append(contentBlock("Prevention", runbook.prevention));
      const sources = node("section", "content-block");
      sources.append(node("h4", "", "Source tickets"), sourceLinks(runbook.source_ticket_ids));
      body.append(sources);
      if (runbook.approved_by || runbook.approved_at) {
        body.append(metadata([
          ["Most recent approver", runbook.approved_by || "Not provided"],
          ["Most recent approval", date(runbook.approved_at)],
        ]));
      }
      if (runbook.error_code) body.append(node("p", "error-message", `Runbook processing error: ${runbook.error_code}. Check Background Work for any job started in this tab.`));
      if (state.runbookErrors.has(id)) {
        const error = node("p", "error-message", state.runbookErrors.get(id));
        error.setAttribute("role", "alert");
        body.append(error);
      }
      if (state.runbookNotices.has(id)) body.append(node("p", "success-message", state.runbookNotices.get(id).text));
      if (hasRole("admin") && runbook.status === "draft") {
        const form = node("form", "review-form");
        const label = node("label", "checkbox-label");
        const checkbox = node("input");
        checkbox.type = "checkbox";
        checkbox.id = `review-runbook-${index}`;
        checkbox.required = true;
        checkbox.disabled = busy || !canWrite() || !state.aiEnabled;
        checkbox.checked = previous.get(id)?.content === details.reviewContent && previous.get(id)?.reviewed === true;
        checkbox.dataset.focusKey = `review:${id}`;
        label.htmlFor = checkbox.id;
        label.append(checkbox, node("span", "", "I reviewed the complete draft and its source tickets. Approval and indexing will enable future recommendations."));
        const approve = node("button", "gen-btn", busy ? "Requesting approval…" : "Approve and index");
        approve.type = "submit";
        approve.dataset.focusKey = `approve:${id}`;
        approve.disabled = !checkbox.checked || checkbox.disabled;
        checkbox.addEventListener("change", () => { approve.disabled = !checkbox.checked || checkbox.disabled; });
        form.append(label, approve);
        form.addEventListener("submit", (event) => {
          event.preventDefault();
          if (checkbox.checked && form.reportValidity()) void approveRunbook(runbook);
        });
        if (!state.aiEnabled) form.append(node("p", "form-hint", "AI indexing is disabled. Approval is unavailable until it is enabled."));
        body.append(form);
      }
      if (hasRole("admin") && ["draft", "approving", "approved"].includes(runbook.status)) {
        const confirmation = node("details", "action-confirmation");
        confirmation.open = previous.get(id)?.revoking || false;
        const title = runbook.status === "approved" ? "Revoke approval" : runbook.status === "approving" ? "Cancel approval" : "Reject draft";
        const trigger = node("summary", "", title);
        trigger.dataset.focusKey = `revoke-intent:${id}`;
        const confirm = actionButton(busy ? "Saving…" : `Confirm: ${title.toLowerCase()}`, "refresh-btn danger-btn", () => revokeRunbook(runbook), `revoke:${id}`);
        confirm.disabled = busy || !canWrite();
        confirmation.append(trigger, node("p", "form-hint", "This blocks future recommendations from this runbook. It does not remove historical recommendations or confirmed outcomes."), confirm);
        body.append(confirmation);
      }
      details.append(summary, body);
      fragment.append(details);
    });
    container.replaceChildren(fragment);
    restoreFocus(container, focus);
  }

  function loadRunbooks() {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(state.runbookOffset) });
    return loadResource("runbooks", params.toString(), () => request(`/runbooks?${params}`), (payload) => {
      if (!Array.isArray(payload.runbooks)) throw malformed();
      state.runbookPagination = pagination(payload.pagination, payload.runbooks.length);
      state.runbooks = payload.runbooks;
      state.runbooks.forEach((runbook) => {
        const id = String(runbook.id);
        if (state.runbookNotices.get(id)?.status !== runbook.status) state.runbookNotices.delete(id);
      });
      $("runbook-count").textContent = count(payload.pagination.total);
      renderRunbooks();
    }, "runbooks-error", "Runbooks", "runbook-list");
  }

  function replaceRunbook(runbook) {
    const index = state.runbooks.findIndex((item) => String(item.id) === String(runbook.id));
    if (index !== -1) state.runbooks[index] = runbook;
    renderRunbooks();
  }

  async function approveRunbook(runbook) {
    if (!hasRole("admin") || !canWrite() || !state.aiEnabled || runbook.status !== "draft") return;
    const id = String(runbook.id);
    state.runbookErrors.delete(id);
    state.runbookNotices.delete(id);
    await mutate(`runbook:${id}`, async () => {
      const result = await request(`/runbooks/${encodeURIComponent(id)}/approve`, { method: "POST" });
      if (!result.runbook?.id || !result.job?.id) throw malformed();
      trackJob(result.job, { type: "approval", title: runbook.title, resourceId: id });
      state.runbookNotices.set(id, { status: "approving", text: "Approval accepted for background indexing. Wait for Approved status before this runbook can support future advice." });
      replaceRunbook(result.runbook);
      announce("Approval queued for indexing. The runbook is not approved for use yet.");
      await Promise.all([loadRunbooks(), loadStats()]);
    }, (error) => state.runbookErrors.set(id, errorText(error)), renderRunbooks);
  }

  async function revokeRunbook(runbook) {
    if (!hasRole("admin") || !canWrite()) return;
    const id = String(runbook.id);
    state.runbookErrors.delete(id);
    state.runbookNotices.delete(id);
    await mutate(`runbook:${id}`, async () => {
      const result = await request(`/runbooks/${encodeURIComponent(id)}/revoke`, { method: "POST" });
      if (!result.runbook?.id) throw malformed();
      state.runbookNotices.set(id, { status: "rejected", text: "Runbook rejected or revoked. Future advice is blocked; historical recommendations are unchanged." });
      replaceRunbook(result.runbook);
      announce("Runbook rejected or revoked. Future recommendations are blocked.");
      await Promise.all([loadRunbooks(), loadStats(), loadInsights()]);
    }, (error) => state.runbookErrors.set(id, errorText(error)), renderRunbooks);
  }

  async function focusRunbook(id) {
    const epoch = state.epoch;
    state.runbookOffset = 0;
    await loadRunbooks();
    if (!state.user || epoch !== state.epoch) return;
    const details = Array.from($("runbook-list").children).find((item) => item.dataset.runbookId === String(id));
    if (details) {
      details.open = true;
      details.querySelector("summary").focus({ preventScroll: true });
      details.scrollIntoView({ block: "nearest", behavior: "auto" });
    } else {
      $("runbooks-heading").focus({ preventScroll: true });
      $("runbooks-panel").scrollIntoView({ block: "nearest", behavior: "auto" });
      announce(`Review the Knowledge Base pages for runbook ${id}. It may not be on the first page.`);
    }
  }

  function patternKey(issue) {
    return JSON.stringify([issue.category, issue.subcategory]);
  }

  function activePattern(key) {
    return Array.from(state.jobs.values()).some((entry) => entry.context.patternKey === key && activeJobStatuses.has(entry.job.status));
  }

  function failedPattern(key) {
    const latest = Array.from(state.jobs.values()).reverse().find((entry) => entry.context.patternKey === key);
    return latest?.job.status === "failed" ? latest : null;
  }

  function trend(issue) {
    if (issue.change_percent === null || issue.change_percent === undefined) return "No tickets in the preceding window";
    const value = numeric(issue.change_percent);
    if (value === 0) return "No change from the preceding window";
    return `${numberFormat.format(Math.abs(Math.round(value * 10) / 10))}% ${value > 0 ? "increase" : "decrease"} from the preceding window`;
  }

  function renderInsights() {
    if (!canSeeInsights() || !state.insights) return;
    const busyKeys = Array.from(state.operations).filter((key) => key.startsWith("generate:") || key.startsWith("job:"));
    const patternJobs = Array.from(state.jobs.values()).filter((entry) => entry.context.patternKey).map((entry) => [entry.context.patternKey, entry.job.id, entry.job.status]);
    if (!changed("insights", [state.insights, canWrite(), isStaff(), state.aiEnabled, busyKeys, patternJobs, Array.from(state.generationErrors)])) return;
    const container = $("insight-list");
    const focus = captureFocus(container);
    const expanded = new Set(Array.from(container.children).filter((item) => item.querySelector("details")?.open).map((item) => item.dataset.patternKey));
    const issues = state.insights.recurring_issues;
    if (!issues.length) {
      container.replaceChildren(node("p", "no-data", "No qualifying patterns in this window. A pattern needs at least 3 classified tickets with the same category and stable subcategory. Try a longer window."));
      return;
    }
    const fragment = document.createDocumentFragment();
    issues.forEach((issue) => {
      const key = patternKey(issue);
      const failed = failedPattern(key);
      const busy = state.operations.has(`generate:${key}`) || activePattern(key) || Boolean(failed && state.operations.has(`job:${failed.job.id}`));
      const item = node("article", "insight-item");
      item.dataset.patternKey = key;
      const heading = node("div", "insight-header");
      heading.append(node("h3", "", `${humanize(issue.category)} · ${humanize(issue.subcategory)}`));
      heading.append(node("span", issue.knowledge_gap ? "gap-label" : "secondary", issue.knowledge_gap ? "No approved runbook" : `${count(issue.approved_runbook_count)} approved runbooks`));
      const metrics = node("dl", "insight-metrics");
      [
        ["Tickets in window", count(issue.ticket_count)],
        ["Preceding window", count(issue.previous_count)],
        ["Unresolved", count(issue.unresolved_count)],
        ["Escalated / failed", `${count(issue.escalation_count)} (${percent(issue.escalation_rate)})`],
        ["Heuristic priority", count(issue.priority_score)],
      ].forEach(([label, value]) => {
        const metric = node("div");
        metric.append(node("dt", "", label), node("dd", "", value));
        metrics.append(metric);
      });
      item.append(heading, metrics, node("p", "secondary", trend(issue)));
      item.append(node("p", "secondary", `${count(issue.draft_runbook_count)} draft runbooks for this pattern.`));
      if (issue.recommended_action) item.append(node("p", "prose", issue.recommended_action));
      const sources = node("details", "sources-details");
      sources.open = expanded.has(key);
      const sourceSummary = node("summary", "", `Review source tickets (${count(Array.isArray(issue.source_ticket_ids) ? issue.source_ticket_ids.length : 0)})`);
      sourceSummary.dataset.focusKey = `insight-sources:${key}`;
      sources.append(sourceSummary, sourceLinks(issue.source_ticket_ids));
      item.append(sources);
      if (isStaff()) {
        const label = busy ? "Draft generation in progress…" : failed ? "Retry failed draft job" : "Generate draft for this pattern";
        const generate = actionButton(label, "gen-btn", () => {
          if (failed) void retryJob(failed);
          else void generateDraft(issue);
        }, `generate:${key}`);
        generate.disabled = busy || !canWrite() || !state.aiEnabled;
        item.append(generate);
        item.append(node("p", "form-hint", !state.aiEnabled ? "AI generation is disabled; manual triage remains available." : failed ? "The existing generation job failed. Retry that saved job rather than requesting another draft." : "Uses at least 3 matching tickets from the last 30 days. Generated content stays draft until administrator approval and indexing."));
      }
      if (state.generationErrors.has(key)) {
        const error = node("p", "error-message", state.generationErrors.get(key));
        error.setAttribute("role", "alert");
        item.append(error);
      }
      fragment.append(item);
    });
    container.replaceChildren(fragment);
    restoreFocus(container, focus);
  }

  function loadInsights() {
    if (!canSeeInsights()) return Promise.resolve(true);
    const days = ["7", "14", "30"].includes($("insight-days").value) ? $("insight-days").value : "14";
    const params = new URLSearchParams({ days, min_tickets: "3", limit: "20" });
    return loadResource("insights", params.toString(), () => request(`/insights?${params}`), (payload) => {
      if (!Array.isArray(payload.recurring_issues) || !payload.window) throw malformed();
      state.insights = payload;
      $("insight-window").textContent = `${payload.window.days}-day window: ${date(payload.window.start, true)}–${date(payload.window.end, true)}. Compared with the equal preceding window beginning ${date(payload.window.previous_start, true)}. Groups require at least ${count(payload.window.min_tickets)} matching tickets.`;
      renderInsights();
    }, "insights-error", "Recurring issues", "insight-list");
  }

  async function generateDraft(issue) {
    if (!canWrite() || !isStaff() || !state.aiEnabled) return;
    const key = patternKey(issue);
    if (activePattern(key)) return;
    state.generationErrors.delete(key);
    await mutate(`generate:${key}`, async () => {
      const result = await request("/runbooks/generate", { method: "POST", body: { category: issue.category, subcategory: issue.subcategory } });
      if (!result.job?.id) throw malformed();
      trackJob(result.job, { type: "generation", title: `${humanize(issue.category)} · ${humanize(issue.subcategory)}`, patternKey: key });
      if (result.job.status === "failed") {
        state.generationErrors.set(key, `The existing draft-generation job failed${result.job.error_code ? `: ${result.job.error_code}` : ""}. Retry the failed job to continue.`);
        announce("An existing failed generation job was returned. Nothing new was queued. Use Retry failed draft job or review Background Work.");
      } else if (result.job.status === "completed") {
        announce("An existing completed generation job was returned. Review its runbook in the Knowledge Base; generation alone does not approve it.");
        await Promise.all([loadRunbooks(), loadStats(), loadInsights()]);
      } else {
        announce(`Draft generation status: ${humanize(result.job.status)}. Watch Background Work for completion or failure. Generated runbooks require administrator approval.`);
      }
    }, (error) => {
      state.generationErrors.set(key, `${errorText(error)}\nIf the request could not be confirmed, check the Knowledge Base before starting another draft.`);
    }, renderInsights);
  }

  function jobLabel(entry) {
    const names = { ticket: "Ticket processing", generation: "Draft generation", approval: "Runbook approval" };
    return `${names[entry.context.type] || "Background work"} · ${entry.context.title || entry.job.id}`;
  }

  function canRetryJob(entry) {
    return isStaff() && entry.context.type !== "ticket" && (entry.context.type !== "approval" || hasRole("admin"));
  }

  function trackJob(job, context) {
    if (!job || !job.id) throw malformed();
    const id = String(job.id);
    const existing = state.jobs.get(id);
    if (existing) state.jobs.delete(id);
    state.jobs.set(id, { job, context: { ...existing?.context, ...context }, pollError: "" });
    if (state.jobs.size > 30) {
      const completed = Array.from(state.jobs).find(([key, entry]) => key !== id && entry.job.status === "completed");
      if (completed) state.jobs.delete(completed[0]);
    }
    renderJobs();
    renderInsights();
    scheduleRefresh();
  }

  function renderJobs() {
    const entries = Array.from(state.jobs.values()).reverse();
    const busyKeys = Array.from(state.operations).filter((key) => key.startsWith("job:"));
    if (!changed("jobs", [entries, busyKeys, canWrite(), state.aiEnabled, state.user?.roles])) return;
    const container = $("job-list");
    const focus = captureFocus(container);
    if (!entries.length) {
      container.replaceChildren(node("p", "no-data", "No jobs tracked in this tab yet."));
      return;
    }
    const fragment = document.createDocumentFragment();
    entries.forEach((entry) => {
      const job = entry.job;
      const item = node("article", "job-item");
      const heading = node("div", "job-header");
      heading.append(node("h3", "", jobLabel(entry)), statusBadge(job.status));
      item.append(heading);
      item.append(node("p", "secondary", `Attempt ${count(job.attempts)} · Updated ${date(job.updated_at)}`));
      item.append(node("p", "secondary", `Job reference: ${job.id}`));
      if (entry.context.type === "generation" && job.status === "completed") item.append(node("p", "secondary", "Generation completed. Generation alone does not approve a runbook; check the Knowledge Base for its current status."));
      if (entry.context.type === "approval" && job.status === "completed") item.append(node("p", "secondary", "Approval work completed. The runbook’s current status determines whether it can provide future recommendations."));
      if (entry.context.type === "ticket" && job.status === "completed") item.append(node("p", "secondary", "Processing completed. Open the ticket to read the actual outcome; job completion does not mean resolution."));
      if (job.status === "failed") {
        const error = node("p", "error-message", `Background work failed${job.error_code ? `: ${job.error_code}` : ""}. Review the resource before retrying. No successful outcome is implied.`);
        error.setAttribute("role", "alert");
        item.append(error);
      }
      if (entry.pollError) {
        const error = node("p", "error-message", `Could not refresh this job. The displayed status may be out of date.\n${entry.pollError}`);
        error.setAttribute("role", "alert");
        item.append(error);
      }
      const actions = node("div", "actions");
      const resourceId = job.resource_id || entry.context.resourceId;
      if (entry.context.type === "ticket" && resourceId) {
        const open = actionButton("Open ticket", "text-button", () => openTicket(resourceId, open), `job-resource:${job.id}`);
        actions.append(open);
      } else if (resourceId) {
        actions.append(actionButton("Review runbook", "text-button", () => { void focusRunbook(resourceId); }, `job-resource:${job.id}`));
      }
      if (job.status === "failed" && canRetryJob(entry)) {
        const busy = state.operations.has(`job:${job.id}`);
        const retry = actionButton(busy ? "Requesting retry…" : "Retry background job", "gen-btn", () => retryJob(entry), `job-retry:${job.id}`);
        retry.disabled = busy || !canWrite() || !state.aiEnabled;
        actions.append(retry);
        if (!state.aiEnabled) item.append(node("p", "form-hint", "AI is disabled. Retrying this job is unavailable."));
      } else if (job.status === "failed" && entry.context.type === "ticket" && (isStaff() || hasRole("requester"))) {
        item.append(node("p", "form-hint", "Open the failed ticket to review whether processing can be retried."));
      }
      if (actions.childElementCount) item.append(actions);
      fragment.append(item);
    });
    container.replaceChildren(fragment);
    restoreFocus(container, focus);
  }

  async function pollJobs(manual = false) {
    const epoch = state.epoch;
    const eligible = Array.from(state.jobs.entries()).filter(([, entry]) =>
      activeJobStatuses.has(entry.job.status) || (manual && entry.job.status === "failed"));
    const offset = state.jobPollOffset % Math.max(1, eligible.length);
    const entries = [...eligible.slice(offset), ...eligible.slice(0, offset)].slice(0, manual ? 10 : 3);
    state.jobPollOffset = (offset + entries.length) % Math.max(1, eligible.length);
    for (const [id, entry] of entries) {
      if (!state.user || epoch !== state.epoch || document.hidden) return;
      if (state.operations.has(`job:${id}`)) continue;
      const revision = state.readRevision;
      try {
        const result = await request(`/jobs/${encodeURIComponent(id)}`);
        if (!result.job || String(result.job.id) !== id) throw malformed();
        if (state.jobs.get(id) !== entry || epoch !== state.epoch || revision !== state.readRevision) continue;
        const previous = entry.job.status;
        entry.job = result.job;
        entry.pollError = "";
        if (entry.context.patternKey && result.job.status !== "failed") state.generationErrors.delete(entry.context.patternKey);
        if (previous !== result.job.status && ["failed", "completed"].includes(result.job.status)) {
          announce(`${jobLabel(entry)}: ${statuses[result.job.status]}. Review Background Work and the resource for the actual outcome.`);
        }
      } catch (error) {
        if (epoch !== state.epoch || ignored(error)) return;
        if (state.jobs.get(id) === entry && revision === state.readRevision) entry.pollError = errorText(error);
        if (error.status === 429) break;
      }
    }
    if (epoch === state.epoch) {
      renderJobs();
      renderInsights();
    }
  }

  async function retryJob(entry) {
    if (!canRetryJob(entry) || !canWrite() || !state.aiEnabled) return;
    const id = String(entry.job.id);
    entry.pollError = "";
    if (entry.context.patternKey) state.generationErrors.delete(entry.context.patternKey);
    await mutate(`job:${id}`, async () => {
      const result = await request(`/jobs/${encodeURIComponent(id)}/retry`, { method: "POST" });
      if (!result.job?.id) throw malformed();
      trackJob(result.job, entry.context);
      announce("Background retry accepted. Wait for the server’s completed or failed state.");
      await Promise.all([loadRunbooks(), loadTickets(), loadSelectedTicket()]);
    }, (error) => {
      entry.pollError = errorText(error);
      if (entry.context.patternKey) state.generationErrors.set(entry.context.patternKey, errorText(error));
    }, () => {
      renderJobs();
      renderInsights();
    });
  }

  function scheduleRefresh() {
    window.clearTimeout(state.refreshTimer);
    if (!state.user || document.hidden || state.signingOut) return;
    const active = Array.from(state.jobs.values()).some((entry) => activeJobStatuses.has(entry.job.status)) ||
      state.tickets.some((ticket) => ["pending", "processing"].includes(ticket.status)) ||
      ["pending", "processing"].includes(state.selectedTicket?.status);
    const delay = Math.max(active ? 7500 : 15000, state.retryAt - Date.now());
    state.refreshTimer = window.setTimeout(() => { void refreshDashboard(); }, delay);
  }

  function refreshDashboard(manual = false) {
    if (state.signingOut || (document.hidden && !manual)) return Promise.resolve();
    if (state.refresh) return state.refresh.promise;
    window.clearTimeout(state.refreshTimer);
    const operation = {};
    state.refresh = operation;
    updateControls();
    if (manual) announce(state.user ? "Refreshing your workspace…" : "Checking your company session…");
    operation.promise = (async () => {
      try {
        const identity = await request("/me", { authCheck: true });
        if (!applyIdentity(identity)) return;
        const epoch = state.epoch;
        if (document.hidden && !manual) return;
        message("app-error");
        await pollJobs(manual);
        if (!state.user || epoch !== state.epoch || (document.hidden && !manual)) return;
        const results = await Promise.all([
          loadStats(), loadTickets(), loadRunbooks(), loadInsights(), loadSelectedTicket(),
        ]);
        if (!state.user || epoch !== state.epoch) return;
        const incomplete = results.some((result) => result === false) || Array.from(state.jobs.values()).some((entry) => entry.pollError);
        $("connection-state").textContent = Date.now() < state.retryAt ? "Rate limited" : incomplete ? "Refresh incomplete" : "Connected";
        $("connection-state").classList.toggle("is-paused", incomplete || Date.now() < state.retryAt);
        renderRunbooks();
        renderInsights();
        renderTicketDetail();
        renderJobs();
        if (manual) announce(incomplete ? "Some data could not be refreshed. Review the panel errors and retry." : `Workspace updated at ${dateFormat.format(new Date())}.`);
      } catch (error) {
        if (!ignored(error)) {
          message("app-error", `Could not verify your company session.\n${errorText(error)}`);
          if (!state.user) {
            $("auth-message").textContent = "We couldn’t check your session. Retry below, or sign in with your company account.";
            $("auth-retry-btn").hidden = false;
          } else {
            $("connection-state").textContent = "Refresh unavailable";
            $("connection-state").classList.add("is-paused");
          }
          announce("Session check failed. Your existing data may be out of date.");
        }
      } finally {
        if (state.refresh === operation) {
          state.refresh = null;
          updateControls();
          scheduleRefresh();
        }
      }
    })();
    return operation.promise;
  }

  let sessionChannel = null;
  if ("BroadcastChannel" in window) {
    try {
      sessionChannel = new BroadcastChannel("neuraldesk-session");
      sessionChannel.addEventListener("message", (event) => {
        if (event.data?.type === "signed-out") endSession("You signed out in another tab. Sign in again to continue.");
      });
    } catch {
      sessionChannel = null;
    }
  }

  async function logout() {
    if (!state.user || state.signingOut) return;
    state.signingOut = true;
    window.clearTimeout(state.refreshTimer);
    updateControls();
    try {
      await request("/auth/logout", { method: "POST" });
      sessionChannel?.postMessage({ type: "signed-out" });
      endSession("You are signed out. Company data has been cleared from this page.");
      window.location.replace("/");
    } catch (error) {
      if (!ignored(error)) message("app-error", `Sign-out could not be confirmed. Try again.\n${errorText(error)}`);
    } finally {
      state.signingOut = false;
      updateControls();
      scheduleRefresh();
    }
  }

  $("ticket-form").addEventListener("submit", submitTicket);
  $("ticket-form").addEventListener("input", () => {
    if (state.submission && state.submission.signature !== JSON.stringify(submissionPayload())) {
      state.submission = null;
      message("submission-retry-note");
      updateControls();
    }
  });
  $("refresh-btn").addEventListener("click", () => { void refreshDashboard(true); });
  $("auth-retry-btn").addEventListener("click", () => { void refreshDashboard(true); });
  $("logout-btn").addEventListener("click", () => { void logout(); });
  $("close-ticket").addEventListener("click", closeTicket);
  $("reload-ticket").addEventListener("click", () => { void loadSelectedTicket(); });
  $("ticket-status").addEventListener("change", () => {
    state.ticketOffset = 0;
    void loadTickets();
  });
  ["tickets", "runbooks"].forEach((name) => {
    ["prev", "next"].forEach((direction) => {
      $(`${name}-${direction}`).addEventListener("click", () => {
        const page = name === "tickets" ? state.ticketPagination : state.runbookPagination;
        if (!page || state.loads.has(name)) return;
        const offset = Math.max(0, page.offset + (direction === "next" ? page.limit : -page.limit));
        if (name === "tickets") {
          state.ticketOffset = offset;
          void loadTickets();
        } else {
          state.runbookOffset = offset;
          void loadRunbooks();
        }
      });
    });
  });
  $("insight-days").addEventListener("change", () => { void loadInsights(); });
  document.addEventListener("visibilitychange", () => {
    window.clearTimeout(state.refreshTimer);
    if (document.hidden) {
      $("connection-state").textContent = "Refresh paused";
      $("connection-state").classList.add("is-paused");
    } else if (state.user) {
      void refreshDashboard();
    }
  });
  window.addEventListener("pagehide", () => {
    endSession("Sign in with your company account to continue.");
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      if (state.refresh) void state.refresh.promise.finally(() => refreshDashboard(true));
      else void refreshDashboard(true);
    }
  });
  updateControls();
  void refreshDashboard(true);
})();
