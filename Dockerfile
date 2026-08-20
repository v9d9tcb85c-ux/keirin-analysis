FROM mcr.microsoft.com/playwright/python:v1.62.0-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--timeout", "0", "--bind", "0.0.0.0:10000", "app:app"]
