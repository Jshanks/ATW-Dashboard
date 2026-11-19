/**
 * ATW Dashboard -- Frontend Application
 *
 * v4.1.0: ArchiveTeam's Choice support, online/offline filter,
 *         group-by-project view (Choice first, then A-Z, then paused/offline).
 * v4.2.0: optional instance name, bulk add / CSV / JSON import with preview,
 *         export link.
 * v4.3.0: select warriors on the cards (checkbox, Shift-click ranges,
 *         Select all, per-group checkboxes); Bulk Settings and Pause/Resume
 *         act on that selection; single-row header.
 * v4.3.1: Settings & Project is a modal opened from the selection bar;
 *         header Pause/Settings buttons removed; more prominent selection bar.
 * v4.3.2: one Apply for project + settings; every field starts "Unchanged";
 *         the button counts pending changes.
 * v4.3.3: mobile layout: toolbar wraps (no page overflow), selection bar
 *         pinned to the visible screen bottom.
 * v4.4.0: immediate warrior restart, restart state on cards, and Choice
 *         labels derived from what Choice warriors actually run.
 * v4.5.0: monitor-only Project Health policy, baseline, canary preview,
 *         persistent decisions, and operator-confirmed manual failover.
 * v4.5.1: editable minimum/full baseline windows and header progress indicator.
 * v4.5.2: rolling health baseline and whole-card Warrior selection.
 * v4.6.0: monitoring control, automatic failover, and destructive baseline reset.
 * v4.6.3: project queue availability in manual and Project Health selectors.
 * v4.6.11: patch changed card regions in place instead of replacing entire cards.
 * v4.6.12: refresh visible cards immediately and throttle offscreen cards.
 * v4.6.13: suspend dashboard rendering while hidden and reconcile on return.
 * v4.7.0: per-Warrior Project Health evidence, fleet placement, and safe switching.
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
    var reconnectTimerId = null;
    var pendingDashboardState = null;
    var renderFrameId = null;
    var instanceFingerprints = {};
    var structuralFingerprints = {};
    var lastCardUpdateTimes = {};
    var visibleInstanceNames = new Set();
    var cardVisibilityObserver = null;
    var OFFSCREEN_CARD_UPDATE_INTERVAL = 60000;
    var pageIsVisible = document.visibilityState === "visible";
    var hiddenStatePending = false;
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
    var projectMetadata = {}; // slug -> structured project record including queue availability

    var grid = document.getElementById("instance-grid");
    var totalOnlineEl = document.getElementById("total-online");
    var totalOfflineEl = document.getElementById("total-offline");
    var totalItemsEl = document.getElementById("total-items");
    var totalDlEl = document.getElementById("total-dl");
    var totalUlEl = document.getElementById("total-ul");
    var wsIndicator = document.getElementById("ws-indicator");
    var wsStatusText = document.getElementById("ws-status-text");
    var settingsPanel = document.getElementById("settings-panel");
    var btnCloseSettings = document.getElementById("btn-close-settings");
    var btnApplySettings = document.getElementById("btn-apply-settings");
    var settingsForm = document.getElementById("settings-form");
    var settingsStatus = document.getElementById("settings-status");
    var settingsSelectionSummary = document.getElementById("settings-selection-summary");
    var addInstanceModal = document.getElementById("add-instance-modal");
    var btnAddInstance = document.getElementById("btn-add-instance");
    var btnCancelAdd = document.getElementById("btn-cancel-add");
    var addInstanceForm = document.getElementById("add-instance-form");
    var editInstanceModal = document.getElementById("edit-instance-modal");
    var editInstanceForm = document.getElementById("edit-instance-form");
    var btnCancelEdit = document.getElementById("btn-cancel-edit");
    var projectSelect = document.getElementById("project-select");
    var settingsDownloaderInput = document.getElementById("settings-downloader");
    var settingsConcurrentSelect = document.getElementById("settings-concurrent");
    var settingsRsyncInput = document.getElementById("settings-rsync");
    var settingsApplyInFlight = false;
    var trackerBar = document.getElementById("tracker-bar");
    var trackerContent = document.getElementById("tracker-stats-content");
    var pauseModal = document.getElementById("pause-modal");
    var btnClosePause = document.getElementById("btn-close-pause");
    var btnPauseSelected = document.getElementById("btn-pause-selected");
    var btnResumeSelected = document.getElementById("btn-resume-selected");
    var pauseSelectionSummary = document.getElementById("pause-selection-summary");
    var connectionBadge = document.getElementById("connection-badge");
    var selectAllVisible = document.getElementById("select-all-visible");
    var selectionCount = document.getElementById("selection-count");
    var selectionToolbarInline = document.getElementById("selection-toolbar-inline");
    var clearSelectionInline = document.getElementById("clear-selection-inline");
    var selectionBar = document.getElementById("selection-bar");
    var selectionBarText = document.getElementById("selection-bar-text");
    var selectionBarCount = document.getElementById("selection-bar-count");
    var selectionHint = document.getElementById("selection-hint");
    var btnSelSettings = document.getElementById("btn-sel-settings");
    var btnSelPause = document.getElementById("btn-sel-pause");
    var btnSelRestart = document.getElementById("btn-sel-restart");
    var restartModal = document.getElementById("restart-modal");
    var restartCountText = document.getElementById("restart-count-text");
    var restartStatus = document.getElementById("restart-status");
    var btnCancelRestart = document.getElementById("btn-cancel-restart");
    var btnConfirmRestart = document.getElementById("btn-confirm-restart");
    var restartRequestInFlight = false;
    var btnSelClear = document.getElementById("btn-sel-clear");

    // ---- Selection state (v4.3.0) ----
    // Not persisted: a page refresh starts with nothing selected, so a bulk
    // action can never hit warriors that were selected in an earlier session.
    var selectedInstances = new Set();
    var lastClickedInstance = null;   // anchor for Shift-click ranges
    var lastGroups = [];              // groups from the latest render (grouped view)
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
    return inst.connection_state + "|" + String(!!inst.restarting) + "|" +
        inst.project_slug + "|" +
        inst.selected_project + "|" +
        inst.bandwidth_down + "|" +
        inst.bandwidth_up + "|" +
        inst.bytes_downloaded + "|" +
        inst.bytes_uploaded + "|" +
        inst.completed_items + "|" +
        inst.error_message + "|" +
        inst.reconnect_attempts + "|" +
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
function updateInstanceCard(card, inst) {
    var template = document.createElement("template");
    template.innerHTML = renderInstanceCard(inst).trim();
    var freshCard = template.content.firstElementChild;
    if (!freshCard) return false;

    card.className = freshCard.className;
    card.setAttribute("aria-selected", freshCard.getAttribute("aria-selected") || "false");

    var slots = ["identity", "project", "metrics", "error", "last-seen"];
    for (var i = 0; i < slots.length; i++) {
        var selector = '[data-card-slot="' + slots[i] + '"]';
        var currentSlot = card.querySelector(selector);
        var freshSlot = freshCard.querySelector(selector);
        if (currentSlot && freshSlot && currentSlot.innerHTML !== freshSlot.innerHTML) {
            currentSlot.innerHTML = freshSlot.innerHTML;
            currentSlot.className = freshSlot.className;
        }
    }
    return true;
}

    // ---- Filter / grouping helpers ----
    function isAutoInstance(inst) {
        return inst.selected_project === AUTO_PROJECT_SLUG;
    }
    function getActualChoiceTargets() {
        var targets = {};
        (dashboardState.instances || []).forEach(function (inst) {
            if (isAutoInstance(inst) && inst.project_slug) targets[inst.project_slug] = true;
        });
        return Object.keys(targets).sort();
    }
    function updateChoiceOptionLabel() {
        var option = projectSelect && projectSelect.querySelector('option[value="auto"]');
        if (!option) return;
        var targets = getActualChoiceTargets();
        option.textContent = "\u2605 " + AUTO_PROJECT_TITLE + (targets.length ? " (\u2192 " + targets.join(", ") + ")" : "");
    }

    function passesFilter(inst) {
        var isOnline = inst.connection_state === "online" || !!inst.restarting;
        if (viewFilter === "online") return isOnline;
        if (viewFilter === "offline") return !isOnline;   // intentional restarts are not outages
        return true;
    }

    function getGroupKey(inst) {
        if (pauseState[inst.name] !== undefined) return GROUP_PAUSED;
        if (inst.connection_state !== "online" && !inst.restarting) return GROUP_OFFLINE;
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
        var policy = projectHealthState && projectHealthState.policy ? projectHealthState.policy : {};
        var projectTitle = projectTitles[groupKey] || groupKey;
        if (groupKey === policy.primary_project) return "Primary · " + projectTitle;
        if (groupKey === policy.fallback_project) return "Secondary · " + projectTitle;
        return projectTitle;
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
            '<span class="text-xs text-blue-400" data-group-selected></span>' +
            '<button type="button" data-select-group="' + escapeHtml(group.key) + '" class="group-select-btn ml-auto text-xs text-indigo-300 hover:text-indigo-200">Select group</button>' +
            '</div>';
    }

    // Layout key changes whenever card ORDER/VISIBILITY must change -> full rebuild
    function computeLayoutKey(instances, groups) {
        var policy = projectHealthState && projectHealthState.policy ? projectHealthState.policy : {};
        var key = viewFilter + "|" + (viewGrouped ? "g" : "f") + "|" + (policy.primary_project || "") + "|" + (policy.fallback_project || "") + "|";
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

    // ---- Selection helpers ----
    function getVisibleInstances() {
        var instances = dashboardState.instances || [];
        return instances.filter(passesFilter);
    }

    // Names that are selected AND still exist, in dashboard order
    function getSelectedNames() {
        var instances = dashboardState.instances || [];
        var names = [];
        for (var i = 0; i < instances.length; i++) {
            if (selectedInstances.has(instances[i].name)) names.push(instances[i].name);
        }
        return names;
    }

    function countSelectedIn(instanceList) {
        var count = 0;
        for (var i = 0; i < instanceList.length; i++) {
            if (selectedInstances.has(instanceList[i].name)) count++;
        }
        return count;
    }

    // Select all of the given instances, or clear them if they are all selected already
    function toggleSelectionFor(instanceList) {
        if (!instanceList.length) return;
        var selectAll = countSelectedIn(instanceList) < instanceList.length;
        for (var i = 0; i < instanceList.length; i++) {
            if (selectAll) selectedInstances.add(instanceList[i].name);
            else selectedInstances.delete(instanceList[i].name);
            syncCardSelection(instanceList[i].name);
        }
        updateSelectionUI();
    }

    function setInstanceSelected(name, isSelected) {
        if (isSelected) selectedInstances.add(name);
        else selectedInstances.delete(name);
        syncCardSelection(name);
    }

    // Update one card's visual and accessible selection state without re-rendering it
    function syncCardSelection(name) {
        var card = findCardByName(name);
        if (!card) return;
        var isSelected = selectedInstances.has(name);
        card.classList.toggle("card-selected", isSelected);
        card.setAttribute("aria-selected", isSelected ? "true" : "false");
        var indicator = card.querySelector("[data-selection-indicator]");
        if (indicator) indicator.classList.toggle("hidden", !isSelected);
    }

    // Drop selections for warriors that no longer exist
    function pruneSelection() {
        var existingNames = new Set((dashboardState.instances || []).map(function (i) { return i.name; }));
        selectedInstances.forEach(function (name) {
            if (!existingNames.has(name)) selectedInstances.delete(name);
        });
        if (lastClickedInstance && !existingNames.has(lastClickedInstance)) lastClickedInstance = null;
    }

    // Drop selections hidden by the Show filter (called when the filter changes)
    function pruneSelectionToVisible() {
        var visibleNames = new Set(getVisibleInstances().map(function (i) { return i.name; }));
        selectedInstances.forEach(function (name) {
            if (!visibleNames.has(name)) selectedInstances.delete(name);
        });
    }

    function describeSelection(selectedNames) {
        var running = 0, paused = 0, offline = 0;
        var instancesByName = {};
        (dashboardState.instances || []).forEach(function (i) { instancesByName[i.name] = i; });
        for (var i = 0; i < selectedNames.length; i++) {
            var inst = instancesByName[selectedNames[i]];
            if (!inst) continue;
            if (pauseState[inst.name] !== undefined) paused++;
            else if (inst.connection_state === "online") running++;
            else offline++;
        }
        var parts = [];
        if (running) parts.push(running + " running");
        if (paused) parts.push(paused + " paused");
        if (offline) parts.push(offline + " offline");
        return { running: running, paused: paused, offline: offline, text: parts.join(" \u00B7 ") };
    }

    // Refresh every selection-dependent control (cheap; safe to call often)
    function updateSelectionUI() {
        var visibleInstances = getVisibleInstances();
        var visibleSelected = countSelectedIn(visibleInstances);
        var selectedNames = getSelectedNames();
        var totalSelected = selectedNames.length;
        var hiddenSelected = totalSelected - visibleSelected;

        selectionToolbarInline.classList.toggle("hidden", totalSelected === 0);
        selectionToolbarInline.classList.toggle("flex", totalSelected > 0);
        selectAllVisible.textContent = visibleSelected === visibleInstances.length && visibleInstances.length ? "Clear visible" : "Select all visible";
        selectionCount.textContent = totalSelected
            ? totalSelected + " of " + (dashboardState.instances || []).length + " selected"
            : "";

        // Group header checkboxes and "N selected" labels
        if (viewGrouped) {
            for (var g = 0; g < lastGroups.length; g++) {
                var header = grid.querySelector('[data-group-header="' + cssEscape(lastGroups[g].key) + '"]');
                if (!header) continue;
                var groupSelected = countSelectedIn(lastGroups[g].members);
                var groupButton = header.querySelector("[data-select-group]");
                if (groupButton) groupButton.textContent = groupSelected === lastGroups[g].members.length && lastGroups[g].members.length ? "Clear group" : (groupSelected ? "Select remaining" : "Select group");
                var groupSelectedLabel = header.querySelector("[data-group-selected]");
                if (groupSelectedLabel) {
                    groupSelectedLabel.textContent = groupSelected
                        ? groupSelected + " of " + lastGroups[g].members.length + " selected" : "";
                }
            }
        }

        // Floating selection bar
        if (totalSelected > 0) {
            var barText = "warrior" + (totalSelected === 1 ? "" : "s") + " selected";
            if (hiddenSelected > 0) barText += " \u00B7 " + hiddenSelected + " hidden by filter";
            selectionBarCount.textContent = totalSelected;
            selectionBarText.textContent = barText;
            if (selectionBar.classList.contains("hidden")) {
                selectionBar.classList.remove("hidden");
                // Restart the slide-in animation each time the bar appears
                selectionBar.classList.remove("sel-bar-enter");
                void selectionBar.offsetWidth;
                selectionBar.classList.add("sel-bar-enter");
            }
            document.body.classList.add("has-selection");
        } else {
            selectionBar.classList.add("hidden");
            document.body.classList.remove("has-selection");
        }
        if (selectionHint) selectionHint.classList.toggle("hidden", totalSelected > 0);

        // Bulk Settings panel + Pause modal summaries
        var selectionInfo = describeSelection(selectedNames);
        var noneSelectedHtml = '<span class="text-amber-400">No warriors selected.</span> ' +
            '<span class="text-gray-500">Tick the checkbox on each card, Shift-click for a range, or use <strong>Select all</strong>.</span>';
        var selectedHtml = '<span class="text-gray-300">Applies to <strong class="text-white">' + totalSelected +
            ' selected warrior' + (totalSelected === 1 ? "" : "s") + '</strong></span>' +
            (selectionInfo.text ? ' <span class="text-gray-500">(' + escapeHtml(selectionInfo.text) + ')</span>' : "");
        settingsSelectionSummary.innerHTML = totalSelected ? selectedHtml : noneSelectedHtml;
        pauseSelectionSummary.innerHTML = totalSelected ? selectedHtml : noneSelectedHtml;
        updateApplyButton();
        btnPauseSelected.disabled = selectionInfo.running + selectionInfo.offline === 0;
        btnResumeSelected.disabled = selectionInfo.paused === 0;
        var restartEligible = (dashboardState.instances || []).filter(function (inst) {
            return selectedInstances.has(inst.name) && inst.connection_state === "online" && !inst.restarting;
        }).length;
        if (btnSelRestart) btnSelRestart.disabled = restartEligible === 0 || restartRequestInFlight;
    }

    // ---- WebSocket ----
    function queueDashboardState(state) {
        // Always retain the latest fleet state, but do no dashboard DOM work
        // while the tab is hidden. Intermediate hidden states are discarded.
        dashboardState = state;
        if (!pageIsVisible) {
            pendingDashboardState = null;
            hiddenStatePending = true;
            if (renderFrameId !== null) {
                window.cancelAnimationFrame(renderFrameId);
                renderFrameId = null;
            }
            return;
        }
        pendingDashboardState = state;
        if (renderFrameId !== null) return;
        renderFrameId = window.requestAnimationFrame(function () {
            var stateToRender = pendingDashboardState;
            pendingDashboardState = null;
            renderFrameId = null;
            if (!stateToRender || !pageIsVisible) {
                if (!pageIsVisible) hiddenStatePending = true;
                return;
            }
            dashboardState = stateToRender;
            hiddenStatePending = false;
            render();
        });
    }
    function handlePageVisibilityChange() {
        pageIsVisible = document.visibilityState === "visible";
        if (!pageIsVisible) {
            hiddenStatePending = true;
            pendingDashboardState = null;
            if (renderFrameId !== null) {
                window.cancelAnimationFrame(renderFrameId);
                renderFrameId = null;
            }
            return;
        }
        loadProjectHealth();
        if (!hiddenStatePending) return;
        hiddenStatePending = false;
        // Prevent an overdue telemetry-only refresh from touching every offscreen
        // card at once. Structural changes still update immediately, and cards
        // synchronize as they enter the observer's preload region.
        var visibleAgainTime = Date.now();
        var instances = dashboardState.instances || [];
        for (var i = 0; i < instances.length; i++) {
            if (!visibleInstanceNames.has(instances[i].name)) {
                lastCardUpdateTimes[instances[i].name] = visibleAgainTime;
            }
        }
        render();
    }
    document.addEventListener("visibilitychange", handlePageVisibilityChange);
    function setConnectionStatus(text) {
        wsStatusText.textContent = text;
        // Status text is visually hidden on narrower headers; keep it as a tooltip
        if (connectionBadge) connectionBadge.title = "Dashboard connection: " + text;
    }

    function connectWebSocket() {
        var protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        var wsUrl = protocol + "//" + window.location.host + "/ws";
        ws = new WebSocket(wsUrl);
        ws.onopen = function () {
            if (reconnectTimerId !== null) {
                clearTimeout(reconnectTimerId);
                reconnectTimerId = null;
            }
            wsReconnectDelay = 1000;
            wsIndicator.className = "w-2 h-2 rounded-full bg-green-500 pulse-online";
            setConnectionStatus("Connected");
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
        queueDashboardState(data);
    } catch (e) { console.error("WS parse error:", e); }
};
        ws.onclose = function () {
            wsIndicator.className = "w-2 h-2 rounded-full bg-red-500";
            setConnectionStatus("Disconnected");
            scheduleReconnect();
        };
        ws.onerror = function () {
            wsIndicator.className = "w-2 h-2 rounded-full bg-red-500";
            setConnectionStatus("Error");
        };
        if (pingIntervalId !== null) { clearInterval(pingIntervalId); }
        pingIntervalId = setInterval(function () {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: "ping" }));
            }
        }, 25000);
    }
    function scheduleReconnect() {
        if (reconnectTimerId !== null) return;
        reconnectTimerId = setTimeout(function () {
            reconnectTimerId = null;
            wsIndicator.className = "w-2 h-2 rounded-full bg-yellow-500 pulse-connecting";
            setConnectionStatus("Reconnecting...");
            connectWebSocket();
        }, wsReconnectDelay);
        wsReconnectDelay = Math.min(wsReconnectDelay * 2, MAX_WS_RECONNECT);
    }

    // ---- Rendering ----
    function getStructuralFingerprint(inst) {
        var itemStates = (inst.items || []).map(function (item) { return item.state; }).join(",");
        var pauseEntry = pauseState[inst.name];
        var pauseKey = pauseEntry ? String(pauseEntry.resume_at || "paused") : "running";
        return [
            inst.connection_state,
            inst.restarting ? "1" : "0",
            inst.current_project || "",
            inst.project_slug || "",
            inst.selected_project || "",
            inst.error_message || "",
            inst.reconnect_attempts || 0,
            pauseKey,
            itemStates
        ].join("|");
    }

    function findLatestInstance(name) {
        var instances = dashboardState.instances || [];
        for (var i = 0; i < instances.length; i++) {
            if (instances[i].name === name) return instances[i];
        }
        return null;
    }

    function refreshCardVisibilityObserver() {
        visibleInstanceNames.clear();
        if (cardVisibilityObserver) cardVisibilityObserver.disconnect();
        if (!("IntersectionObserver" in window)) {
            var fallbackCards = grid.querySelectorAll("[data-instance-name]");
            for (var f = 0; f < fallbackCards.length; f++) {
                visibleInstanceNames.add(fallbackCards[f].dataset.instanceName);
            }
            return;
        }
        cardVisibilityObserver = new IntersectionObserver(function (entries) {
            for (var i = 0; i < entries.length; i++) {
                var card = entries[i].target;
                var name = card.dataset.instanceName;
                if (!name) continue;
                if (entries[i].isIntersecting) {
                    visibleInstanceNames.add(name);
                    var latest = findLatestInstance(name);
                    if (!latest) continue;
                    var latestFingerprint = getInstanceFingerprint(latest);
                    if (instanceFingerprints[name] !== latestFingerprint) {
                        updateInstanceCard(card, latest);
                        instanceFingerprints[name] = latestFingerprint;
                        structuralFingerprints[name] = getStructuralFingerprint(latest);
                        lastCardUpdateTimes[name] = Date.now();
                    }
                } else {
                    visibleInstanceNames.delete(name);
                }
            }
        }, { root: null, rootMargin: "240px 0px", threshold: 0 });

        var cards = grid.querySelectorAll("[data-instance-name]");
        for (var c = 0; c < cards.length; c++) cardVisibilityObserver.observe(cards[c]);
    }

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
    updateChoiceOptionLabel();

    if (!state.instances || state.instances.length === 0) {
        grid.innerHTML = '<div class="col-span-full text-center text-gray-500 py-12">' +
            '<p class="text-lg">No warrior instances yet.</p>' +
            '<p class="text-sm mt-2">Click <strong>Add Instance</strong> to connect your first warrior.</p></div>';
        instanceFingerprints = {};
        structuralFingerprints = {};
        lastCardUpdateTimes = {};
        visibleInstanceNames.clear();
        if (cardVisibilityObserver) cardVisibilityObserver.disconnect();
        knownInstances = new Set();
        selectedInstances.clear();
        lastGroups = [];
        updateSelectionUI();
        return;
    }

    var currentNames = new Set(state.instances.map(function (i) { return i.name; }));
    var structureChanged = !setsEqual(currentNames, knownInstances);
    if (structureChanged) {
        knownInstances = currentNames;
        pruneSelection();
    }

    var groups = viewGrouped ? buildGroups(state.instances) : [];
    lastGroups = groups;
    var layoutKey = computeLayoutKey(state.instances, groups);

    if (structureChanged || layoutKey !== lastLayoutKey) {
        // Full rebuild: instances added/removed, filter/grouping changed,
        // a warrior moved between groups, or a group was collapsed/expanded
        var html = "";
        instanceFingerprints = {};
        structuralFingerprints = {};
        lastCardUpdateTimes = {};
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
        var rebuildTime = Date.now();
        for (var f = 0; f < state.instances.length; f++) {
            var rebuiltInstance = state.instances[f];
            instanceFingerprints[rebuiltInstance.name] = getInstanceFingerprint(rebuiltInstance);
            structuralFingerprints[rebuiltInstance.name] = getStructuralFingerprint(rebuiltInstance);
            lastCardUpdateTimes[rebuiltInstance.name] = rebuildTime;
        }
        if (visibleCount === 0 && !(viewGrouped && groups.length > 0)) {
            html = '<div class="text-center text-gray-500 py-12" style="grid-column: 1 / -1;">' +
                '<p class="text-sm">No warriors match the <strong>' + escapeHtml(viewFilter) + '</strong> filter.</p></div>';
        }
        grid.innerHTML = html;
        refreshCardVisibilityObserver();
        lastLayoutKey = layoutKey;
        updateSelectionUI();
        return;
    }

    // Visible/near-visible cards update immediately. Offscreen telemetry-only
    // changes are coalesced and applied at most once per minute. Structural
    // changes still apply immediately regardless of visibility.
    var targetedUpdateTime = Date.now();
    for (var idx2 = 0; idx2 < state.instances.length; idx2++) {
        var changedInst = state.instances[idx2];
        var name = changedInst.name;
        var fingerprint = getInstanceFingerprint(changedInst);
        if (instanceFingerprints[name] === fingerprint) continue;

        var card = findCardByName(name);
        if (!card) {
            instanceFingerprints[name] = fingerprint;
            structuralFingerprints[name] = getStructuralFingerprint(changedInst);
            continue;
        }

        var structuralFingerprint = getStructuralFingerprint(changedInst);
        var structureChangedForCard = structuralFingerprints[name] !== structuralFingerprint;
        var cardIsVisible = visibleInstanceNames.has(name);
        var lastUpdate = lastCardUpdateTimes[name] || 0;
        var offscreenUpdateDue = targetedUpdateTime - lastUpdate >= OFFSCREEN_CARD_UPDATE_INTERVAL;

        if (!structureChangedForCard && !cardIsVisible && !offscreenUpdateDue) continue;

        updateInstanceCard(card, changedInst);
        instanceFingerprints[name] = fingerprint;
        structuralFingerprints[name] = structuralFingerprint;
        lastCardUpdateTimes[name] = targetedUpdateTime;
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
    updateSelectionUI();
}

    function cssEscape(value) {
        if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(value);
        return String(value).replace(/["\\]/g, "\\$&");
    }

    function renderInstanceCard(inst) {
        var isRestarting = !!inst.restarting;
        var stateClass = isRestarting ? "border-blue-400 restarting-card" : "border-state-" + inst.connection_state;
        var isOnline = inst.connection_state === "online";
        var isPaused = pauseState[inst.name] !== undefined;
        var statusDot = isRestarting
            ? '<span class="restart-spinner" title="Restarting" aria-label="Restarting"></span>'
            : isOnline
            ? '<span class="w-2.5 h-2.5 rounded-full bg-green-500 pulse-online" title="Online"></span>'
            : inst.connection_state === "connecting"
                ? '<span class="w-2.5 h-2.5 rounded-full bg-yellow-500 pulse-connecting" title="Connecting"></span>'
                : inst.connection_state === "auth_failed"
                    ? '<span class="w-2.5 h-2.5 rounded-full bg-purple-500" title="Auth Failed"></span>'
                    : '<span class="w-2.5 h-2.5 rounded-full bg-red-500" title="Offline"></span>';
        var projectBadgeHtml = "";
        if (isRestarting) {
            projectBadgeHtml = '<span class="restart-badge">Restarting\u2026</span>';
        } else if (isOnline && isAutoInstance(inst)) {
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
        var editBtn = '<button data-edit-instance="' + escapeHtml(inst.name) + '" class="text-gray-600 hover:text-blue-400 " title="Edit" aria-label="Edit ' + escapeHtml(inst.name) + '"><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg></button>';
        var removeBtn = '<button data-remove-instance="' + escapeHtml(inst.name) + '" class="text-gray-600 hover:text-red-400 " title="Remove" aria-label="Remove ' + escapeHtml(inst.name) + '"><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg></button>';
        var isSelected = selectedInstances.has(inst.name);
        var selectBox = '<span data-selection-indicator class="card-selection-indicator' + (isSelected ? '' : ' hidden') + '" aria-hidden="true">&#10003;</span>';

        return '<div class="instance-card bg-gray-900 border-l-4 ' + stateClass + ' rounded-lg p-4 relative group' + (isSelected ? " card-selected" : "") + '" data-instance-name="' + escapeHtml(inst.name) + '" role="option" tabindex="0" aria-selected="' + (isSelected ? "true" : "false") + '">' +
            // Name row: the name truncates instead of running under the badge/buttons
            // (host:port default names are long on narrow cards)
            '<div data-card-slot="identity" class="flex items-center gap-2 ' + (projectBadgeHtml ? "mb-1.5" : "mb-2") + '">' + selectBox + statusDot +
                '<h3 class="font-semibold text-sm truncate min-w-0" title="' + escapeHtml(inst.name) + '">' + escapeHtml(inst.name) + '</h3>' + reconnectInfo +
            '</div>' +
            // Edit/remove overlay the end of the name row on hover, so they never take width from the name
            '<div class="card-actions absolute top-3 right-3 flex items-center gap-1.5 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">' + editBtn + removeBtn + '</div>' +
            '<div data-card-slot="project" class="' + (projectBadgeHtml ? "mb-2 flex" : "hidden") + '">' + projectBadgeHtml + '</div>' +
            '<div data-card-slot="metrics" class="text-xs text-gray-400 space-y-0.5">' +
'<p><span class="text-gray-500">URL:</span> <a href="' + escapeHtml(inst.url) + '" target="_blank" class="text-blue-400 hover:text-blue-300 hover:underline">' + escapeHtml(inst.url) + '</a></p>' +
            (isOnline && !isPaused ? '<p><span class="text-gray-500">Items:</span> ' + itemsLine + '</p>' + doneHtml + bwHtml : "") +
            '</div>' +
            '<div data-card-slot="error">' + errorMsg + '</div>' +
            '<div data-card-slot="last-seen">' + (!isOnline && !isRestarting && inst.last_seen ? '<p class="text-xs text-gray-600 mt-2">Last seen: ' + formatTime(inst.last_seen) + '</p>' : "") + '</div>' +
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
        pauseActionStatus.textContent = "";
        pauseActionStatus.className = "text-sm mb-3";
        updateSelectionUI();
    }
    btnClosePause.addEventListener("click", function () { pauseModal.classList.add("hidden"); });
    btnPauseSelected.addEventListener("click", async function () {
        var selected = getSelectedNames().filter(function (name) { return !pauseState[name]; });
        if (!selected.length) { showToast("No running warriors selected", "error"); return; }
        var duration = pauseDuration.value ? parseFloat(pauseDuration.value) : null;
        pauseActionStatus.className = "text-sm mb-3 text-yellow-400";
        var result = await runBulkBatches(selected, "Pausing", pauseActionStatus, async function (batch) {
            var response = await fetch("/api/pause", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ instance_names: batch, duration_hours: duration }) });
            if (!response.ok) throw new Error(await readErrorDetail(response));
            return countResults(await response.json());
        });
        pauseActionStatus.textContent = result.ok + " paused" + (result.failed ? ", " + result.failed + " failed" : "");
        pauseActionStatus.className = "text-sm mb-3 " + (result.failed ? "text-yellow-400" : "text-green-400");
        showToast("Paused " + result.ok + " instance(s)", result.failed ? "error" : "success");
        loadPauseStatus(); setTimeout(updateSelectionUI, 500);
    });
    btnResumeSelected.addEventListener("click", async function () {
        var selected = getSelectedNames().filter(function (name) { return !!pauseState[name]; });
        if (!selected.length) { showToast("No paused warriors selected", "error"); return; }
        pauseActionStatus.className = "text-sm mb-3 text-yellow-400";
        var result = await runBulkBatches(selected, "Resuming", pauseActionStatus, async function (batch) {
            var response = await fetch("/api/resume", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ instance_names: batch }) });
            if (!response.ok) throw new Error(await readErrorDetail(response));
            return countResults(await response.json());
        });
        pauseActionStatus.textContent = result.ok + " resumed" + (result.failed ? ", " + result.failed + " failed" : "");
        pauseActionStatus.className = "text-sm mb-3 " + (result.failed ? "text-yellow-400" : "text-green-400");
        showToast("Resumed " + result.ok + " instance(s)", result.failed ? "error" : "success");
        loadPauseStatus(); setTimeout(updateSelectionUI, 500);
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
    loadProjectHealth();
        } catch (e) { showToast("Failed: " + e.message, "error"); }
    });

    // ---- Settings Panel ----
    function openSettingsPanel() {
        // Every open starts from "Unchanged" so nothing carries over from last time
        settingsForm.reset();
        setSettingsStatus("", "");
        settingsPanel.classList.remove("hidden");
        updateSelectionUI();
        projectSelect.focus();
    }
    function closeSettingsPanel() { settingsPanel.classList.add("hidden"); }
    btnCloseSettings.addEventListener("click", closeSettingsPanel);
    // Click on the dimmed backdrop closes the modal
    settingsPanel.addEventListener("click", function (e) { if (e.target === settingsPanel) closeSettingsPanel(); });
    pauseModal.addEventListener("click", function (e) { if (e.target === pauseModal) pauseModal.classList.add("hidden"); });

    // Esc: close the open modal; with no modal open, clear the selection
    document.addEventListener("keydown", function (e) {
        if (e.key !== "Escape") return;
        if (!settingsPanel.classList.contains("hidden")) { closeSettingsPanel(); return; }
        if (!pauseModal.classList.contains("hidden")) { pauseModal.classList.add("hidden"); return; }
        if (restartModal && !restartModal.classList.contains("hidden")) { closeRestartModal(); return; }
        if (!addInstanceModal.classList.contains("hidden")) return;   // handled by the Add modal
        if (selectedInstances.size > 0) btnSelClear.click();
    });
    // ---- Settings & Project: pending changes + single Apply ----
    var MIN_RSYNC_THREADS = 1;
    var MAX_RSYNC_THREADS = 40;

    function setSettingsStatus(message, tone) {
        var toneClass = { info: "text-yellow-400", ok: "text-green-400", warn: "text-amber-400", error: "text-red-400" }[tone] || "text-gray-400";
        settingsStatus.textContent = message;
        settingsStatus.className = "text-sm mt-4 min-h-[1.25rem] " + toneClass;
    }

    function getProjectLabel(slug) {
        var option = projectSelect.querySelector('option[value="' + cssEscape(slug) + '"]');
        return option ? option.textContent : slug;
    }

    // Read the form. Empty = unchanged. Returns { project, settings, labels[], error }
    function collectPendingChanges() {
        var pending = { project: "", settings: {}, labels: [], error: "" };

        if (projectSelect.value) {
            pending.project = projectSelect.value;
            pending.labels.push("Project \u2192 " + getProjectLabel(projectSelect.value));
        }

        var nickname = settingsDownloaderInput.value.trim();
        if (nickname) {
            pending.settings.downloader = nickname;
            pending.labels.push("Nickname \u2192 " + nickname);
        }

        if (settingsConcurrentSelect.value) {
            pending.settings.concurrent_items = parseInt(settingsConcurrentSelect.value, 10);
            pending.labels.push("Concurrent items \u2192 " + settingsConcurrentSelect.value);
        }

        var rsyncText = settingsRsyncInput.value.trim();
        // A number input reports "" for unparseable text; badInput tells us it wasn't really empty
        if (rsyncText || (settingsRsyncInput.validity && settingsRsyncInput.validity.badInput)) {
            var rsyncThreads = Number(rsyncText);
            if (!rsyncText || !Number.isInteger(rsyncThreads) || rsyncThreads < MIN_RSYNC_THREADS || rsyncThreads > MAX_RSYNC_THREADS) {
                pending.error = "Rsync threads must be a whole number from " + MIN_RSYNC_THREADS + " to " + MAX_RSYNC_THREADS + ".";
            } else {
                pending.settings.shared_rsync_threads = rsyncThreads;
                pending.labels.push("Rsync threads \u2192 " + rsyncThreads);
            }
        }
        return pending;
    }

    function updateApplyButton() {
        if (!btnApplySettings) return;
        if (settingsApplyInFlight) return;   // keep "Applying…" until done
        var pending = collectPendingChanges();
        var selectedCount = selectedInstances.size;
        var changeCount = pending.labels.length;

        settingsRsyncInput.classList.toggle("border-red-500", !!pending.error);
        // Dropdowns showing "Unchanged" use the same grey as the text-field placeholders
        projectSelect.classList.toggle("is-unchanged", !projectSelect.value);
        settingsConcurrentSelect.classList.toggle("is-unchanged", !settingsConcurrentSelect.value);
        if (pending.error) {
            btnApplySettings.disabled = true;
            btnApplySettings.textContent = "Fix rsync threads";
            btnApplySettings.title = pending.error;
            return;
        }
        btnApplySettings.disabled = changeCount === 0 || selectedCount === 0;
        btnApplySettings.textContent = changeCount === 0
            ? "No changes"
            : "Apply " + changeCount + " change" + (changeCount === 1 ? "" : "s");
        btnApplySettings.title = changeCount === 0
            ? "Change a field to enable Apply"
            : pending.labels.join("\n");
    }

    settingsForm.addEventListener("input", updateApplyButton);
    settingsForm.addEventListener("change", updateApplyButton);

    function countResults(responseData) {
        var results = (responseData && responseData.results) ? Object.values(responseData.results) : [];
        var okCount = results.filter(function (r) { return r.status === "ok"; }).length;
        return { ok: okCount, failed: results.length - okCount };
    }

    var BULK_ACTION_BATCH_SIZE = 10;

    function bulkActionProgressText(action, startIndex, batchLength, total) {
        return action + " instances " + (startIndex + 1) + "–" + (startIndex + batchLength) + " of " + total;
    }

    async function runBulkBatches(instanceNames, action, statusElement, requestBatch) {
        var aggregate = { ok: 0, failed: 0 };
        for (var startIndex = 0; startIndex < instanceNames.length; startIndex += BULK_ACTION_BATCH_SIZE) {
            var batch = instanceNames.slice(startIndex, startIndex + BULK_ACTION_BATCH_SIZE);
            statusElement.textContent = bulkActionProgressText(action, startIndex, batch.length, instanceNames.length);
            try {
                var result = await requestBatch(batch);
                aggregate.ok += Number(result.ok || 0);
                aggregate.failed += Number(result.failed || 0);
            } catch (error) {
                aggregate.failed += batch.length;
                console.error(action + " batch failed:", error);
            }
        }
        return aggregate;
    }

    async function postBulk(url, body) {
        var res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        if (!res.ok) throw new Error(await readErrorDetail(res));
        return countResults(await res.json());
    }

    // Enter in any field or clicking Apply submits the form
    settingsForm.addEventListener("submit", async function (e) {
        e.preventDefault();
        if (settingsApplyInFlight || btnApplySettings.disabled) return;
        var selected = getSelectedNames();
        if (selected.length === 0) { setSettingsStatus("No warriors selected.", "error"); return; }
        var pending = collectPendingChanges();
        if (pending.error) { setSettingsStatus(pending.error, "error"); return; }
        if (!pending.labels.length) return;

        var hasSettings = Object.keys(pending.settings).length > 0;
        var outcomeParts = [];
        var anyProblem = false;

        settingsApplyInFlight = true;
        btnApplySettings.disabled = true;
        btnApplySettings.textContent = "Applying\u2026";
        setSettingsStatus("Applying to " + selected.length + " warrior" + (selected.length === 1 ? "" : "s") + "\u2026", "info");

        try {
            if (hasSettings) {
                var settingsResult = await runBulkBatches(selected, "Applying settings to", settingsStatus, function (batch) {
                    return postBulk("/api/settings/bulk", { instance_names: batch, settings: pending.settings });
                });
                outcomeParts.push("Settings: " + settingsResult.ok + " updated" + (settingsResult.failed ? ", " + settingsResult.failed + " failed" : ""));
                if (settingsResult.failed) anyProblem = true;
            }
            if (pending.project) {
                var projectResult = await runBulkBatches(selected, "Applying project to", settingsStatus, function (batch) {
                    return postBulk("/api/project/bulk", { instance_names: batch, project_name: pending.project });
                });
                outcomeParts.push("Project: " + projectResult.ok + " switched" + (projectResult.failed ? ", " + projectResult.failed + " failed" : ""));
                if (projectResult.failed) anyProblem = true;
            }
        } finally {
            settingsApplyInFlight = false;
        }

        var outcomeText = outcomeParts.join(" \u00B7 ");
        if (anyProblem) {
            // Stay open so the user can see what failed and retry
            setSettingsStatus(outcomeText, "warn");
            updateApplyButton();
            showToast(outcomeText, "error");
        } else {
            showToast(outcomeText, "success");
            closeSettingsPanel();
        }
    });
    // ---- Projects (sessionStorage cached) ----
var PROJECTS_CACHE_KEY = "atw-projects-cache-v3";  // v3: structured queue metadata
var PROJECTS_CACHE_TTL = 1800000; // 30 minutes; backend owns the same refresh interval

function loadProjects() {
    var cached = sessionStorage.getItem(PROJECTS_CACHE_KEY);
    if (cached) {
        try {
            var parsed = JSON.parse(cached);
            if (Date.now() - parsed.timestamp < PROJECTS_CACHE_TTL) {
                applyProjectList(parsed.data);
                // Display cached data immediately, then revalidate in the background.
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

function formatTodoItems(items) {
    if (items === null || items === undefined || !Number.isFinite(Number(items))) return null;
    var value = Math.max(0, Number(items));
    if (value >= 1000000000) return (value / 1000000000).toFixed(value >= 10000000000 ? 1 : 2).replace(/\.?0+$/, "") + "B";
    if (value >= 1000000) return (value / 1000000).toFixed(value >= 100000000 ? 1 : 2).replace(/\.?0+$/, "") + "M";
    if (value >= 1000) return (value / 1000).toFixed(value >= 100000 ? 0 : 1).replace(/\.?0+$/, "") + "K";
    return Math.round(value).toLocaleString();
}

function projectSelectorLabel(slug, fallbackName) {
    if (slug === AUTO_PROJECT_SLUG) return fallbackName;
    var metadata = projectMetadata[slug];
    if (!metadata) return fallbackName;
    var formattedCount = formatTodoItems(metadata.todo_items);
    if (formattedCount === null) return fallbackName + " · to do unavailable";
    return fallbackName + " · " + formattedCount + " to do" + (metadata.todo_stale ? " (stale)" : "");
}

function applyProjectList(data) {
    if (!Array.isArray(data)) return;
    // Backend already orders the list: ArchiveTeam's Choice first, then A-Z
    var previousValue = projectSelect.value;   // keep a choice made before a refresh
    var html = '<option value="">Unchanged</option>';
    for (var i = 0; i < data.length; i++) {
        var item = data[i];
        var slug = typeof item === "string" ? item : (item.slug || item.name || String(item));
        var displayName = typeof item === "string" ? item : (item.name || item.slug || String(item));
        if (slug === AUTO_PROJECT_SLUG) {
            displayName = "\u2605 " + AUTO_PROJECT_TITLE;
            var actualTargets = getActualChoiceTargets();
            if (actualTargets.length) displayName += " (\u2192 " + actualTargets.join(", ") + ")";
            else if (item.resolves_to) displayName += " (\u2192 " + item.resolves_to + ")";
        }
        if (typeof item === "object") {
            projectMetadata[slug] = item;
            if (item.title) projectTitles[slug] = item.title;
        }
        displayName = projectSelectorLabel(slug, displayName);
        html += '<option value="' + escapeHtml(slug) + '">' + escapeHtml(displayName) + '</option>';
    }
    projectSelect.innerHTML = html;
    projectSelect.value = previousValue;
    if (projectSelect.value !== previousValue) projectSelect.value = "";
    updateApplyButton();
}
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
        var viewToolbar = document.getElementById("view-toolbar");
        var columnsControl = document.getElementById("columns-control");
        if (!viewToolbar) return;
        var controls = document.createElement("div");
        controls.id = "view-controls";
        // Two nowrap clusters inside a wrapping row, so phones get two short lines
        controls.className = "flex items-center gap-x-4 gap-y-2 flex-wrap";
        var buttonClass = "view-btn px-2 py-0.5 text-xs rounded bg-gray-800 text-gray-400 hover:bg-gray-700";
        controls.innerHTML =
            '<div class="flex items-center gap-1 whitespace-nowrap">' +
                '<span class="text-xs text-gray-500 mr-1">Show:</span>' +
                '<button type="button" class="' + buttonClass + '" data-view-filter="all">All</button>' +
                '<button type="button" class="' + buttonClass + '" data-view-filter="online">Online</button>' +
                '<button type="button" class="' + buttonClass + '" data-view-filter="offline">Offline</button>' +
            '</div>' +
            '<div class="flex items-center gap-1 whitespace-nowrap">' +
                '<span class="text-xs text-gray-500 mr-1">Group:</span>' +
                '<button type="button" class="' + buttonClass + '" data-view-grouped="0">Flat</button>' +
                '<button type="button" class="' + buttonClass + '" data-view-grouped="1">By project</button>' +
            '</div>';
        viewToolbar.insertBefore(controls, columnsControl || null);

        controls.addEventListener("click", function (e) {
            var filterBtn = e.target.closest("[data-view-filter]");
            if (filterBtn) {
                viewFilter = filterBtn.dataset.viewFilter;
                localStorage.setItem(VIEW_FILTER_KEY, viewFilter);
                pruneSelectionToVisible();   // Select-all semantics follow the filter
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
grid.addEventListener("click", function (event) {
    var groupButton = event.target.closest("button[data-select-group]");
    if (groupButton) {
        for (var groupIndex = 0; groupIndex < lastGroups.length; groupIndex++) {
            if (lastGroups[groupIndex].key === groupButton.dataset.selectGroup) { toggleSelectionFor(lastGroups[groupIndex].members); break; }
        }
        return;
    }
    var groupToggle = event.target.closest("[data-toggle-group]");
    if (groupToggle) {
        var groupKey = groupToggle.dataset.toggleGroup;
        if (collapsedGroups[groupKey]) delete collapsedGroups[groupKey]; else collapsedGroups[groupKey] = true;
        localStorage.setItem(COLLAPSED_GROUPS_KEY, JSON.stringify(collapsedGroups)); render(); return;
    }
    var editButton = event.target.closest("[data-edit-instance]");
    if (editButton) { editInstance(editButton.dataset.editInstance); return; }
    var removeButton = event.target.closest("[data-remove-instance]");
    if (removeButton) { removeInstance(removeButton.dataset.removeInstance); return; }
    if (event.target.closest("a, button, input, select, textarea, label")) return;
    var card = event.target.closest("[data-instance-name]");
    if (!card) return;
    var clickedName = card.dataset.instanceName;
    var newState = !selectedInstances.has(clickedName);
    if (event.shiftKey && lastClickedInstance) {
        var orderedNames = Array.from(grid.querySelectorAll("[data-instance-name]")).map(function (item) { return item.dataset.instanceName; });
        var anchorIndex = orderedNames.indexOf(lastClickedInstance), clickedIndex = orderedNames.indexOf(clickedName);
        if (anchorIndex !== -1 && clickedIndex !== -1) {
            for (var rangeIndex = Math.min(anchorIndex, clickedIndex); rangeIndex <= Math.max(anchorIndex, clickedIndex); rangeIndex++) setInstanceSelected(orderedNames[rangeIndex], newState);
        } else setInstanceSelected(clickedName, newState);
    } else setInstanceSelected(clickedName, newState);
    lastClickedInstance = clickedName; updateSelectionUI();
});

grid.addEventListener("keydown", function (event) {
    if (event.key !== " " && event.key !== "Enter") return;
    if (event.target.closest("a, button, input, select, textarea")) return;
    var card = event.target.closest("[data-instance-name]");
    if (!card) return;
    event.preventDefault();
    var name = card.dataset.instanceName;
    setInstanceSelected(name, !selectedInstances.has(name));
    lastClickedInstance = name; updateSelectionUI();
});

    // ---- Immediate restart (/api/stop_now) ----
    function getRestartEligibleNames() {
        return (dashboardState.instances || []).filter(function (inst) {
            return selectedInstances.has(inst.name) && inst.connection_state === "online" && !inst.restarting;
        }).map(function (inst) { return inst.name; });
    }
    function closeRestartModal() {
        if (restartRequestInFlight) return;
        restartModal.classList.add("hidden");
        restartStatus.textContent = "";
    }
    function openRestartModal() {
        var selectedCount = getSelectedNames().length;
        var eligible = getRestartEligibleNames();
        var skipped = selectedCount - eligible.length;
        if (!eligible.length) { showToast("No online warriors are eligible to restart", "error"); return; }
        restartCountText.textContent = eligible.length + " online warrior" + (eligible.length === 1 ? "" : "s") + " will restart" +
            (skipped ? "; " + skipped + " offline or already restarting will be skipped" : "") + ".";
        restartStatus.textContent = "";
        btnConfirmRestart.disabled = false;
        btnConfirmRestart.textContent = "Restart " + eligible.length + " now";
        restartModal.classList.remove("hidden");
        btnCancelRestart.focus();
    }
    btnSelRestart.addEventListener("click", openRestartModal);
    btnCancelRestart.addEventListener("click", closeRestartModal);
    restartModal.addEventListener("click", function (e) { if (e.target === restartModal) closeRestartModal(); });
    btnConfirmRestart.addEventListener("click", async function () {
        if (restartRequestInFlight) return;
        var eligible = getRestartEligibleNames();
        if (!eligible.length) { closeRestartModal(); return; }
        restartRequestInFlight = true;
        btnConfirmRestart.disabled = true;
        btnCancelRestart.disabled = true;
        btnConfirmRestart.textContent = "Restarting\u2026";
        restartStatus.textContent = "Sending restart requests in batches of 10\u2026";
        restartStatus.className = "text-sm min-h-[1.25rem] mb-3 text-yellow-400";
        updateSelectionUI();
        try {
            var accepted = [];
            var result = await runBulkBatches(eligible, "Restarting", restartStatus, async function (batch) {
                var response = await fetch("/api/restart", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ instance_names: batch }) });
                if (!response.ok) throw new Error(await readErrorDetail(response));
                var batchResult = await response.json();
                Object.keys(batchResult.results || {}).forEach(function (name) {
                    if (batchResult.results[name].status === "ok") accepted.push(name);
                });
                return { ok: Number(batchResult.accepted || 0), failed: Number(batchResult.failed || 0) };
            });
            (dashboardState.instances || []).forEach(function (inst) {
                if (accepted.indexOf(inst.name) !== -1) {
                    inst.restarting = true; inst.items = [];
                    inst.bandwidth_down = 0; inst.bandwidth_up = 0;
                    selectedInstances.delete(inst.name);
                }
            });
            var message = result.ok + " restart" + (result.ok === 1 ? "" : "s") + " accepted" +
                (result.failed ? "; " + result.failed + " failed" : "");
            showToast(message, result.failed ? "info" : "success");
            restartStatus.textContent = message;
            restartStatus.className = "text-sm min-h-[1.25rem] mb-3 " + (result.failed ? "text-amber-400" : "text-green-400");
            render();
            setTimeout(function () {
                restartRequestInFlight = false; btnCancelRestart.disabled = false;
                restartModal.classList.add("hidden"); updateSelectionUI();
            }, result.failed ? 1800 : 500);
            return;
        } catch (error) {
            restartStatus.textContent = "Restart failed: " + error.message;
            restartStatus.className = "text-sm min-h-[1.25rem] mb-3 text-red-400";
            showToast("Restart failed: " + error.message, "error");
        }
        restartRequestInFlight = false;
        btnConfirmRestart.disabled = false; btnCancelRestart.disabled = false;
        btnConfirmRestart.textContent = "Retry restart"; updateSelectionUI();
    });

    // ---- Select all / selection bar ----
    selectAllVisible.addEventListener("click", function () {
        toggleSelectionFor(getVisibleInstances());
    });
    btnSelClear.addEventListener("click", function () {
        selectedInstances.forEach(function (name) { selectedInstances.delete(name); syncCardSelection(name); });
        lastClickedInstance = null;
        updateSelectionUI();
    });
    clearSelectionInline.addEventListener("click", function () { btnSelClear.click(); });
    btnSelSettings.addEventListener("click", openSettingsPanel);
    btnSelPause.addEventListener("click", function () {
        loadPauseStatus();
        populatePauseModal();
        pauseModal.classList.remove("hidden");
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

    // ---- Project Health (v4.6.0: monitoring, automation, and reset) ----
    var projectHealthState = null;
    var projectHealthPreview = null;
    var projectHealthBusy = false;
    var PROJECT_HEALTH_LOG_PAGE_SIZE = 10;
    var projectHealthLogVisibleCount = PROJECT_HEALTH_LOG_PAGE_SIZE;
    var projectHealthModal = document.getElementById("project-health-modal");
    var btnProjectHealth = document.getElementById("btn-project-health");
    var btnCloseProjectHealth = document.getElementById("btn-close-project-health");
    var btnRefreshProjectHealth = document.getElementById("btn-refresh-project-health");
    var projectHealthPolicyForm = document.getElementById("project-health-policy-form");
    var btnProjectHealthLogMore = document.getElementById("btn-project-health-log-more");
    var btnProjectHealthLogLess = document.getElementById("btn-project-health-log-less");
    var primaryProjectSelect = document.getElementById("project-health-primary-project");
    var fallbackProjectSelect = document.getElementById("project-health-fallback-project");
    var canaryPercentInput = document.getElementById("project-health-canary-percent");
    var minimumBaselineHoursInput = document.getElementById("project-health-minimum-baseline-hours");
    var fullBaselineHoursInput = document.getElementById("project-health-full-baseline-hours");
    var projectHealthHeaderIndicator = document.getElementById("project-health-header-indicator");
    var projectHealthHeaderIndicatorLabel = document.getElementById("project-health-header-indicator-label");
    var projectHealthHeaderIndicatorDetail = document.getElementById("project-health-header-indicator-detail");
    var btnPreviewGroups = document.getElementById("btn-preview-project-health-groups");
    var btnSaveRoles = document.getElementById("btn-save-project-health-roles");
    var btnFailoverSecondary = document.getElementById("btn-project-health-failover-secondary");
    var btnFailoverPrimary = document.getElementById("btn-project-health-failover-primary");
    var PROJECT_HEALTH_REFRESH_INTERVAL = 60000;
    var monitoringEnabledInput = document.getElementById("project-health-monitoring-enabled");
    var automaticFailoverEnabledInput = document.getElementById("project-health-automatic-failover-enabled");
    var stalledEvaluationsInput = document.getElementById("project-health-stalled-evaluations");
    var cooldownHoursInput = document.getElementById("project-health-cooldown-hours");
    var stabilizationMinutesInput = document.getElementById("project-health-stabilization-minutes");
    var monitoringBadge = document.getElementById("project-health-monitoring-badge");
    var automationBadge = document.getElementById("project-health-automation-badge");
    var monitoringOffBanner = document.getElementById("project-health-off-banner");
    var automationStateElement = document.getElementById("project-health-automation-state");
    var resetDialog = document.getElementById("project-health-reset-dialog");
    var resetConfirmationInput = document.getElementById("project-health-reset-confirmation");
    var resetHelp = document.getElementById("project-health-reset-help");
    var btnOpenReset = document.getElementById("btn-open-project-health-reset");
    var btnCancelReset = document.getElementById("btn-cancel-project-health-reset");
    var btnConfirmReset = document.getElementById("btn-confirm-project-health-reset");
    var primaryChangeDialog = document.getElementById("project-health-primary-change-dialog");
    var primaryChangeSummary = document.getElementById("project-health-primary-change-summary");
    var primaryChangeEligible = document.getElementById("project-health-primary-change-eligible");
    var primaryChangeAlready = document.getElementById("project-health-primary-change-already");
    var primaryChangeOther = document.getElementById("project-health-primary-change-other");
    var primaryChangeUnknown = document.getElementById("project-health-primary-change-unknown");
    var btnPrimaryChangeNow = document.getElementById("btn-project-health-primary-change-now");
    var btnPrimaryChangeLater = document.getElementById("btn-project-health-primary-change-later");

    function projectHealthErrorDetail(response) {
        return response.json().then(function (data) {
            return data && data.detail ? data.detail : "Request failed (HTTP " + response.status + ")";
        }).catch(function () {
            return "Request failed (HTTP " + response.status + ")";
        });
    }

    function setProjectHealthMessage(message, type) {
        var element = document.getElementById("project-health-message");
        if (!element) return;
        if (!message) {
            element.classList.add("hidden");
            element.textContent = "";
            return;
        }
        element.className = "rounded-lg border px-4 py-3 text-sm";
        if (type === "error") element.classList.add("border-red-800", "bg-red-950/40", "text-red-300");
        else if (type === "success") element.classList.add("border-green-800", "bg-green-950/40", "text-green-300");
        else element.classList.add("border-blue-800", "bg-blue-950/40", "text-blue-300");
        element.textContent = message;
    }

    function setProjectHealthBusy(isBusy) {
        projectHealthBusy = isBusy;
        [btnRefreshProjectHealth, btnPreviewGroups, btnSaveRoles, btnFailoverSecondary, btnFailoverPrimary].forEach(function (button) {
            if (button) button.dataset.busy = isBusy ? "1" : "0";
        });
        updateProjectHealthActions();
    }

    function projectHealthRequest(url, options) {
        options = options || {};
        options.headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
        return fetch(url, options).then(function (response) {
            if (!response.ok) return projectHealthErrorDetail(response).then(function (detail) { throw new Error(detail); });
            return response.json();
        });
    }

    function populateProjectHealthProjects() {
        if (!primaryProjectSelect || !fallbackProjectSelect) return;
        var currentPrimary = primaryProjectSelect.value;
        var currentFallback = fallbackProjectSelect.value;
        var slugs = Object.keys(projectTitles).filter(function (slug) { return slug && slug !== AUTO_PROJECT_SLUG; });
        dashboardState.instances.forEach(function (instance) {
            var slug = instance.project_slug || "";
            if (slug && slug !== AUTO_PROJECT_SLUG && slugs.indexOf(slug) < 0) slugs.push(slug);
        });
        slugs.sort(function (a, b) { return String(projectTitles[a] || a).localeCompare(String(projectTitles[b] || b)); });
        function fill(select, placeholder, selected) {
            var html = '<option value="">' + escapeHtml(placeholder) + '</option>';
            slugs.forEach(function (slug) {
                var baseName = projectHealthProjectLabel(slug);
                html += '<option value="' + escapeHtml(slug) + '">' + escapeHtml(projectSelectorLabel(slug, baseName)) + '</option>';
            });
            select.innerHTML = html;
            select.value = selected || "";
            if (selected && select.value !== selected) {
                var option = document.createElement("option");
                option.value = selected;
                option.textContent = selected;
                select.appendChild(option);
                select.value = selected;
            }
        }
        fill(primaryProjectSelect, "Select primary project", currentPrimary);
        fill(fallbackProjectSelect, "Select fallback project", currentFallback);
    }

    function projectHealthProjectLabel(slug) {
        var policy = projectHealthState && projectHealthState.policy ? projectHealthState.policy : {};
        var title = projectTitles[slug] || slug;
        if (slug === policy.primary_project) return "Primary · " + title;
        if (slug === policy.fallback_project) return "Secondary · " + title;
        return title;
    }
    function formatActivityRatio(value) {
        if (value === null || value === undefined || !isFinite(value)) return "Unavailable";
        return value.toFixed(2) + "x (" + Math.round(value * 100) + "%)";
    }

    function healthStateClass(state) {
        if (state === "Healthy") return "project-health-state-healthy";
        if (state === "Recovering") return "project-health-state-recovering";
        if (state === "Reduced activity") return "project-health-state-reduced";
        if (state === "Likely stalled") return "project-health-state-stalled";
        if (state === "Insufficient volume") return "project-health-state-insufficient";
        return "project-health-state-building";
    }

    function renderProjectHealthGroup(elementId, items) {
        var element = document.getElementById(elementId);
        if (!element) return;
        if (!items || !items.length) {
            element.innerHTML = '<p class="text-sm text-gray-500">No Warriors in this group.</p>';
            return;
        }
        element.innerHTML = items.map(function (item) {
            return '<div class="project-health-group-item"><span class="font-medium text-gray-200">' + escapeHtml(item.name) + '</span><span class="text-xs text-gray-500">' + escapeHtml(item.host || "") + '</span></div>';
        }).join("");
    }

    function roleAssignmentsToPreview(roles) {
        var byName = {};
        dashboardState.instances.forEach(function (instance) { byName[instance.name] = instance; });
        var preview = { primary_canaries: [], fallback_fleet: [] };
        Object.keys(roles || {}).sort().forEach(function (name) {
            var instance = byName[name] || { name: name, host: "" };
            var item = { name: name, host: instance.host || "", project: instance.project_slug || "" };
            if (roles[name] === "primary_canary") preview.primary_canaries.push(item);
            else if (roles[name] === "fallback_fleet") preview.fallback_fleet.push(item);
        });
        return preview;
    }

    function renderProjectHealthDecisionLog(entries) {
        var body = document.getElementById("project-health-decision-log");
        var count = document.getElementById("project-health-log-count");
        var controls = document.getElementById("project-health-log-controls");
        if (!body) return;
        entries = Array.isArray(entries) ? entries : [];
        var visibleEntries = entries.slice(0, projectHealthLogVisibleCount);
        if (!entries.length) {
            body.innerHTML = '<tr><td colspan="4" class="py-6 text-center text-gray-500">No Project Health decisions recorded.</td></tr>';
        } else {
            body.innerHTML = visibleEntries.map(function (entry) {
                var details = entry.message || "";
                if (entry.details && entry.details.to_state) details += " (" + entry.details.to_state + ")";
                if (entry.details && entry.details.reason) details += " · " + String(entry.details.reason).replace(/_/g, " ");
                return '<tr class="border-b border-gray-800/70 align-top"><td class="py-3 pr-4 whitespace-nowrap text-gray-400">' + escapeHtml(new Date(entry.timestamp * 1000).toLocaleString()) + '</td><td class="py-3 pr-4 text-gray-300">' + escapeHtml(String(entry.event || "").replace(/_/g, " ")) + '</td><td class="py-3 pr-4 text-gray-400">' + escapeHtml(entry.actor || "") + '</td><td class="py-3 text-gray-300">' + escapeHtml(details) + '</td></tr>';
            }).join("");
        }
        if (count) count.textContent = "Showing " + visibleEntries.length + " of " + entries.length + " entries";
        if (controls) controls.classList.toggle("hidden", entries.length <= PROJECT_HEALTH_LOG_PAGE_SIZE);
        if (btnProjectHealthLogMore) btnProjectHealthLogMore.classList.toggle("hidden", visibleEntries.length >= entries.length);
        if (btnProjectHealthLogLess) btnProjectHealthLogLess.classList.toggle("hidden", projectHealthLogVisibleCount <= PROJECT_HEALTH_LOG_PAGE_SIZE);
    }

    function formatProjectHealthTodo(value) {
        if (value === null || value === undefined || !isFinite(Number(value))) return "TODO unavailable";
        var amount = Math.max(0, Number(value));
        if (amount >= 1000000000) return (Math.round(amount / 100000000) / 10) + "B TODO";
        if (amount >= 1000000) return (Math.round(amount / 100000) / 10) + "M TODO";
        if (amount >= 1000) return (Math.round(amount / 100) / 10) + "K TODO";
        return Math.round(amount).toLocaleString() + " TODO";
    }

    function formatProjectHealthBytes(value) {
        value = Math.max(0, Number(value || 0));
        var units = ["B", "KiB", "MiB", "GiB", "TiB"];
        var unitIndex = 0;
        while (value >= 1024 && unitIndex < units.length - 1) {
            value /= 1024;
            unitIndex += 1;
        }
        var precision = value >= 100 || unitIndex === 0 ? 0 : 1;
        return value.toFixed(precision).replace(/\.0$/, "") + " " + units[unitIndex];
    }

    function formatProjectHealthRunway(minutes) {
        if (minutes === null || minutes === undefined || !isFinite(Number(minutes))) return "Estimated runway unavailable.";
        minutes = Number(minutes);
        if (minutes < 60) return "Estimated runway: " + Math.max(0, Math.round(minutes)) + " minutes";
        if (minutes < 1440) return "Estimated runway: " + (Math.round(minutes / 6) / 10) + " hours";
        return "Estimated runway: " + (Math.round(minutes / 144) / 10) + " days";
    }

    function formatProjectHealthAge(seconds) {
        if (seconds === null || seconds === undefined || !isFinite(Number(seconds))) return "Unavailable";
        seconds = Math.max(0, Number(seconds));
        if (seconds < 60) return Math.round(seconds) + "s ago";
        return Math.round(seconds / 60) + "m ago";
    }

    function activeBaselineWindow(readiness, policy) {
        readiness = readiness || {};
        policy = policy || {};
        var useFull = Boolean(readiness.full_established);
        var windowData = useFull ? readiness.full : readiness.minimum;
        var configuredHours = useFull
            ? Number(policy.full_baseline_hours || 24)
            : Number(policy.minimum_baseline_hours || 6);
        return {
            label: useFull ? "Full baseline" : "Minimum baseline",
            hours: Number((windowData && windowData.hours) || configuredHours),
            data: windowData || {}
        };
    }

    function projectHealthExplanation(evaluation, policy, runtime, readiness) {
        if (!evaluation) return "Project Health has not evaluated the configured primary project yet.";
        var primary = (policy && policy.primary_project) || evaluation.project || "The primary project";
        var queue = evaluation.queue || {};
        var confirmation = runtime ? Number(runtime.consecutive_stalled_evaluations || 0) : 0;
        var required = policy ? Number(policy.stalled_evaluations_required || 3) : 3;
        if (evaluation.state === "Building baseline") return primary + " is building a normalized per-Warrior baseline before automated switching is available.";
        if (evaluation.state === "Failover cooldown") return "Automatic switching is in cooldown. Primary health continues to be evaluated, but no automatic transition will occur until cooldown expires.";
        if (String(evaluation.state).indexOf("Evaluating stalled project ") === 0) return primary + " is below the stalled threshold. Project Health is confirming the condition before moving the Fallback fleet.";
        if (String(evaluation.state).indexOf("Evaluating primary recovery ") === 0) return primary + " has met the recovery tests. Project Health is confirming sustained recovery and at least 60 minutes of queue runway before returning the fleet.";
        if (evaluation.state === "Failing over") return "Project Health is moving eligible Fallback-fleet Warriors to the configured secondary project.";
        if (evaluation.state === "Returning to primary") return "Project Health is returning eligible Fallback-fleet Warriors to the recovered primary project.";
        if (evaluation.state === "Queue exhausted") return primary + " reports zero queued items. Automatic failover confirmation is " + confirmation + " of " + required + ".";
        if (evaluation.state === "Queue nearly exhausted") return primary + " has an estimated queue runway under " + Math.round(queue.low_runway_threshold_minutes || 30) + " minutes. Automatic failover confirmation is " + confirmation + " of " + required + ".";
        if (evaluation.state === "Likely stalled") return primary + " reports queued work, but both item and byte activity are below 25% of baseline. Automatic failover confirmation is " + confirmation + " of " + required + ".";
        if (evaluation.state === "Monitoring delayed") return "The baseline is retained, but the latest tracker sample is too old for a current automatic decision.";
        if (evaluation.state === "Building baseline") {
            var minimumHours = Number((readiness && readiness.minimum && readiness.minimum.hours) || (policy && policy.minimum_baseline_hours) || 6);
            return primary + " has not yet established the configured " + minimumHours + "-hour minimum baseline with at least 75% coverage.";
        }
        if (evaluation.state === "Healthy") return primary + " is processing normally. At least one activity signal remains above the stalled threshold" + (queue.available ? ", and the tracker reports " + formatProjectHealthTodo(queue.todo_items).replace(" TODO", " queued items") : "") + ".";
        return primary + " is currently classified as " + String(evaluation.state || "unavailable").toLowerCase() + ".";
    }

    function formatBaselineHours(hours) {
        if (!isFinite(hours) || hours <= 0) return "0h";
        if (hours < 1) return Math.round(hours * 60) + "m";
        return (Math.round(hours * 10) / 10).toString().replace(/\.0$/, "") + "h";
    }

    function renderProjectHealthHeaderIndicator(state) {
        if (!projectHealthHeaderIndicator || !projectHealthHeaderIndicatorLabel || !projectHealthHeaderIndicatorDetail) return;
        var policy = state && state.policy ? state.policy : {};
        var primary = policy.primary_project || "";
        var evaluation = state && state.health ? state.health[primary] : null;
        var readiness = evaluation && evaluation.baseline ? evaluation.baseline : null;
        var runtime = state && state.runtime ? state.runtime : {};
        var stateName = evaluation ? evaluation.state : "Unavailable";
        var classes = "project-health-header-indicator";
        var label = "Project Health: " + stateName;
        var detail = primary ? String(projectTitles[primary] || primary) : "No primary project";
        if (policy.monitoring_enabled === false) {
            label = "Project Health: Off";
            detail = "Monitoring disabled";
            classes += " project-health-header-off";
        } else if (!readiness || !readiness.established || stateName === "Building baseline") {
            var coverage = readiness ? Number(readiness.coverage_percent || 0) : 0;
            label = "Project Health: Building baseline";
            detail = coverage.toFixed(1) + "% coverage";
            classes += " project-health-header-building";
        } else if (stateName === "Failover cooldown") {
            label = "Project Health: Failover cooldown";
            detail = primary ? String(projectTitles[primary] || primary) : "Cooldown active";
            classes += " project-health-header-building";
        } else if (stateName.indexOf("Evaluating stalled project ") === 0) {
            label = "Project Health: " + stateName;
            detail = primary ? String(projectTitles[primary] || primary) : "Primary project";
            classes += " project-health-header-warning";
        } else if (stateName.indexOf("Evaluating primary recovery ") === 0) {
            label = "Project Health: " + stateName;
            detail = "Waiting to return to " + (projectTitles[primary] || primary || "primary");
            classes += " project-health-header-building";
        } else if (stateName === "Failing over" || stateName === "Returning to primary") {
            classes += " project-health-header-attention";
        } else if (stateName === "Healthy" || stateName === "Recovering") {
            classes += " project-health-header-healthy";
        } else if (stateName === "Queue exhausted" || stateName === "Queue nearly exhausted" || stateName === "Likely stalled" || stateName === "Reduced activity") {
            classes += " project-health-header-warning";
        } else if (stateName === "Monitoring delayed") {
            classes += " project-health-header-warning";
        } else {
            classes += " project-health-header-attention";
        }
        projectHealthHeaderIndicator.className = classes;
        projectHealthHeaderIndicatorLabel.textContent = label;
        projectHealthHeaderIndicatorDetail.textContent = detail;
        projectHealthHeaderIndicator.classList.remove("hidden");
    }

    function setTextIfPresent(elementId, value) {
        var element = document.getElementById(elementId);
        if (element) element.textContent = value;
    }
    function renderProjectHealthOperationalEvidence(state, evaluation) {
        var policy = state.policy || {};
        var runtime = state.runtime || {};
        var readiness = evaluation && evaluation.baseline ? evaluation.baseline : {};
        var recent = evaluation && evaluation.recent ? evaluation.recent : {};
        var queue = evaluation && evaluation.queue ? evaluation.queue : {};
        var stateName = evaluation ? evaluation.state : "Unavailable";
        setTextIfPresent("project-health-current-state", stateName);
        setTextIfPresent("project-health-current-state-detail", policy.primary_project ? projectHealthProjectLabel(policy.primary_project) : "No primary project configured.");
        setTextIfPresent("project-health-queue-todo", formatProjectHealthTodo(queue.todo_items));
        setTextIfPresent("project-health-queue-runway", queue.stale ? "Queue observation is stale." : formatProjectHealthRunway(queue.runway_minutes));
        setTextIfPresent("project-health-recent-items", (Math.round(Number(recent.items_per_minute || 0) * 10) / 10) + " items/Warrior/min");
        setTextIfPresent("project-health-recent-bytes", formatProjectHealthBytes(Number(recent.bytes || 0) / Math.max(1, Number(recent.window_seconds || 3600) / 60)) + "/Warrior/min · newest sample " + formatProjectHealthAge(recent.latest_sample_age_seconds));
        setTextIfPresent("project-health-comparison-ratios", "Items " + formatActivityRatio(evaluation && evaluation.item_activity_ratio) + " · Bytes " + formatActivityRatio(evaluation && evaluation.byte_activity_ratio));
        setTextIfPresent("project-health-comparison-detail", readiness.established ? "Normalized baseline established" : "Normalized baseline not established");
        var interval = state.thresholds ? Number(state.thresholds.evaluation_interval_seconds || 300) : 300;
        setTextIfPresent("project-health-evaluation-interval", Math.round(interval / 60) + " minutes");
        setTextIfPresent("project-health-confirmation-progress", Number(runtime.consecutive_stalled_evaluations || 0) + " / " + Number(policy.stalled_evaluations_required || 3));
        setTextIfPresent("project-health-latest-sample", formatProjectHealthAge(recent.latest_sample_age_seconds));
        var secondaryEvaluation = state.health && policy.fallback_project ? state.health[policy.fallback_project] : null;
        var secondaryRecent = secondaryEvaluation && secondaryEvaluation.recent ? secondaryEvaluation.recent : {};
        var secondaryQueue = secondaryEvaluation && secondaryEvaluation.queue ? secondaryEvaluation.queue : {};
        setTextIfPresent("project-health-fallback-queue", secondaryEvaluation ? formatProjectHealthTodo(secondaryQueue.todo_items) : "Unavailable");
        setTextIfPresent("project-health-secondary-items", secondaryEvaluation ? (Math.round(Number(secondaryRecent.items_per_minute || 0) * 10) / 10) + " items/Warrior/min" : "Unavailable");
        setTextIfPresent("project-health-secondary-bytes", secondaryEvaluation ? formatProjectHealthBytes(Number(secondaryRecent.bytes || 0) / Math.max(1, Number(secondaryRecent.window_seconds || 3600) / 60)) + "/Warrior/min" : "No secondary activity sample.");
        setTextIfPresent("project-health-secondary-todo", secondaryEvaluation ? formatProjectHealthTodo(secondaryQueue.todo_items) : "TODO unavailable");
        setTextIfPresent("project-health-secondary-runway", secondaryEvaluation && secondaryQueue.stale ? "Queue observation is stale." : formatProjectHealthRunway(secondaryQueue.runway_minutes));
        var placement = state.fleet_placement || {};
        var counts = placement.counts || {};
        setTextIfPresent("project-health-placement-primary", Number(counts.primary || 0));
        setTextIfPresent("project-health-placement-secondary", Number(counts.secondary || 0));
        setTextIfPresent("project-health-placement-other", Number(counts.other || 0));
        setTextIfPresent("project-health-placement-unknown", Number(counts.unknown || 0));
        var eligibleWarriors = Number(recent.average_eligible_warriors || 0);
        setTextIfPresent("project-health-eligible-warriors", (Math.round(eligibleWarriors * 10) / 10) + " eligible Warriors");
        setTextIfPresent("project-health-normalization-detail", recent.normalization === "per_eligible_warrior" ? "Items and bytes are normalized per eligible Warrior." : "Waiting for normalized samples.");
        setTextIfPresent("project-health-decision-explanation", projectHealthExplanation(evaluation, policy, runtime, readiness));
        if (readiness.minimum) {
            var baselineWindow = activeBaselineWindow(readiness, policy);
            var baselineData = baselineWindow.data;
            setTextIfPresent("project-health-coverage-detail", baselineWindow.label + " (" + baselineWindow.hours + "h) coverage: " + Number(baselineData.coverage_percent || 0).toFixed(1) + "% · " + Number(baselineData.sample_count || 0) + " / " + Number(baselineData.expected_sample_count || 0) + " samples · " + Number(baselineData.missing_sample_count || 0) + " missing · 75% required");
        } else {
            setTextIfPresent("project-health-coverage-detail", "Coverage details unavailable.");
        }
    }
    function renderProjectHealth(state) {
        projectHealthState = state;
        populateProjectHealthProjects();
        var policy = state.policy || {};
        primaryProjectSelect.value = policy.primary_project || "";
        fallbackProjectSelect.value = policy.fallback_project || "";
        canaryPercentInput.value = policy.canary_percent || 5;
        minimumBaselineHoursInput.value = policy.minimum_baseline_hours || 6;
        fullBaselineHoursInput.value = policy.full_baseline_hours || 24;
        monitoringEnabledInput.checked = policy.monitoring_enabled !== false;
        automaticFailoverEnabledInput.checked = Boolean(policy.automatic_failover_enabled);
        stalledEvaluationsInput.value = policy.stalled_evaluations_required || 3;
        cooldownHoursInput.value = policy.cooldown_hours || 6;
        stabilizationMinutesInput.value = policy.resume_stabilization_minutes || 30;
        monitoringBadge.textContent = policy.monitoring_enabled !== false ? "Monitoring on" : "Monitoring off";
        automationBadge.textContent = policy.automatic_failover_enabled ? "Automatic failover on" : "Automatic failover off";
        monitoringOffBanner.classList.toggle("hidden", policy.monitoring_enabled !== false);
        var runtime = state.runtime || {};
        var automationReasons = state.automation && state.automation.reasons ? state.automation.reasons : [];
        automationStateElement.textContent = runtime.failover_in_progress ? "Automatic failover is in progress." :
            (runtime.automatic_failover_state === "confirming_stall" ? "Stall confirmation " + runtime.consecutive_stalled_evaluations + " of " + policy.stalled_evaluations_required + "." :
            (policy.automatic_failover_enabled ? (automationReasons.length ? "Automatic failover armed but blocked: " + automationReasons.join("; ") + "." : "Automatic failover is armed.") : "Automatic failover is disabled."));
        var evaluation = (state.health || {})[policy.primary_project] || null;
        var stateElement = document.getElementById("project-health-primary-state");
        stateElement.textContent = evaluation ? evaluation.state : "Not configured";
        stateElement.className = "project-health-value " + healthStateClass(evaluation ? evaluation.state : "Building baseline");
        var readiness = evaluation ? evaluation.baseline : null;
        var displayedCoverage = readiness ? (readiness.phase === "building" ? readiness.minimum.coverage_percent : readiness.full.coverage_percent) : 0;
        document.getElementById("project-health-coverage").textContent = displayedCoverage.toFixed(1) + "%";
        if (!readiness) {
            document.getElementById("project-health-baseline-detail").textContent = "Configure a primary project";
        } else if (readiness.phase === "full") {
            document.getElementById("project-health-baseline-detail").textContent = "Full " + readiness.full.hours + "-hour baseline";
        } else if (readiness.phase === "provisional") {
            document.getElementById("project-health-baseline-detail").textContent = "Provisional; building toward " + readiness.full.hours + " hours";
        } else {
            document.getElementById("project-health-baseline-detail").textContent = "Requires " + readiness.minimum.hours + " elapsed hours at 75% coverage";
        }
        renderProjectHealthHeaderIndicator(state);
        document.getElementById("project-health-item-ratio").textContent = formatActivityRatio(evaluation && evaluation.item_activity_ratio);
        document.getElementById("project-health-byte-ratio").textContent = formatActivityRatio(evaluation && evaluation.byte_activity_ratio);

        var failover = state.failover || { available: false, message: "Failover unavailable." };
        var failoverStatus = document.getElementById("project-health-failover-status");
        failoverStatus.textContent = failover.message;
        failoverStatus.className = "mt-3 rounded-lg border px-4 py-3 text-sm " + (failover.available ? "border-green-800/60 bg-green-950/30 text-green-300" : "border-amber-800/60 bg-amber-950/30 text-amber-300");

        if (!projectHealthPreview) projectHealthPreview = roleAssignmentsToPreview(state.roles || {});
        renderProjectHealthGroup("project-health-primary-canaries", projectHealthPreview.primary_canaries);
        renderProjectHealthGroup("project-health-fallback-fleet", projectHealthPreview.fallback_fleet);
        document.getElementById("project-health-canary-count").textContent = projectHealthPreview.primary_canaries.length + " Warriors";
        document.getElementById("project-health-fallback-count").textContent = projectHealthPreview.fallback_fleet.length + " Warriors";
        renderProjectHealthOperationalEvidence(state, evaluation);
        renderProjectHealthDecisionLog(state.decision_log || []);
        lastLayoutKey = "";
        if (viewGrouped) render();
        updateProjectHealthActions();
    }

    function updateProjectHealthActions() {
        if (!projectHealthState) return;
        var policy = projectHealthState.policy || {};
        var evaluation = (projectHealthState.health || {})[policy.primary_project];
        var baselineComplete = Boolean(evaluation && evaluation.baseline && evaluation.baseline.complete);
        btnPreviewGroups.disabled = projectHealthBusy || !policy.primary_project || !policy.fallback_project;
        btnSaveRoles.disabled = projectHealthBusy || !projectHealthPreview || !projectHealthPreview.primary_canaries.length;
        var monitoringEnabled = projectHealthState.policy && projectHealthState.policy.monitoring_enabled !== false;
        var failoverAvailable = monitoringEnabled && Boolean(projectHealthState.failover && projectHealthState.failover.available);
        btnOpenReset.disabled = projectHealthBusy || !(projectHealthState.policy && projectHealthState.policy.primary_project);
        var policy = projectHealthState ? (projectHealthState.policy || {}) : {};
        var assignments = projectHealthState
            ? (
                projectHealthState.roles ||
                projectHealthState.role_assignments ||
                {}
            )
            : {};
        var hasFallbackFleet = Object.keys(assignments).some(function (name) { return assignments[name] === "fallback_fleet"; });
        btnFailoverSecondary.disabled = projectHealthBusy || !hasFallbackFleet || !policy.fallback_project;
        btnFailoverPrimary.disabled = projectHealthBusy || !hasFallbackFleet || !policy.primary_project;
        btnRefreshProjectHealth.disabled = projectHealthBusy;
    }

    function loadProjectHealth() {
        setProjectHealthBusy(true);
        return projectHealthRequest("api/project-health").then(function (state) {
            projectHealthPreview = null;
            renderProjectHealth(state);
            setProjectHealthMessage("", "info");
        }).catch(function (error) {
            setProjectHealthMessage(error.message, "error");
        }).finally(function () { setProjectHealthBusy(false); });
    }

    function openProjectHealth() {
        projectHealthModal.classList.remove("hidden");
        document.body.classList.add("overflow-hidden");
        loadProjectHealth();
    }

    function closeProjectHealth() {
        projectHealthModal.classList.add("hidden");
        document.body.classList.remove("overflow-hidden");
        projectHealthLogVisibleCount = PROJECT_HEALTH_LOG_PAGE_SIZE;
    }

    btnProjectHealth.addEventListener("click", openProjectHealth);
    projectHealthHeaderIndicator.addEventListener("click", openProjectHealth);
    btnCloseProjectHealth.addEventListener("click", closeProjectHealth);
    btnRefreshProjectHealth.addEventListener("click", loadProjectHealth);
    if (btnProjectHealthLogMore) btnProjectHealthLogMore.addEventListener("click", function () {
        projectHealthLogVisibleCount += PROJECT_HEALTH_LOG_PAGE_SIZE;
        renderProjectHealthDecisionLog(projectHealthState ? projectHealthState.decision_log : []);
    });
    if (btnProjectHealthLogLess) btnProjectHealthLogLess.addEventListener("click", function () {
        projectHealthLogVisibleCount = PROJECT_HEALTH_LOG_PAGE_SIZE;
        renderProjectHealthDecisionLog(projectHealthState ? projectHealthState.decision_log : []);
    });
    projectHealthModal.addEventListener("click", function (event) { if (event.target === projectHealthModal) closeProjectHealth(); });

    monitoringEnabledInput.addEventListener("change", function () {
        if (!monitoringEnabledInput.checked && !window.confirm("Turn off Project Health monitoring? Baseline collection, health evaluation, manual failover, and automatic failover will stop. Existing configuration and history will be retained, and Warrior projects will remain unchanged.")) {
            monitoringEnabledInput.checked = true;
        }
    });

    automaticFailoverEnabledInput.addEventListener("change", function () {
        if (automaticFailoverEnabledInput.checked && !window.confirm("Enable automatic failover and failback? The saved Fallback fleet may switch to secondary after the configured stalled confirmations, then return to primary after cooldown, sustained recovery, and at least 60 minutes of primary queue runway. Primary canaries remain on primary during failover.")) {
            automaticFailoverEnabledInput.checked = false;
        }
    });

    function closePrimaryChangeDialog() {
        if (primaryChangeDialog) primaryChangeDialog.classList.add("hidden");
    }
    function openPrimaryChangeDialog(result) {
        if (!primaryChangeDialog) return;
        var placement = result.fleet_placement || {};
        var counts = placement.counts || {};
        var instances = placement.instances || [];
        var target = result.primary_project || "";
        var eligible = instances.filter(function (item) {
            return (item.role === "fallback_fleet" || item.role === "primary_canary") &&
                item.online && !item.restarting && item.project !== target &&
                (item.placement === "primary" || item.placement === "secondary");
        }).length;
        var already = instances.filter(function (item) {
            return (item.role === "fallback_fleet" || item.role === "primary_canary") &&
                item.project === target;
        }).length;
        primaryChangeSummary.textContent = "Primary project changed from " + (projectTitles[result.previous_primary_project] || result.previous_primary_project || "unconfigured") + " to " + (projectTitles[target] || target) + ". Switch eligible Fallback-fleet Warriors and primary canaries now?";
        primaryChangeEligible.textContent = eligible;
        primaryChangeAlready.textContent = already;
        primaryChangeOther.textContent = Number(counts.other || 0);
        primaryChangeUnknown.textContent = Number(counts.unknown || 0);
        btnPrimaryChangeNow.disabled = eligible === 0;
        primaryChangeDialog.classList.remove("hidden");
        btnPrimaryChangeLater.focus();
    }
    function formatManualSwitchSummary(result) {
        return Number(result.switched || 0) + " switched · " +
            Number(result.already_at_target || 0) + " already target · " +
            Number(result.skipped_unrelated_project || 0) + " other skipped · " +
            Number(result.skipped_project_unknown || 0) + " unknown skipped · " +
            Number(result.failed || 0) + " failed";
    }
    projectHealthPolicyForm.addEventListener("submit", function (event) {
        event.preventDefault();
        var primaryProject = primaryProjectSelect.value;
        var fallbackProject = fallbackProjectSelect.value;
        var canaryPercent = parseInt(canaryPercentInput.value, 10);
        var minimumBaselineHours = parseInt(minimumBaselineHoursInput.value, 10);
        var fullBaselineHours = parseInt(fullBaselineHoursInput.value, 10);
        var stalledEvaluations = parseInt(stalledEvaluationsInput.value, 10);
        var cooldownHours = parseInt(cooldownHoursInput.value, 10);
        var stabilizationMinutes = parseInt(stabilizationMinutesInput.value, 10);
        if (!primaryProject || !fallbackProject) return setProjectHealthMessage("Select both a primary and fallback project.", "error");
        if (primaryProject === fallbackProject) return setProjectHealthMessage("Primary and fallback projects must be different.", "error");
        if (!Number.isInteger(canaryPercent) || canaryPercent < 1 || canaryPercent > 100) return setProjectHealthMessage("Canary allocation must be a whole number from 1 to 100.", "error");
        if (!Number.isInteger(minimumBaselineHours) || minimumBaselineHours < 1 || minimumBaselineHours > 48) return setProjectHealthMessage("Minimum baseline must be a whole number from 1 to 48 hours.", "error");
        if (!Number.isInteger(fullBaselineHours) || fullBaselineHours < 1 || fullBaselineHours > 48) return setProjectHealthMessage("Full baseline must be a whole number from 1 to 48 hours.", "error");
        if (fullBaselineHours < minimumBaselineHours) return setProjectHealthMessage("Full baseline cannot be shorter than the minimum baseline.", "error");
        if (!Number.isInteger(stalledEvaluations) || stalledEvaluations < 2 || stalledEvaluations > 10) return setProjectHealthMessage("Stalled evaluations must be from 2 to 10.", "error");
        if (!Number.isInteger(cooldownHours) || cooldownHours < 1 || cooldownHours > 48) return setProjectHealthMessage("Cooldown must be from 1 to 48 hours.", "error");
        if (!Number.isInteger(stabilizationMinutes) || stabilizationMinutes < 15 || stabilizationMinutes > 120) return setProjectHealthMessage("Resume stabilization must be from 15 to 120 minutes.", "error");
        setProjectHealthBusy(true);
        projectHealthRequest("api/project-health/policy", { method: "PUT", body: JSON.stringify({ primary_project: primaryProject, fallback_project: fallbackProject, canary_percent: canaryPercent, minimum_baseline_hours: minimumBaselineHours, full_baseline_hours: fullBaselineHours, monitoring_enabled: monitoringEnabledInput.checked, automatic_failover_enabled: automaticFailoverEnabledInput.checked, stalled_evaluations_required: stalledEvaluations, cooldown_hours: cooldownHours, resume_stabilization_minutes: stabilizationMinutes }) })
            .then(function (result) {
                projectHealthPreview = null;
                showToast("Project Health policy saved", "success");
                if (result.primary_changed) openPrimaryChangeDialog(result);
                return loadProjectHealth();
            })
            .catch(function (error) { setProjectHealthMessage(error.message, "error"); })
            .finally(function () { setProjectHealthBusy(false); });
    });

    btnPreviewGroups.addEventListener("click", function () {
        var percent = parseInt(canaryPercentInput.value, 10);
        setProjectHealthBusy(true);
        projectHealthRequest("api/project-health/preview", { method: "POST", body: JSON.stringify({ canary_percent: percent }) })
            .then(function (preview) { projectHealthPreview = preview; renderProjectHealthGroup("project-health-primary-canaries", preview.primary_canaries); renderProjectHealthGroup("project-health-fallback-fleet", preview.fallback_fleet); document.getElementById("project-health-canary-count").textContent = preview.primary_canaries.length + " Warriors"; document.getElementById("project-health-fallback-count").textContent = preview.fallback_fleet.length + " Warriors"; updateProjectHealthActions(); })
            .catch(function (error) { setProjectHealthMessage(error.message, "error"); })
            .finally(function () { setProjectHealthBusy(false); });
    });

    btnSaveRoles.addEventListener("click", function () {
        if (!projectHealthPreview) return;
        setProjectHealthBusy(true);
        projectHealthRequest("api/project-health/roles", { method: "PUT", body: JSON.stringify({ primary_canaries: projectHealthPreview.primary_canaries.map(function (item) { return item.name; }), fallback_fleet: projectHealthPreview.fallback_fleet.map(function (item) { return item.name; }) }) })
            .then(function () { showToast("Project Health group assignments saved", "success"); return loadProjectHealth(); })
            .catch(function (error) { setProjectHealthMessage(error.message, "error"); })
            .finally(function () { setProjectHealthBusy(false); });
    });

    function closeBaselineResetDialog() {
        resetDialog.classList.add("hidden");
        resetConfirmationInput.value = "";
        btnConfirmReset.disabled = true;
    }
    btnOpenReset.addEventListener("click", function () {
        var primary = projectHealthState && projectHealthState.policy ? projectHealthState.policy.primary_project : "";
        if (!primary) return;
        var phrase = "RESET " + primary;
        resetHelp.textContent = "Type " + phrase + " exactly. Coverage will return to 0% and must rebuild.";
        resetConfirmationInput.placeholder = phrase;
        resetDialog.classList.remove("hidden");
        resetConfirmationInput.focus();
    });
    btnCancelReset.addEventListener("click", closeBaselineResetDialog);
    resetDialog.addEventListener("click", function (event) { if (event.target === resetDialog) closeBaselineResetDialog(); });
    resetConfirmationInput.addEventListener("input", function () {
        var primary = projectHealthState && projectHealthState.policy ? projectHealthState.policy.primary_project : "";
        btnConfirmReset.disabled = resetConfirmationInput.value.trim() !== "RESET " + primary;
    });
    btnConfirmReset.addEventListener("click", function () {
        var phrase = resetConfirmationInput.value.trim();
        if (!phrase || btnConfirmReset.disabled) return;
        setProjectHealthBusy(true);
        projectHealthRequest("api/project-health/baseline/reset", { method: "POST", body: JSON.stringify({ confirmation: phrase, actor: "dashboard" }) })
            .then(function (result) {
                closeBaselineResetDialog();
                showToast("Deleted " + result.deleted_samples + " baseline samples for " + result.project, "success");
                return loadProjectHealth();
            })
            .catch(function (error) { setProjectHealthMessage(error.message, "error"); })
            .finally(function () { setProjectHealthBusy(false); });
    });

    if (btnPrimaryChangeLater) btnPrimaryChangeLater.addEventListener("click", closePrimaryChangeDialog);
    if (primaryChangeDialog) primaryChangeDialog.addEventListener("click", function (event) { if (event.target === primaryChangeDialog) closePrimaryChangeDialog(); });
    if (btnPrimaryChangeNow) btnPrimaryChangeNow.addEventListener("click", function () {
        btnPrimaryChangeNow.disabled = true;
        projectHealthRequest("api/project-health/failover", { method: "POST", body: JSON.stringify({ target: "primary", actor: "dashboard-primary-change" }) })
            .then(function (result) {
                closePrimaryChangeDialog();
                showToast("Primary-project switch: " + formatManualSwitchSummary(result), result.failed ? "info" : "success");
                return loadProjectHealth();
            })
            .catch(function (error) { setProjectHealthMessage(error.message, "error"); btnPrimaryChangeNow.disabled = false; });
    });
    function executeManualProjectSwitch(target) {
        var policy = projectHealthState ? (projectHealthState.policy || {}) : {};
        var targetProject = target === "primary" ? policy.primary_project : policy.fallback_project;
        if (!targetProject) {
            setProjectHealthMessage("No " + target + " project is configured.", "error");
            return;
        }
        if (!window.confirm("Move the saved Fallback fleet to " + targetProject + " (" + target + ")? Primary canaries will remain unchanged.")) return;

        setProjectHealthBusy(true);
        projectHealthRequest("api/project-health/failover", {
            method: "POST",
            body: JSON.stringify({ target: target, actor: "dashboard" })
        })
            .then(function (result) {
                showToast(
                    "Manual switch to " + target + ": " + formatManualSwitchSummary(result),
                    result.failed ? "info" : "success"
                );
                if (result.project_health) {
                    projectHealthState = result.project_health;
                    renderProjectHealth(projectHealthState);
                }
                return loadProjectHealth();
            })
            .catch(function (error) { setProjectHealthMessage(error.message, "error"); })
            .finally(function () { setProjectHealthBusy(false); });
    }

    btnFailoverSecondary.addEventListener("click", function () {
        executeManualProjectSwitch("secondary");
    });
    btnFailoverPrimary.addEventListener("click", function () {
        executeManualProjectSwitch("primary");
    });


loadConfigCached();
    connectWebSocket();
    loadProjects();
    initChart();
    loadHistory();
    loadTrackerStats();
    loadPauseStatus();
    loadProjectHealth();
    setInterval(function () {
        if (pageIsVisible && !projectHealthBusy) loadProjectHealth();
    }, PROJECT_HEALTH_REFRESH_INTERVAL);

})();
