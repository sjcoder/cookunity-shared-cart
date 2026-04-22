FROM python:3.12-slim

WORKDIR /app

COPY scrape.py serve.py ./
COPY cookunity ./cookunity

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["python", "serve.py", "--host", "0.0.0.0", "--port", "8000", "--no-open"]
