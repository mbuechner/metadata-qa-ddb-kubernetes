FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose the HTTP port
EXPOSE 8080

# Command to run the Flask app with Gunicorn
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:8080", "app:app", "--worker-class", "eventlet"]
