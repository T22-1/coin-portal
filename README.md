# Coin Portal MVP (Crackout Dealer Portal) — Local-first

This is a local-first, private dealer portal MVP built with Django + Postgres.
It includes:
- Inventory items with stable internal IDs (INV-######)
- Containers/tubes (TUBE-######)
- Zebra-friendly Code128 labels as 2"×1" PDFs
- Scan-to-open item view
- Sale Batch mode: scan scan scan, then enter sold prices, complete sale (logs comps + marks sold)

## Fast start on macOS (no coding needed beyond copy/paste)

1) Install Docker Desktop for Mac
2) In Terminal, go to this folder
3) Run:

```bash
docker compose up --build
```

Then open:
- Admin: http://localhost:8000/admin  (login: admin / admin12345)
- Portal: http://localhost:8000/

## Changing the admin password
In docker-compose.yml set `DJANGO_SUPERUSER_PASSWORD`.

## Render deployment

Deploy this as a Docker web service. The Dockerfile runs migrations and starts
Gunicorn on Render's assigned `PORT`.

Set these Render environment variables:

```text
DJANGO_DEBUG=0
DJANGO_SECRET_KEY=<generate a long random secret>
DJANGO_ALLOWED_HOSTS=<your-service-name>.onrender.com
CSRF_TRUSTED_ORIGINS=https://<your-service-name>.onrender.com
DATABASE_URL=<Render Postgres internal database URL>
DJANGO_SUPERUSER_USERNAME=admin
DJANGO_SUPERUSER_EMAIL=<your email>
DJANGO_SUPERUSER_PASSWORD=<temporary strong password>
```

After the first successful deploy, change the admin password in Django Admin and
remove `DJANGO_SUPERUSER_PASSWORD` from Render.
