FROM python:3.11-slim

WORKDIR /app

# Install dependencies (service + base project deps for DRY imports)
COPY services/hf_elim_pubsub/requirements.txt /tmp/svc.txt
COPY requirements.txt /tmp/base.txt
RUN pip install --no-cache-dir -r /tmp/svc.txt -r /tmp/base.txt \
    && rm /tmp/svc.txt /tmp/base.txt

# Copy project source for DRY imports (src.analysis, src.data_collection)
COPY src/ src/

# Copy service
COPY services/hf_elim_pubsub/main.py services/hf_elim_pubsub/main.py

ENV PORT=8080

CMD exec gunicorn \
    --bind :$PORT \
    --workers 2 \
    --threads 4 \
    --timeout 120 \
    services.hf_elim_pubsub.main:app
