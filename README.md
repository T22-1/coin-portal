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

## Production/cloud
This repo is structured so it can be deployed to a managed host later (Render/Railway/Fly/DigitalOcean).
