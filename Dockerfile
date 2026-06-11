FROM python:3.11-slim

WORKDIR /app

# Copy all project files for installation
COPY pyproject.toml .
COPY gateway/ ./gateway/
COPY LICENSE .
COPY README.md .

RUN pip install --no-cache-dir .

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import socket; s=socket.socket(); s.settimeout(3); s.connect(('localhost', 8080)); s.close()"

RUN addgroup --system app && adduser --system --ingroup app app
USER app

CMD ["uvicorn", "gateway.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
