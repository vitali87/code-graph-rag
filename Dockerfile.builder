FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install uv

# Copy project files
COPY . .

# Install dependencies
RUN uv sync

# Install PyInstaller
RUN uv add --dev pyinstaller

# Build binary
RUN uv run python build_binary.py

# Copy binary to output directory
RUN mkdir -p /output && cp dist/graph-code-* /output/