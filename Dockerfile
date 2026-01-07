
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install uv (Python package manager)
RUN pip install uv

# Copy project files
COPY . .

# Install Python dependencies
RUN uv sync --frozen

# Build frontend
WORKDIR /app/frontend
RUN npm install && npm run build

WORKDIR /app

# Expose port (Railway sets PORT env var)
EXPOSE 8001

# Start the backend
CMD ["uv", "run", "python", "-m", "backend.main"]

