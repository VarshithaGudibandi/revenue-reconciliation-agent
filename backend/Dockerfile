# Build from the repository root: docker build -f backend/Dockerfile .

FROM node:22-slim AS frontend

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim

RUN useradd --system --create-home app && mkdir /data && chown app /data
WORKDIR /app
COPY --from=build /app /app
COPY backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY --from=frontend /frontend/dist /app/static
ENV PATH="/app/.venv/bin:$PATH" RECON_STORAGE_DIR=/data RECON_STATIC_DIR=/app/static

USER app
EXPOSE 8000
CMD ["uvicorn", "recon.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
