(() => {
  "use strict";

  const el = (id) => document.getElementById(id);
  const mobileNavigation = window.matchMedia("(max-width: 880px)");
  const state = {
    snapshot: null,
    activityMetric: "forms",
    activityBucket: "month",
    requestId: 0,
    views: new Map(),
    sorts: Object.create(null),
    toastTimer: null,
    desktopSidebarOpen: true,
    activityChart: null,
    period: "all",
    customRange: null,
  };
  const number = (value) => value == null ? "—" : Number(value).toLocaleString();

  function text(id, value) {
    if (el(id)) el(id).textContent = value ?? "—";
  }

  function show(element, visible) {
    if (element) element.hidden = !visible;
  }

  function node(tag, className = "", value) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value !== undefined) item.textContent = value;
    return item;
  }

  function addCell(row, value, className = "") {
    row.appendChild(node("td", className, value));
  }

  function formatDate(value, includeTime = false) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    const options = { year: "numeric", month: "short", day: "numeric" };
    if (includeTime) Object.assign(options, { hour: "numeric", minute: "2-digit" });
    return date.toLocaleString(undefined, options);
  }

  function kpi(name, value) {
    document.querySelector(`[data-kpi="${name}"] .kpi-value`).textContent = value;
  }

  function populateSelect(select, rows, defaultLabel, current, valueKey, labelKey) {
    select.replaceChildren(new Option(defaultLabel, "all"));
    rows
      .filter((row) => String(row[valueKey]) !== "all")
      .forEach((row) => select.appendChild(new Option(row[labelKey], row[valueKey])));
    select.value = current === "all" || rows.some((row) => String(row[valueKey]) === current)
      ? current
      : "all";
  }

  async function requestJson(path, options = {}) {
    const response = await fetch(`./${path}`, {
      cache: "no-store",
      credentials: "same-origin",
      ...options,
      headers: { Accept: "application/json", ...(options.headers || {}) },
    });
    if (response.status === 204) return null;
    let payload;
    try {
      payload = await response.json();
    } catch {
      throw new Error("The dashboard server returned an unexpected response.");
    }
    if (!response.ok) throw new Error(payload.message || "The request failed.");
    return payload;
  }

  function showToast(message) {
    text("toastText", message);
    el("toast").classList.add("show");
    window.clearTimeout(state.toastTimer);
    state.toastTimer = window.setTimeout(() => el("toast").classList.remove("show"), 2800);
  }

  function totalRow(columnCount, label, totals) {
    const row = node("tr", "total-row");
    row.dataset.total = "true";
    for (let index = 0; index < columnCount; index += 1) {
      const numeric = Object.hasOwn(totals, index);
      addCell(row, index === 0 ? label : numeric ? number(totals[index]) : "", numeric ? "numeric" : "");
    }
    return row;
  }

  function sortedRows(report, sourceRows) {
    const sort = state.sorts[report];
    if (!sort) return sourceRows;
    const direction = sort.direction === "ascending" ? 1 : -1;
    return [...sourceRows].sort((left, right) => {
      const leftValue = left[sort.key];
      const rightValue = right[sort.key];
      const leftMissing = leftValue == null || leftValue === "";
      const rightMissing = rightValue == null || rightValue === "";
      if (leftMissing || rightMissing) {
        if (leftMissing && rightMissing) return 0;
        return leftMissing ? 1 : -1;
      }

      let comparison;
      if (sort.type === "number") {
        comparison = Number(leftValue) - Number(rightValue);
      } else if (sort.type === "date") {
        comparison = new Date(leftValue).getTime() - new Date(rightValue).getTime();
      } else {
        comparison = String(leftValue).localeCompare(String(rightValue), undefined, {
          numeric: true,
          sensitivity: "base",
        });
      }
      return comparison * direction;
    });
  }

  function updateSortIndicators(report) {
    const activeSort = state.sorts[report];
    document.querySelectorAll(`.sort-button[data-report="${report}"]`).forEach((button) => {
      const active = activeSort && button.dataset.key === activeSort.key;
      button.closest("th").setAttribute("aria-sort", active ? activeSort.direction : "none");
    });
  }

  function updateMetricButtons() {
    document.querySelectorAll(".kpi-table-button").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.chartMetric === state.activityMetric));
      button.disabled = !state.snapshot;
    });
  }

  function selectActivityMetric(button) {
    state.activityMetric = button.dataset.chartMetric;
    renderActivityChart(state.snapshot?.activitySeries || null);
    updateMetricButtons();
  }

  function renderProjects(sourceRows) {
    const rows = sortedRows("projects", sourceRows);
    const body = el("projectBody");
    body.replaceChildren();
    rows.forEach((item) => {
      const row = document.createElement("tr");
      const projectCell = node("td", "primary-cell");
      projectCell.append(node("strong", "", item.project), node("span", "", "A+ project"));
      row.appendChild(projectCell);
      addCell(row, number(item.sites), "numeric");
      addCell(row, number(item.records), "numeric");
      addCell(row, number(item.formsCompleted), "numeric");
      addCell(row, number(item.people), "numeric");
      addCell(row, formatDate(item.mostRecentCompletion, true));
      body.appendChild(row);
    });
    if (rows.length) body.appendChild(totalRow(6, "Total", {
      2: rows.reduce((sum, row) => sum + row.records, 0),
      3: rows.reduce((sum, row) => sum + row.formsCompleted, 0),
    }));
    show(el("projectEmpty"), rows.length === 0);
    updateSortIndicators("projects");
  }

  function renderSites(sourceRows) {
    const rows = sortedRows("sites", sourceRows);
    const body = el("siteBody");
    body.replaceChildren();
    rows.forEach((item) => {
      const row = document.createElement("tr");
      row.dataset.search = `${item.site} ${item.siteId} ${item.project}`.toLowerCase();
      row.dataset.forms = String(item.formsCompleted);
      const siteCell = node("td", "primary-cell");
      siteCell.append(node("strong", "", item.site), node("span", "", `Site ${item.siteId}`));
      row.appendChild(siteCell);
      addCell(row, item.project);
      addCell(row, number(item.records), "numeric");
      addCell(row, number(item.formsCompleted), "numeric");
      addCell(row, number(item.people), "numeric");
      addCell(row, formatDate(item.mostRecentCompletion, true));
      body.appendChild(row);
    });
    if (rows.length) body.appendChild(totalRow(6, "Total", {
      2: rows.reduce((sum, row) => sum + row.records, 0),
      3: rows.reduce((sum, row) => sum + row.formsCompleted, 0),
    }));
    filterTable("siteBody", "siteSearch", "siteMeta", "siteEmpty");
    updateSortIndicators("sites");
  }

  function renderPeople(sourceRows) {
    const rows = sortedRows("people", sourceRows);
    const body = el("personBody");
    body.replaceChildren();
    rows.forEach((item) => {
      const row = document.createElement("tr");
      row.dataset.search = `${item.person} ${item.site} ${item.siteId} ${item.project}`.toLowerCase();
      row.dataset.forms = String(item.formsCompleted);
      const personCell = node("td", "primary-cell");
      personCell.append(node("strong", "", item.person));
      row.appendChild(personCell);
      addCell(row, item.site);
      addCell(row, item.project);
      addCell(row, number(item.formsCompleted), "numeric");
      addCell(row, formatDate(item.mostRecentCompletion, true));
      body.appendChild(row);
    });
    if (rows.length) body.appendChild(totalRow(5, "Total", {
      3: rows.reduce((sum, row) => sum + row.formsCompleted, 0),
    }));
    filterTable("personBody", "personSearch", "personMeta", "personEmpty");
    updateSortIndicators("people");
  }

  const activityColors = {
    brisk: "#001b61",
    prism: "#2762b4",
    train: "#e0001b",
  };
  const fallbackActivityColors = ["#167047", "#8a5aa7", "#b36a18"];

  function formatTrendBucket(value, bucket) {
    const date = new Date(bucket === "hour" ? value : value + "T00:00:00");
    if (Number.isNaN(date.getTime())) return value;
    if (bucket === "hour") {
      return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    }
    if (bucket === "month") {
      return date.toLocaleDateString(undefined, { month: "short", year: "numeric" });
    }
    const label = date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    return bucket === "week" ? "Week of " + label : label;
  }

  function destroyActivityChart() {
    if (!state.activityChart) return;
    state.activityChart.destroy();
    state.activityChart = null;
  }

  function renderActivityChart(activity) {
    destroyActivityChart();

    const container = el("activityChartContainer");
    const empty = el("activityEmpty");
    const canvas = el("activityChart");
    const recordsView = state.activityMetric === "records";
    const title = recordsView ? "Records created over time" : "Completed forms over time";
    text("activityTrendTitle", title);
    canvas.setAttribute("aria-label", title);
    canvas.textContent = title;
    if (recordsView) activity = state.snapshot?.recordActivitySeries || null;
    activity = state.snapshot?.activityViews?.[state.activityMetric]?.[state.activityBucket] || activity;
    document.querySelectorAll("[data-chart-bucket]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.chartBucket === (activity?.bucket || state.activityBucket)));
    });
    if (!activity) {
      show(container, false);
      show(empty, false);
      text("activityCaption", recordsView ? "Loading record creation activity…" : "Loading completion activity…");
      return;
    }

    const series = activity.series || [];
    const chartedTotal = Number(activity.chartedTotal || 0);
    const unavailableTotal = Number(activity.unavailableTotal || 0);
    const unit = activity.bucket === "hour" ? "hourly" : activity.bucket === "day"
      ? "daily"
      : activity.bucket === "week"
        ? "weekly"
        : "monthly";
    const countLabel = number(chartedTotal) + (recordsView ? " record" : " completed form")
      + (chartedTotal === 1 ? "" : "s");
    let caption = countLabel + (recordsView ? " created" : "") + " shown as " + unit + " stacked bars.";
    if (unavailableTotal) {
      caption += " " + number(unavailableTotal) + (recordsView ? " current record" : " current completed form")
        + (unavailableTotal === 1 ? " has" : "s have")
        + (recordsView ? " no available creation date" : " no available completion date")
        + " and cannot be charted.";
    }
    text("activityCaption", caption);
    canvas.setAttribute(
      "aria-label",
      title + ". " + caption
    );

    if (!chartedTotal || !series.length) {
      show(container, false);
      empty.textContent = recordsView
        ? "No dated record-creation activity is available for these filters."
        : unavailableTotal
          ? "No dated completion activity is available for these filters."
          : "No completion activity is available for these filters.";
      show(empty, true);
      return;
    }
    if (typeof window.Chart !== "function") {
      show(container, false);
      empty.textContent = "The chart library could not be loaded.";
      show(empty, true);
      return;
    }

    show(container, true);
    show(empty, false);
    const buckets = activity.buckets || [];
    const projectColor = (item, index) => activityColors[item.id]
      || fallbackActivityColors[index % fallbackActivityColors.length];
    state.activityChart = new window.Chart(canvas, {
      type: "bar",
      data: {
        labels: buckets.map((item) => formatTrendBucket(item.start, activity.bucket)),
        datasets: series.map((item, index) => {
          const color = projectColor(item, index);
          return {
            label: item.label,
            data: item.values,
            backgroundColor: color,
            borderColor: color,
            borderRadius: 4,
            borderSkipped: false,
            maxBarThickness: 42,
          };
        }),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 180 },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            display: true,
            position: "bottom",
            labels: { boxWidth: 11, boxHeight: 11, padding: 16, usePointStyle: true, pointStyle: "rectRounded" },
          },
          tooltip: {
            callbacks: {
              title: (items) => items.length
                ? formatTrendBucket(buckets[items[0].dataIndex].start, activity.bucket)
                : "",
              label: (item) => item.dataset.label + ": " + number(item.parsed.y)
                + (recordsView ? " created" : " completed"),
              footer: (items) => "Total: " + number(
                items.reduce((sum, item) => sum + Number(item.parsed.y || 0), 0)
              ),
            },
          },
        },
        scales: {
          x: {
            stacked: true,
            grid: { display: false },
            ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 12 },
          },
          y: {
            stacked: true,
            beginAtZero: true,
            ticks: { precision: 0 },
            title: { display: true, text: recordsView ? "Records created" : "Completed forms" },
            grid: { color: "#edf1f5" },
          },
        },
      },
    });
  }

  function applySort(button) {
    const { report, key, type } = button.dataset;
    const current = state.sorts[report];
    const firstDirection = type === "date" ? "descending" : "ascending";
    state.sorts[report] = {
      key,
      type,
      direction: current?.key === key && current.direction === "ascending"
        ? "descending"
        : current?.key === key && current.direction === "descending"
          ? "ascending"
          : firstDirection,
    };
    const rowsByReport = {
      projects: "projectRows",
      sites: "siteRows",
      people: "personRows",
    };
    const sourceRows = state.snapshot?.[rowsByReport[report]] || [];
    if (report === "projects") renderProjects(sourceRows);
    if (report === "sites") renderSites(sourceRows);
    if (report === "people") renderPeople(sourceRows);
  }

  function filterTable(bodyId, searchId, metaId, emptyId) {
    const query = el(searchId).value.trim().toLowerCase();
    const rows = Array.from(el(bodyId).querySelectorAll("tr:not([data-total])"));
    let count = 0;
    rows.forEach((row) => {
      const matches = !query || row.dataset.search.includes(query);
      row.hidden = !matches;
      count += Number(matches);
    });
    text(metaId, `${count} row${count === 1 ? "" : "s"} shown`);
    show(el(emptyId), count === 0);
  }

  function render(snapshot) {
    state.snapshot = snapshot;
    text("pageTitle", snapshot.title);
    text("pageSubtitle", snapshot.subtitle);
    populateSelect(el("projectFilter"), snapshot.projects, "All projects", snapshot.selectedProject, "id", "label");
    populateSelect(el("siteFilter"), snapshot.availableSites, "All sites", snapshot.selectedSite, "id", "label");
    populateSelect(el("periodFilter"), snapshot.periodOptions, "All completed forms", snapshot.period, "value", "label");

    state.period = snapshot.period;
    if (snapshot.period === "custom") {
      state.customRange = { startDate: snapshot.startDate, endDate: snapshot.endDate };
    }
    show(el("editDateRange"), snapshot.period === "custom");

    el("freshness").dataset.state = snapshot.stale || snapshot.state !== "ready" ? "warning" : "ready";
    el("freshness").querySelector("span:last-child").firstChild.textContent = snapshot.stale ? "Cached fallback · " : "Updated · ";
    text("lastUpdated", formatDate(snapshot.updatedAt, true));

    const metrics = snapshot.metrics;
    kpi("records", number(metrics.recordsEntered));
    kpi("forms", number(metrics.completedForms));
    kpi("sites", number(metrics.participatingSites));
    kpi("people", number(metrics.dataEnterers));
    const periodLabel = snapshot.periodOptions.find((item) => item.value === snapshot.period)?.label || "Current filters";
    text("formsKpiFooter", snapshot.period === "custom"
      ? `${formatDate(snapshot.startDate + "T00:00:00")} – ${formatDate(snapshot.endDate + "T00:00:00")}`
      : periodLabel);

    renderActivityChart(snapshot.activitySeries);
    updateMetricButtons();
    renderProjects(snapshot.projectRows);
    renderSites(snapshot.siteRows);
    renderPeople(snapshot.personRows);

    const warnings = snapshot.warnings || [];
    el("dashboardNotice").textContent = warnings.join(" ");
    show(el("dashboardNotice"), warnings.length > 0);
    window.requestAnimationFrame(updateActiveSection);
  }

  function clearDashboard() {
    state.snapshot = null;
    kpi("records", "—");
    kpi("forms", "—");
    kpi("sites", "—");
    kpi("people", "—");
    text("formsKpiFooter", "Across current filters");
    renderActivityChart(null);
    updateMetricButtons();
    renderProjects([]);
    renderSites([]);
    renderPeople([]);
    el("dashboardNotice").textContent = "";
    show(el("dashboardNotice"), false);
  }

  function syncCustomDateRange() {
    el("startDateFilter").max = el("endDateFilter").value || "9999-12-31";
    el("endDateFilter").min = el("startDateFilter").value || "0001-01-01";
  }

  function openCustomDateRange() {
    // Edit a draft so Cancel and Escape preserve the currently applied report.
    el("startDateFilter").value = state.customRange?.startDate || "";
    el("endDateFilter").value = state.customRange?.endDate || "";
    syncCustomDateRange();
    el("dateRangeDialog").showModal();
  }

  function filtersFromControls() {
    return {
      project: el("projectFilter").value || "all",
      site: el("siteFilter").value || "all",
      period: el("periodFilter").value || "all",
      ...(el("periodFilter").value === "custom" ? state.customRange : {}),
    };
  }

  function filterKey(filters) {
    return `${filters.project}|${filters.site}|${filters.period}|${filters.startDate || ""}|${filters.endDate || ""}`;
  }

  function dashboardUrl(path, filters) {
    const query = new URLSearchParams(filters);
    return `${path}?${query.toString()}`;
  }

  function setRequestInProgress(loading, showingData = false) {
    const button = el("refreshButton");
    button.disabled = loading;
    button.classList.toggle("is-refreshing", loading);
    button.setAttribute("aria-busy", String(loading));
    button.querySelector("span").textContent = loading ? "Refreshing…" : "Refresh data";
    if (!loading) return;

    el("freshness").dataset.state = "refreshing";
    el("freshness").querySelector("span:last-child").firstChild.textContent = showingData
      ? "Refreshing · showing cached data from "
      : "Loading · ";
    text("lastUpdated", showingData && state.snapshot
      ? formatDate(state.snapshot.updatedAt, true)
      : "waiting for data");
  }

  function showRequestError(error) {
    const showingData = Boolean(state.snapshot);
    el("dashboardNotice").textContent = showingData
      ? `${error.message} The cached data shown below has been retained.`
      : error.message;
    show(el("dashboardNotice"), true);
    el("freshness").dataset.state = showingData ? "warning" : "error";
    el("freshness").querySelector("span:last-child").firstChild.textContent = showingData
      ? "Refresh failed · showing cached data from "
      : "Unable to load · ";
    text("lastUpdated", showingData
      ? formatDate(state.snapshot.updatedAt, true)
      : "no data available");
  }

  async function loadDashboard(force = false) {
    if (el("periodFilter").value === "custom" && !state.customRange) {
      openCustomDateRange();
      return;
    }
    const requestId = ++state.requestId;
    const filters = filtersFromControls();
    const key = filterKey(filters);
    const memorySnapshot = state.views.get(key) || null;

    if (memorySnapshot) render(memorySnapshot);
    else clearDashboard();
    setRequestInProgress(true, Boolean(memorySnapshot));

    const cachedRequest = memorySnapshot
      ? Promise.resolve({ snapshot: null })
      : requestJson(dashboardUrl("api/dashboard/cached", filters)).then(
        (snapshot) => ({ snapshot }),
        (error) => ({ error }),
      );
    const freshRequest = (force
      ? requestJson("api/dashboard/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(filters),
      })
      : requestJson(dashboardUrl("api/dashboard", filters))).then(
      (snapshot) => ({ snapshot }),
      (error) => ({ error }),
    );

    const cachedResult = await cachedRequest;
    if (requestId !== state.requestId) return;
    if (cachedResult.snapshot) {
      state.views.set(key, cachedResult.snapshot);
      render(cachedResult.snapshot);
      setRequestInProgress(true, true);
    }

    const freshResult = await freshRequest;
    if (requestId !== state.requestId) return;
    if (freshResult.error) {
      showRequestError(freshResult.error);
    } else {
      state.views.set(key, freshResult.snapshot);
      render(freshResult.snapshot);
      if (force) showToast(freshResult.snapshot.stale
        ? "Refresh failed; cached data retained."
        : "Dashboard data refreshed.");
    }
    setRequestInProgress(false);
  }

  const exportDefinitions = {
    projects: { rows: "projectRows", headers: ["Project", "Sites", "Records Entered", "Forms completed", "Data enterers", "Most recent completion"], values: (row) => [row.project, row.sites, row.records, row.formsCompleted, row.people, row.mostRecentCompletion || ""] },
    sites: { rows: "siteRows", headers: ["Site", "Site ID", "Project", "Records Entered", "Forms completed", "Data enterers", "Most recent completion"], values: (row) => [row.site, row.siteId, row.project, row.records ?? "Unavailable", row.formsCompleted, row.people, row.mostRecentCompletion || ""] },
    people: { rows: "personRows", headers: ["Person", "Site", "Site ID", "Project", "Forms completed", "Most recent completion"], values: (row) => [row.person, row.site, row.siteId, row.project, row.formsCompleted, row.mostRecentCompletion || ""] },
  };

  function exportCsv(report) {
    if (!state.snapshot || !exportDefinitions[report]) return;
    const definition = exportDefinitions[report];
    const quote = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`;
    const rows = sortedRows(report, state.snapshot[definition.rows]).map(definition.values);
    const csv = [definition.headers, ...rows].map((row) => row.map(quote).join(",")).join("\r\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `aplus-${report}-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  }

  function sidebarIsOpen() {
    return mobileNavigation.matches
      ? el("sidebar").classList.contains("open")
      : !el("dashboardLayout").classList.contains("sidebar-collapsed");
  }

  function setSidebar(open) {
    const sidebar = el("sidebar");
    const layout = el("dashboardLayout");
    if (mobileNavigation.matches) {
      layout.classList.remove("sidebar-collapsed");
      sidebar.classList.toggle("open", open);
      el("sidebarBackdrop").classList.toggle("open", open);
      document.body.classList.toggle("navigation-open", open);
    } else {
      state.desktopSidebarOpen = open;
      sidebar.classList.remove("open");
      el("sidebarBackdrop").classList.remove("open");
      document.body.classList.remove("navigation-open");
      layout.classList.toggle("sidebar-collapsed", !open);
    }
    sidebar.toggleAttribute("inert", !open);
    sidebar.setAttribute("aria-hidden", String(!open));
    el("menuButton").setAttribute("aria-expanded", String(open));
    el("menuButton").setAttribute("aria-label", open ? "Collapse navigation" : "Open navigation");
  }

  function syncSidebarToViewport() {
    setSidebar(mobileNavigation.matches ? false : state.desktopSidebarOpen);
  }

  function setActiveSection(sectionId) {
    document.querySelectorAll(".nav a").forEach((link) => {
      const active = link.getAttribute("href") === `#${sectionId}`;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "location");
      else link.removeAttribute("aria-current");
    });
  }

  function updateActiveSection() {
    const sections = Array.from(document.querySelectorAll("#overview, #trends, #projects, #sites, #people"));
    const marker = window.scrollY + el("topbar").offsetHeight + 40;
    let current = sections[0];
    sections.forEach((section) => {
      const sectionTop = section.getBoundingClientRect().top + window.scrollY;
      if (sectionTop <= marker) current = section;
    });
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) {
      current = sections.at(-1);
    }
    setActiveSection(current.id);
  }

  function installScrollSpy() {
    let scheduled = false;
    const scheduleUpdate = () => {
      if (scheduled) return;
      scheduled = true;
      window.requestAnimationFrame(() => {
        updateActiveSection();
        scheduled = false;
      });
    };
    window.addEventListener("scroll", scheduleUpdate, { passive: true });
    window.addEventListener("resize", scheduleUpdate);
    updateActiveSection();
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-chart-bucket]").forEach((button) => button.addEventListener("click", () => {
      state.activityBucket = button.dataset.chartBucket;
      renderActivityChart(state.snapshot?.activitySeries || null);
    }));
    document.querySelectorAll(".kpi-table-button").forEach((button) => button.addEventListener("click", () => selectActivityMetric(button)));
    el("refreshButton").addEventListener("click", () => loadDashboard(true));
    el("applyFilters").addEventListener("click", () => loadDashboard());
    el("projectFilter").addEventListener("change", () => { el("siteFilter").value = "all"; loadDashboard(); });
    el("siteFilter").addEventListener("change", () => loadDashboard());
    el("periodFilter").addEventListener("change", () => {
      if (el("periodFilter").value === "custom") {
        el("periodFilter").value = state.period;
        openCustomDateRange();
      } else {
        state.period = el("periodFilter").value;
        show(el("editDateRange"), false);
        loadDashboard();
      }
    });
    el("editDateRange").addEventListener("click", openCustomDateRange);
    ["cancelDateRange", "closeDateRange"].forEach((id) => {
      el(id).addEventListener("click", () => el("dateRangeDialog").close());
    });
    el("dateRangeForm").addEventListener("submit", (event) => {
      event.preventDefault();
      state.customRange = {
        startDate: el("startDateFilter").value,
        endDate: el("endDateFilter").value,
      };
      state.period = "custom";
      el("periodFilter").value = "custom";
      show(el("editDateRange"), true);
      el("dateRangeDialog").close();
      loadDashboard();
    });
    ["startDateFilter", "endDateFilter"].forEach((id) => {
      el(id).addEventListener("input", syncCustomDateRange);
    });
    el("siteSearch").addEventListener("input", () => filterTable("siteBody", "siteSearch", "siteMeta", "siteEmpty"));
    el("personSearch").addEventListener("input", () => filterTable("personBody", "personSearch", "personMeta", "personEmpty"));
    el("printButton").addEventListener("click", () => window.print());
    window.addEventListener("beforeprint", () => state.activityChart?.resize(900, 360));
    window.addEventListener("afterprint", () => state.activityChart?.resize());
    document.querySelectorAll(".export-button").forEach((button) => button.addEventListener("click", () => exportCsv(button.dataset.report)));
    document.querySelectorAll(".sort-button").forEach((button) => button.addEventListener("click", () => applySort(button)));
    el("menuButton").addEventListener("click", () => setSidebar(!sidebarIsOpen()));
    el("sidebarCloseButton").addEventListener("click", () => setSidebar(false));
    el("sidebarBackdrop").addEventListener("click", () => setSidebar(false));
    document.querySelectorAll(".nav a").forEach((link) => link.addEventListener("click", () => {
      setActiveSection(link.hash.slice(1));
      if (mobileNavigation.matches) setSidebar(false);
    }));
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && mobileNavigation.matches && sidebarIsOpen()) setSidebar(false);
    });
    mobileNavigation.addEventListener("change", syncSidebarToViewport);
    syncSidebarToViewport();
    installScrollSpy();
    loadDashboard();
  });
})();
