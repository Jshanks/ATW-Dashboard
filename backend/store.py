"""Persistent JSON storage for warrior instance configurations."""

import json
import os
import threading
import logging

logger = logging.getLogger(__name__)
def _safe_log(value):
    """Strip newlines/control characters from user input before logging."""
    if not isinstance(value, str):
        return value
    return value.replace('\n', '').replace('\r', '')

DATA_DIR = os.environ.get("DATA_DIR", "data")
INSTANCES_FILE = os.path.join(DATA_DIR, "instances.json")

_lock = threading.Lock()


def _ensure_file():
    """Create data directory and instances.json if they do not exist."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(INSTANCES_FILE):
        with open(INSTANCES_FILE, "w") as fh:
            json.dump([], fh)
        logger.info("Created empty instances file at %s", INSTANCES_FILE)


def load():
    """Load all instance configs from disk. Returns a list of dicts."""
    with _lock:
        _ensure_file()
        try:
            with open(INSTANCES_FILE, "r") as fh:
                data = json.load(fh)
            if not isinstance(data, list):
                logger.warning("instances.json was not a list, resetting")
                return []
            return data
        except (json.JSONDecodeError, IOError) as exc:
            logger.error("Failed to read instances.json: %s", exc)
            return []


def save(instances):
    """Write the full instances list to disk."""
    with _lock:
        _ensure_file()
        with open(INSTANCES_FILE, "w") as fh:
            json.dump(instances, fh, indent=2)
        logger.debug("Saved %d instances to disk", len(instances))


def get_all():
    """Return all saved instance configs."""
    return load()


def add(instance_dict):
    """Append a new instance config and persist."""
    instances = load()
    instances.append(instance_dict)
    save(instances)
    logger.info("Persisted new instance: %s", instance_dict.get("name", "?"))
    return True


def remove(name):
    """Remove an instance by name and persist. Returns True if found."""
    instances = load()
    before = len(instances)
    instances = [i for i in instances if i.get("name") != name]
    if len(instances) == before:
        return False
    save(instances)
    logger.info("Removed instance from store: %s", _safe_log(name))
    return True


def update(name, fields):
    """Update fields on an existing instance. Returns True if found."""
    instances = load()
    found = False
    for inst in instances:
        if inst.get("name") == name:
            for k, v in fields.items():
                if v is not None:
                    inst[k] = v
            found = True
            break
    if found:
        save(instances)
        logger.info("Updated instance in store: %s", _safe_log(name))
    return found
