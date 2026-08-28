# The remote MCP server
# Only what the server needs travels into the image: its two packages, the trained model and
# the seed extract. Never `scripts/` nor `notebooks/` to keep the dependencies simple and the
# image of the deployed container small.
FROM python:3.12-slim

# The uv version is pinned for the same reason as in the workflow: a broken build must mean
# the project broke, not that upstream moved.
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies on a layer of their own, so editing the source does not resolve them again.
# `--locked` refuses to drift from uv.lock, and `--no-dev` leaves the test tooling out.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src/mcp_core/ src/mcp_core/
COPY src/fraud_mcp/ src/fraud_mcp/
COPY artifacts/model.json artifacts/
COPY data/seed/ data/seed/
RUN uv sync --locked --no-dev

# Absolute paths, so that nothing inside the container depends on where the package resolves
# its repository root.
ENV PATH="/app/.venv/bin:$PATH" \
    FRAUD_MCP_DB=/app/data/fraud.db \
    FRAUD_MCP_MODEL=/app/artifacts/model.json \
    FRAUD_MCP_SEED=/app/data/seed

# The server provider injects PORT and the entry point listens on 0.0.0.0; 8080 is only the
# local default.
EXPOSE 8080

CMD ["fraud-mcp-http"]
