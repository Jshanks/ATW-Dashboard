FROM python:3.15-alpine AS base

# Patch OS-level vulnerabilities.
RUN apk upgrade --no-cache

WORKDIR /app

# Install build dependencies before installing Python packages so packages with
# C extensions can compile if a compatible wheel is unavailable.
RUN apk add --no-cache --virtual .build-deps build-base

COPY requirements.txt .

# Remove the setuptools copy inherited from the base image, including any stale
# distribution metadata, before installing the fixed version. The final Python
# check examines every setuptools distribution visible to importlib.metadata,
# not only the first version returned.
RUN python -m pip install \
        --no-cache-dir \
        --upgrade \
        "pip" \
    && python -m pip uninstall \
        --yes \
        setuptools \
        || true

RUN find /usr/local/lib/python*/site-packages \
        -maxdepth 1 \
        \( \
            -name 'setuptools' \
            -o -name 'setuptools-*.dist-info' \
            -o -name 'setuptools-*.egg-info' \
            -o -name '_distutils_hack' \
        \) \
        -exec rm -rf '{}' +

RUN python -m pip install \
        --no-cache-dir \
        -r requirements.txt \
    && python -m pip install \
        --no-cache-dir \
        --upgrade \
        "setuptools>=78.1.1" \
    && python - <<'PY'
import importlib.metadata

def version_tuple(value):
    numeric_parts = []

    for part in value.split("."):
        digits = "".join(character for character in part if character.isdigit())

        if not digits:
            break

        numeric_parts.append(int(digits))

    while len(numeric_parts) < 3:
        numeric_parts.append(0)

    return tuple(numeric_parts[:3])

setuptools_versions = sorted({
    distribution.version
    for distribution in importlib.metadata.distributions()
    if (distribution.metadata.get("Name") or "").lower() == "setuptools"
})

print("Detected setuptools distributions:", setuptools_versions)

assert setuptools_versions, "setuptools is not installed"

vulnerable_versions = [
    version
    for version in setuptools_versions
    if version_tuple(version) < (78, 1, 1)
]

assert not vulnerable_versions, (
    "Vulnerable setuptools metadata remains installed: "
    + ", ".join(vulnerable_versions)
)
PY

# Print the remaining setuptools files and installed version in the build log.
RUN python -m pip show setuptools \
    && find /usr/local/lib/python*/site-packages \
        -maxdepth 1 \
        \( \
            -name 'setuptools*' \
            -o -name '_distutils_hack' \
        \) \
        -print

# Remove compilers and headers after dependency installation.
RUN apk del .build-deps

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY config.example.yml ./config.example.yml

RUN addgroup -S appuser \
    && adduser -S -G appuser appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION
ENV CONFIG_PATH=config.yml
ENV DASHBOARD_PORT=8080
ENV LOG_LEVEL=info

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/api/health')" || exit 1

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080"]