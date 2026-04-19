FROM python:3.12-slim

# Install only what's needed (no ttyd!)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      docker.io \
      gcc \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    psutil \
    requests \
    urllib3

COPY app/ .

EXPOSE 7681

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7681", "--workers", "1"]
