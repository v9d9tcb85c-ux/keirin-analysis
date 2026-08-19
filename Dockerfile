FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium
COPY . .
ENV PORT=8080
CMD ["sh","-c","gunicorn -w 1 -b 0.0.0.0:${PORT:-8080} --timeout 600 --max-requests 200 --max-requests-jitter 20 app:app"]
