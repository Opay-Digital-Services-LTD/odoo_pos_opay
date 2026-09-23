import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from odoo import SUPERUSER_ID, api
from odoo.api import Transaction
from odoo.addons.pos_opay.models.opay_payment_attempt import (
    OPayWebhookValidationError,
)
from odoo.addons.pos_opay.services.opay_api import (
    OPayClient,
    OPayCreatePaymentResult,
)
from odoo.modules.registry import DummyRLock, Registry
from odoo.service.model import retrying
from odoo.tests.common import BaseCase, get_db_name, tagged
from odoo.tools import mute_logger


class ControlledOPayClient:
    """Thread-safe fake client with controllable first Create/Query calls."""

    def __init__(
        self, *, create_release=None, query_release=None, query_status="SUCCESS"
    ):
        self.create_release = create_release
        self.query_release = query_release
        self.query_status = query_status
        self.create_entered = threading.Event()
        self.query_entered = threading.Event()
        self._mutex = threading.Lock()
        self.create_calls = []
        self.query_calls = []

    def create_payment(self, *, out_order_no, amount, currency):
        with self._mutex:
            self.create_calls.append((out_order_no, amount, currency))
            call_number = len(self.create_calls)
        if call_number == 1:
            self.create_entered.set()
            if self.create_release and not self.create_release.wait(timeout=10):
                raise AssertionError("Timed out while holding the fake Create call.")
        return OPayCreatePaymentResult(
            out_order_no=out_order_no,
            order_no=f"OPAY-{out_order_no[-12:]}",
        )

    def query_payment(self, *, out_order_no, order_no=None):
        with self._mutex:
            self.query_calls.append((out_order_no, order_no))
            call_number = len(self.query_calls)
        if call_number == 1:
            self.query_entered.set()
            if self.query_release and not self.query_release.wait(timeout=10):
                raise AssertionError("Timed out while holding the fake Query call.")
        return {
            "outOrderNo": out_order_no,
            "orderNo": order_no or f"OPAY-{out_order_no[-12:]}",
            "status": self.query_status,
            "amount": "10.00",
            "currency": "NGN",
        }


