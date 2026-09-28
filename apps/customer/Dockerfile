FROM python:3.12-slim

RUN pip install --no-cache-dir \
    flask==3.0.3 \
    psycopg2-binary==2.9.9 \
    boto3==1.35.0 \
    gunicorn==22.0.0

WORKDIR /app
COPY app.py .

EXPOSE 3000
CMD ["gunicorn", "--bind", "0.0.0.0:3000", "--workers", "2", "--threads", "4", "app:app"]