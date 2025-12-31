# Use a lightweight Python base image
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set working directory
WORKDIR /app

# Copy dependency files first for better caching
COPY pyproject.toml .
# If you have a lockfile, copy it too
# COPY uv.lock .

# Install dependencies
RUN uv pip install --system -r pyproject.toml

# Copy the rest of the application
COPY . .

# Create directories for persistent data
RUN mkdir -p data media

# Expose the port the app runs on
EXPOSE 5001

# Run the application
# We use uvicorn directly or via python main.py
CMD ["python", "main.py"]
