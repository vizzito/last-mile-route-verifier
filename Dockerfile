FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/ scripts/

ENTRYPOINT ["python", "scripts/check_distance_osrm.py"]
CMD ["--help"]
