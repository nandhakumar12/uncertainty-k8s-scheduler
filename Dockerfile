# Build the UKS scheduler as a container image
# Base image: Python 3.9 slim to keep the image small
FROM python:3.9-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (better Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/
COPY data/ ./data/
COPY scheduler_daemon.py .
COPY run_experiments.py .
COPY plot_results.py .

# Default command runs the real scheduler daemon
CMD ["python", "scheduler_daemon.py"]
