# Web agent + chat UI. The graph lives in FalkorDB (Cloud or any server): see README > Deploy.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

RUN pip install --no-cache-dir uv==0.12.2

WORKDIR /app
# Dependencies first (from the lockfile), so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY data/repos.json ./data/repos.json
RUN uv sync --frozen --no-dev

RUN useradd --create-home app && chown -R app /app
USER app

# FORWARDED_ALLOW_IPS="*": the host's proxy addresses vary, so trust X-Forwarded-For.
ENV PATH="/app/.venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8000 \
    FORWARDED_ALLOW_IPS="*"
EXPOSE 8000
CMD ["blast-web"]
