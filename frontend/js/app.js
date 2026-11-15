/**
 * ATW Dashboard -- Frontend Application
 *
 * v4.1.0: ArchiveTeam's Choice support, online/offline filter,
 *         group-by-project view (Choice first, then A-Z, then paused/offline).
 * v4.2.0: optional instance name, bulk add / CSV / JSON import with preview,
 *         export link.
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
    var CHART_REBUILD_INTERVAL = 3600000; // 1 hour
    var lastChartRebuild = Date.now(); 
    var pingIntervalId = null;
    var instanceFingerprints = {};
    var dataSourceTracker = [];

    // ---- View options (filter / grouping), persisted in localStorage ----
    var AUTO_PROJECT_SLUG = "auto";
    var AUTO_PROJECT_TITLE = "ArchiveTeam's Choice";
    var VIEW_FILTER_KEY = "atw-view-filter";        // "all" | "online" | "offline"
    var VIEW_GROUPED_KEY = "atw-view-grouped";      // "1" | "0"
    var COLLAPSED_GROUPS_KEY = "atw-collapsed-groups";
    var GROUP_PAUSED = "__paused";
    var GROUP_OFFLINE = "__offline";
    var GROUP_UNASSIGNED = "__unassigned";
    var viewFilter = localStorage.getItem(VIEW_FILTER_KEY) || "all";
    if (viewFilter !== "all" && viewFilter !== "online" && viewFilter !== "offline") viewFilter = "all";
    var viewGrouped = localStorage.getItem(VIEW_GROUPED_KEY) === "1";
    var collapsedGroups = {};
    try { collapsedGroups = JSON.parse(localStorage.getItem(COLLAPSED_GROUPS_KEY) || "{}") || {}; }
    catch (ignored) { collapsedGroups = {}; }
    var lastLayoutKey = "";
    var projectTitles = {};  // slug -> display title, filled from /api/projects

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
    var _escapeDiv = document.createElement("div");
    function escapeHtml(str) {
        if (!str) return "";
        _escapeDiv.textContent = str;
        return _escapeDiv.innerHTML;
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
        setTimeout(function () {
            if (toast.parentNode) toast.parentNode.removeChild(toast);
        }, 300);
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
    // ---- Instance diffing helpers ----
function getInstanceFingerprint(inst) {
    var pauseInfo = pauseState[inst.name];
    var pauseKey = pauseInfo
        ? String(pauseInfo.remaining_seconds) + "|" + String(pauseInfo.project_slug || "")
        : "np";
    var itemStates = "";
    if (inst.items) {
        for (var i = 0; i < inst.items.length; i++) {
            itemStates += inst.items[i].state + ",";
        }
    }
    return inst.connection_state + "|" +
        inst.project_slug + "|" +
        inst.selected_project + "|" +
        inst.bandwidth_down + "|" +
        inst.bandwidth_up + "|" +
        inst.bytes_downloaded + "|" +
        inst.bytes_uploaded + "|" +
        inst.completed_items + "|" +
        inst.error_message + "|" +
        inst.reconnect_attempts + "|" +
        inst.last_seen + "|" +
        pauseKey + "|" +
        itemStates;
}

function findCardByName(name) {
    var children = grid.children;
    for (var i = 0; i < children.length; i++) {
        if (children[i].dataset && children[i].dataset.instanceName === name) {
            return children[i];
        }
    }
    return null;
}

    // ---- Filter / grouping helpers ----
    function isAutoInstance(inst) {
        return inst.selected_project === AUTO_PROJECT_SLUG;
    }

    function passesFilter(inst) {
        var isOnline = inst.connection_state === "online";
        if (viewFilter === "online") return isOnline;
        if (viewFilter === "offline") return !isOnline;   // offline, connecting, auth_failed
        return true;
    }

    function getGroupKey(inst) {
        if (pauseState[inst.name] !== undefined) return GROUP_PAUSED;
        if (inst.connection_state !== "online") return GROUP_OFFLINE;
        if (isAutoInstance(inst)) return AUTO_PROJECT_SLUG;
        return inst.project_slug || inst.selected_project || GROUP_UNASSIGNED;
    }

    function groupSortRank(groupKey) {
        if (groupKey === AUTO_PROJECT_SLUG) return 0;
        if (groupKey === GROUP_UNASSIGNED) return 2;
        if (groupKey === GROUP_PAUSED) return 3;
        if (groupKey === GROUP_OFFLINE) return 4;
        return 1;  // real projects, sorted A-Z among themselves
    }

    function compareGroupKeys(a, b) {
        var rankDifference = groupSortRank(a) - groupSortRank(b);
        if (rankDifference !== 0) return rankDifference;
        return a.toLowerCase() < b.toLowerCase() ? -1 : (a.toLowerCase() > b.toLowerCase() ? 1 : 0);
    }

    function getGroupLabel(groupKey, members) {
        if (groupKey === AUTO_PROJECT_SLUG) {
            // Show what Choice is currently running (from the members themselves)
            var runningSlugs = {};
            for (var i = 0; i < members.length; i++) {
                if (members[i].project_slug) runningSlugs[members[i].project_slug] = true;
            }
            var runningList = Object.keys(runningSlugs).sort();
            return "\u2605 " + AUTO_PROJECT_TITLE + (runningList.length ? " \u2192 " + runningList.join(", ") : "");
        }
        if (groupKey === GROUP_PAUSED) return "\u23F8 Paused";
        if (groupKey === GROUP_OFFLINE) return "Offline / Connecting";
        if (groupKey === GROUP_UNASSIGNED) return "No project selected";
        return groupKey;
    }

    // Returns [{ key, members: [inst...] }] in display order, filter applied
    function buildGroups(instances) {
        var groupsByKey = {};
        for (var i = 0; i < instances.length; i++) {
            var inst = instances[i];
            if (!passesFilter(inst)) continue;
            var key = getGroupKey(inst);
            if (!groupsByKey[key]) groupsByKey[key] = [];
            groupsByKey[key].push(inst);
        }
        var orderedKeys = Object.keys(groupsByKey).sort(compareGroupKeys);
        return orderedKeys.map(function (key) { return { key: key, members: groupsByKey[key] }; });
    }

    function getGroupStatsHtml(members) {
        var onlineCount = 0, downTotal = 0, upTotal = 0;
        for (var i = 0; i < members.length; i++) {
            if (members[i].connection_state === "online") onlineCount++;
            downTotal += members[i].bandwidth_down || 0;
            upTotal += members[i].bandwidth_up || 0;
        }
        var html = '<span class="text-gray-400">' + members.length + ' warrior' + (members.length === 1 ? "" : "s") + '</span>';
        if (onlineCount > 0) {
            html += '<span class="text-gray-600 mx-1">&middot;</span>' +
                '<span class="text-cyan-400">' + fmtBytes(downTotal) + ' &#x2193;</span> ' +
                '<span class="text-orange-400">' + fmtBytes(upTotal) + ' &#x2191;</span>';
        }
        return html;
    }

    function renderGroupHeader(group) {
        var isCollapsed = !!collapsedGroups[group.key];
        var chevron = isCollapsed ? "\u25B8" : "\u25BE";
        var accentClass = group.key === AUTO_PROJECT_SLUG ? "text-amber-300"
            : (group.key === GROUP_OFFLINE ? "text-red-400"
            : (group.key === GROUP_PAUSED ? "text-amber-400" : "text-blue-300"));
        return '<div class="group-header flex items-center gap-3 pt-2 pb-1 border-b border-gray-800" style="grid-column: 1 / -1;" data-group-header="' + escapeHtml(group.key) + '">' +
            '<button type="button" data-toggle-group="' + escapeHtml(group.key) + '" class="flex items-center gap-2 text-sm font-semibold ' + accentClass + ' hover:text-white" aria-expanded="' + (isCollapsed ? "false" : "true") + '">' +
            '<span class="text-gray-500 w-3 inline-block">' + chevron + '</span>' +
            '<span data-group-label>' + escapeHtml(getGroupLabel(group.key, group.members)) + '</span></button>' +
            '<span class="text-xs" data-group-stats>' + getGroupStatsHtml(group.members) + '</span>' +
            '</div>';
    }

    // Layout key changes whenever card ORDER/VISIBILITY must change -> full rebuild
    function computeLayoutKey(instances, groups) {
        var key = viewFilter + "|" + (viewGrouped ? "g" : "f") + "|";
        if (viewGrouped) {
            for (var g = 0; g < groups.length; g++) {
                key += groups[g].key + (collapsedGroups[groups[g].key] ? "(c)" : "") + ":";
                for (var m = 0; m < groups[g].members.length; m++) key += groups[g].members[m].name + ",";
                key += ";";
            }
        } else {
            for (var i = 0; i < instances.length; i++) {
                if (passesFilter(instances[i])) key += instances[i].name + ",";
            }
        }
        return key;
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
        if (data.history) {
            applyHistoryData(data.history);
            data.history = null;
        }
        if (data.tracker_stats !== undefined) {
            applyTrackerData(data);
            data.tracker_stats = null;
        }
        if (data.pause_status) {
            applyPauseData(data.pause_status);
            data.pause_status = null;
        }
        dashboardState = data;
        render();
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
        grid.innerHTML = '<div class="col-span-full text-center text-gray-500 py-12">' +
            '<p class="text-lg">No warrior instances yet.</p>' +
            '<p class="text-sm mt-2">Click <strong>Add Instance</strong> to connect your first warrior.</p></div>';
        instanceFingerprints = {};
        knownInstances = new Set();
        return;
    }

    var currentNames = new Set(state.instances.map(function (i) { return i.name; }));
    var structureChanged = !setsEqual(currentNames, knownInstances);
    if (structureChanged) {
        knownInstances = currentNames;
        updateInstanceCheckboxes(state.instances);
    }

    var groups = viewGrouped ? buildGroups(state.instances) : [];
    var layoutKey = computeLayoutKey(state.instances, groups);

    if (structureChanged || layoutKey !== lastLayoutKey) {
        // Full rebuild: instances added/removed, filter/grouping changed,
        // a warrior moved between groups, or a group was collapsed/expanded
        var html = "";
        instanceFingerprints = {};
        var visibleCount = 0;
        if (viewGrouped) {
            for (var g = 0; g < groups.length; g++) {
                html += renderGroupHeader(groups[g]);
                if (collapsedGroups[groups[g].key]) continue;
                for (var m = 0; m < groups[g].members.length; m++) {
                    html += renderInstanceCard(groups[g].members[m]);
                    visibleCount++;
                }
            }
        } else {
            for (var idx = 0; idx < state.instances.length; idx++) {
                if (!passesFilter(state.instances[idx])) continue;
                html += renderInstanceCard(state.instances[idx]);
                visibleCount++;
            }
        }
        // Fingerprint every instance (hidden ones too) so targeted updates stay quiet
        for (var f = 0; f < state.instances.length; f++) {
            instanceFingerprints[state.instances[f].name] = getInstanceFingerprint(state.instances[f]);
        }
        if (visibleCount === 0 && !(viewGrouped && groups.length > 0)) {
            html = '<div class="text-center text-gray-500 py-12" style="grid-column: 1 / -1;">' +
                '<p class="text-sm">No warriors match the <strong>' + escapeHtml(viewFilter) + '</strong> filter.</p></div>';
        }
        grid.innerHTML = html;
        lastLayoutKey = layoutKey;
        return;
    }

    // Targeted update — only touch cards whose data actually changed
    for (var idx2 = 0; idx2 < state.instances.length; idx2++) {
        var changedInst = state.instances[idx2];
        var fingerprint = getInstanceFingerprint(changedInst);
        if (instanceFingerprints[changedInst.name] === fingerprint) {
            continue; // Nothing changed — skip entirely
        }
        var card = findCardByName(changedInst.name);
        if (card) {
            card.outerHTML = renderInstanceCard(changedInst);
        }
        instanceFingerprints[changedInst.name] = fingerprint;
    }

    // Refresh group header labels/stats in place (cheap: one per group)
    if (viewGrouped) {
        for (var h = 0; h < groups.length; h++) {
            var header = grid.querySelector('[data-group-header="' + cssEscape(groups[h].key) + '"]');
            if (!header) continue;
            var statsSpan = header.querySelector("[data-group-stats]");
            var newStats = getGroupStatsHtml(groups[h].members);
            if (statsSpan && statsSpan.innerHTML !== newStats) statsSpan.innerHTML = newStats;
            var labelSpan = header.querySelector("[data-group-label]");
            var newLabel = getGroupLabel(groups[h].key, groups[h].members);
            if (labelSpan && labelSpan.textContent !== newLabel) labelSpan.textContent = newLabel;
        }
    }
}

    function cssEscape(value) {
        if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(value);
        return String(value).replace(/["\\]/g, "\\$&");
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
        if (isOnline && isAutoInstance(inst)) {
            // ArchiveTeam's Choice: star + the project it is actually running
            var runningSlug = inst.project_slug || "resolving\u2026";
            projectBadgeHtml = '<span class="project-badge" title="' + escapeHtml(AUTO_PROJECT_TITLE + " \u2192 " + runningSlug) + '">\u2605 ' + escapeHtml(runningSlug) + '</span>';
        } else if (isOnline && inst.project_slug) {
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
        return '<div class="instance-card bg-gray-900 border-l-4 ' + stateClass + ' rounded-lg p-4 relative group" data-instance-name="' + escapeHtml(inst.name) + '">' +
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
                            label: function(ctx) {
    if (ctx.dataset.yAxisID === "yData") {
        var source = dataSourceTracker[ctx.dataIndex] ? " (from tracker)" : "";
        return "Data: " + fmtTotal(ctx.parsed.y) + source;
    }
    return "Items: " + fmtNum(ctx.parsed.y) + " (from tracker)";
}
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
    	dataSourceTracker = [];
    	for (var i = 0; i < buckets.length; i++) {
        	var ts = buckets[i].t * 1000;
        	var trackerBytes = buckets[i].tracker_bytes || 0;
        	var localBytes = buckets[i].bytes || 0;
        	var isTracker = trackerBytes > 0;
        	var displayBytes = isTracker ? trackerBytes : localBytes;
        	dataPoints.push({ x: ts, y: displayBytes });
        	dataSourceTracker.push(isTracker);
        	itemPoints.push({ x: ts, y: buckets[i].items });
        	cumulativeBytes += displayBytes;
        	cumulativeItems += buckets[i].items;
    }
        activityChart.data.datasets[0].data = dataPoints;
        activityChart.data.datasets[1].data = itemPoints;
        activityChart.data.datasets[0].barThickness = "flex";
        activityChart.data.datasets[0].maxBarThickness = Math.max(4, Math.min(20, Math.floor(800 / Math.max(buckets.length, 1))));
        if (Date.now() - lastChartRebuild > CHART_REBUILD_INTERVAL) {
            activityChart.destroy();
            initChart();
            activityChart.data.datasets[0].data = dataPoints;
            activityChart.data.datasets[1].data = itemPoints;
            activityChart.data.datasets[0].barThickness = "flex";
            activityChart.data.datasets[0].maxBarThickness = Math.max(4, Math.min(20, Math.floor(800 / Math.max(buckets.length, 1))));
            lastChartRebuild = Date.now();
    }
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
        var sep = i > 0 ? '<span class="text-gray-600 mx-1">|</span>' : "";
        var userLine = "";
        if (s.user_items_done > 0 || s.user_bytes > 0) {
            userLine = '<span class="text-cyan-400">You:</span> ' + fmtNum(s.user_items_done) + ' items (' + fmtTotal(s.user_bytes) + ')';
        } else {
            userLine = '<span class="text-cyan-400">You:</span> no data yet';
        }
        html += sep +
            '<span class="inline-flex items-center gap-1.5 whitespace-nowrap">' +
            '<span class="text-gray-500">▸</span> <a href="https://tracker.archiveteam.org/' + encodeURIComponent(s.project) + '" target="_blank" class="font-semibold text-blue-400 hover:text-blue-300 hover:underline">' + escapeHtml(s.project) + '</a>' +
            '<span class="text-gray-500">—</span> ' + userLine +
            '</span>';
    }
    trackerContent.innerHTML = '<div class="flex flex-wrap items-center gap-x-3 gap-y-1">' + html + '</div>';
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
        if (viewGrouped) render();  // paused warriors move to the Paused group
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

    // ---- Projects (sessionStorage cached) ----
var PROJECTS_CACHE_KEY = "atw-projects-cache-v2";  // v2: sorted list with Choice entry
var PROJECTS_CACHE_TTL = 600000; // 10 minutes in ms

function loadProjects() {
    var cached = sessionStorage.getItem(PROJECTS_CACHE_KEY);
    if (cached) {
        try {
            var parsed = JSON.parse(cached);
            if (Date.now() - parsed.timestamp < PROJECTS_CACHE_TTL) {
                applyProjectList(parsed.data);
                return;
            }
        } catch (ignored) { /* stale or corrupt — fetch fresh */ }
    }
    fetch("/api/projects")
        .then(function (res) { return res.json(); })
        .then(function (data) {
            if (!Array.isArray(data)) return;
            sessionStorage.setItem(
                PROJECTS_CACHE_KEY,
                JSON.stringify({ data: data, timestamp: Date.now() })
            );
            applyProjectList(data);
        })
        .catch(function (e) { console.error("Load projects error:", e); });
}

