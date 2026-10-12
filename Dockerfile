FROM python:3.14-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy exporter
COPY exporter.py .

# Run as non-root user
RUN useradd -m -u 1000 exporter
USER exporter

EXPOSE 9100

CMD ["python", "-u", "exporter.py"]
