FROM python:3.12-alpine

WORKDIR /app
COPY server/main.py /app/main.py

ENV PORT=8080 \
    DB_PATH=/data/game.db \
    PYTHONUNBUFFERED=1

RUN addgroup -S app && adduser -S -G app app \
    && mkdir -p /data \
    && chown -R app:app /app /data

USER app
VOLUME ["/data"]
EXPOSE 8080

CMD ["python", "/app/main.py"]
