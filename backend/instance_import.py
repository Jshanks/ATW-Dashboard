"""Parse bulk-add / import input into warrior instance configs.

Pure functions only: no disk, network, or client access. main.py passes in
the existing instance list and decides what to do with the result, which
keeps this module easy to test and safe to call for previews (dry runs).

Accepted input formats (auto-detected):

1. Line syntax: one entry per line; commas or whitespace also separate entries.
   Lines starting with '#' are comments.
       192.168.1.50                 -> 192.168.1.50:8001
       192.168.1.50:8002            -> one warrior on port 8002
       192.168.1.50:8001-8099       -> port range on one host (99 warriors)
       192.168.1.50-60              -> last-octet IP range, port 8001
       192.168.1.50-60:8001         -> IP range with explicit port
       warrior-box.lan:8001         -> hostnames are fine (no IP range though)
       [fd00::50]:8001              -> IPv6 must be bracketed

2. CSV with a header row containing a "host" column. Optional columns:
   name, port, http_username, http_password. Built easily in Excel.

3. JSON: either the dashboard's own export
       {"format": "atw-dashboard-instances", "version": 1, "instances": [...]}
   or a bare list of instance objects (the raw data/instances.json file).

Every entry ends up either in `to_add` or in `skipped` (with a reason), so the
preview can show the user exactly what will and will not happen.
"""

import csv
import io
import ipaddress
import json
import re

DEFAULT_PORT = 8001
MAX_INSTANCES_PER_IMPORT = 1000  # Stops a typo (e.g. :1-65535) from adding thousands
MAX_NAME_LENGTH = 100
EXPORT_FORMAT_NAME = "atw-dashboard-instances"
EXPORT_FORMAT_VERSION = 1

# RFC 1123 hostname: labels of letters/digits/hyphens, no leading/trailing hyphen
_HOSTNAME_PATTERN = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(\.([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*$"
)
# IPv4 with an optional last-octet range: a.b.c.d or a.b.c.d-e
_IPV4_RANGE_PATTERN = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:-(\d{1,3}))?$")
# Port, or port range: 8001 or 8001-8099
_PORT_RANGE_PATTERN = re.compile(r"^(\d{1,5})(?:-(\d{1,5}))?$")


class ImportError_(ValueError):
    """Raised when the whole input is unusable (bad JSON, over the cap, etc.)."""


# ------------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------------
def default_instance_name(host, port):
    """Default name for an unnamed instance: host:port ([v6]:port for IPv6)."""
    if ":" in host:
        return "[" + host + "]:" + str(port)
    return host + ":" + str(port)


def endpoint_key(host, port):
    """Case-insensitive identity of a warrior endpoint, used for duplicate checks."""
    return host.strip().lower() + "|" + str(int(port))


def _is_valid_port(port):
    return isinstance(port, int) and 1 <= port <= 65535


def _validate_host(host):
    """Return the normalized host, or raise ValueError with a readable reason."""
    host = (host or "").strip()
    if not host:
        raise ValueError("missing host")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    # Looks like an IPv4 address but failed parsing (e.g. 300.1.1.1)
    if re.match(r"^[\d.]+$", host):
        raise ValueError("invalid IP address '" + host + "'")
    if not _HOSTNAME_PATTERN.match(host):
        raise ValueError("invalid hostname '" + host + "'")
    return host.lower()


def _parse_port(port_value):
    """Accept int or numeric string; blank means default. Raise ValueError if invalid."""
    if port_value is None or (isinstance(port_value, str) and not port_value.strip()):
        return DEFAULT_PORT
    try:
        port = int(str(port_value).strip())
    except ValueError:
        raise ValueError("invalid port '" + str(port_value) + "'")
    if not _is_valid_port(port):
        raise ValueError("port " + str(port) + " out of range 1-65535")
    return port


def _clean_name(name_value):
    """Strip control characters and limit length; '' means 'use default name'."""
    if name_value is None:
        return ""
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(name_value)).strip()
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError("name longer than " + str(MAX_NAME_LENGTH) + " characters")
    return name


