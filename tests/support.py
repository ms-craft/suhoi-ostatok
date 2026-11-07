import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.work import run_job


PASSWORD = "unique-synthetic-password-42"


def image_bytes(color="white", file_format="PNG"):
    with BytesIO() as output:
        Image.new("RGB", (40, 30), color=color).save(output, format=file_format)
        return output.getvalue()


def photograph(color="white", filename="worksheet.jpg"):
    return ("files", (filename, image_bytes(color, "JPEG"), "image/jpeg"))


def document_pixel(content):
    with Image.open(BytesIO(content)) as image:
        return image.convert("RGB").getpixel((0, 0))


@dataclass
class Harness:
    app: FastAPI
    client: TestClient
    payment_secret: str

    def register(self, email=None):
        credentials = {"email": email or f"teacher-{uuid4().hex}@example.com", "password": PASSWORD}
        response = self.client.post("/v1/auth/register", json=credentials)
        assert response.status_code == 201, response.text
        token = response.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}, credentials

    def checkout(self, auth):
        response = self.client.post("/v1/subscription/checkout", headers=auth, json={"plan_code": "demo"})
        assert response.status_code == 201, response.text
        assert response.json()["demonstration"] is True
        return response.json()

    def event(self, checkout_id, *, kind="payment_succeeded", sequence=1, start=None, end=None):
        start = start or (datetime.now(timezone.utc) - timedelta(minutes=1))
        end = end or (start + timedelta(days=30))
        event = {
            "id": uuid4().hex,
            "checkout_id": checkout_id,
            "type": kind,
            "sequence": sequence,
        }
        if kind in {"payment_succeeded", "renewed"}:
            event.update(period_start=start.isoformat(), period_end=end.isoformat())
        return event

    def webhook(self, event, *, secret=None):
        body = json.dumps(event).encode()
        signing_secret = self.payment_secret if secret is None else secret
        signature = hmac.new(signing_secret.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post(
            "/v1/payments/webhook",
            content=body,
            headers={"Content-Type": "application/json", "X-Payment-Signature": signature},
        )

    def subscribe(self, auth):
        checkout = self.checkout(auth)
        event = self.event(checkout["checkout_id"])
        response = self.webhook(event)
        assert response.status_code == 200, response.text
        assert response.json()["applied"] is True
        return event

    def subscription(self, auth):
        response = self.client.get("/v1/subscription", headers=auth)
        assert response.status_code == 200, response.text
        return response.json()

    def upload(self, auth, *files):
        return self.client.post("/v1/submissions", headers=auth, files=files or [photograph()])

    def queued_submission(self, auth, *files):
        response = self.upload(auth, *files)
        assert response.status_code == 202, response.text
        payload = response.json()
        assert payload["status"] == "queued"
        return payload

    def submission(self, auth, submission_id):
        response = self.client.get(f"/v1/submissions/{submission_id}", headers=auth)
        assert response.status_code == 200, response.text
        return response.json()

    def results(self, auth, submission_id):
        response = self.client.get(f"/v1/submissions/{submission_id}/results", headers=auth)
        assert response.status_code == 200, response.text
        return response.json()

    def process(self, job_id):
        return run_job(self.app.state, job_id)
