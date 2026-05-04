FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r sandbox \
    && useradd -r -g sandbox -d /app -s /bin/bash sandbox

COPY sandbox_http_server.py /app/sandbox_http_server.py

RUN mkdir -p /sandbox /tmp \
    && chown -R sandbox:sandbox /sandbox /tmp \
    && chmod a+rX /app/sandbox_http_server.py

ENV PYTHONUNBUFFERED=1
