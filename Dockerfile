FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY migrations ./migrations
COPY data ./data

RUN useradd --create-home --uid 1000 maika
USER maika

CMD ["python", "-m", "maika"]
