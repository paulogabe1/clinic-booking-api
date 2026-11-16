# Clinic Booking API

A FastAPI service and single-page booking UI implementing all five levels of the
clinic booking assessment contract.

## Run locally

Requires Python 3.10 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:API_KEY = "local-development-key"
python -m uvicorn main:app --reload
```

Open `http://127.0.0.1:8000`, enter the API key, then load the example
appointments. `POST /reset` replaces the example schedule and clears bookings.

## API

All API routes require `X-API-Key`; configure the secret as `API_KEY`. The
service provides `POST /reset`, `GET /slots`, `POST /bookings`,
`GET /bookings`, `GET /bookings/{id}`, and `DELETE /bookings/{id}`. Responses
and status codes follow the supplied OpenAPI contract. SQLite transactions and
a partial unique index prevent two confirmed bookings for the same slot;
idempotency records, rate-limit attempts, slots, and bookings are stored in the
database file. Set `DATABASE_PATH` to choose another database location.

## Deploy to Render

The included `render.yaml` can be deployed as a Render Blueprint. Render
generates the `API_KEY` secret automatically. Copy that value into the web
page's API key field when using the UI. The service uses SQLite in its app
filesystem; its data survives requests while the instance is running, but a
free Render instance has ephemeral storage and may lose data on restart or
redeploy. Use a persistent disk or managed PostgreSQL for restart-safe
production storage.
