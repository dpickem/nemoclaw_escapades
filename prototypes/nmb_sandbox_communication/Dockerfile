FROM python:3.13-slim AS builder

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /build

RUN pip install --no-cache-dir --upgrade pip

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r sandbox \
    && useradd -r -g sandbox -d /app -s /bin/bash sandbox

COPY --from=builder /usr/local/lib/python3.13/site-packages/ /usr/local/lib/python3.13/site-packages/
COPY --from=builder /usr/local/bin/ /usr/local/bin/
COPY --chown=root:root prototypes/nmb_sandbox_communication/nmb_smoke.py \
    /app/prototypes/nmb_sandbox_communication/nmb_smoke.py

RUN mkdir -p /sandbox /tmp \
    && chown -R sandbox:sandbox /sandbox /tmp \
    && chmod a+rX /app/prototypes/nmb_sandbox_communication/nmb_smoke.py

ENV PYTHONUNBUFFERED=1

