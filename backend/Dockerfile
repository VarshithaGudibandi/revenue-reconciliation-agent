FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim

RUN useradd --system --create-home app
WORKDIR /app
COPY --from=build /app /app
COPY alembic.ini ./
COPY migrations ./migrations
ENV PATH="/app/.venv/bin:$PATH"

USER app
EXPOSE 8000
CMD ["uvicorn", "recon.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
