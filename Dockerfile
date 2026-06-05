FROM python:3.11-slim

WORKDIR /app

# Copy all project files for installation
COPY pyproject.toml .
COPY gateway/ ./gateway/
COPY LICENSE .

# Create placeholder README if it doesn't exist
RUN touch README.md || true

RUN pip install --no-cache-dir .

EXPOSE 8080

CMD ["uvicorn", "gateway.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
