FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5050

# 生产用 gunicorn (4 worker)，开发可改 python app.py
CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "4", "--access-logfile", "-", "app:app"]
