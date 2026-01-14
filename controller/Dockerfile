FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Create non-root user with proper passwd entry (required for kopf identity detection)
RUN useradd --uid 1000 --gid 0 --no-create-home appuser
USER 1000:0

CMD ["python", "-u", "main.py"]
