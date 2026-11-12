FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/
RUN chmod +x /app/render-start.sh
RUN python manage.py collectstatic --noinput

EXPOSE 8000

# Invoice extraction can wait 45 seconds for AI before using the local reader.
CMD ["/app/render-start.sh"]
