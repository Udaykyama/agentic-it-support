FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN addgroup -g 10001 runbooksignal \
    && adduser -D -H -u 10001 -G runbooksignal runbooksignal

WORKDIR /app
COPY server.py ./

USER 10001:10001
EXPOSE 8080

CMD ["python", "/app/server.py"]
