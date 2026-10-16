FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Install dependencies (cached unless lockfile changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

# Use the dependencies baked into the image, without network access at startup.
ENV PYTHONUNBUFFERED=1 UV_NO_SYNC=1

CMD ["uv", "run", "python", "main.py"]
