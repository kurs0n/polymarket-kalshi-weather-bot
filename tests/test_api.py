"""API contract and access-control tests.

These tests run against an in-memory SQLite database and never trigger the
scheduler lifespan, so no external APIs are called.
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.main import app
from backend.config import settings
from backend.models.database import Base, BotState, Signal, Trade, get_db


class ApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.Session = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

        def override_get_db():
            db = cls.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(get_db, None)

    def setUp(self):
        db = self.Session()
        try:
            db.query(Trade).delete()
            db.query(Signal).delete()
            db.query(BotState).delete()
            db.add(BotState(bankroll=10000.0, total_trades=0, winning_trades=0, total_pnl=0.0, is_running=True))
            db.commit()
        finally:
            db.close()

    def test_health_and_root(self):
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "healthy"})

        root = self.client.get("/")
        self.assertEqual(root.status_code, 200)
        self.assertEqual(root.json()["status"], "ok")
        self.assertIn("simulation_mode", root.json())

    def test_stats_and_empty_collections(self):
        stats = self.client.get("/api/stats")
        self.assertEqual(stats.status_code, 200)
        self.assertEqual(stats.json()["bankroll"], 10000.0)
        self.assertEqual(stats.json()["win_rate"], 0.0)

        self.assertEqual(self.client.get("/api/trades").json(), [])
        self.assertEqual(self.client.get("/api/equity-curve").json(), [])
        self.assertEqual(self.client.get("/api/calibration").json(), {"buckets": [], "summary": None})

    def test_openapi_documents_mutating_endpoints(self):
        spec = self.client.get("/openapi.json")
        self.assertEqual(spec.status_code, 200)
        for path in ("/api/run-scan", "/api/settle-trades", "/api/simulate-trade", "/api/bot/reset"):
            with self.subTest(path=path):
                self.assertIn(path, spec.json()["paths"])

    def test_mutating_endpoints_require_api_key_when_configured(self):
        with patch.object(settings, "API_KEY", "secret-key"):
            missing = self.client.post("/api/bot/reset")
            wrong = self.client.post("/api/bot/reset", headers={"X-API-Key": "nope"})
            allowed = self.client.post("/api/bot/reset", headers={"X-API-Key": "secret-key"})

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["new_bankroll"], settings.INITIAL_BANKROLL)

    def test_mutating_endpoints_are_open_when_no_api_key_configured(self):
        with patch.object(settings, "API_KEY", None):
            response = self.client.post("/api/bot/reset")

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
