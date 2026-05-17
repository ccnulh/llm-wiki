# Build stage
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libmagic1 \
    ffmpeg \
    libsm6 \
    libxext6 \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install yt-dlp for video downloads
RUN pip install --no-cache-dir yt-dlp

# Install OCR dependencies
RUN pip install --no-cache-dir pytesseract Pillow easyocr

# Copy application code
COPY . .

# Create directories
RUN mkdir -p /app/wiki /app/raw /app/config /app/data

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PORT=5000

# Expose port
EXPOSE 5000

# Run with gunicorn - longer timeout for large file processing
CMD ["gunicorn", "app:app", "-b", "0.0.0.0:5000", "-w", "1", "--timeout", "600"]