function applyProjectList(data) {
    if (!Array.isArray(data)) return;
    // Backend already orders the list: ArchiveTeam's Choice first, then A-Z
    var html = '<option value="">-- Select Project --</option>';
    for (var i = 0; i < data.length; i++) {
        var item = data[i];
        var slug = typeof item === "string" ? item : (item.slug || item.name || String(item));
        var displayName = typeof item === "string" ? item : (item.name || item.slug || String(item));
        if (slug === AUTO_PROJECT_SLUG) {
            displayName = "\u2605 " + AUTO_PROJECT_TITLE;
            if (item.resolves_to) displayName += " (\u2192 " + item.resolves_to + ")";
        }
        if (typeof item === "object" && item.title) projectTitles[slug] = item.title;
        html += '<option value="' + escapeHtml(slug) + '">' + escapeHtml(displayName) + '</option>';
    }
    projectSelect.innerHTML = html;
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

    // ---- Add Instance(s) ----
    var addTabButtons = addInstanceModal.querySelectorAll("[data-add-tab]");
    var addPanes = addInstanceModal.querySelectorAll("[data-add-pane]");
    var bulkAddText = document.getElementById("bulk-add-text");
    var bulkAddFile = document.getElementById("bulk-add-file");
    var bulkAddFileName = document.getElementById("bulk-add-file-name");
    var bulkAddPreview = document.getElementById("bulk-add-preview");
    var bulkAddSummary = document.getElementById("bulk-add-summary");
    var bulkAddListAdd = document.getElementById("bulk-add-list-add");
    var bulkAddListSkip = document.getElementById("bulk-add-list-skip");
    var btnBulkPreview = document.getElementById("btn-bulk-preview");
    var btnBulkConfirm = document.getElementById("btn-bulk-confirm");
    var btnCancelBulk = document.getElementById("btn-cancel-bulk");
    var BULK_PREVIEW_LIST_LIMIT = 200;   // rows shown per list; counts are always exact
    var MAX_IMPORT_FILE_BYTES = 450000;  // backend accepts up to 500,000 characters
    var bulkPreviewedText = null;        // text the current preview belongs to

    function showAddTab(tabName) {
        for (var i = 0; i < addTabButtons.length; i++) {
            var isActive = addTabButtons[i].dataset.addTab === tabName;
            addTabButtons[i].setAttribute("aria-selected", isActive ? "true" : "false");
            addTabButtons[i].classList.toggle("border-blue-500", isActive);
            addTabButtons[i].classList.toggle("text-white", isActive);
            addTabButtons[i].classList.toggle("border-transparent", !isActive);
            addTabButtons[i].classList.toggle("text-gray-400", !isActive);
        }
        for (var j = 0; j < addPanes.length; j++) {
            addPanes[j].classList.toggle("hidden", addPanes[j].dataset.addPane !== tabName);
        }
    }
    for (var tabIndex = 0; tabIndex < addTabButtons.length; tabIndex++) {
        addTabButtons[tabIndex].addEventListener("click", function () { showAddTab(this.dataset.addTab); });
    }

    function resetBulkAdd() {
        bulkAddText.value = "";
        bulkAddFile.value = "";
        bulkAddFileName.textContent = "";
        clearBulkPreview();
    }

    function clearBulkPreview() {
        bulkPreviewedText = null;
        bulkAddPreview.classList.add("hidden");
        bulkAddSummary.innerHTML = "";
        bulkAddListAdd.innerHTML = "";
        bulkAddListSkip.innerHTML = "";
        setConfirmButton(0);
    }

    function setConfirmButton(addCount) {
        btnBulkConfirm.disabled = addCount < 1;
        btnBulkConfirm.textContent = "Add " + addCount + " warrior" + (addCount === 1 ? "" : "s");
    }

    function closeAddModal() {
        addInstanceModal.classList.add("hidden");
        addInstanceForm.reset();
        resetBulkAdd();
        showAddTab("single");
    }

    async function readErrorDetail(response) {
        try {
            var errorBody = await response.json();
            if (typeof errorBody.detail === "string") return errorBody.detail;
            // FastAPI validation errors: [{loc, msg}, ...]
            if (Array.isArray(errorBody.detail) && errorBody.detail.length) {
                return errorBody.detail.map(function (item) {
                    var field = Array.isArray(item.loc) ? item.loc[item.loc.length - 1] : "";
                    return (field ? field + ": " : "") + item.msg;
                }).join("; ");
            }
        } catch (ignored) { /* non-JSON error body */ }
        return "HTTP " + response.status;
    }

    btnAddInstance.addEventListener("click", function () {
        addInstanceModal.classList.remove("hidden");
        var hostInput = addInstanceForm.querySelector('input[name="host"]');
        if (hostInput) hostInput.focus();
    });
    btnCancelAdd.addEventListener("click", closeAddModal);
    btnCancelBulk.addEventListener("click", closeAddModal);
    addInstanceModal.addEventListener("keydown", function (e) {
        if (e.key === "Escape") closeAddModal();
    });

    // Single add: name is optional; the backend names it host:port when blank
    addInstanceForm.addEventListener("submit", async function (e) {
        e.preventDefault();
        var fd = new FormData(addInstanceForm);
        var body = {
            host: String(fd.get("host") || "").trim(),
            port: parseInt(fd.get("port"), 10) || 8001
        };
        var typedName = String(fd.get("name") || "").trim();
        if (typedName) body.name = typedName;
        if (fd.get("http_username")) body.http_username = fd.get("http_username");
        if (fd.get("http_password")) body.http_password = fd.get("http_password");
        try {
            var res = await fetch("/api/instances", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
            if (res.ok) {
                var result = await res.json();
                showToast("Instance added: " + (result.instance || body.host), "success");
                closeAddModal();
            } else {
                showToast("Error: " + (await readErrorDetail(res)), "error");
            }
        } catch (err) { showToast("Error: " + err.message, "error"); }
    });

    // Any edit to the bulk text invalidates the current preview
    bulkAddText.addEventListener("input", function () {
        if (bulkPreviewedText !== null && bulkAddText.value !== bulkPreviewedText) clearBulkPreview();
    });

    bulkAddFile.addEventListener("change", function () {
        var selectedFile = bulkAddFile.files && bulkAddFile.files[0];
        if (!selectedFile) return;
        if (selectedFile.size > MAX_IMPORT_FILE_BYTES) {
            showToast("File too large (" + fmtTotal(selectedFile.size) + "). Limit is " + fmtTotal(MAX_IMPORT_FILE_BYTES) + ".", "error");
            bulkAddFile.value = "";
            return;
        }
        var reader = new FileReader();
        reader.onload = function () {
            // Strip a UTF-8 BOM (Excel adds one to "CSV UTF-8" saves)
            bulkAddText.value = String(reader.result || "").replace(/^\uFEFF/, "");
            bulkAddFileName.textContent = selectedFile.name;
            clearBulkPreview();
            runBulkRequest(true);
        };
        reader.onerror = function () { showToast("Could not read " + selectedFile.name, "error"); };
        reader.readAsText(selectedFile);
    });

    function renderBulkList(container, rows, rowToHtml, heading, headingClass) {
        if (!rows.length) { container.innerHTML = ""; container.classList.add("hidden"); return; }
        container.classList.remove("hidden");
        var html = '<div class="font-sans font-semibold mb-1 ' + headingClass + '">' + heading + ' (' + rows.length + ')</div>';
        var shownRows = rows.slice(0, BULK_PREVIEW_LIST_LIMIT);
        for (var i = 0; i < shownRows.length; i++) html += rowToHtml(shownRows[i]);
        if (rows.length > shownRows.length) {
            html += '<div class="text-gray-500 font-sans mt-1">&hellip; and ' + (rows.length - shownRows.length) + ' more</div>';
        }
        container.innerHTML = html;
    }

    function renderBulkPreview(result) {
        var addRows = result.to_add || [];
        var skipRows = result.skipped || [];
        var formatLabel = { lines: "text", csv: "CSV", json: "JSON" }[result.format] || result.format;
        bulkAddPreview.classList.remove("hidden");
        bulkAddSummary.innerHTML =
            '<span class="text-green-400 font-semibold">' + addRows.length + ' to add</span>' +
            '<span class="text-gray-600 mx-2">&middot;</span>' +
            '<span class="' + (skipRows.length ? "text-amber-400" : "text-gray-500") + '">' + skipRows.length + ' skipped</span>' +
            '<span class="text-gray-600 mx-2">&middot;</span>' +
            '<span class="text-gray-500">read as ' + escapeHtml(formatLabel) + '</span>';
        renderBulkList(bulkAddListAdd, addRows, function (row) {
            var defaultName = (row.host.indexOf(":") !== -1 ? "[" + row.host + "]" : row.host) + ":" + row.port;
            var customName = row.name && row.name !== defaultName
                ? ' <span class="text-gray-500">(' + escapeHtml(row.name) + ')</span>' : "";
            return '<div class="text-gray-300">+ ' + escapeHtml(defaultName) + customName + '</div>';
        }, "Will add", "text-green-400");
        renderBulkList(bulkAddListSkip, skipRows, function (row) {
            var target = row.host ? escapeHtml(row.host + (row.port ? ":" + row.port : "")) : escapeHtml(row.source);
            return '<div class="text-gray-400">&ndash; ' + target + ' <span class="text-amber-400/80">' + escapeHtml(row.reason) + '</span></div>';
        }, "Skipped", "text-amber-400");
    }

    var bulkRequestInFlight = false;
    async function runBulkRequest(isDryRun) {
        if (bulkRequestInFlight) return;
        var inputText = bulkAddText.value;
        if (!inputText.trim()) { showToast("Paste warriors or load a file first", "error"); return; }
        if (!isDryRun && inputText !== bulkPreviewedText) {
            // Text changed after preview: preview again instead of adding blind
            clearBulkPreview();
            return runBulkRequest(true);
        }
        bulkRequestInFlight = true;
        btnBulkPreview.disabled = true;
        btnBulkConfirm.disabled = true;
        try {
            var res = await fetch("/api/instances/bulk", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ text: inputText, dry_run: isDryRun })
            });
            if (!res.ok) {
                clearBulkPreview();
                showToast("Import error: " + (await readErrorDetail(res)), "error");
                return;
            }
            var result = await res.json();
            if (isDryRun) {
                bulkPreviewedText = inputText;
                renderBulkPreview(result);
                setConfirmButton((result.to_add || []).length);
            } else {
                var skippedCount = (result.skipped || []).length;
                showToast("Added " + result.added_count + " warrior" + (result.added_count === 1 ? "" : "s") +
                    (skippedCount ? " (" + skippedCount + " skipped)" : ""), "success");
                closeAddModal();
            }
        } catch (err) {
            showToast("Import error: " + err.message, "error");
        } finally {
            bulkRequestInFlight = false;
            btnBulkPreview.disabled = false;
            if (bulkPreviewedText !== null && bulkAddText.value === bulkPreviewedText) {
                btnBulkConfirm.disabled = btnBulkConfirm.textContent === "Add 0 warriors";
            }
        }
    }
    btnBulkPreview.addEventListener("click", function () { runBulkRequest(true); });
    btnBulkConfirm.addEventListener("click", function () { runBulkRequest(false); });

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
    var CARD_FIXED_WIDTH = "minmax(0, 16.666%)"; // matches 6-column (1/6) card width
    var colBtns = document.querySelectorAll(".col-btn");

    function applyGridColumns(n) {
        n = parseInt(n) || 4;
        if (n < 1) n = 1;
        if (n > 8) n = 8;
        // On mobile (< 768px), always single column
        if (window.innerWidth < 768) {
            grid.style.gridTemplateColumns = "1fr";
        } else {
            grid.style.gridTemplateColumns = "repeat(" + n + ", " + CARD_FIXED_WIDTH + ")";
	    grid.style.justifyContent = "center";
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

    // ---- View controls (filter + grouping), injected beside the column selector ----
    function buildViewControls() {
        if (!colBtns.length) return;
        var columnsRow = colBtns[0].parentElement;
        var hostRow = columnsRow.parentElement || columnsRow;
        var controls = document.createElement("div");
        controls.id = "view-controls";
        controls.className = "flex items-center gap-1 mr-4";
        var buttonClass = "view-btn px-2 py-0.5 text-xs rounded bg-gray-800 text-gray-400 hover:bg-gray-700";
        controls.innerHTML =
            '<span class="text-xs text-gray-500 mr-1">Show:</span>' +
            '<button type="button" class="' + buttonClass + '" data-view-filter="all">All</button>' +
            '<button type="button" class="' + buttonClass + '" data-view-filter="online">Online</button>' +
            '<button type="button" class="' + buttonClass + '" data-view-filter="offline">Offline</button>' +
            '<span class="text-xs text-gray-500 ml-3 mr-1">Group:</span>' +
            '<button type="button" class="' + buttonClass + '" data-view-grouped="0">Flat</button>' +
            '<button type="button" class="' + buttonClass + '" data-view-grouped="1">By project</button>';
        // Insert before the "Columns:" label so it sits left of the column buttons
        var columnsLabel = null;
        for (var i = 0; i < hostRow.children.length; i++) {
            if ((hostRow.children[i].textContent || "").indexOf("Columns") !== -1 &&
                hostRow.children[i].contains(colBtns[0]) === false) { columnsLabel = hostRow.children[i]; break; }
        }
        if (columnsLabel) hostRow.insertBefore(controls, columnsLabel);
        else hostRow.insertBefore(controls, hostRow.firstChild);
        if (!hostRow.classList.contains("flex")) hostRow.classList.add("flex", "items-center", "flex-wrap");

        controls.addEventListener("click", function (e) {
            var filterBtn = e.target.closest("[data-view-filter]");
            if (filterBtn) {
                viewFilter = filterBtn.dataset.viewFilter;
                localStorage.setItem(VIEW_FILTER_KEY, viewFilter);
                updateViewButtons();
                render();
                return;
            }
            var groupBtn = e.target.closest("[data-view-grouped]");
            if (groupBtn) {
                viewGrouped = groupBtn.dataset.viewGrouped === "1";
                localStorage.setItem(VIEW_GROUPED_KEY, viewGrouped ? "1" : "0");
                updateViewButtons();
                render();
            }
        });
        updateViewButtons();
    }

    function updateViewButtons() {
        var buttons = document.querySelectorAll("#view-controls .view-btn");
        for (var i = 0; i < buttons.length; i++) {
            var button = buttons[i];
            var isActive = button.dataset.viewFilter !== undefined
                ? button.dataset.viewFilter === viewFilter
                : (button.dataset.viewGrouped === "1") === viewGrouped;
            button.classList.toggle("bg-blue-600", isActive);
            button.classList.toggle("text-white", isActive);
            button.classList.toggle("bg-gray-800", !isActive);
            button.classList.toggle("text-gray-400", !isActive);
            button.setAttribute("aria-pressed", isActive ? "true" : "false");
        }
    }

    buildViewControls();

    // ---- Event delegation for instance cards ----
grid.addEventListener("click", function (e) {
    var groupToggle = e.target.closest("[data-toggle-group]");
    if (groupToggle) {
        var groupKey = groupToggle.dataset.toggleGroup;
        if (collapsedGroups[groupKey]) delete collapsedGroups[groupKey];
        else collapsedGroups[groupKey] = true;
        localStorage.setItem(COLLAPSED_GROUPS_KEY, JSON.stringify(collapsedGroups));
        render();
        return;
    }
    var editBtn = e.target.closest("[data-edit-instance]");
    if (editBtn) {
        editInstance(editBtn.dataset.editInstance);
        return;
    }
    var removeBtn = e.target.closest("[data-remove-instance]");
    if (removeBtn) {
        removeInstance(removeBtn.dataset.removeInstance);
        return;
    }
});

    // ---- Init ----
    // ---- Version Badge (sessionStorage cached) ----
var CONFIG_CACHE_KEY = "atw-config-cache";
var CONFIG_CACHE_TTL = 300000; // 5 minutes in ms

function loadConfigCached() {
    var cached = sessionStorage.getItem(CONFIG_CACHE_KEY);
    if (cached) {
        try {
            var parsed = JSON.parse(cached);
            if (Date.now() - parsed.timestamp < CONFIG_CACHE_TTL) {
                applyVersionBadge(parsed.data);
                return;
            }
        } catch (ignored) { /* stale or corrupt — fetch fresh */ }
    }
    fetch("/api/config")
        .then(function (res) { return res.json(); })
        .then(function (cfg) {
            if (!cfg) return;
            sessionStorage.setItem(
                CONFIG_CACHE_KEY,
                JSON.stringify({ data: cfg, timestamp: Date.now() })
            );
            applyVersionBadge(cfg);
        })
        .catch(function () {});
}

function applyVersionBadge(cfg) {
    if (!cfg || !cfg.version || cfg.version === "dev") return;
    var badge = document.getElementById("version-badge");
    if (badge) {
        badge.textContent = cfg.version;
        badge.title = "ATW Dashboard version";
        badge.classList.remove("hidden");
    }
}

loadConfigCached();
    connectWebSocket();
    loadProjects();
    initChart();
    loadHistory();
    loadTrackerStats();
    loadPauseStatus();

})();
