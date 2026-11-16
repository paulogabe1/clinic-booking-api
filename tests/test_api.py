import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["API_KEY"] = "test-api-key"
os.environ["ADMIN_KEY"] = "test-admin-key"
database_directory = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = str(Path(database_directory.name) / "test.sqlite3")

import main


class BookingApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(main.app)
        cls.client = cls.client_context.__enter__()
        cls.headers = {"X-API-Key": "test-api-key"}

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        database_directory.cleanup()

    def setUp(self):
        tomorrow = datetime.now(timezone.utc) + timedelta(days=2)
        self.slots = [
            {
                "id": f"slot-{index}",
                "provider": "Dr Ada",
                "start": (tomorrow + timedelta(hours=index)).isoformat(),
                "end": (tomorrow + timedelta(hours=index, minutes=30)).isoformat(),
            }
            for index in range(1, 5)
        ]
        response = self.client.post(
            "/reset", headers=self.headers, json={"slots": self.slots}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")

    def create(self, slot_id="slot-1", email="patient@example.com", key=None):
        headers = dict(self.headers)
        if key:
            headers["Idempotency-Key"] = key
        return self.client.post(
            "/bookings",
            headers=headers,
            json={"slot_id": slot_id, "name": "  Ada Obi  ", "email": email},
        )

    def test_api_key_validation_and_slot_availability(self):
        self.assertEqual(self.client.get("/slots").status_code, 401)
        response = self.client.get("/slots", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(slot["available"] for slot in response.json()["slots"]))

    def test_booking_validation_and_time_zone_ordering(self):
        incomplete = self.client.post(
            "/bookings", headers=self.headers, json={"slot_id": "slot-1"}
        )
        self.assertEqual(incomplete.status_code, 400)
        invalid_email = self.client.post(
            "/bookings",
            headers=self.headers,
            json={"slot_id": "slot-1", "name": "Ada", "email": "not-an-email"},
        )
        self.assertEqual(invalid_email.status_code, 400)

        earlier = datetime(2030, 1, 1, 10, tzinfo=timezone(timedelta(hours=2)))
        later = datetime(2030, 1, 1, 8, 30, tzinfo=timezone.utc)
        response = self.client.post(
            "/reset",
            headers=self.headers,
            json={
                "slots": [
                    {
                        "id": "later",
                        "provider": "Dr Ada",
                        "start": later.isoformat(),
                        "end": (later + timedelta(minutes=30)).isoformat(),
                    },
                    {
                        "id": "earlier",
                        "provider": "Dr Ada",
                        "start": earlier.isoformat(),
                        "end": (earlier + timedelta(minutes=30)).isoformat(),
                    },
                ]
            },
        )
        self.assertEqual(response.status_code, 200)
        listed = self.client.get("/slots", headers=self.headers).json()["slots"]
        self.assertEqual([slot["id"] for slot in listed], ["earlier", "later"])

    def test_idempotency_validation_and_cancellation(self):
        first = self.create(key="retry-1")
        repeated = self.create(key="retry-1")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(repeated.status_code, 201)
        self.assertEqual(first.json(), repeated.json())
        self.assertEqual(first.json()["name"], "Ada Obi")
        self.assertEqual(first.json()["email"], "patient@example.com")
        self.assertEqual(
            self.create(slot_id="slot-2", key="retry-1").status_code, 422
        )
        booking_id = first.json()["id"]
        self.assertEqual(self.client.get(f"/bookings/{booking_id}", headers=self.headers).status_code, 200)
        cancelled = self.client.delete(f"/bookings/{booking_id}", headers=self.headers)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual(self.create().status_code, 201)

    def test_concurrent_requests_cannot_double_book(self):
        def book(index):
            return self.create(email=f"patient{index}@example.com").status_code

        with ThreadPoolExecutor(max_workers=8) as executor:
            statuses = list(executor.map(book, range(8)))
        self.assertEqual(statuses.count(201), 1)
        self.assertEqual(statuses.count(409), 7)

    def test_admin_can_clear_bookings_without_removing_slots(self):
        created = self.create()
        self.assertEqual(created.status_code, 201)
        endpoint = "/admin/clear-bookings"
        self.assertEqual(
            self.client.post(endpoint, headers=self.headers).status_code, 401
        )

        headers = {**self.headers, "X-Admin-Key": "test-admin-key"}
        cleared = self.client.post(endpoint, headers=headers)
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json(), {"ok": True, "message": "bookings cleared"})
        self.assertEqual(
            self.client.get(
                f"/bookings/{created.json()['id']}", headers=self.headers
            ).status_code,
            404,
        )
        slots = self.client.get("/slots", headers=self.headers).json()["slots"]
        self.assertEqual(len(slots), len(self.slots))
        self.assertTrue(all(slot["available"] for slot in slots))

    def test_pagination_and_attempt_rate_limit(self):
        for index in range(3):
            self.assertEqual(
                self.create(slot_id=f"slot-{index + 1}", key=f"page-{index}").status_code,
                201,
            )
        first_page = self.client.get(
            "/bookings", headers=self.headers, params={"email": "PATIENT@example.com", "limit": 1}
        ).json()
        self.assertEqual(len(first_page["bookings"]), 1)
        self.assertIsNotNone(first_page["next_cursor"])
        second_page = self.client.get(
            "/bookings",
            headers=self.headers,
            params={
                "email": "patient@example.com",
                "limit": 1,
                "cursor": first_page["next_cursor"],
            },
        ).json()
        self.assertEqual(len(second_page["bookings"]), 1)
        self.assertNotEqual(first_page["bookings"][0]["id"], second_page["bookings"][0]["id"])

        self.client.post(
            "/reset", headers=self.headers, json={"slots": self.slots}
        )
        for _ in range(5):
            self.assertEqual(self.create(slot_id="missing").status_code, 404)
        limited = self.create(slot_id="missing")
        self.assertEqual(limited.status_code, 429)
        self.assertIn("Retry-After", limited.headers)


if __name__ == "__main__":
    unittest.main()
