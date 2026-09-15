"""Acceptance tests attached to the PR (unchanged across fix attempts).

The Verifier's verdict is decided by running exactly this file against the
Fixer's revision — never by the Fixer's own claim.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from payments import LedgerError, PaymentLedger  # noqa: E402


class TestPaymentLedger(unittest.TestCase):

    def test_single_charge_recorded(self):
        ledger = PaymentLedger()
        ledger.charge(order_id=1, payment_id=100, amount_cents=500)
        self.assertEqual(ledger.total_for_order(1), 500)

    def test_replay_same_payment_is_idempotent(self):
        ledger = PaymentLedger()
        ledger.charge(order_id=1, payment_id=100, amount_cents=500)
        ledger.charge(order_id=1, payment_id=100, amount_cents=500)   # client retry, same payment id
        self.assertEqual(ledger.total_for_order(1), 500)

    def test_second_payment_request_for_same_order_is_idempotent(self):
        ledger = PaymentLedger()
        ledger.charge(order_id=1, payment_id=100, amount_cents=500)
        ledger.charge(order_id=1, payment_id=101, amount_cents=500)   # gateway retry, NEW payment id
        self.assertEqual(ledger.total_for_order(1), 500, "an order must never be charged twice")

    def test_different_orders_are_independent(self):
        ledger = PaymentLedger()
        ledger.charge(order_id=1, payment_id=100, amount_cents=500)
        ledger.charge(order_id=2, payment_id=200, amount_cents=700)
        self.assertEqual(ledger.total_for_order(1), 500)
        self.assertEqual(ledger.total_for_order(2), 700)

    def test_non_positive_amount_rejected(self):
        ledger = PaymentLedger()
        with self.assertRaises(LedgerError):
            ledger.charge(order_id=1, payment_id=100, amount_cents=0)


if __name__ == "__main__":
    unittest.main()
