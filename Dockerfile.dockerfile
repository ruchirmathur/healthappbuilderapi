# Use a suitable Python base image (3.9 or later recommended)
FROM python:3.9-slim-buster

# Set working directory
WORKDIR /app

# Copy requirements file
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose the port
EXPOSE 8080

# Start the ASGI server using Gunicorn with Uvicorn workers (CORRECTED - NO EXEC)
CMD gunicorn --bind :8080 --workers 1 --worker-class uvicorn.workers.UvicornWorker --threads 8 app:app  # Or main:app if your main file is named main.py