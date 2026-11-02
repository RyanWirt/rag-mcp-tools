FROM python:3.12-slim AS base

WORKDIR /app
RUN pip install --no-cache-dir uv==0.13.6
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY services ./services
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["uvicorn", "services.rag_api.app:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS dev
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

FROM base AS runtime
