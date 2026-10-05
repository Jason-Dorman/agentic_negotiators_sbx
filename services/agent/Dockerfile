# syntax=docker/dockerfile:1
# The agent service image (stage 2.5): `python -m agent`, one instance per container — agent-a for
# the buyer, agent-b for the seller, the same image configured apart (docs/architecture.md 3.3).
#
# Built from the repository root, because the uv workspace resolves from its root lockfile:
#
#     docker build -f services/agent/Dockerfile -t agent-negotiation/agent:local .
#
# The protocol package is installed without its `tools` extra, so the image has no RPC client
# (ADR-035), and nothing of the backend is in it. The root secret arrives at run time through the
# variable its reference names, and never enters the image.

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
COPY services/agent services/agent
# A workspace member is discovered from its pyproject; the backend's code is not installed here.
COPY services/api/pyproject.toml services/api/pyproject.toml
RUN uv sync --frozen --no-dev --no-editable --package negotiation-agent

FROM python:3.12.11-slim-bookworm
RUN useradd --system --uid 10001 --no-create-home agent
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    AGENT_HOST=0.0.0.0
USER agent
CMD ["python", "-m", "agent"]
