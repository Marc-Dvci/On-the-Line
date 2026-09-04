FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements.lock ./
RUN python -m pip install setuptools==80.9.0 \
    && python -m pip install --require-hashes -r requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip wheel --no-deps --no-build-isolation . --wheel-dir /wheels

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN addgroup --system ontheline && adduser --system --ingroup ontheline --home /app ontheline
COPY requirements.lock ./
COPY --from=builder /wheels /wheels
RUN python -m pip install --require-hashes -r requirements.lock \
    && python -m pip install --no-deps /wheels/on_the_line-1.0.0-py3-none-any.whl \
    && rm -rf /wheels requirements.lock
COPY scripts/preflight.py /app/scripts/preflight.py

RUN mkdir -p /app/data && chown -R ontheline:ontheline /app
USER ontheline
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; host=os.environ.get('OTL_ALLOWED_HOSTS','localhost').split(',')[0]; request=urllib.request.Request('http://127.0.0.1:8080/ping', headers={'Host':host,'X-Forwarded-Proto':'https'}); urllib.request.urlopen(request, timeout=2)"

CMD ["ontheline", "serve", "--host", "0.0.0.0", "--port", "8080"]
