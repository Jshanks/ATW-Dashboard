/**
 * ATW Dashboard -- Frontend Application
 */
(function () {
    "use strict";

    var ws = null;
    var wsReconnectDelay = 1000;
    var MAX_WS_RECONNECT = 30000;
    var dashboardState = { instances: [], total_online: 0, total_offline: 0, total_items_active: 0 };
    var knownInstances = new Set();

    // Idle states — items in these states get collapsed into a summary line
    var IDLE_STATES = { "getting_task": true, "waiting": true, "unknown": true };

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
        if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
        if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + " MB";
        return (bytes / 1073741824).toFixed(2) + " GB";
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
        setInterval(function () {
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

        // Count total items and working (non-idle) items across all instances
        var aggDl = 0, aggUl = 0;
        var totalItems = 0, workingItems = 0;
        if (state.instances) {
            for (var i = 0; i < state.instances.length; i++) {
                var inst = state.instances[i];
                aggDl += inst.bandwidth_down || 0;
                aggUl += inst.bandwidth_up || 0;
                if (inst.items) {
                    for (var j = 0; j < inst.items.length; j++) {
                        totalItems++;
                        if (!IDLE_STATES[inst.items[j].state]) {
                            workingItems++;
                        }
                    }
                }
            }
        }
        totalItemsEl.textContent = workingItems + "/" + totalItems;
        totalDlEl.textContent = fmtBytes(aggDl);
        totalUlEl.textContent = fmtBytes(aggUl);

        if (!state.instances || state.instances.length === 0) {
            grid.innerHTML = '<div class="col-span-full text-center text-gray-500 py-12">' +
                '<p class="text-lg">No warrior instances yet.</p>' +
                '<p class="text-sm mt-2">Click <strong>Add Instance</strong> to connect your first warrior.</p></div>';
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
        var statusDot = isOnline
            ? '<span class="w-2.5 h-2.5 rounded-full bg-green-500 pulse-online"></span>'
            : inst.connection_state === "connecting"
                ? '<span class="w-2.5 h-2.5 rounded-full bg-yellow-500 pulse-connecting"></span>'
                : inst.connection_state === "auth_failed"
                    ? '<span class="w-2.5 h-2.5 rounded-full bg-purple-500"></span>'
                    : '<span class="w-2.5 h-2.5 rounded-full bg-red-500"></span>';

        var stateLabel = inst.connection_state.replace("_", " ").toUpperCase();

        // Bandwidth
        var bwHtml = "";
        if (isOnline && (inst.bandwidth_down > 0 || inst.bandwidth_up > 0 || inst.bytes_downloaded > 0)) {
            bwHtml = '<p><span class="text-gray-500">BW:</span> ' +
                '<span class="text-cyan-400">' + fmtBytes(inst.bandwidth_down || 0) + ' &#x2193;</span> ' +
                '<span class="text-orange-400">' + fmtBytes(inst.bandwidth_up || 0) + ' &#x2191;</span></p>';
            if (inst.bytes_downloaded > 0 || inst.bytes_uploaded > 0) {
                bwHtml += '<p><span class="text-gray-500">Total:</span> ' +
                    '<span class="text-cyan-300">' + fmtTotal(inst.bytes_downloaded || 0) + ' &#x2193;</span> ' +
                    '<span class="text-orange-300">' + fmtTotal(inst.bytes_uploaded || 0) + ' &#x2191;</span></p>';
            }
        }

        // Split items into idle (getting_task/waiting) and active (everything else)
        var idleItems = [];
        var activeItems = [];
        if (inst.items) {
            for (var i = 0; i < inst.items.length; i++) {
                if (IDLE_STATES[inst.items[i].state]) {
                    idleItems.push(inst.items[i]);
                } else {
                    activeItems.push(inst.items[i]);
                }
            }
        }

        var totalCount = (inst.items && inst.items.length) ? inst.items.length : 0;

        // Build items HTML — active items shown in full, idle collapsed to one line
        var itemsHtml = "";
        if (totalCount > 0) {
            itemsHtml = '<div class="items-container mt-3">';

            // Active items — full detail
            for (var a = 0; a < activeItems.length; a++) {
                var item = activeItems[a];
                var badgeClass = "badge-" + item.state;
                var taskDesc = item.task_description || "";
                var itemName = item.item_name || "";
                var fullDesc = itemName;
                if (taskDesc && taskDesc !== itemName) {
                    fullDesc = itemName + " \u2014 " + taskDesc;
                }
                itemsHtml += '<div class="item-row">' +
                    '<span class="item-badge ' + badgeClass + '">' + escapeHtml(item.state) + '</span>' +
                    '<span class="item-desc" title="' + escapeHtml(fullDesc) + '">' + escapeHtml(fullDesc) + '</span>' +
                    '</div>';
            }

            // Idle items — collapsed summary
            if (idleItems.length > 0) {
                itemsHtml += '<div class="idle-summary">' +
                    '<span class="idle-dot"></span>' +
                    '<span>' + idleItems.length + ' item' + (idleItems.length !== 1 ? 's' : '') + ' waiting for tasks</span>' +
                    '</div>';
            }

            itemsHtml += '</div>';
        } else if (isOnline) {
            itemsHtml = '<p class="text-xs text-gray-500 mt-3">No active items</p>';
        }

        var errorMsg = inst.error_message
            ? '<p class="text-xs text-red-400 mt-2 truncate" title="' + escapeHtml(inst.error_message) + '">' + escapeHtml(inst.error_message) + '</p>'
            : "";

        var reconnectInfo = !isOnline && inst.reconnect_attempts > 0
            ? '<span class="text-xs text-gray-500 ml-2">(attempt ' + inst.reconnect_attempts + ')</span>'
            : "";

        var editBtn = '<button data-edit-instance="' + escapeHtml(inst.name) + '"' +
            ' class="text-gray-600 hover:text-blue-400 opacity-0 group-hover:opacity-100 transition-opacity" title="Edit instance">' +
            '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg></button>';
        var removeBtn = '<button data-remove-instance="' + escapeHtml(inst.name) + '"' +
            ' class="text-gray-600 hover:text-red-400 opacity-0 group-hover:opacity-100 transition-opacity" title="Remove instance">' +
            '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg></button>';

        // Items line: "Items: 2 working / 6" or "Items: 6 idle" or "Items: 6"
        var itemsLine = "";
        if (totalCount > 0) {
            if (activeItems.length > 0) {
                itemsLine = '<span class="text-green-400">' + activeItems.length + ' working</span> / ' + totalCount;
            } else {
                itemsLine = totalCount + ' <span class="text-gray-500">idle</span>';
            }
        } else {
            itemsLine = "0";
        }

        return '<div class="instance-card bg-gray-900 border-l-4 ' + stateClass + ' rounded-lg p-4 relative group">' +
            '<div class="absolute top-2 right-2 flex items-center gap-1">' + editBtn + removeBtn + '</div>' +
            '<div class="flex items-center gap-2 mb-2">' + statusDot +
            '<h3 class="font-semibold text-sm">' + escapeHtml(inst.name) + '</h3>' +
            '<span class="text-xs text-gray-500">' + stateLabel + reconnectInfo + '</span></div>' +
            '<div class="text-xs text-gray-400 space-y-0.5">' +
            '<p><span class="text-gray-500">URL:</span> ' + escapeHtml(inst.url) + '</p>' +
            (isOnline ? '<p><span class="text-gray-500">Project:</span> <span class="text-white font-medium">' + escapeHtml(inst.current_project || "N/A") + '</span></p>' +
            '<p><span class="text-gray-500">Items:</span> ' + itemsLine + '</p>' +
            bwHtml : "") +
            '</div>' + errorMsg + itemsHtml +
            (inst.last_seen ? '<p class="text-xs text-gray-600 mt-2">Last seen: ' + formatTime(inst.last_seen) + '</p>' : "") +
            '</div>';
    }

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
        var d = formData.get("downloader"), c = formData.get("concurrent_items");
        var u = formData.get("http_username"), p = formData.get("http_password");
        var r = formData.get("shared_rsync_threads");
        if (d) settings.downloader = d;
        if (c) settings.concurrent_items = parseInt(c);
        if (u) settings.http_username = u;
        if (p) settings.http_password = p;
        if (r) settings.shared_rsync_threads = parseInt(r);
        if (Object.keys(settings).length === 0) { showToast("No settings specified", "error"); return; }

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
            settingsStatus.textContent = ok + " OK" + (fail > 0 ? ", " + fail + " failed" : "");
            settingsStatus.className = "text-sm self-center ml-4 " + (fail > 0 ? "text-yellow-400" : "text-green-400");
            showToast("Settings applied to " + ok + " instance(s)", fail > 0 ? "error" : "success");
        } catch (e) {
            settingsStatus.textContent = "Request failed";
            settingsStatus.className = "text-sm self-center ml-4 text-red-400";
            showToast("Failed: " + e.message, "error");
        }
    });

    function updateInstanceCheckboxes(instances) {
        var html = "";
        for (var i = 0; i < instances.length; i++) {
            var inst = instances[i];
            var dot = inst.connection_state === "online" ? "bg-green-500" : "bg-red-500";
            html += '<label class="flex items-center gap-2 bg-gray-800 px-3 py-1.5 rounded-lg cursor-pointer text-sm hover:bg-gray-700 transition-colors">' +
                '<input type="checkbox" class="instance-checkbox rounded" value="' + escapeHtml(inst.name) + '" checked>' +
                '<span class="w-2 h-2 rounded-full ' + dot + '"></span>' +
                escapeHtml(inst.name) + '</label>';
        }
        instanceCheckboxes.innerHTML = html;
    }

    // ---- Project selector ----
    async function loadProjects() {
        try {
            var res = await fetch("/api/projects");
            if (!res.ok) return;
            var projects = await res.json();
            var html = '<option value="">-- Select Project --</option>';
            for (var i = 0; i < projects.length; i++) {
                html += '<option value="' + escapeHtml(projects[i].name) + '">' +
                    escapeHtml(projects[i].title || projects[i].name) + '</option>';
            }
            projectSelect.innerHTML = html;
        } catch (e) {
            console.error("Failed to load projects:", e);
        }
    }

    btnApplyProject.addEventListener("click", async function () {
        var proj = projectSelect.value;
        if (!proj) { showToast("Select a project first", "error"); return; }
        var selected = [];
        document.querySelectorAll(".instance-checkbox:checked").forEach(function (cb) { selected.push(cb.value); });
        if (selected.length === 0) { showToast("No instances selected", "error"); return; }

        projectStatus.textContent = "Applying...";
        projectStatus.className = "text-sm ml-2 text-yellow-400";
        try {
            var res = await fetch("/api/project/bulk", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ instance_names: selected, project_name: proj }),
            });
            var data = await res.json();
            var ok = Object.values(data.results).filter(function (r) { return r.status === "ok"; }).length;
            var fail = Object.values(data.results).filter(function (r) { return r.status !== "ok"; }).length;
            projectStatus.textContent = ok + " OK" + (fail > 0 ? ", " + fail + " failed" : "");
            projectStatus.className = "text-sm ml-2 " + (fail > 0 ? "text-yellow-400" : "text-green-400");
            showToast("Project set on " + ok + " instance(s)", fail > 0 ? "error" : "success");
        } catch (e) {
            projectStatus.textContent = "Failed";
            projectStatus.className = "text-sm ml-2 text-red-400";
            showToast("Failed: " + e.message, "error");
        }
    });

    // ---- Add Instance ----
    btnAddInstance.addEventListener("click", function () { addInstanceModal.classList.remove("hidden"); });
    btnCancelAdd.addEventListener("click", function () { addInstanceModal.classList.add("hidden"); });
    addInstanceForm.addEventListener("submit", async function (e) {
        e.preventDefault();
        var fd = new FormData(addInstanceForm);
        var body = { name: fd.get("name"), host: fd.get("host"), port: parseInt(fd.get("port")) || 8001 };
        var hu = fd.get("http_username"), hp = fd.get("http_password");
        if (hu) body.http_username = hu;
        if (hp) body.http_password = hp;
        try {
            var res = await fetch("/api/instances", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            if (res.ok) {
                showToast('Instance "' + body.name + '" added', "success");
                addInstanceModal.classList.add("hidden");
                addInstanceForm.reset();
            } else {
                var data = await res.json();
                showToast("Error: " + (data.detail || "Unknown"), "error");
            }
        } catch (e) { showToast("Failed: " + e.message, "error"); }
    });

    // ---- Edit Instance ----
    btnCancelEdit.addEventListener("click", function () { editInstanceModal.classList.add("hidden"); });
    async function editInstance(name) {
        try {
            var res = await fetch("/api/instances/" + encodeURIComponent(name));
            if (!res.ok) { showToast("Failed to fetch instance details", "error"); return; }
            var data = await res.json();
            editInstanceForm.querySelector('[name="name"]').value = data.name || name;
            editInstanceForm.querySelector('[name="host"]').value = data.host || "";
            editInstanceForm.querySelector('[name="port"]').value = data.port || 8001;
            editInstanceForm.querySelector('[name="http_username"]').value = "";
            editInstanceForm.querySelector('[name="http_password"]').value = "";
            editInstanceModal.classList.remove("hidden");
        } catch (e) { showToast("Error: " + e.message, "error"); }
    }
    editInstanceForm.addEventListener("submit", async function (e) {
        e.preventDefault();
        var fd = new FormData(editInstanceForm);
        var name = fd.get("name");
        var body = { host: fd.get("host"), port: parseInt(fd.get("port")) || 8001 };
        var hu = fd.get("http_username"), hp = fd.get("http_password");
        if (hu) body.http_username = hu;
        if (hp) body.http_password = hp;
        try {
            var res = await fetch("/api/instances/" + encodeURIComponent(name), {
                method: "PUT", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            if (res.ok) {
                showToast('Instance "' + name + '" updated', "success");
                editInstanceModal.classList.add("hidden");
            } else {
                var data = await res.json();
                showToast("Error: " + (data.detail || "Unknown"), "error");
            }
        } catch (e) { showToast("Failed: " + e.message, "error"); }
    });

    // ---- Remove Instance ----
    async function removeInstance(name) {
        if (!confirm('Remove instance "' + name + '" from the dashboard?')) return;
        try {
            var res = await fetch("/api/instances/" + encodeURIComponent(name), { method: "DELETE" });
            if (res.ok) { showToast('Instance "' + name + '" removed', "info"); }
            else { showToast("Failed to remove instance", "error"); }
        } catch (e) { showToast("Error: " + e.message, "error"); }
    }

    // ---- Init ----
    connectWebSocket();
    loadProjects();
})();