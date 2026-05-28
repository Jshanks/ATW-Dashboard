# ATW Dashboard

A centralized monitoring and control dashboard for managing multiple [ArchiveTeam Warrior](https://wiki.archiveteam.org/index.php/ArchiveTeam_Warrior) instances.

![License](https://img.shields.io/badge/license-MIT-blue.svg)

## Features

- **Web-Based Instance Management** -- Add, edit, and remove warrior instances directly from the dashboard UI. No config files to edit.
- **Persistent Storage** -- Instance configurations are saved to `data/instances.json` inside a Docker named volume and survive container restarts and rebuilds.
- **Multi-Instance Monitoring** -- Connect to warriors across single-IP:multi-port or multi-IP topologies.
- **Real-Time Status** -- See per-item pipeline status (waiting, downloading, processing, uploading) and current project for each instance.
- **Bulk Configuration** -- Change nickname, concurrent items, HTTP credentials, and rsync threads across all (or selected) instances at once.
- **Auto-Reconnect** -- Automatically reconnects to instances as they go offline and come back up, with exponential backoff.
- **Docker Deployment** -- Ship as a single Docker container. All configuration via environment variables.

## Quick Start

### Using Docker Compose (Recommended)

```bash
# Clone the repository
git clone https://github.com/yourusername/atw-dashboard.git
cd atw-dashboard

# Start the dashboard
docker compose up -d

# Visit http://localhost:8080
# Click "Add Instance" to connect your first warrior
```

That's it. The named volume `atw-data` is created automatically on first run.

### Using Docker

```bash
docker volume create atw-data

docker run -d \\
  --name atw-dashboard \\
  -p 8080:8080 \\
  -v atw-data:/app/data \\
  -e DASHBOARD_TITLE="ATW Dashboard" \\
  -e POLL_INTERVAL=5 \\
  ghcr.io/yourusername/atw-dashboard:latest
```

### Manual (Development)

```bash
pip install -r requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8080 --reload
# Visit http://localhost:8080
```

## Managing Instances

All instance management is done through the web UI -- no config files needed:

1. **Add**: Click "Add Instance" in the header. Enter a name, host IP, port (default 8001), and optional HTTP credentials.
2. **Edit**: Hover over an instance card and click the pencil icon. Change the host, port, or credentials and click Save. The connection restarts automatically.
3. **Remove**: Hover over an instance card and click the X icon. Confirm the removal.

All changes are persisted to `data/instances.json` automatically.

## Configuration

All dashboard settings are controlled via **environment variables** in your compose file:

```yaml
environment:
  - LOG_LEVEL=info
  - DASHBOARD_TITLE=ATW Dashboard
  - POLL_INTERVAL=5
  - RECONNECT_BASE=5
  - RECONNECT_MAX=60
```

A `config.yml` file is also supported as a fallback but is entirely optional. Environment variables always take precedence.

## Data Persistence

Instance configurations are stored in `data/instances.json` inside the container. A **named volume** keeps this data safe across container restarts and image updates:

```yaml
volumes:
  - atw-data:/app/data

volumes:
  atw-data:
```

To back up your instance list:

```bash
docker cp atw-dashboard:/app/data/instances.json ./instances-backup.json
```

To restore:

```bash
docker cp ./instances-backup.json atw-dashboard:/app/data/instances.json
docker restart atw-dashboard
```

## Architecture

```
+---------------------------------------------------+
|                  ATW Dashboard (:8080)              |
|                                                    |
|  +----------+   WebSocket    +------------------+ |
|  | Frontend  |<------------>|  FastAPI Backend   | |
|  | (Browser) |              | + Instance Store   | |
|  +----------+              +--------+---------+ |
|                                      |           |
+--------------------------------------+-----------+
                                       | HTTP Poll
                   +-------------------+-------------------+
                   v                   v                   v
            +-------------+   +-------------+    +-------------+
            |  ATW :8001  |   |  ATW :8002  |    |  ATW :8001  |
            | 192.168.1.1 |   | 192.168.1.1 |    |  10.0.0.50  |
            +-------------+   +-------------+    +-------------+
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DASHBOARD_TITLE` | `ATW Dashboard` | Dashboard title shown in the header |
| `POLL_INTERVAL` | `5` | Seconds between status polls per instance |
| `RECONNECT_BASE` | `5` | Base reconnect delay in seconds |
| `RECONNECT_MAX` | `60` | Maximum reconnect delay in seconds |
| `DATA_DIR` | `data` | Directory for instances.json persistence |
| `CONFIG_PATH` | `config.yml` | Optional config file path (env vars take precedence) |
| `LOG_LEVEL` | `info` | Logging level (debug, info, warning, error) |

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/instances` | List all instances and status |
| GET | `/api/instances/{name}` | Get a specific instance |
| POST | `/api/instances` | Add a new instance (persisted) |
| PUT | `/api/instances/{name}` | Edit instance connection details (persisted) |
| DELETE | `/api/instances/{name}` | Remove an instance (persisted) |
| POST | `/api/instances/{name}/settings` | Push warrior settings (nickname, concurrent, etc.) |
| POST | `/api/settings/bulk` | Bulk push warrior settings to multiple instances |
| GET | `/api/health` | Health check |
| GET | `/api/config` | Dashboard configuration |
| WS | `/ws` | Real-time status WebSocket |


