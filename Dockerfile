FROM python:3.11-slim

WORKDIR /app

# libusb is required for Basler USB3 cameras via pypylon
RUN apt-get update && apt-get install -y --no-install-recommends \
    libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY templates/ templates/

EXPOSE 8080

CMD ["python", "server.py"]
