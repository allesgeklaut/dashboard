FROM python:3.12-slim

# Install only what's needed (no ttyd!)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Dedicated non-root user for the app process
RUN useradd --system --no-create-home --uid 1000 homelab

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    psutil \
    requests \
    urllib3 \
    python-dotenv

COPY app/ .
RUN chown -R homelab:homelab /app

USER homelab

EXPOSE 7681

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7681", "--workers", "1"]
