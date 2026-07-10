# Minimal image for the agent-insights sidecar (stdlib only, no deps).
FROM python:3.12-slim

WORKDIR /app
COPY app.py .

# Non-root for good measure.
RUN useradd --system --no-create-home appuser
USER appuser

EXPOSE 8099

# TEMPO_URL defaults to the compose service name; override via environment.
ENV TEMPO_URL=http://lgtm:3200 \
    PORT=8099 \
    LISTEN_ADDR=0.0.0.0

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/health',timeout=3).status==200 else 1)"

CMD ["python", "app.py"]
