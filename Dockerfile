FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip wheel --index-url "$PIP_INDEX_URL" --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels \
    && groupadd --gid 10001 fitnesspro \
    && useradd --uid 10001 --gid fitnesspro --create-home fitnesspro
COPY app ./app
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini config.yml ./
COPY data/knowledge/raw /opt/fitnesspro/knowledge/raw
COPY data/knowledge/sources.json /opt/fitnesspro/knowledge/sources.json
COPY meals.json ./meals.json
RUN mkdir -p /app/data && chown fitnesspro:fitnesspro /app/data
USER fitnesspro
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
