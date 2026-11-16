# Clinic Booking App

This project is a small website for booking clinic appointments. A patient can
see available times, book one, and get a booking reference. The API also lets
you find and cancel bookings.

## Try it on your computer

You need Python 3.10 or newer. In PowerShell, run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:API_KEY = "local-development-key"
$env:ADMIN_KEY = "local-admin-key"
python -m uvicorn main:app --reload
```

Open `http://127.0.0.1:8000`, enter `local-development-key` for API access, and
use `local-admin-key` if you choose to clear bookings. A few example
appointments are added automatically.

## What the API does

Every API request needs the `X-API-Key` header. The key is a shared password
that Render keeps in the `API_KEY` setting. The website asks for the key and
uses it when it talks to the API.

- `GET /slots` shows appointment times. You can add `?provider=Dr%20Ade` to
  show times for one provider.
- `POST /bookings` books a time. Send the slot ID, patient's name, and email.
- `GET /bookings/{id}` looks up a booking by its reference.
- `DELETE /bookings/{id}` cancels a booking and frees the time.
- `GET /bookings?email=...` lists a patient's bookings. Add `limit` to choose
  how many to show at once. If there are more, use the returned `next_cursor`
  value as the `cursor` for the next request.
- `POST /reset` replaces the appointment schedule and deletes existing bookings.
  **Do not use this on a live service unless you mean to clear its bookings.**
- `POST /admin/clear-bookings` removes all bookings but keeps appointment times.
  It needs both the regular API key and a separate admin key in the
  `X-Admin-Key` header. The website has a button for this action and asks for
  the admin key each time.

## How it avoids common problems

- **A slot can't be booked twice:** the database only allows one confirmed
  booking per appointment. It also handles booking requests one at a time when
  they arrive together.
- **Safe retries:** a request can include an `Idempotency-Key`, a unique label
  for that attempt. If the connection drops and the same request is sent again
  with the same label, the API returns the original answer instead of making a
  second booking. Reusing the label for different details is rejected.
- **Booking limits:** each email address can make up to five booking attempts
  in one minute. Failed attempts count too.
- **Saved data:** appointments, bookings, and retry information are stored in
  SQLite, a small database kept in a file.

## Deploy to Render

The `render.yaml` file describes the service for Render. Render can create the
`API_KEY` and `ADMIN_KEY` for you. Find both values in the Render dashboard's
Environment settings. Enter the API key on the website to use it; the clear
bookings button will ask for the admin key when needed. Keep the admin key
private.

The free Render service uses temporary file storage. The SQLite database
survives normal requests, but its contents may disappear if Render restarts or
redeploys the service. For data that must survive restarts, move the database
to a managed database such as PostgreSQL or use persistent storage.

The API key is a shared password, not a separate login for each patient. Keep
it private. Before using this with real patients, replace the shared key with
proper patient sign-in.
