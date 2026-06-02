FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=5232

WORKDIR /app

RUN addgroup --system caldav \
    && adduser --system --ingroup caldav --home /app caldav

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY server.py README.md ./

RUN mkdir -p /data \
    && chown -R caldav:caldav /app /data

USER caldav

EXPOSE 5232
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '5232'), timeout=3).read()"]

CMD ["python", "server.py", "serve"]
