FROM python:3.11-slim

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    sudo \
    git \
    curl \
    wget \
    ffmpeg libomp5 libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash hatani \
    && mkdir -p /home/hatani/workspaces \
    && chown -R hatani:hatani /home/hatani

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY requirements.txt .
RUN uv pip install --system --no-cache --index-strategy unsafe-best-match -r requirements.txt

# Install Playwright and its browser dependencies
RUN playwright install --with-deps chromium && chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"

COPY . .


CMD ["python", "main.py"]