# ------------------------------------------------------------------
# Line syntax
# ------------------------------------------------------------------
def _split_host_and_port(token):
    """Split 'host[:ports]' into (host_part, port_part or ''), handling [IPv6]:port."""
    if token.startswith("["):
        closing_index = token.find("]")
        if closing_index == -1:
            raise ValueError("unclosed '[' in IPv6 address")
        host_part = token[1:closing_index]
        remainder = token[closing_index + 1:]
        if remainder and not remainder.startswith(":"):
            raise ValueError("unexpected text after IPv6 address")
        return host_part, remainder[1:]
    if token.count(":") > 1:
        raise ValueError("IPv6 addresses must be in brackets, e.g. [fd00::1]:8001")
    if ":" in token:
        host_part, port_part = token.split(":", 1)
        return host_part, port_part
    return token, ""


def _expand_ports(port_part):
    """'' -> [8001]; '8002' -> [8002]; '8001-8099' -> [8001..8099]."""
    if not port_part:
        return [DEFAULT_PORT]
    match = _PORT_RANGE_PATTERN.match(port_part)
    if not match:
        raise ValueError("invalid port or port range '" + port_part + "'")
    first_port = int(match.group(1))
    last_port = int(match.group(2)) if match.group(2) else first_port
    if not (_is_valid_port(first_port) and _is_valid_port(last_port)):
        raise ValueError("port out of range 1-65535 in '" + port_part + "'")
    if last_port < first_port:
        raise ValueError("port range is backwards: '" + port_part + "'")
    return list(range(first_port, last_port + 1))


def _expand_hosts(host_part):
    """'192.168.1.50-60' -> 11 hosts; anything else -> [validated host]."""
    match = _IPV4_RANGE_PATTERN.match(host_part)
    if match and match.group(5) is not None:
        octets = [int(match.group(index)) for index in range(1, 5)]
        last_octet_end = int(match.group(5))
        if any(octet > 255 for octet in octets) or last_octet_end > 255:
            raise ValueError("octet out of range 0-255 in '" + host_part + "'")
        if last_octet_end < octets[3]:
            raise ValueError("IP range is backwards: '" + host_part + "'")
        prefix = ".".join(str(octet) for octet in octets[:3])
        return [prefix + "." + str(last_octet) for last_octet in range(octets[3], last_octet_end + 1)]
    return [_validate_host(host_part)]


def _parse_line_syntax(text):
    """Yield (source_label, candidate_dict_or_None, error_or_None)."""
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        for token in re.split(r"[,\s]+", line):
            if not token:
                continue
            source_label = "line " + str(line_number) + ": " + token
            try:
                host_part, port_part = _split_host_and_port(token)
                hosts = _expand_hosts(host_part)
                ports = _expand_ports(port_part)
            except ValueError as parse_error:
                yield source_label, None, str(parse_error)
                continue
            if len(hosts) * len(ports) > MAX_INSTANCES_PER_IMPORT:
                raise ImportError_(
                    source_label + " expands to " + str(len(hosts) * len(ports)) +
                    " warriors (limit " + str(MAX_INSTANCES_PER_IMPORT) + " per import)"
                )
            for host in hosts:
                for port in ports:
                    yield source_label, {"name": "", "host": host, "port": port}, None


# ------------------------------------------------------------------
# CSV and JSON
# ------------------------------------------------------------------
def _looks_like_csv(text):
    """CSV if the first non-comment line is a header with a 'host' column."""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        header_fields = [field.strip().strip('"').lower() for field in line.split(",")]
        return "host" in header_fields
    return False


def _record_to_candidate(record):
    """Convert a CSV row / JSON object to a candidate dict. Raise ValueError if invalid."""
    if not isinstance(record, dict):
        raise ValueError("entry is not an object")
    normalized = {str(key).strip().lower(): value for key, value in record.items() if key is not None}
    candidate = {
        "name": _clean_name(normalized.get("name")),
        "host": _validate_host(str(normalized.get("host") or "")),
        "port": _parse_port(normalized.get("port")),
    }
    # Carried through if present (e.g. re-importing a raw instances.json backup)
    for credential_field in ("http_username", "http_password"):
        credential_value = normalized.get(credential_field)
        if isinstance(credential_value, str) and credential_value:
            candidate[credential_field] = credential_value
    return candidate


def _parse_csv(text):
    lines_without_comments = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )
    reader = csv.DictReader(io.StringIO(lines_without_comments))
    for row_number, row in enumerate(reader, start=2):  # row 1 is the header
        if not any((value or "").strip() for value in row.values() if isinstance(value, str)):
            continue  # blank row
        source_label = "CSV row " + str(row_number)
        try:
            yield source_label, _record_to_candidate(row), None
        except ValueError as parse_error:
            yield source_label, None, str(parse_error)


