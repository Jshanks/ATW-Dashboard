/**
 * ATW Dashboard -- Frontend Application v3.0
 */
(function () {
    "use strict";

    var ws = null;
    var wsReconnectDelay = 1000;
    var MAX_WS_RECONNECT = 30000;
    var dashboardState = { instances: [], total_online: 0, total_offline: 0, total_items_active: 0 };
    var knownInstances = new Set();
    var IDLE_STATES = { "getting_task": true, "waiting": true, "unknown": true };
    var pauseState = {};

    var activityChart = null;
    var CHART_REFRESH_INTERVAL = 30000;
    var pingIntervalId = null;

    var grid = document.getElementById("instance-grid");
    var totalOnlineEl = document.getElementById("total-online");
    var totalOfflineEl = document.getElementById("total-offline");
    var totalItemsEl = document.getElementById("total-items");
    var totalDlEl = document.getElementById("total-dl");
    var totalUlEl = document.getElementById("total-ul");
    var wsIndicator = document.getElementById("ws-indicator");
    var wsStatusText = document.getElementById("ws-status-text");
    var settingsPanel = document.getElementById("settings-panel");
    var btnSettingsPanel = document.getElementById("btn-settings-panel");
    var btnCloseSettings = document.getElementById("btn-close-settings");
    var btnApplySettings = document.getElementById("btn-apply-settings");
    var settingsForm = document.getElementById("settings-form");
    var settingsStatus = document.getElementById("settings-status");
    var instanceCheckboxes = document.getElementById("instance-checkboxes");
    var selectAllCheckbox = document.getElementById("select-all");
    var addInstanceModal = document.getElementById("add-instance-modal");
    var btnAddInstance = document.getElementById("btn-add-instance");
    var btnCancelAdd = document.getElementById("btn-cancel-add");
    var addInstanceForm = document.getElementById("add-instance-form");
    var editInstanceModal = document.getElementById("edit-instance-modal");
    var editInstanceForm = document.getElementById("edit-instance-form");
    var btnCancelEdit = document.getElementById("btn-cancel-edit");
    var projectSelect = document.getElementById("project-select");
    var btnApplyProject = document.getElementById("btn-apply-project");
    var projectStatus = document.getElementById("project-status");
    var trackerBar = document.getElementById("tracker-bar");
    var trackerContent = document.getElementById("tracker-stats-content");
    var pauseModal = document.getElementById("pause-modal");
    var btnPauseModal = document.getElementById("btn-pause-modal");
    var btnClosePause = document.getElementById("btn-close-pause");
    var btnPauseSelected = document.getElementById("btn-pause-selected");
    var btnResumeSelected = document.getElementById("btn-resume-selected");
    var pauseSelectAll = document.getElementById("pause-select-all");
    var pauseInstanceList = document.getElementById("pause-instance-list");
    var pauseDuration = document.getElementById("pause-duration");
    var pauseActionStatus = document.getElementById("pause-action-status");
    var pauseBanner = document.getElementById("pause-banner");
    var pauseBannerText = document.getElementById("pause-banner-text");
    var btnBannerResume = document.getElementById("btn-banner-resume");

    // ---- Utility ----
    function escapeHtml(str) {
        if (!str) return "";
        var div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }
    function formatTime(isoStr) {
        try { return new Date(isoStr).toLocaleTimeString(); }
        catch (e) { return isoStr; }
    }
    function setsEqual(a, b) {
        if (a.size !== b.size) return false;
        for (var item of a) { if (!b.has(item)) return false; }
        return true;
    }
    function showToast(message, type) {
        type = type || "info";
        var container = document.getElementById("toast-container");
        var toast = document.createElement("div");
        toast.className = "toast toast-" + type;
        toast.textContent = message;
        container.appendChild(toast);
        setTimeout(function () {
            toast.style.opacity = "0";
            toast.style.transition = "opacity 0.3s ease";
            setTimeout(function () { toast.remove(); }, 300);
        }, 4000);
    }
    function fmtBytes(bps) {
        if (bps < 1024) return bps.toFixed(0) + " B/s";
        if (bps < 1048576) return (bps / 1024).toFixed(1) + " KB/s";
        return (bps / 1048576).toFixed(2) + " MB/s";
    }
    function fmtTotal(bytes) {
        if (bytes < 1024) return bytes + " B";
        if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KiB";
        if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + " MiB";
        if (bytes < 1099511627776) return (bytes / 1073741824).toFixed(2) + " GiB";
        return (bytes / 1099511627776).toFixed(2) + " TiB";
    }
    function fmtNum(n) {
        if (n === null || n === undefined) return "0";
        if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
        if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
        if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
        return String(n);
    }
    function fmtCountdown(seconds) {
        if (seconds === null || seconds === undefined) return "indefinite";
        if (seconds <= 0) return "resuming...";
        var h = Math.floor(seconds / 3600);
        var m = Math.floor((seconds % 3600) / 60);
        if (h > 0) return h + "h " + m + "m";
        return m + "m";
    }

    // ---- WebSocket ----
    function connectWebSocket() {
        var protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        var wsUrl = protocol + "//" + window.location.host + "/ws";
        ws = new WebSocket(wsUrl);
        ws.onopen = function () {
            wsReconnectDelay = 1000;
            wsIndicator.className = "w-2 h-2 rounded-full bg-green-500 pulse-online";
            wsStatusText.textContent = "Connected";
        };
        ws.onmessage = function (event) {
            try {
                var data = JSON.parse(event.data);
                if (data.type === "pong") return;
                dashboardState = data;
                render();
                if (data.history) applyHistoryData(data.history);
                if (data.tracker_stats !== undefined) applyTrackerData(data);
                if (data.pause_status) applyPauseData(data.pause_status);
            } catch (e) { console.error("WS parse error:", e); }
        };
        ws.onclose = function () {
            wsIndicator.className = "w-2 h-2 rounded-full bg-red-500";
            wsStatusText.textContent = "Disconnected";
            scheduleReconnect();
        };
        ws.onerror = function () {
            wsIndicator.className = "w-2 h-2 rounded-full bg-red-500";
            wsStatusText.textContent = "Error";
        };
        if (pingIntervalId !== null) { clearInterval(pingIntervalId); }
        pingIntervalId = setInterval(function () {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: "ping" }));
            }
        }, 25000);
    }
    function scheduleReconnect() {
        setTimeout(function () {
            wsIndicator.className = "w-2 h-2 rounded-full bg-yellow-500 pulse-connecting";
            wsStatusText.textContent = "Reconnecting...";
            connectWebSocket();
        }, wsReconnectDelay);
        wsReconnectDelay = Math.min(wsReconnectDelay * 2, MAX_WS_RECONNECT);
    }

    // ---- Rendering ----
    function render() {
        var state = dashboardState;
        totalOnlineEl.textContent = state.total_online || 0;
        totalOfflineEl.textContent = state.total_offline || 0;
        var aggDl = 0, aggUl = 0, workingItems = 0;
        if (state.instances) {
            for (var i = 0; i < state.instances.length; i++) {
                var inst = state.instances[i];
                aggDl += inst.bandwidth_down || 0;
                aggUl += inst.bandwidth_up || 0;
                if (inst.items) {
                    for (var j = 0; j < inst.items.length; j++) {
                        if (!IDLE_STATES[inst.items[j].state]) workingItems++;
                    }
                }
            }
        }
        totalItemsEl.textContent = workingItems;
        totalDlEl.textContent = fmtBytes(aggDl);
        totalUlEl.textContent = fmtBytes(aggUl);
        if (!state.instances || state.instances.length === 0) {
            grid.innerHTML = '<div class="col-span-full text-center text-gray-500 py-12"><p class="text-lg">No warrior instances yet.</p><p class="text-sm mt-2">Click <strong>Add Instance</strong> to connect your first warrior.</p></div>';
            return;
        }
        var currentNames = new Set(state.instances.map(function (i) { return i.name; }));
        var html = "";
        for (var idx = 0; idx < state.instances.length; idx++) {
            html += renderInstanceCard(state.instances[idx]);
        }
        grid.innerHTML = html;
        if (!setsEqual(currentNames, knownInstances)) {
            knownInstances = currentNames;
            updateInstanceCheckboxes(state.instances);
        }
        document.querySelectorAll("[data-edit-instance]").forEach(function (btn) {
            btn.addEventListener("click", function () { editInstance(btn.dataset.editInstance); });
        });
        document.querySelectorAll("[data-remove-instance]").forEach(function (btn) {
            btn.addEventListener("click", function () { removeInstance(btn.dataset.removeInstance); });
        });
    }

    function renderInstanceCard(inst) {
        var stateClass = "border-state-" + inst.connection_state;
        var isOnline = inst.connection_state === "online";
        var isPaused = pauseState[inst.name] !== undefined;
        var statusDot = isOnline
            ? '<span class="w-2.5 h-2.5 rounded-full bg-green-500 pulse-online" title="Online"></span>'
            : inst.connection_state === "connecting"
                ? '<span class="w-2.5 h-2.5 rounded-full bg-yellow-500 pulse-connecting" title="Connecting"></span>'
                : inst.connection_state === "auth_failed"
                    ? '<span class="w-2.5 h-2.5 rounded-full bg-purple-500" title="Auth Failed"></span>'
                    : '<span class="w-2.5 h-2.5 rounded-full bg-red-500" title="Offline"></span>';
        var projectBadgeHtml = "";
        if (isOnline && inst.project_slug) {
            projectBadgeHtml = '<span class="project-badge" title="' + escapeHtml(inst.current_project || inst.project_slug) + '">' + escapeHtml(inst.project_slug) + '</span>';
        }
        if (isPaused) {
            var ps = pauseState[inst.name];
            var remaining = ps.remaining_seconds;
            var pauseText = remaining !== null ? fmtCountdown(remaining) : "indefinite";
            projectBadgeHtml = '<span class="pause-badge pause-pulse" title="Paused \u2014 ' + pauseText + '">\u23F8 paused</span>';
        }
        var bwHtml = "";
        if (isOnline && !isPaused && (inst.bandwidth_down > 0 || inst.bandwidth_up > 0 || inst.bytes_downloaded > 0)) {
            bwHtml = '<p><span class="text-gray-500">BW:</span> <span class="text-cyan-400">' + fmtBytes(inst.bandwidth_down || 0) + ' &#x2193;</span> <span class="text-orange-400">' + fmtBytes(inst.bandwidth_up || 0) + ' &#x2191;</span></p>';
            if (inst.bytes_downloaded > 0 || inst.bytes_uploaded > 0) {
                bwHtml += '<p><span class="text-gray-500">Total:</span> <span class="text-cyan-300">' + fmtTotal(inst.bytes_downloaded || 0) + ' &#x2193;</span> <span class="text-orange-300">' + fmtTotal(inst.bytes_uploaded || 0) + ' &#x2191;</span></p>';
            }
        }
        var idleCount = 0, activeCount = 0;
        if (inst.items) {
            for (var i = 0; i < inst.items.length; i++) {
                if (IDLE_STATES[inst.items[i].state]) idleCount++;
                else activeCount++;
            }
        }
        var itemsLine = "";
        if (idleCount + activeCount > 0) {
            var parts = [];
            if (activeCount > 0) parts.push('<span class="text-green-400">' + activeCount + " active</span>");
            if (idleCount > 0) parts.push('<span class="text-gray-500">' + idleCount + " idle</span>");
            itemsLine = parts.join(' <span class="text-gray-600">&middot;</span> ');
        } else {
            itemsLine = '<span class="text-gray-500">0</span>';
        }
        var doneHtml = "";
        if (isOnline && inst.completed_items > 0) {
            doneHtml = '<p><span class="text-gray-500">Done:</span> <span class="text-green-300">' + fmtNum(inst.completed_items) + ' items</span></p>';
        }
        var errorMsg = inst.error_message ? '<p class="text-xs text-red-400 mt-2 truncate" title="' + escapeHtml(inst.error_message) + '">' + escapeHtml(inst.error_message) + '</p>' : "";
        var reconnectInfo = !isOnline && inst.reconnect_attempts > 0 ? '<span class="text-xs text-gray-500 ml-1">(attempt ' + inst.reconnect_attempts + ')</span>' : "";
        var editBtn = '<button data-edit-instance="' + escapeHtml(inst.name) + '" class="text-gray-600 hover:text-blue-400 opacity-0 group-hover:opacity-100 transition-opacity" title="Edit"><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg></button>';
        var removeBtn = '<button data-remove-instance="' + escapeHtml(inst.name) + '" class="text-gray-600 hover:text-red-400 opacity-0 group-hover:opacity-100 transition-opacity" title="Remove"><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg></button>';
        return '<div class="instance-card bg-gray-900 border-l-4 ' + stateClass + ' rounded-lg p-4 relative group">' +
            '<div class="absolute top-2 right-2 flex items-center gap-1.5">' + projectBadgeHtml + editBtn + removeBtn + '</div>' +
            '<div class="flex items-center gap-2 mb-2">' + statusDot + '<h3 class="font-semibold text-sm">' + escapeHtml(inst.name) + '</h3>' + reconnectInfo + '</div>' +
            '<div class="text-xs text-gray-400 space-y-0.5">' +
'<p><span class="text-gray-500">URL:</span> <a href="' + escapeHtml(inst.url) + '" target="_blank" class="text-blue-400 hover:text-blue-300 hover:underline">' + escapeHtml(inst.url) + '</a></p>' +
            (isOnline && !isPaused ? '<p><span class="text-gray-500">Items:</span> ' + itemsLine + '</p>' + doneHtml + bwHtml : "") +
            '</div>' + errorMsg +
            (inst.last_seen ? '<p class="text-xs text-gray-600 mt-2">Last seen: ' + formatTime(inst.last_seen) + '</p>' : "") +
            '</div>';
    }

    // ---- 24h Activity Chart ----
    function initChart() {
        var ctx = document.getElementById("activity-chart").getContext("2d");
        activityChart = new Chart(ctx, {
            type: "bar",
            data: {
                datasets: [
                    { type: "bar", label: "Data Used", data: [], backgroundColor: "#f59e0b80", borderColor: "#f59e0b", borderWidth: 1, yAxisID: "yData", order: 2 },
                    { type: "line", label: "Items Done", data: [], borderColor: "#818cf8", backgroundColor: "#818cf820", borderWidth: 2, pointRadius: 0, tension: 0.3, fill: false, yAxisID: "yItems", order: 1 }
                ]
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                interaction: { mode: "index", intersect: false },
                plugins: {
                    legend: { labels: { color: "#9ca3af", boxWidth: 12, padding: 16, font: { size: 11 } } },
                    tooltip: {
                        backgroundColor: "#1f2937", titleColor: "#e5e7eb", bodyColor: "#d1d5db", borderColor: "#374151", borderWidth: 1,
                        callbacks: {
                            title: function(items) { if (items.length > 0) { var d = new Date(items[0].parsed.x); return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); } return ""; },
                            label: function(ctx) { if (ctx.dataset.yAxisID === "yData") return "Data: " + fmtTotal(ctx.parsed.y); return "Items: " + fmtNum(ctx.parsed.y) + " (from tracker)"; }
                        }
                    }
                },
                scales: {
                    x: { type: "time", time: { unit: "hour", displayFormats: { hour: "HH:mm" } }, grid: { color: "#1f2937" }, ticks: { color: "#6b7280", maxTicksLimit: 12, font: { size: 10 } }, offset: true },
                    yData: { type: "linear", position: "left", beginAtZero: true, grid: { color: "#1f293780" }, ticks: { color: "#f59e0b", font: { size: 10 }, callback: function(val) { return fmtTotal(val); } }, title: { display: true, text: "Data", color: "#f59e0b", font: { size: 10 } } },
                    yItems: { type: "linear", position: "right", beginAtZero: true, grid: { drawOnChartArea: false }, ticks: { color: "#818cf8", font: { size: 10 }, callback: function(val) { return fmtNum(val); } }, title: { display: true, text: "Items", color: "#818cf8", font: { size: 10 } } }
                }
            }
        });
    }
    function loadHistory() {
        fetch("/api/history").then(function(res) { if (!res.ok) return; return res.json(); }).then(function(data) {
            if (!data) return;
            applyHistoryData(data);
        }).catch(function(e) { console.error("History load error:", e); });
    }
    function applyHistoryData(data) {
        if (!data || !data.buckets) return;
        var buckets = data.buckets;
        var dataPoints = [], itemPoints = [];
        var cumulativeBytes = 0, cumulativeItems = 0;
        for (var i = 0; i < buckets.length; i++) {
            var ts = buckets[i].t * 1000;
            dataPoints.push({ x: ts, y: buckets[i].bytes });
            itemPoints.push({ x: ts, y: buckets[i].items });
            cumulativeBytes += buckets[i].bytes;
            cumulativeItems += buckets[i].items;
        }
        activityChart.data.datasets[0].data = dataPoints;
        activityChart.data.datasets[1].data = itemPoints;
        activityChart.data.datasets[0].barThickness = "flex";
        activityChart.data.datasets[0].maxBarThickness = Math.max(4, Math.min(20, Math.floor(800 / Math.max(buckets.length, 1))));
        activityChart.update("none");
        updateCumulativeStats(cumulativeBytes, cumulativeItems);
    }

    // ---- Cumulative 24h Stats Display ----
    function updateCumulativeStats(totalBytes, totalItems) {
        var statsEl = document.getElementById("chart-24h-stats");
        if (!statsEl) {
            // Find the "Activity — Last 24 Hours" heading
            var headings = document.querySelectorAll("h2");
            var headingEl = null;
            for (var h = 0; h < headings.length; h++) {
                var txt = headings[h].textContent || "";
                if (txt.indexOf("Activity") !== -1 && txt.indexOf("24") !== -1) {
                    headingEl = headings[h];
                    break;
                }
            }
            if (!headingEl) return;
            // Create a flex wrapper for just the heading + stats (not the chart)
            var flexRow = document.createElement("div");
            flexRow.style.display = "flex";
            flexRow.style.alignItems = "center";
            flexRow.style.justifyContent = "space-between";
            flexRow.style.flexWrap = "wrap";
            flexRow.style.gap = "0.5rem";
            flexRow.className = "mb-2";
            // Insert the wrapper where the heading is, then move the heading into it
            headingEl.parentElement.insertBefore(flexRow, headingEl);
            headingEl.classList.remove("mb-2");
            flexRow.appendChild(headingEl);
            // Create and append the stats element alongside the heading
            statsEl = document.createElement("div");
            statsEl.id = "chart-24h-stats";
            statsEl.className = "flex items-center gap-3 text-xs";
            flexRow.appendChild(statsEl);
        }
        statsEl.innerHTML =
            '<span class="text-gray-500">24h Total:</span> ' +
            '<span class="text-amber-400 font-semibold">' + fmtTotal(totalBytes) + '</span>' +
            '<span class="text-gray-600 mx-1">•</span>' +
            '<span class="text-indigo-400 font-semibold">' + fmtNum(totalItems) + ' items</span>';
    }

    // ---- Tracker Stats ----
    function loadTrackerStats() {
        fetch("/api/tracker").then(function(res) { if (!res.ok) return; return res.json(); }).then(function(data) {
            if (!data) return;
            applyTrackerData(data);
        }).catch(function(e) { console.error("Tracker stats error:", e); });
    }
    function applyTrackerData(data) {
        if (!data || !data.tracker_stats || data.tracker_stats.length === 0) { trackerBar.classList.add("hidden"); return; }
        var html = "";
        for (var i = 0; i < data.tracker_stats.length; i++) {
            var s = data.tracker_stats[i];
            var sep = i > 0 ? '<span class="text-gray-700 mx-2">|</span>' : "";
            var userLine = "";
            if (s.user_items_done > 0 || s.user_bytes > 0) {
                userLine = '<span class="text-cyan-400">You: ' + fmtNum(s.user_items_done) + ' items (' + fmtTotal(s.user_bytes) + ')</span>';
            } else {
                userLine = '<span class="text-gray-500">You: no data yet</span>';
            }
            html += sep + '<div class="flex items-center gap-2 flex-wrap">' +
                '<span class="text-white font-medium">&#x1F3DB;&#xFE0F; ' + escapeHtml(s.project) + '</span>' +
                '<span class="text-gray-600">&#x2014;</span>' + userLine +
                '<span class="text-gray-600">&#x00B7;</span>' +
                '<span class="text-gray-400">' + fmtNum(s.items_done) + ' done + ' + fmtNum(s.items_out) + ' out + ' + fmtNum(s.items_todo) + ' todo</span>' +
                '<span class="text-gray-600">&#x00B7;</span>' +
                '<span class="text-gray-400">' + fmtTotal(s.total_data_bytes) + ' total</span></div>';
        }
        trackerContent.innerHTML = html;
        trackerBar.classList.remove("hidden");
    }

    // ---- Pause / Resume ----
    function loadPauseStatus() {
        fetch("/api/pause-status").then(function(res) { if (!res.ok) return; return res.json(); }).then(function(data) {
            if (!data) return;
            applyPauseData(data);
        }).catch(function(e) { console.error("Pause status error:", e); });
    }
    function applyPauseData(data) {
        if (!data) return;
        pauseState = data.paused || {};
        updatePauseBanner();
    }
    function updatePauseBanner() {
        var count = Object.keys(pauseState).length;
        if (count === 0) { pauseBanner.classList.add("hidden"); return; }
        var earliest = null, allIndefinite = true;
        for (var name in pauseState) {
            var rs = pauseState[name].remaining_seconds;
            if (rs !== null) { allIndefinite = false; if (earliest === null || rs < earliest) earliest = rs; }
        }
        var text = "\u23F8 " + count + " instance" + (count > 1 ? "s" : "") + " paused";
        if (allIndefinite) { text += " indefinitely"; }
        else if (earliest !== null) { text += " \u2014 earliest resume in " + fmtCountdown(earliest); }
        pauseBannerText.textContent = text;
        pauseBanner.classList.remove("hidden");
    }
    function populatePauseModal() {
        var instances = dashboardState.instances || [];
        var html = "";
        for (var i = 0; i < instances.length; i++) {
            var inst = instances[i];
            var isPaused = pauseState[inst.name] !== undefined;
            var statusHtml = "";
            if (isPaused) {
                var ps = pauseState[inst.name];
                statusHtml = ps.remaining_seconds !== null
                    ? '<span class="text-amber-400 text-xs">\u23F8 ' + fmtCountdown(ps.remaining_seconds) + ' remaining</span>'
                    : '<span class="text-amber-400 text-xs">\u23F8 Paused indefinitely</span>';
            } else if (inst.connection_state === "online") {
                statusHtml = '<span class="text-green-400 text-xs">\u25CF Running</span>';
            } else {
                statusHtml = '<span class="text-gray-500 text-xs">\u25CB ' + inst.connection_state.replace("_", " ") + '</span>';
            }
            html += '<label class="flex items-center justify-between bg-gray-800 px-3 py-2 rounded-lg cursor-pointer hover:bg-gray-700 transition-colors">' +
                '<div class="flex items-center gap-2"><input type="checkbox" class="pause-instance-cb rounded" value="' + escapeHtml(inst.name) + '" checked>' +
                '<span class="text-sm">' + escapeHtml(inst.name) + '</span></div>' + statusHtml + '</label>';
        }
        pauseInstanceList.innerHTML = html;
        pauseActionStatus.textContent = "";
        pauseActionStatus.className = "text-sm mb-3";
    }
    btnPauseModal.addEventListener("click", function () {
        loadPauseStatus();
        setTimeout(function () { populatePauseModal(); pauseModal.classList.remove("hidden"); }, 200);
    });
    btnClosePause.addEventListener("click", function () { pauseModal.classList.add("hidden"); });
    pauseSelectAll.addEventListener("change", function () {
        var checked = pauseSelectAll.checked;
        document.querySelectorAll(".pause-instance-cb").forEach(function (cb) { cb.checked = checked; });
    });
    btnPauseSelected.addEventListener("click", async function () {
        var selected = [];
        document.querySelectorAll(".pause-instance-cb:checked").forEach(function (cb) { if (!pauseState[cb.value]) selected.push(cb.value); });
        if (selected.length === 0) { showToast("No running instances selected", "error"); return; }
        var durVal = pauseDuration.value;
        var duration = durVal ? parseFloat(durVal) : null;
        pauseActionStatus.textContent = "Pausing...";
        pauseActionStatus.className = "text-sm mb-3 text-yellow-400";
        try {
            var res = await fetch("/api/pause", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ instance_names: selected, duration_hours: duration }) });
            var data = await res.json();
            var ok = Object.values(data.results).filter(function (r) { return r.status === "ok"; }).length;
            var fail = Object.values(data.results).filter(function (r) { return r.status !== "ok"; }).length;
            pauseActionStatus.textContent = ok + " paused" + (fail > 0 ? ", " + fail + " failed" : "");
            pauseActionStatus.className = "text-sm mb-3 " + (fail > 0 ? "text-yellow-400" : "text-green-400");
            showToast("Paused " + ok + " instance(s)", fail > 0 ? "error" : "success");
            loadPauseStatus(); setTimeout(populatePauseModal, 500);
        } catch (e) { pauseActionStatus.textContent = "Failed: " + e.message; pauseActionStatus.className = "text-sm mb-3 text-red-400"; }
    });
    btnResumeSelected.addEventListener("click", async function () {
        var selected = [];
        document.querySelectorAll(".pause-instance-cb:checked").forEach(function (cb) { if (pauseState[cb.value]) selected.push(cb.value); });
        if (selected.length === 0) { showToast("No paused instances selected", "error"); return; }
        pauseActionStatus.textContent = "Resuming...";
        pauseActionStatus.className = "text-sm mb-3 text-yellow-400";
        try {
            var res = await fetch("/api/resume", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ instance_names: selected }) });
            var data = await res.json();
            var ok = Object.values(data.results).filter(function (r) { return r.status === "ok"; }).length;
            var fail = Object.values(data.results).filter(function (r) { return r.status !== "ok"; }).length;
            pauseActionStatus.textContent = ok + " resumed" + (fail > 0 ? ", " + fail + " failed" : "");
            pauseActionStatus.className = "text-sm mb-3 " + (fail > 0 ? "text-yellow-400" : "text-green-400");
            showToast("Resumed " + ok + " instance(s)", fail > 0 ? "error" : "success");
            loadPauseStatus(); setTimeout(populatePauseModal, 500);
        } catch (e) { pauseActionStatus.textContent = "Failed: " + e.message; pauseActionStatus.className = "text-sm mb-3 text-red-400"; }
    });
    btnBannerResume.addEventListener("click", async function () {
        var allPaused = Object.keys(pauseState);
        if (allPaused.length === 0) return;
        try {
            var res = await fetch("/api/resume", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ instance_names: allPaused }) });
            var data = await res.json();
            var ok = Object.values(data.results).filter(function (r) { return r.status === "ok"; }).length;
            showToast("Resumed " + ok + " instance(s)", "success");
            loadPauseStatus();
        } catch (e) { showToast("Failed: " + e.message, "error"); }
    });

    // ---- Settings Panel ----
    btnSettingsPanel.addEventListener("click", function () { settingsPanel.classList.toggle("hidden"); });
    btnCloseSettings.addEventListener("click", function () { settingsPanel.classList.add("hidden"); });
    selectAllCheckbox.addEventListener("change", function () {
        var checked = selectAllCheckbox.checked;
        document.querySelectorAll(".instance-checkbox").forEach(function (cb) { cb.checked = checked; });
    });
    btnApplySettings.addEventListener("click", async function () {
        var selected = [];
        document.querySelectorAll(".instance-checkbox:checked").forEach(function (cb) { selected.push(cb.value); });
        if (selected.length === 0) { showToast("No instances selected", "error"); return; }
        var formData = new FormData(settingsForm);
        var settings = {};
        var d = formData.get("downloader");
        var c = formData.get("concurrent_items");
        var r = formData.get("shared_rsync_threads");
        if (d) settings.downloader = d;
        if (c) settings.concurrent_items = parseInt(c);
        if (r) settings.shared_rsync_threads = parseInt(r);
        if (Object.keys(settings).length === 0) { showToast("No settings to apply", "error"); return; }
        settingsStatus.textContent = "Applying...";
        settingsStatus.className = "text-sm self-center ml-4 text-yellow-400";
        try {
            var res = await fetch("/api/settings/bulk", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ instance_names: selected, settings: settings }),
            });
            var data = await res.json();
            var ok = Object.values(data.results).filter(function (r) { return r.status === "ok"; }).length;
            var fail = Object.values(data.results).filter(function (r) { return r.status !== "ok"; }).length;
            settingsStatus.textContent = ok + " updated" + (fail > 0 ? ", " + fail + " failed" : "");
            settingsStatus.className = "text-sm self-center ml-4 " + (fail > 0 ? "text-yellow-400" : "text-green-400");
            showToast("Settings applied to " + ok + " instance(s)", fail > 0 ? "error" : "success");
        } catch (e) {
            settingsStatus.textContent = "Error: " + e.message;
            settingsStatus.className = "text-sm self-center ml-4 text-red-400";
        }
    });
    function updateInstanceCheckboxes(instances) {
        var html = "";
        for (var i = 0; i < instances.length; i++) {
            html += '<label class="flex items-center gap-2 bg-gray-800 px-3 py-1.5 rounded-lg text-sm cursor-pointer">' +
                '<input type="checkbox" class="instance-checkbox rounded" value="' + escapeHtml(instances[i].name) + '" checked>' +
                escapeHtml(instances[i].name) + '</label>';
        }
        instanceCheckboxes.innerHTML = html;
    }

    // ---- Projects ----
    function loadProjects() {
        fetch("/api/projects").then(function(res) { return res.json(); }).then(function(data) {
            if (!Array.isArray(data)) return;
            var html = '<option value="">-- Select Project --</option>';
            for (var i = 0; i < data.length; i++) {
                var item = data[i];
                var slug = typeof item === "string" ? item : (item.slug || item.name || String(item));
                var name = typeof item === "string" ? item : (item.name || item.slug || String(item));
                html += '<option value="' + escapeHtml(slug) + '">' + escapeHtml(name) + '</option>';
            }
            projectSelect.innerHTML = html;
        }).catch(function(e) { console.error("Load projects error:", e); });
    }
    btnApplyProject.addEventListener("click", async function () {
        var slug = projectSelect.value;
        if (!slug) { showToast("Select a project first", "error"); return; }
        var selected = [];
        document.querySelectorAll(".instance-checkbox:checked").forEach(function (cb) { selected.push(cb.value); });
        if (selected.length === 0) { showToast("No instances selected", "error"); return; }
        projectStatus.textContent = "Applying...";
        projectStatus.className = "text-sm ml-2 text-yellow-400";
        try {
            var res = await fetch("/api/project/bulk", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ instance_names: selected, project_name: slug }),
            });
            var data = await res.json();
            var ok = Object.values(data.results).filter(function (r) { return r.status === "ok"; }).length;
            projectStatus.textContent = ok + " updated";
            projectStatus.className = "text-sm ml-2 text-green-400";
            showToast("Project applied to " + ok + " instance(s)", "success");
        } catch (e) {
            projectStatus.textContent = "Error";
            projectStatus.className = "text-sm ml-2 text-red-400";
        }
    });

    // ---- Add Instance ----
    btnAddInstance.addEventListener("click", function () { addInstanceModal.classList.remove("hidden"); });
    btnCancelAdd.addEventListener("click", function () { addInstanceModal.classList.add("hidden"); });
    addInstanceForm.addEventListener("submit", async function (e) {
        e.preventDefault();
        var fd = new FormData(addInstanceForm);
        var body = { name: fd.get("name"), host: fd.get("host"), port: parseInt(fd.get("port")) || 8001 };
        if (fd.get("http_username")) body.http_username = fd.get("http_username");
        if (fd.get("http_password")) body.http_password = fd.get("http_password");
        try {
            var res = await fetch("/api/instances", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
            if (res.ok) { showToast("Instance added: " + body.name, "success"); addInstanceModal.classList.add("hidden"); addInstanceForm.reset(); }
            else { var err = await res.json(); showToast("Error: " + (err.detail || "Failed"), "error"); }
        } catch (e) { showToast("Error: " + e.message, "error"); }
    });

    // ---- Edit Instance ----
    btnCancelEdit.addEventListener("click", function () { editInstanceModal.classList.add("hidden"); });
    function editInstance(name) {
        var inst = null;
        for (var i = 0; i < dashboardState.instances.length; i++) {
            if (dashboardState.instances[i].name === name) { inst = dashboardState.instances[i]; break; }
        }
        if (!inst) return;
        editInstanceForm.elements.name.value = inst.name;
        editInstanceForm.elements.host.value = inst.host;
        editInstanceForm.elements.port.value = inst.port;
        editInstanceForm.elements.http_username.value = "";
        editInstanceForm.elements.http_password.value = "";
        editInstanceModal.classList.remove("hidden");
    }
    editInstanceForm.addEventListener("submit", async function (e) {
        e.preventDefault();
        var fd = new FormData(editInstanceForm);
        var name = fd.get("name");
        var body = { host: fd.get("host"), port: parseInt(fd.get("port")) || 8001 };
        var u = fd.get("http_username"), p = fd.get("http_password");
        if (u !== null) body.http_username = u;
        if (p !== null) body.http_password = p;
        try {
            var res = await fetch("/api/instances/" + encodeURIComponent(name), { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
            if (res.ok) { showToast("Instance updated: " + name, "success"); editInstanceModal.classList.add("hidden"); }
            else { var err = await res.json(); showToast("Error: " + (err.detail || "Failed"), "error"); }
        } catch (e) { showToast("Error: " + e.message, "error"); }
    });

    // ---- Remove Instance ----
    async function removeInstance(name) {
        if (!confirm("Remove instance " + name + "?")) return;
        try {
            var res = await fetch("/api/instances/" + encodeURIComponent(name), { method: "DELETE" });
            if (res.ok) { showToast("Instance removed: " + name, "success"); }
            else { showToast("Failed to remove " + name, "error"); }
        } catch (e) { showToast("Error: " + e.message, "error"); }
    }

    // ---- Grid Column Selector ----
    var GRID_COL_KEY = "atw-grid-cols";
    var colBtns = document.querySelectorAll(".col-btn");

    function applyGridColumns(n) {
        n = parseInt(n) || 4;
        if (n < 1) n = 1;
        if (n > 8) n = 8;
        // On mobile (< 768px), always single column
        if (window.innerWidth < 768) {
            grid.style.gridTemplateColumns = "1fr";
        } else {
            grid.style.gridTemplateColumns = "repeat(" + n + ", minmax(0, 1fr))";
        }
        // Highlight active button
        for (var i = 0; i < colBtns.length; i++) {
            var btn = colBtns[i];
            if (parseInt(btn.dataset.cols) === n) {
                btn.classList.remove("bg-gray-800", "text-gray-400");
                btn.classList.add("bg-blue-600", "text-white");
            } else {
                btn.classList.remove("bg-blue-600", "text-white");
                btn.classList.add("bg-gray-800", "text-gray-400");
            }
        }
    }

    // Attach click handlers to column selector buttons
    for (var ci = 0; ci < colBtns.length; ci++) {
        colBtns[ci].addEventListener("click", function () {
            var cols = this.dataset.cols;
            localStorage.setItem(GRID_COL_KEY, cols);
            applyGridColumns(cols);
        });
    }

    // Re-evaluate on resize (snap to 1 col on mobile)
    window.addEventListener("resize", function () {
        var saved = localStorage.getItem(GRID_COL_KEY) || "4";
        applyGridColumns(saved);
    });

    // Apply saved preference on load
    applyGridColumns(localStorage.getItem(GRID_COL_KEY) || "4");


    // ---- Init ----
    // ---- Version Badge ----
    fetch("/api/config").then(function(res) { return res.json(); }).then(function(cfg) {
        if (!cfg || !cfg.version || cfg.version === "dev") return;
        var badge = document.getElementById("version-badge");
        if (badge) {
            badge.textContent = cfg.version;
            badge.title = "ATW Dashboard version";
            badge.classList.remove("hidden");
        }
    }).catch(function() {});
    connectWebSocket();
    loadProjects();
    initChart();
    loadHistory();
    loadTrackerStats();
    loadPauseStatus();

})();