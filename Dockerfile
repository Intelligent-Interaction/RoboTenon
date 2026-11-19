FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY robotenon ./robotenon

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1

