FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock* README.md ./
RUN uv sync --no-dev --all-extras --no-install-project
COPY . .
RUN uv sync --no-dev --all-extras
ENV PATH="/app/.venv/bin:$PATH"
