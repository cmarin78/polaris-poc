FROM python:3.12-slim

# hadolint ignore=DL3008
RUN pip install --break-system-packages --no-cache-dir \
    flask \
    psycopg2-binary \
    boto3 \
    gunicorn

WORKDIR /app

COPY app.py /app/app.py

EXPOSE 3000

# gunicorn for production (4 workers, threaded)
CMD ["gunicorn", "--bind", "0.0.0.0:3000", "--workers", "2", "--threads", "4", "--access-logfile", "-", "app:app"]