# Lightweight image — no browser needed since we connect directly to
# wss://demo-api-eu.po.market via Python WebSocket.
FROM python:3.12-slim

WORKDIR /app

# System deps for lxml/matplotlib/pillow (used by tg/chart.py)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libjpeg-dev zlib1g-dev libpng-dev libfreetype6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir websockets

COPY . /app

ENV PYTHONUNBUFFERED=1

# journal/candles.db lives in a Volume mount for persistence
RUN mkdir -p /app/journal /app/strategy/user

EXPOSE 8080

CMD ["python3", "main.py"]
