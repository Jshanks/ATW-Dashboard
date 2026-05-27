# ATW Dashboard

A centralized monitoring and control dashboard for managing multiple [ArchiveTeam Warrior](https://wiki.archiveteam.org/index.php/ArchiveTeam_Warrior) instances.

![License](https://img.shields.io/badge/license-MIT-blue.svg)

## Features

- **Web-Based Instance Management** -- Add, edit, and remove warrior instances directly from the dashboard UI. No config files to edit.
- **Persistent Storage** -- Instance configurations are saved to `data/instances.json` and survive container restarts.
- **Multi-Instance Monitoring** -- Connect to warriors across single-IP:multi-port or multi-IP topologies.
- **Real-Time Status** -- See per-item pipeline status (waiting, downloading, processing, uploading) and current project for each instance.
- **Bulk Configuration** -- Change nickname, concurrent items, HTTP credentials, and rsync threads across all (or selected) instances at once.
- **Auto-Reconnect** -- Automatically reconnects to instances as they go offline and come back up, with exponential backoff.
- **Docker Deployment** -- Ship as a single Docker container.

## Quick Start

### Using Docker Compose (Recommended)

```bash
# Clone the repository
git clone https://github.com/yourusername/atw-dashboard.git
cd atw-dashboard

# Create data directory for persistence
mkdir -p data

# Start the dashboard
docker compose up -d

# Visit http://localhost:8080
# Click "Add Instance" to connect your first warrior
```

### Using Docker

```bash
docker run -d \
  --name atw-dashboard \
  -p 8080:8080 \
  -v ./data:/app/data \
  ghcr.io/yourusername/atw-dashboard:latest
```

### Manual (Development)

```bash
pip install -r requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8080 --reload
# Visit http://localhost:8080
```

## Managing Instances

All instance management is done through the web UI:

1. **Add**: Click "Add Instance" in the header. Enter a name, host IP, port (default 8001), and optional HTTP credentials.
2. **Edit**: Hover over an instance card and click the pencil icon. Change the host, port, or credentials and click Save. The connection will restart automatically.
3. **Remove**: Hover over an instance card and click the X icon. Confirm the removal.

All changes are persisted to `data/instances.json` automatically.

## Configuration

The optional `config.yml` file controls **dashboard-level settings only** (not instances):

```yaml
dashboard:
  title: "ATW Dashboard"
  poll_interval: 5        # seconds between status polls
  reconnect_base: 5       # base reconnect delay in seconds
  reconnect_max: 60       # max reconnect delay in seconds
```

These can also be set via environment variables: `DASHBOARD_TITLE`, `POLL_INTERVAL`, `RECONNECT_BASE`, `RECONNECT_MAX`.

## Data Persistence

Instance configurations are stored in `data/instances.json`. Mount this directory as a volume to persist across container restarts:

```yaml
volumes:
  - ./data:/app/data
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
| `DASHBOARD_TITLE` | `ATW Dashboard` | Dashboard title |
| `POLL_INTERVAL` | `5` | Seconds between status polls |
| `RECONNECT_BASE` | `5` | Base reconnect delay (seconds) |
| `RECONNECT_MAX` | `60` | Max reconnect delay (seconds) |
| `DATA_DIR` | `data` | Directory for instances.json |
| `CONFIG_PATH` | `config.yml` | Optional config file path |
| `LOG_LEVEL` | `info` | Logging level |

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/instances` | List all instances and status |
| GET | `/api/instances/{name}` | Get a specific instance |
| POST | `/api/instances` | Add a new instance (persisted) |
| PUT | `/api/instances/{name}` | Edit instance connection details (persisted) |
| DELETE | `/api/instances/{name}` | Remove an instance (persisted) |
| POST | `/api/instances/{name}/settings` | Push warrior settings |
| POST | `/api/settings/bulk` | Bulk push warrior settings |
| WS | `/ws` | Real-time status WebSocket |

## Contributing

Pull requests welcome! Please open an issue first to discuss major changes.

## License

MIT -- see [LICENSE](LICENSE)
