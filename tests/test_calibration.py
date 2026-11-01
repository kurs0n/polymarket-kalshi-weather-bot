"""Calibration summary tests."""

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.routes.calibration import _compute_calibration_summary
from backend.models.database import Base, Signal


class CalibrationSummaryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_summary_scores_settled_signals(self):
        self.db.add_all(
            [
                Signal(
                    market_ticker="M1",
                    direction="yes",
                    model_probability=0.8,
                    edge=0.2,
                    outcome_correct=True,
                    settlement_value=1.0,
                ),
                Signal(
                    market_ticker="M2",
                    direction="no",
                    model_probability=0.6,
                    edge=-0.1,
                    outcome_correct=False,
                    settlement_value=0.0,
                ),
                Signal(market_ticker="M3", direction="yes", model_probability=0.5, edge=0.1),
            ]
        )
        self.db.commit()

        summary = _compute_calibration_summary(self.db)

        self.assertEqual(summary.total_signals, 3)
        self.assertEqual(summary.total_with_outcome, 2)
        self.assertAlmostEqual(summary.accuracy, 0.5)
        self.assertAlmostEqual(summary.avg_predicted_edge, 0.15)
        self.assertAlmostEqual(summary.avg_actual_edge, 0.05)
        self.assertAlmostEqual(summary.brier_score, 0.2)

    def test_summary_is_none_without_signals(self):
        self.assertIsNone(_compute_calibration_summary(self.db))


if __name__ == "__main__":
    unittest.main()
