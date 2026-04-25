# Playwright image: Chromium pre-installed for the auto-relogin flow
# (used briefly every 12h to refresh PO session).
FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . /app

ENV PYTHONUNBUFFERED=1

RUN mkdir -p /app/data /app/strategy/user

EXPOSE 8080

CMD ["python3", "main.py"]
