# FlaVR Bot image. Built by Portainer from this repo (docker-compose.yml), or by hand:
#   docker build -t flavr-bot .
# bookworm (Debian 12) kept from when the image carried Chromium for the calendar picture (that moved
# to FlaVR Promo on 2026-11-20); staying on it means no surprise base image change.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY flavrbot ./flavrbot

# the bot's database lives here (a named volume in docker-compose.yml, so redeploys keep it)
VOLUME ["/data"]
# not root inside the container
RUN useradd --system --uid 1000 flavr && mkdir -p /data && chown flavr /data
USER flavr

# Stream Core sends to this port (home network only; never forward it on the router)
EXPOSE 3870
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('LISTEN_PORT','3870'),timeout=4)"

CMD ["python", "-m", "flavrbot"]
