FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir .
ENV MARKETFLOW_RULES=/app/config/extraction.json
CMD ["python", "-m", "marketflow.worker"]
