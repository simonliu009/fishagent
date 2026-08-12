FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONPATH=/app/src

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra agent

COPY . .

CMD ["uv", "run", "uvicorn", "fishagent.web.app:app", "--host", "0.0.0.0", "--port", "3008"]
