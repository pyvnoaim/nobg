FROM python:3.12-slim

# rembg -> opencv-python-headless still wants libglib/libgomp at import time
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY remove_bg.py .

ENV U2NET_HOME=/models
EXPOSE 7860
CMD ["python", "remove_bg.py"]
