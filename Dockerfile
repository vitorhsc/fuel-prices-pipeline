FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first to take advantage of Docker layer caching
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY config ./config

# Run as an unprivileged user
RUN useradd --create-home pipeline && mkdir -p /app/data && chown -R pipeline /app/data
USER pipeline

ENTRYPOINT ["python", "-m", "fuel_prices"]
CMD ["run"]
