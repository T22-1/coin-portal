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

## Incoming purchases

Incoming Inventory stages uploads without creating stock. Review the seller,
order number, purchase date, shipping, tax, and each extracted row. Select a
destination (Numismatic, Raw, Tubes, Products), then move the purchase to
Awaiting Arrival or Received / Quality Check. Enter received quantities, inspect
the goods, select rows, and use Approve & Add to Inventory. Archive once every
row has been approved or excluded. Archived purchases can be reopened without
reimporting completed rows.

Row cost means the entire merchandise line, not a unit price. Shipping and tax
are allocated proportionally by merchandise cost (by unit count for zero-cost
purchases). Costs and ordered quantities lock after the first approval; split
mixed lots and resolve purchase totals before then. Each tube record represents
one roll, with coins per roll recorded separately in its label. Product cost
lots may be split by a cent so quantity times unit cost exactly matches the invoice.

PDF, CSV, TSV, TXT, XLSX, PNG, JPEG, and WebP uploads are accepted up to 20 MB.
Set `OPENAI_API_KEY` on Render for AI document reading, including photos and
scanned PDFs. `OPENAI_INVOICE_MODEL` defaults to `gpt-4.1-mini`. Documents are sent
to that provider when configured. Without AI, text PDFs and spreadsheet files
use the local reader; unrecognized documents stay available for manual review.
AI suggestions never bypass receiving or approval. The original file is retained.
Duplicate file hashes and seller/order identifiers route back to the existing
purchase. Use a persistent MEDIA_ROOT storage volume for retained documents.

Deploy this update with the new dependency and migrations before serving traffic:

```bash
python -m pip install -r requirements.txt
python manage.py migrate --noinput
```

The Docker startup already runs migrations. Incoming users need the corresponding
view/add/change incoming-batch permissions plus add permission for each destination.

Invoice reading is synchronous. Gunicorn's Docker startup timeout defaults to
120 seconds (`GUNICORN_TIMEOUT`), allowing the AI reader's 45-second timeout and
local fallback to complete. If Render uses a custom start command instead of the
Docker CMD, include `--timeout 120` in that Gunicorn command too. A log entry
containing `WORKER TIMEOUT` indicates the worker was stopped while processing;
other database or storage errors require their own traceback diagnosis.
