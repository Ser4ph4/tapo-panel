FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py collector.py ./
COPY templates ./templates
COPY static ./static

RUN mkdir -p /data
VOLUME ["/data"]

ENV PORT=5000
EXPOSE 5000

# 1 worker só: o scheduler de coleta roda em background thread dentro do processo,
# múltiplos workers gunicorn duplicariam a coleta.
CMD ["gunicorn", "--workers=1", "--threads=4", "--bind=0.0.0.0:5000", "app:app"]
