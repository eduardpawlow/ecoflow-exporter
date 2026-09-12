FROM python:3.12-alpine@sha256:b64631e04e4920160c50fbe8d8df828f7f35f06f425cb44aa09bca53e708a35a

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt
COPY ecoflow_exporter.py delta3_decoder.py delta3_pb2.py LICENSE THIRD_PARTY_NOTICES.md ./
USER 65534:65534
EXPOSE 9090
CMD ["python", "ecoflow_exporter.py"]
