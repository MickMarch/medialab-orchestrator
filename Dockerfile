FROM python:3.12-slim

ARG APP_VERSION=0.0.0
LABEL org.opencontainers.image.version=$APP_VERSION

WORKDIR /app

# git is required at build time: medialab-contracts is a git-ref uv dependency,
# so uv sync must clone it. Kept in the image build only, not a runtime need.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install uv --no-cache-dir

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-dev --frozen --no-cache --no-install-project

COPY src/ ./src/
RUN SETUPTOOLS_SCM_PRETEND_VERSION=${APP_VERSION} uv sync --no-dev --frozen --no-cache

# The SQLite job store and the bind-mounted media dir both live under /app at
# runtime; appuser owns the data dir so the store can create the DB file.
RUN useradd --no-create-home --shell /bin/false appuser \
    && mkdir -p /app/data \
    && chown -R appuser /app/data
USER appuser

EXPOSE 8000

CMD ["/app/.venv/bin/medialab-orchestrator"]