def _parse_json(text):
    try:
        data = json.loads(text)
    except json.JSONDecodeError as decode_error:
        raise ImportError_("invalid JSON: " + str(decode_error))
    if isinstance(data, dict):
        if data.get("format") not in (None, EXPORT_FORMAT_NAME):
            raise ImportError_("unrecognized JSON format '" + str(data.get("format")) + "'")
        data = data.get("instances")
    if not isinstance(data, list):
        raise ImportError_("JSON must be a list of instances or an ATW Dashboard export")
    for index, record in enumerate(data, start=1):
        source_label = "JSON entry " + str(index)
        try:
            yield source_label, _record_to_candidate(record), None
        except ValueError as parse_error:
            yield source_label, None, str(parse_error)


def detect_format(text):
    stripped_text = text.lstrip()
    if stripped_text.startswith("[") and not re.match(r"^\[[0-9A-Fa-f:]", stripped_text):
        return "json"  # '[' followed by hex/colon is a bracketed IPv6 line instead
    if stripped_text.startswith("{"):
        return "json"
    if _looks_like_csv(text):
        return "csv"
    return "lines"


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------
def parse_bulk_input(text, existing_instances):
    """Parse input and classify every entry against the existing instances.

    Args:
        text: raw text from the bulk-add box or an uploaded file.
        existing_instances: list of instance dicts currently configured.

    Returns:
        {
          "format": "lines" | "csv" | "json",
          "to_add":  [{"name", "host", "port", [http_username, http_password]}...],
          "skipped": [{"source", "host", "port", "reason"}...],
        }

    Raises:
        ImportError_: if the input as a whole cannot be used.
    """
    if not text or not text.strip():
        raise ImportError_("nothing to import")

    input_format = detect_format(text)
    if input_format == "json":
        parsed_entries = _parse_json(text)
    elif input_format == "csv":
        parsed_entries = _parse_csv(text)
    else:
        parsed_entries = _parse_line_syntax(text)

    existing_names = {str(instance.get("name", "")).lower() for instance in existing_instances}
    existing_endpoints = {
        endpoint_key(str(instance.get("host", "")), instance.get("port", DEFAULT_PORT))
        for instance in existing_instances
        if instance.get("host")
    }
    batch_names = set()
    batch_endpoints = set()
    to_add = []
    skipped = []

    for source_label, candidate, parse_error in parsed_entries:
        if parse_error:
            skipped.append({"source": source_label, "host": "", "port": None, "reason": parse_error})
            continue

        host = candidate["host"]
        port = candidate["port"]
        candidate_endpoint = endpoint_key(host, port)
        candidate["name"] = candidate["name"] or default_instance_name(host, port)
        candidate_name_lower = candidate["name"].lower()

        skip_reason = None
        if candidate_endpoint in existing_endpoints:
            skip_reason = "already in dashboard"
        elif candidate_endpoint in batch_endpoints:
            skip_reason = "duplicate in this import"
        elif candidate_name_lower in existing_names:
            skip_reason = "name '" + candidate["name"] + "' already in use"
        elif candidate_name_lower in batch_names:
            skip_reason = "name '" + candidate["name"] + "' repeated in this import"

        if skip_reason:
            skipped.append({"source": source_label, "host": host, "port": port, "reason": skip_reason})
            continue

        if len(to_add) >= MAX_INSTANCES_PER_IMPORT:
            raise ImportError_(
                "import would add more than " + str(MAX_INSTANCES_PER_IMPORT) +
                " warriors; split it into smaller batches"
            )

        to_add.append(candidate)
        batch_names.add(candidate_name_lower)
        batch_endpoints.add(candidate_endpoint)

    return {"format": input_format, "to_add": to_add, "skipped": skipped}


def build_export(existing_instances, include_credentials=False):
    """Build the export document. Credentials are omitted unless explicitly requested."""
    exported_instances = []
    for instance in existing_instances:
        exported_entry = {
            "name": instance.get("name", ""),
            "host": instance.get("host", ""),
            "port": instance.get("port", DEFAULT_PORT),
        }
        if include_credentials:
            for credential_field in ("http_username", "http_password"):
                if instance.get(credential_field):
                    exported_entry[credential_field] = instance[credential_field]
        exported_instances.append(exported_entry)
    return {
        "format": EXPORT_FORMAT_NAME,
        "version": EXPORT_FORMAT_VERSION,
        "count": len(exported_instances),
        "instances": exported_instances,
    }
