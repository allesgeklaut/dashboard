FROM ubuntu:24.04

RUN apt-get update && apt-get install -y \
    python3 python3-pip ttyd \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages \
    textual psutil requests urllib3 python-dotenv

WORKDIR /app
COPY dashboard.py ./

ENV TERM=xterm-256color
ENV PYTHONUNBUFFERED=1

EXPOSE 7681

CMD ["ttyd", "--port", "7681", "--writable", "--max-clients", "3", \
     "/usr/bin/python3", "-u", "dashboard.py"]