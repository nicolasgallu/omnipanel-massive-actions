# Imagen del worker de acciones masivas (Cloud Run SERVICE, no Job: corre
# siempre con min-instances=1 y hace pull de los lotes).
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8080
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
