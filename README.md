# ATW Dashboard

A responsive, real-time monitoring and control dashboard for fleets of [ArchiveTeam Warrior](https://wiki.archiveteam.org/index.php/ArchiveTeam_Warrior) instances.

ATW Dashboard is designed for operators running many Warriors across one or more hosts. It provides a single view of project state, bandwidth, tracker activity, in-progress items, and Project Health, together with bulk controls for project selection, settings, pause/resume, import/export, and immediate restart.

![ATW Dashboard](dashboard_screenshot.png)

## Highlights

- **Live fleet monitoring**: WebSocket updates for every Warrior, with per-card smart diffing so only changed cards re-render.
- **ArchiveTeam's Choice support**: Choice is pinned to the top of the project list. Cards and grouped views show the project Choice Warriors are actually running.
- **Filtering and grouping**: Show all, online, or offline Warriors; display a flat grid or group Warriors by actual project.
- **Project Health labels**: Grouped project headings identify the configured projects as `Primary · Project Name` and `Secondary · Project Name` while retaining ordinary names for third-party projects.
- **Card-based selection**: Select Warriors directly on their cards, Shift-click ranges, select every visible Warrior, or select an entire project group, including collapsed groups.
- **Bulk Settings & Project**: Apply nickname, concurrent-item count, rsync-thread count, and project changes through one confirmation action. Every field defaults to **Unchanged**.
- **Pause / Resume**: Pause selected Warriors for 1, 3, 6, 12, or 24 hours, or indefinitely, with persistent timed auto-resume.
- **Immediate restart**: Restart selected Warriors through the native Warrior `/api/stop_now` endpoint to abandon stuck work and return items to the tracker.
- **Sequential bulk progress**: Settings, project changes, pause, resume, and restart run in groups of 10. One status message is replaced for each group instead of appending a scrolling progress log.
- **Bulk add, import, and export**: Add port ranges or IP ranges, import CSV/JSON, preview changes before applying, and export the current instance inventory.
- **Optional instance names**: If a name is omitted, the dashboard uses `host:port`.
- **24-hour activity chart**: Combined bandwidth and completed-item history with cumulative totals.
- **Tracker integration**: Live per-project ArchiveTeam tracker metrics, contribution deltas, TODO estimates, and estimated queue runway.
- **Project Health automation**: Per-Warrior normalized baselines, queue-aware automatic failover, automatic recovery confirmation, and automatic failback to primary.
- **Responsive interface**: Desktop and mobile layouts, a floating action bar pinned to the visible screen, and a Project Health badge beside the connection indicator.
- **Persistent state**: Instance inventory, chart history, pause state, tracker baselines, Project Health policy, normalized Project Health samples, role assignments, cooldowns, and decision history survive dashboard restarts.

## Important Restart Behavior

The **Restart now** action calls the Warrior's native `/api/stop_now` endpoint.

This action:

- stops the Warrior process immediately;
- abandons in-progress items so the tracker can redistribute them;
- briefly disconnects the Warrior from the dashboard;
- relies on the Warrior container's restart policy to start the process again.

Warrior containers must use a restart policy such as:

```yaml
restart: unless-stopped
```

or:

```yaml
restart: always
```

Restart requests are sent in groups of 10. The existing progress text is replaced as each group starts:

```text
Restarting instances 1–10 of 99
Restarting instances 11–20 of 99
Restarting instances 21–30 of 99
```

Only one progress line is displayed at a time. Restarting remains separate from Pause/Resume because restarting discards active work.

## Interface and Fleet Controls

### Selecting Warriors

Warriors are selected on dashboard cards:

- click a card checkbox to select one Warrior;
- Shift-click another checkbox to select the visible range between them;
- use **Select all** to select every Warrior shown by the current filter;
- in grouped mode, use the group-header checkbox to select every Warrior in that project, including Warriors hidden inside a collapsed group.

Selections are not saved between browser sessions. Refreshing the page clears the selection to reduce the risk of applying a later bulk action to a stale selection.

When at least one Warrior is selected, a floating action bar provides:

- **Settings & project**
- **Pause / Resume**
- **Restart now**
- **Clear selection**

### Settings & Project

The settings dialog uses one **Apply** button. Each field starts as **Unchanged**.

| Field | Valid values |
|---|---|
| Project | Unchanged, ArchiveTeam's Choice, or an available project |
| Nickname | Any non-empty nickname; blank means unchanged |
| Concurrent Items | 1–6, or unchanged |
| Rsync Threads | 1–40, or unchanged |

The Apply button displays the number of pending changes. Settings are sent first, followed by the project switch. Operations are processed in groups of 10. Partial failures remain visible in the dialog so the operation can be reviewed or retried.

### Filters and Groups

The dashboard supports:

- **Show**: All, Online, Offline
- **Group**: Flat, By project

ArchiveTeam's Choice is always the first project group. Configured Project Health projects are decorated using their actual project placement:

```text
Primary · Telegram
Secondary · URLs
US Government
YouTube
```

Other projects are ordered alphabetically, followed by paused and offline/unassigned groups. Group state remains keyed by the raw project slug, so changing Project Health policy does not reset collapsed groups.

Intentional restarts are tracked separately and do not count as outages in the Offline total or filter.

## Project Health

Project Health is an optional automation layer for keeping a fleet productively assigned when a primary ArchiveTeam project becomes stalled, paused, or exhausted.

Project Health is designed for a non-mission-critical volunteer archive fleet. The goal is to avoid leaving Warriors idle for extended periods while still preventing rapid project switching based on one transient sample.

### Project Roles

Each managed Warrior may be assigned one Project Health role:

| Role | Behavior |
|---|---|
| Primary canary | Remains on the primary project during ordinary failover so primary recovery can continue to be measured |
| Fallback fleet | May automatically move between primary and secondary projects |
| Unassigned | Not controlled by Project Health switching |

When the configured primary project is changed, the dashboard can prompt to move both the Fallback fleet and primary canaries to the new primary.

### Normalized Baselines

Project Health records project-specific tracker activity and the number of eligible dashboard-managed Warriors contributing to each sample.

The baseline uses:

```text
items per eligible Warrior-hour
bytes per eligible Warrior-hour
```

rather than raw fleet totals.

Eligible Warriors are:

- managed by this dashboard;
- assigned the `fallback_fleet` role;
- online;
- not paused;
- not restarting;
- actually running the evaluated project.

This prevents a deliberate fleet-size reduction from appearing as a proportional throughput collapse. For example:

```text
100 Warriors at 60,000 items/hour = 600 items/Warrior-hour
 50 Warriors at 30,000 items/hour = 600 items/Warrior-hour
```

The normalized activity ratio remains approximately `1.00x`.

Samples created before normalized denominators were introduced are not used for automated baseline decisions. After upgrading from an older release, Project Health rebuilds a normalized baseline.

### Baseline Requirements

The minimum baseline requires:

- the configured minimum elapsed window;
- at least 75% sample coverage;
- sufficient item or byte activity volume;
- valid eligible-Warrior denominators.

Operator-facing baseline prerequisites use the state:

```text
Project Health: Building baseline
```

Detailed evidence still identifies whether the limiting factor is elapsed time, coverage, or activity volume.

After the minimum baseline is established, a longer configured full baseline may replace the shorter comparison window for more stable activity norms.

### Automatic Failover

Project Health evaluates the primary project every five minutes. Automatic failover requires three consecutive adverse evaluations using distinct tracker samples.

The header may progress through:

```text
Project Health: Evaluating stalled project 1/3
Project Health: Evaluating stalled project 2/3
Project Health: Evaluating stalled project 3/3
Project Health: Failing over
Project Health: Failover cooldown
```

A throughput stall requires both normalized item and byte activity to remain below 25% of baseline. Queue evidence can also identify an exhausted or nearly exhausted project.

During failover:

- eligible Fallback-fleet Warriors on primary move to secondary;
- primary canaries remain on primary;
- Warriors already on secondary are reported separately;
- Warriors on unrelated third projects are skipped;
- Warriors with an unknown current project are skipped;
- offline, paused, restarting, or missing Warriors are reported as failures.

### Queue-Aware Decisions

TODO values are treated as queue evidence rather than as the only health signal.

- Fresh TODO `0`: queue exhausted
- Estimated queue runway under 30 minutes: queue nearly exhausted
- TODO unavailable: neutral
- Large positive TODO plus normalized item and byte activity below 25%: stalled distribution or processing

TODO polling is adaptive:

| Evidence | Normal operation | During confirmation |
|---|---:|---:|
| Primary TODO | 15 minutes | 5 minutes |
| Secondary TODO | 30 minutes | 5 minutes |
| Final destination check | Not applicable | Immediately before switching |

Tracker activity samples continue at one-minute resolution.

### Cooldown

After automatic failover or automatic failback, Project Health starts the configured shared cooldown.

While cooldown is active, the header reports:

```text
Project Health: Failover cooldown
```

Health evidence continues to be collected, but no automatic transition occurs until cooldown expires.

### Automatic Failback

After cooldown, the primary can recover through the primary canaries. Automatic failback requires:

- the fleet is in secondary or mixed mode;
- the normalized primary baseline is established;
- the latest primary sample is current;
- normalized item or byte activity reaches at least 50% of primary baseline;
- primary TODO evidence is available and current;
- estimated primary queue runway is at least 60 minutes;
- recovery persists for three consecutive five-minute evaluations.

The header may progress through:

```text
Project Health: Evaluating primary recovery 1/3
Project Health: Evaluating primary recovery 2/3
Project Health: Evaluating primary recovery 3/3
Project Health: Returning to primary
Project Health: Failover cooldown
```

Automatic failback moves eligible Fallback-fleet Warriors from secondary to primary. Third-project and unknown-project assignments remain untouched.

### Manual Switching

The Project Health panel provides:

- **Fail over to secondary**
- **Fail over to primary**

Manual switching uses the same safe placement rules as automation and reports:

```text
switched
already_at_target
skipped_unrelated_project
skipped_project_unknown
failed
```

Manual switching does not require automatic failover to be enabled.

### Changing the Primary Project

Saving a different primary project displays a **Switch now / Not now** prompt.

- **Not now** saves the policy without moving Warriors.
- **Switch now** moves eligible Fallback-fleet Warriors and primary canaries to the new primary.
- Warriors on unrelated third projects remain untouched.
- Warriors with an unknown current project remain untouched.

The preview displays eligible, already-at-target, other-project, and unknown-project counts.

### Project Health Evidence

The Project Health panel displays:

- primary recent activity;
- primary tracker queue and estimated runway;
- primary normalized baseline ratios;
- secondary recent activity;
- secondary tracker queue and estimated runway;
- average eligible-Warrior population;
- primary, secondary, other, and unknown placement counts;
- failover/failback confirmation progress;
- the latest sample age;
- a plain-language decision explanation;
- a decision log showing 10 entries initially, with **Show 10 more** and **Show less**.

## Adding and Importing Warriors

### Single Warrior

Open **Add Instances**, choose **Single**, and provide:

- Host
- Port, default `8001`
- Optional name
- Optional HTTP authentication

If the name is blank, the dashboard uses `host:port`. Duplicate `host:port` combinations are rejected.

### Bulk Input

Open **Add Instances**, choose **Bulk / Import**, and enter one or more lines.

```text
192.168.1.50
192.168.1.50:8002
192.168.1.50:8001-8099
192.168.1.50-60
192.168.1.50-60:8001
warrior-box.lan:8001
[fd00::50]:8001
```

| Syntax | Meaning |
|---|---|
| `192.168.1.50` | One Warrior on default port 8001 |
| `192.168.1.50:8002` | One Warrior on an explicit port |
| `192.168.1.50:8001-8099` | Port range on one host |
| `192.168.1.50-60` | Last-octet IP range on port 8001 |
| `192.168.1.50-60:8001` | Last-octet IP range on an explicit port |
| `[fd00::50]:8001` | Bracketed IPv6 address |

Text after `#` is treated as a comment:

```text
192.168.1.50:8001-8099  # host 1
192.168.1.60-70          # one Warrior per IP
```

The dashboard previews every candidate before import. Invalid entries and duplicates are skipped with a reason. Imports are limited to 1,000 Warriors per request.

### CSV Import

CSV must contain a `host` column. Other columns are optional:

```csv
name,host,port,http_username,http_password
,192.168.1.50,8001,,
warrior-2,192.168.1.50,8002,,
```

A blank name defaults to `host:port`, and a blank port defaults to `8001`.

### JSON Import and Export

**Export** downloads the saved instance inventory as JSON. Credentials are excluded by default.

The exported file can be imported into another ATW Dashboard. A raw `data/instances.json` list is also accepted.

## Tech Stack

| Component | Technology |
|---|---|
| Backend | Python 3.13+, FastAPI, Pydantic, httpx |
| Frontend | Vanilla JavaScript, Tailwind CSS, Chart.js 4 |
| Warrior communication | SockJS xhr-polling plus native Warrior HTTP endpoints |
| Dashboard updates | WebSocket broadcast from FastAPI to connected browsers |
| Storage | JSON files; no database required |
| Container image | Alpine-based Python image |

## Quick Start

### Docker Compose

```yaml
services:
  atw-dashboard:
    image: ghcr.io/jshanks/atw-dashboard:latest
    container_name: atw-dashboard
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
    environment:
      - LOG_LEVEL=info
    restart: unless-stopped
```

Start the dashboard:

```bash
docker compose up -d
```

Open:

```text
http://localhost:8080
```

Then choose **Add Instances**.

### Build Locally

```bash
git clone https://github.com/Jshanks/ATW-Dashboard.git
cd ATW-Dashboard
docker compose up -d --build
```

### Manual Python Start

```bash
pip install -r requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8080
```

## Configuration

Configuration can be supplied through environment variables or `config.yml`.

| Environment variable | Default | Purpose |
|---|---|---|
| `DATA_DIR` | `/app/data` in the container | Persistent state directory |
| `DASHBOARD_TITLE` | `ATW Dashboard` | Dashboard title |
| `POLL_INTERVAL` | `5` | Warrior polling interval in seconds |
| `RECONNECT_BASE` | `5` | Base reconnect delay in seconds |
| `RECONNECT_MAX` | `60` | Maximum reconnect delay in seconds |
| `LOG_LEVEL` | `info` | Python logging level |
| `APP_VERSION` | `dev` | Version displayed by the dashboard |

Project Health policy, including primary, secondary, role assignments, baseline windows, confirmation count, cooldown, and automatic switching, is configured through the Project Health interface and stored persistently.

## Persistent Data

All persistent state is stored under `DATA_DIR`.

| File | Purpose |
|---|---|
| `instances.json` | Warrior host, port, display name, and optional HTTP credentials |
| `history.json` | 24-hour activity graph data |
| `pause.json` | Paused-Warrior state and auto-resume times |
| `tracker_baseline.json` | Per-project tracker totals used for accurate deltas |
| `project_health.json` | Project Health policy, roles, normalized samples, queue evidence, cooldown, runtime state, and decision log |

Back up the complete data directory before changing storage or moving the dashboard.

## Architecture

```text
Browser
   ⇅ WebSocket
FastAPI dashboard
   ├─ SockJS xhr-polling ── Warrior instances
   ├─ Warrior HTTP API ─── settings, projects, pause, immediate restart
   ├─ ArchiveTeam tracker ─ project and contribution metrics
   └─ JSON persistence ──── /app/data
```

The backend opens a SockJS session to each Warrior, receives real-time events such as `project.refresh`, bandwidth, and `project.item.task`, and aggregates the data into dashboard state. State is broadcast to connected browsers when the serialized payload changes.

The frontend maintains a fingerprint for each Warrior card and redraws only cards whose visible data changed. Structural changes, such as filtering, grouping, collapsing a group, moving a Warrior between projects, or changing Project Health policy, rebuild the relevant grid layout.

The project list is fetched from WarriorHQ and cached. ArchiveTeam's Choice is a synthetic first entry with the Warrior value `auto`; the dashboard separately tracks the selected value and the project actually running. This preserves Choice through pause/resume while keeping tracker links and Project Health placement accurate.

Project Health obtains project-specific cumulative tracker totals for the configured downloader nickname, calculates deltas, and divides those deltas by the eligible dashboard-managed Warrior population for that project. Primary and secondary activity are evaluated independently and are never blended.

## API Overview

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/instances` | Current dashboard state |
| POST | `/api/instances` | Add one Warrior |
| POST | `/api/instances/bulk` | Preview or apply bulk import |
| GET | `/api/instances/export` | Export saved Warrior inventory |
| PUT | `/api/instances/{name}` | Edit a Warrior |
| DELETE | `/api/instances/{name}` | Remove a Warrior |
| POST | `/api/settings/bulk` | Apply settings to selected Warriors |
| POST | `/api/project/bulk` | Change project for selected Warriors |
| POST | `/api/pause` | Pause selected Warriors |
| POST | `/api/resume` | Resume selected Warriors |
| POST | `/api/restart` | Immediately restart selected Warriors |
| GET | `/api/projects` | Cached ArchiveTeam project list |
| GET | `/api/tracker` | Current tracker summary |
| GET | `/api/history` | Activity chart data |
| GET | `/api/project-health` | Current Project Health policy, readiness, evidence, placement, runtime state, and decision log |
| PUT | `/api/project-health/policy` | Save Project Health policy |
| PUT | `/api/project-health/roles` | Save primary-canary and Fallback-fleet role assignments |
| POST | `/api/project-health/failover` | Manually switch Project Health-managed Warriors to primary or secondary |
| POST | `/api/project-health/baseline/reset` | Reset the selected Project Health baseline |

## Security and Release Workflow

Recommended release flow:

1. Push the target commit to `main`.
2. Run CodeQL, Gitleaks, Trivy filesystem scanning, Project Health smoke tests, and a Trivy scan of the locally built container image.
3. Do not publish an image from `main`.
4. After validation passes, create a version tag on the same commit.
5. The tag workflow builds the release image, verifies compiled Tailwind delivery over HTTP, runs WebSocket and Project Health smoke tests inside the release image, scans the image, and publishes the versioned image plus `latest`.
6. Verify published image digests.

Release images are published only from `v*` tags. Main-branch pushes are for validation and security scanning.

## Operational Notes

- The dashboard must be able to reach each Warrior's HTTP port, normally `8001`.
- HTTP authentication can be configured per Warrior if required.
- Immediate restart requires the Warrior container to restart automatically.
- If ArchiveTeam's Choice points to a project that is not runnable, the Warrior may fall through to another runnable project. The dashboard displays the project actually reported by Choice Warriors.
- Browser preferences such as filters, grouping, column count, and collapsed groups are stored locally in the browser.
- Selecting a different Show filter clears selected Warriors that are no longer visible.
- The configured tracker nickname should represent the dashboard-managed fleet consistently. Project Health tracker queries remain project-specific.
- After upgrading from a pre-normalization Project Health release, allow the normalized baseline to rebuild before expecting automatic failover or failback.
- Automatic failback does not occur until cooldown has expired, recovery has persisted for three five-minute evaluations, and the primary has at least 60 minutes of estimated queue runway.

> [!WARNING]
> This project is **vibecoded** and intended for **personal use only**. It was built iteratively with AI assistance and has not been formally tested or audited.
>
> **No support will be provided. Pull requests will not be reviewed or merged.**

## License

This project is licensed under the [Creative Commons Attribution-NonCommercial 4.0 International License](LICENSE) (CC BY-NC 4.0).

You may share and adapt this work for non-commercial purposes with attribution. See [LICENSE](LICENSE) for the full terms.

© 2026
