# Imagen del migrador (job de un solo uso en Azure Container Apps). Contexto de construcción: ./db
FROM python:3.12-slim
WORKDIR /srv
RUN pip install --no-cache-dir "psycopg[binary]==3.2.*"
COPY core ./core
COPY pagos ./pagos
COPY migrar.py .
RUN useradd --system --uid 10001 app
USER app
CMD ["python", "migrar.py"]
