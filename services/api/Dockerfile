# syntax=docker/dockerfile:1
# The backend image (stage 2.5): `python -m api`, the operator API on port 8000.
#
# Built from the repository root, because the uv workspace resolves from its root lockfile:
#
#     docker build -f services/api/Dockerfile -t agent-negotiation/api:local .
#
# Only the backend and the protocol package are installed, non-editable, from `uv.lock` exactly
# (`--frozen`); no development tools. The scenario templates are copied in and loaded at start-up.
# Keys never enter the image: they arrive at run time as environment references (ADR-049).

FROM ghcr.io/astral-sh/uv:0.12.17 AS uv

FROM python:3.12.11-slim-bookworm AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY packages/protocol packages/protocol
COPY services/api services/api
# A workspace member is discovered from its pyproject; the agent's code is not installed here.
COPY services/agent/pyproject.toml services/agent/pyproject.toml
RUN uv sync --frozen --no-dev --no-editable --package negotiation-api

FROM python:3.12.11-slim-bookworm
RUN useradd --system --uid 10001 --no-create-home api
COPY --from=build /opt/venv /opt/venv
COPY scenarios /app/scenarios
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    SCENARIOS_DIR=/app/scenarios \
    API_HOST=0.0.0.0 \
    API_PORT=8000
WORKDIR /app
USER api
EXPOSE 8000
CMD ["python", "-m", "api"]
