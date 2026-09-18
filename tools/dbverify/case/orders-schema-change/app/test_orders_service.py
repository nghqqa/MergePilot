"""Unit tests shipped with the PR. They pass on every revision of the
migration because they exercise the code, not the historical data."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from orders_service import OrderError, OrderService  # noqa: E402


class TestOrderService(unittest.TestCase):

    def test_create_order_requires_customer(self):
        svc = OrderService()
        with self.assertRaises(OrderError):
            svc.create_order(1, None, 100)

    def test_create_order_rejects_non_positive_amount(self):
        svc = OrderService()
        with self.assertRaises(OrderError):
            svc.create_order(1, 42, 0)

    def test_create_order_rejects_duplicate_id(self):
        svc = OrderService()
        svc.create_order(1, 42, 100)
        with self.assertRaises(OrderError):
            svc.create_order(1, 43, 100)

    def test_payment_marks_order_paid(self):
        svc = OrderService()
        svc.create_order(7, 42, 250)
        svc.record_payment(700, 7, 250)
        self.assertEqual(svc.orders[7].status, "paid")

    def test_second_payment_for_same_order_is_idempotent(self):
        svc = OrderService()
        svc.create_order(7, 42, 250)
        first = svc.record_payment(700, 7, 250)
        replay = svc.record_payment(701, 7, 250)
        self.assertIs(first, replay)
        self.assertEqual(len(svc.payments_by_order), 1)

    def test_payment_amount_must_match_order(self):
        svc = OrderService()
        svc.create_order(7, 42, 250)
        with self.assertRaises(OrderError):
            svc.record_payment(700, 7, 999)

    def test_payment_for_unknown_order_rejected(self):
        svc = OrderService()
        with self.assertRaises(OrderError):
            svc.record_payment(700, 404, 100)


if __name__ == "__main__":
    unittest.main()
