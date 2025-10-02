FROM python:3.13.7-slim

WORKDIR /app

COPY requirements.txt .
ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

EXPOSE 8000

# Change log level if you want to
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", "--log-level", "info"]
