FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh","-c","gunicorn -w 1 -b 0.0.0.0:${PORT:-10000} --timeout 600 app:app"]
