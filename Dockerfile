# Playwright image bundles Chromium + system deps for headless Chrome.
FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

WORKDIR /app

# Copy python deps first for layer cache
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy app
COPY . /app

# Chrome user-data-dir persists at /chrome-data (mount a Railway Volume here)
ENV CHROME_USER_DATA_DIR=/chrome-data \
    CDP_URL=http://localhost:9222 \
    PYTHONUNBUFFERED=1

RUN chmod +x /app/scripts/start.sh
EXPOSE 9222

ENTRYPOINT ["/app/scripts/start.sh"]