@tagged("post_install", "-at_install")
class TestOPayConcurrency(BaseCase):
    """Exercise real PostgreSQL row locks using one cursor per worker."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.registry = Registry(get_db_name())
        cls.startClassPatcher(patch.object(Registry, "_lock", DummyRLock()))

    def setUp(self):
        super().setUp()
        # PostgreSQL logs the serialization failures intentionally exercised by
        # this class at ERROR before Odoo's retrying() helper retries them. Keep
        # those expected test-only logs from making an Odoo.sh build appear
        # failed while retaining the real transactions, locks, and assertions.
        self._expected_log_mute = mute_logger(
            "odoo.sql_db",
            "odoo.addons.pos_opay.models.opay_payment_attempt",
        )
        self._expected_log_mute.__enter__()
        self.addCleanup(self._expected_log_mute.__exit__)
        token = uuid4().hex.upper()
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            payment_method = env["pos.payment.method"].create(
                {
                    "name": f"OPay concurrency terminal {token}",
                    "payment_method_type": "terminal",
                    "use_payment_terminal": "opay",
                    "opay_head_merchant_id": f"HEAD-{token}",
                    "opay_merchant_id": f"MERCHANT-{token}",
                    "opay_terminal_sn": f"SN-{token}",
                    "opay_client_auth_key": "FAKE-CONCURRENCY-AUTH-KEY",
                    "opay_public_key": "FAKE-CONCURRENCY-OPAY-PUBLIC-KEY",
                    "opay_merchant_private_key": "FAKE-CONCURRENCY-PRIVATE-KEY",
                    "opay_sub_scene_enum": "FAKE-SUB-SCENE",
                }
            )
            config = env["pos.config"].create(
                {
                    "name": f"OPay concurrency POS {token}",
                    "payment_method_ids": [(6, 0, [payment_method.id])],
                }
            )
            session = env["pos.session"].create(
                {
                    "name": f"OPay concurrency session {token}",
                    "config_id": config.id,
                    "user_id": SUPERUSER_ID,
                    "state": "opened",
                }
            )
            self.company_id = env.company.id
            self.payment_method_id = payment_method.id
            self.config_id = config.id
            self.session_id = session.id
        self.addCleanup(self._cleanup_records)

    def _cleanup_records(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["pos.opay.payment.attempt"].sudo().search(
                [("payment_method_id", "=", self.payment_method_id)]
            ).unlink()
            env["pos.session"].sudo().browse(self.session_id).exists().unlink()
            env["pos.config"].sudo().browse(self.config_id).exists().unlink()
            env["pos.payment.method"].sudo().browse(
                self.payment_method_id
            ).exists().unlink()

    def _environment(self, cr):
        # Module upgrade tests run while the main thread owns Registry._lock.
        # Bind this worker's independent cursor to the already loaded registry
        # instead of making api.Environment look it up again from the thread.
        if cr.transaction is None:
            cr.transaction = Transaction(self.registry)
        return api.Environment(
            cr,
            SUPERUSER_ID,
            {"allowed_company_ids": [self.company_id]},
        )

    def _create_request(self, reference):
        return {
            "reference": reference,
            "amount": 10.0,
            "session_id": self.session_id,
        }

    def _status_request(self, reference):
        return {"reference": reference, "session_id": self.session_id}

    def _call_payment_method(
        self, method_name, request, *, pid_ready=None, pid_box=None
    ):
        with self.registry.cursor() as cr:
            if pid_ready is not None:
                cr.execute("SELECT pg_backend_pid()")
                pid_box.append(cr.fetchone()[0])
                pid_ready.set()
            env = self._environment(cr)
            payment_method = env["pos.payment.method"].browse(self.payment_method_id)
            result = retrying(
                lambda: getattr(payment_method, method_name)(request), env
            )
            return result

    def _apply_result(
        self,
        attempt_id,
        result,
        source="webhook",
        *,
        pid_ready=None,
        pid_box=None,
    ):
        with self.registry.cursor() as cr:
            if pid_ready is not None:
                cr.execute("SELECT pg_backend_pid()")
                pid_box.append(cr.fetchone()[0])
                pid_ready.set()
            env = self._environment(cr)
            attempt = env["pos.opay.payment.attempt"].browse(attempt_id)
            return retrying(
                lambda: attempt.apply_authenticated_result(
                    result, source=source, notify=False
                ),
                env,
            )

    def _create_attempt(self, reference=None):
        reference = reference or str(uuid4())
        out_order_no = reference.replace("-", "").upper()
        order_no = f"OPAY-{out_order_no[-12:]}"
        with self.registry.cursor() as cr:
            env = self._environment(cr)
            payment_method = env["pos.payment.method"].browse(self.payment_method_id)
            session = env["pos.session"].browse(self.session_id)
            attempt_model = env["pos.opay.payment.attempt"].sudo()
            values = attempt_model._new_attempt_values(
                payment_method,
                reference,
                out_order_no,
                "10.00",
                session=session,
            )
            values.update({"status": "waiting", "order_no": order_no})
            attempt = attempt_model.create(values)
            payment_method.sudo().write(
                {
                    "opay_latest_payment_reference": reference,
                    "opay_latest_out_order_no": out_order_no,
                    "opay_latest_order_no": order_no,
                    "opay_latest_amount": "10.00",
                    "opay_latest_status": "waiting",
                }
            )
            return attempt.id, reference, self._provider_result(
                reference, order_no, "SUCCESS"
            )

    @staticmethod
    def _provider_result(reference, order_no, status):
        return {
            "outOrderNo": reference.replace("-", "").upper(),
            "orderNo": order_no,
            "status": status,
            "amount": "10.00",
            "currency": "NGN",
        }

    def _attempt_rows(self):
        with self.registry.cursor() as cr:
            env = self._environment(cr)
            return env["pos.opay.payment.attempt"].sudo().search_read(
                [("payment_method_id", "=", self.payment_method_id)],
                [
                    "payment_reference",
                    "out_order_no",
                    "order_no",
                    "status",
                    "finalized_at",
                    "last_source",
                ],
                order="id",
            )

    def _wait_for_database_lock(self, backend_pid, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.registry.cursor() as cr:
                cr.execute(
                    """
                    SELECT wait_event_type
                      FROM pg_stat_activity
                     WHERE pid = %s
                    """,
                    [backend_pid],
                )
                row = cr.fetchone()
            if row and row[0] == "Lock":
                return True
            time.sleep(0.02)
        return False

    def test_same_uuid_concurrent_create_calls_opay_once(self):
        reference = str(uuid4())
        release_create = threading.Event()
        client = ControlledOPayClient(create_release=release_create)
        try:
            with patch.object(OPayClient, "from_payment_method", return_value=client):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(
                        self._call_payment_method,
                        "opay_create_payment_request",
                        self._create_request(reference),
                    )
                    self.assertTrue(client.create_entered.wait(timeout=3))
                    second = executor.submit(
                        self._call_payment_method,
                        "opay_create_payment_request",
                        self._create_request(reference),
                    )
                    time.sleep(0.15)
                    self.assertEqual(len(client.create_calls), 1)
                    release_create.set()
                    first_result = first.result(timeout=5)
                    second_result = second.result(timeout=5)
        finally:
            release_create.set()

        self.assertEqual(len(client.create_calls), 1)
        self.assertEqual(first_result["status"], "waiting")
        self.assertEqual(second_result["status"], "waiting")
        self.assertTrue(second_result["reused"])
        attempts = self._attempt_rows()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["payment_reference"], reference)
        self.assertEqual(attempts[0]["status"], "waiting")

    def test_same_terminal_competing_create_calls_opay_once(self):
        first_reference = str(uuid4())
        second_reference = str(uuid4())
        release_create = threading.Event()
        client = ControlledOPayClient(create_release=release_create)
        try:
            with patch.object(OPayClient, "from_payment_method", return_value=client):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(
                        self._call_payment_method,
                        "opay_create_payment_request",
                        self._create_request(first_reference),
                    )
                    self.assertTrue(client.create_entered.wait(timeout=3))
                    second = executor.submit(
                        self._call_payment_method,
                        "opay_create_payment_request",
                        self._create_request(second_reference),
                    )
                    time.sleep(0.15)
                    self.assertEqual(len(client.create_calls), 1)
                    release_create.set()
                    first_result = first.result(timeout=5)
                    second_result = second.result(timeout=5)
        finally:
            release_create.set()

        self.assertEqual(len(client.create_calls), 1)
        self.assertEqual(first_result["status"], "waiting")
        self.assertEqual(second_result["status"], "terminal_blocked")
        attempts = self._attempt_rows()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["payment_reference"], first_reference)

    def test_create_and_recovery_wait_for_same_payment_method_lock(self):
        reference = str(uuid4())
        release_create = threading.Event()
        recovery_started = threading.Event()
        client = ControlledOPayClient(
            create_release=release_create, query_status="PENDING"
        )

        def recover():
            recovery_started.set()
            return self._call_payment_method(
                "opay_resolve_create_outcome", self._status_request(reference)
            )

        try:
            with patch.object(OPayClient, "from_payment_method", return_value=client):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    create_future = executor.submit(
                        self._call_payment_method,
                        "opay_create_payment_request",
                        self._create_request(reference),
                    )
                    self.assertTrue(client.create_entered.wait(timeout=3))
                    recovery_future = executor.submit(recover)
                    self.assertTrue(recovery_started.wait(timeout=3))
                    time.sleep(0.15)
                    self.assertFalse(recovery_future.done())
                    self.assertEqual(len(client.query_calls), 0)
                    release_create.set()
                    create_result = create_future.result(timeout=5)
                    recovery_result = recovery_future.result(timeout=5)
        finally:
            release_create.set()

        self.assertEqual(create_result["status"], "waiting")
        self.assertEqual(recovery_result["status"], "PENDING")
        self.assertFalse(recovery_result.get("local_attempt_missing", False))
        self.assertEqual(len(client.create_calls), 1)
        self.assertEqual(len(client.query_calls), 1)
        self.assertEqual(len(self._attempt_rows()), 1)

    def test_query_success_and_webhook_success_converge_without_deadlock(self):
        attempt_id, reference, success_result = self._create_attempt()
        release_query = threading.Event()
        client = ControlledOPayClient(query_release=release_query)
        webhook_pid_ready = threading.Event()
        webhook_pid = []
        try:
            with patch.object(OPayClient, "from_payment_method", return_value=client):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    query_future = executor.submit(
                        self._call_payment_method,
                        "opay_get_payment_status",
                        self._status_request(reference),
                    )
                    self.assertTrue(client.query_entered.wait(timeout=3))
                    webhook_future = executor.submit(
                        self._apply_result,
                        attempt_id,
                        success_result,
                        "webhook",
                        pid_ready=webhook_pid_ready,
                        pid_box=webhook_pid,
                    )
                    self.assertTrue(webhook_pid_ready.wait(timeout=3))
                    self.assertTrue(
                        self._wait_for_database_lock(webhook_pid[0]),
                        "The webhook worker should serialize with the active Query.",
                    )
                    release_query.set()
                    query_result = query_future.result(timeout=8)
                    webhook_result = webhook_future.result(timeout=8)
        finally:
            release_query.set()

        self.assertEqual(query_result["status"], "SUCCESS")
        self.assertFalse(webhook_result)
        self.assertEqual(len(client.query_calls), 1)
        attempt = self._attempt_rows()[0]
        self.assertEqual(attempt["status"], "SUCCESS")
        self.assertTrue(attempt["finalized_at"])

    def test_duplicate_success_is_idempotent_under_concurrency(self):
        attempt_id, _reference, success_result = self._create_attempt()
        barrier = threading.Barrier(2)

        def apply_success(source):
            barrier.wait(timeout=3)
            return self._apply_result(attempt_id, success_result, source)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(apply_success, "query")
            second = executor.submit(apply_success, "webhook")
            results = [first.result(timeout=5), second.result(timeout=5)]

        self.assertCountEqual(results, [True, False])
        attempt = self._attempt_rows()[0]
        first_finalized_at = attempt["finalized_at"]
        self.assertEqual(attempt["status"], "SUCCESS")
        self.assertTrue(first_finalized_at)
        self.assertFalse(self._apply_result(attempt_id, success_result, "webhook"))
        self.assertEqual(self._attempt_rows()[0]["finalized_at"], first_finalized_at)

    def test_conflicting_final_results_never_overwrite_first_accepted(self):
        for conflicting_status in ("CANCEL", "FAIL"):
            with self.subTest(conflicting_status=conflicting_status):
                attempt_id, reference, success_result = self._create_attempt()
                conflict_result = self._provider_result(
                    reference, success_result["orderNo"], conflicting_status
                )
                barrier = threading.Barrier(2)

                def apply(result, source):
                    barrier.wait(timeout=3)
                    try:
                        return ("accepted", result["status"], self._apply_result(
                            attempt_id, result, source
                        ))
                    except OPayWebhookValidationError as error:
                        return ("rejected", result["status"], error.reason_code)

                with ThreadPoolExecutor(max_workers=2) as executor:
                    success_future = executor.submit(apply, success_result, "query")
                    conflict_future = executor.submit(
                        apply, conflict_result, "webhook"
                    )
                    outcomes = [
                        success_future.result(timeout=5),
                        conflict_future.result(timeout=5),
                    ]

                accepted = [outcome for outcome in outcomes if outcome[0] == "accepted"]
                rejected = [outcome for outcome in outcomes if outcome[0] == "rejected"]
                self.assertEqual(len(accepted), 1)
                self.assertEqual(len(rejected), 1)
                self.assertEqual(rejected[0][2], "conflicting_final_status")
                attempt = self._attempt_rows()[-1]
                self.assertEqual(attempt["status"], accepted[0][1])
                self.assertTrue(attempt["finalized_at"])
                finalized_at = attempt["finalized_at"]
                rejected_result = (
                    conflict_result
                    if attempt["status"] == "SUCCESS"
                    else success_result
                )
                with self.assertRaises(OPayWebhookValidationError):
                    self._apply_result(attempt_id, rejected_result, "webhook")
                unchanged_attempt = self._attempt_rows()[-1]
                self.assertEqual(unchanged_attempt["status"], attempt["status"])
                self.assertEqual(
                    unchanged_attempt["finalized_at"], finalized_at
                )

    def test_concurrent_query_requests_query_opay_once_after_success(self):
        _attempt_id, reference, _success_result = self._create_attempt()
        release_query = threading.Event()
        client = ControlledOPayClient(query_release=release_query)
        second_pid_ready = threading.Event()
        second_pid = []
        try:
            with patch.object(OPayClient, "from_payment_method", return_value=client):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(
                        self._call_payment_method,
                        "opay_get_payment_status",
                        self._status_request(reference),
                    )
                    self.assertTrue(client.query_entered.wait(timeout=3))
                    second = executor.submit(
                        self._call_payment_method,
                        "opay_get_payment_status",
                        self._status_request(reference),
                        pid_ready=second_pid_ready,
                        pid_box=second_pid,
                    )
                    self.assertTrue(second_pid_ready.wait(timeout=3))
                    self.assertTrue(self._wait_for_database_lock(second_pid[0]))
                    release_query.set()
                    results = [first.result(timeout=8), second.result(timeout=8)]
        finally:
            release_query.set()

        self.assertEqual(
            [result["status"] for result in results], ["SUCCESS", "SUCCESS"]
        )
        self.assertEqual(len(client.query_calls), 1)
        attempt = self._attempt_rows()[0]
        self.assertEqual(attempt["status"], "SUCCESS")
        self.assertTrue(attempt["finalized_at"])
