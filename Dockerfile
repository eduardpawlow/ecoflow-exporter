FROM python:3.12-alpine@sha256:b64631e04e4920160c50fbe8d8df828f7f35f06f425cb44aa09bca53e708a35a

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --no-deps setuptools==84.0.0 wheel==0.48.0 packaging==26.3 \
    && pip install --no-cache-dir --no-build-isolation -r requirements.txt
COPY ecoflow_exporter.py LICENSE ./
USER 65534:65534
EXPOSE 9090
CMD ["python", "ecoflow_exporter.py"]
