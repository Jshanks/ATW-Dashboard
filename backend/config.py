"""Configuration loader for ATW Dashboard (dashboard settings only)."""

import os
import logging

logger = logging.getLogger(__name__)

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


class DashboardConfig:
    """Dashboard-level settings. Warrior instances are managed via the web GUI."""

    def __init__(self):
        self.title = "ATW Dashboard"
        self.poll_interval = 5
        self.reconnect_base = 5
        self.reconnect_max = 60

    @classmethod
    def load(cls, path=None):
        config_path = path or os.environ.get("CONFIG_PATH", "config.yml")
        config = cls()

        raw = {}
        if HAS_YAML:
            try:
                with open(config_path, "r") as fh:
                    raw = yaml.safe_load(fh) or {}
            except FileNotFoundError:
                logger.info("No config.yml found at %s, using env vars / defaults.", config_path)
            except Exception as exc:
                logger.warning("Error reading config.yml: %s", exc)

        dash = raw.get("dashboard", {})
        config.title = os.environ.get(
            "DASHBOARD_TITLE",
            dash.get("title", config.title),
        )
        config.poll_interval = int(os.environ.get(
            "POLL_INTERVAL",
            dash.get("poll_interval", config.poll_interval),
        ))
        config.reconnect_base = int(os.environ.get(
            "RECONNECT_BASE",
            dash.get("reconnect_base", config.reconnect_base),
        ))
        config.reconnect_max = int(os.environ.get(
            "RECONNECT_MAX",
            dash.get("reconnect_max", config.reconnect_max),
        ))

        logger.info(
            "Dashboard config: poll=%ds, reconnect=%d-%ds",
            config.poll_interval, config.reconnect_base, config.reconnect_max,
        )
        return config
