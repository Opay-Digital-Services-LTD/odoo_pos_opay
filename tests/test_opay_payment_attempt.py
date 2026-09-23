import base64
from datetime import timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from psycopg2 import IntegrityError

from odoo import fields
from odoo.addons.pos_opay.controllers.main import PosOpayController
from odoo.addons.pos_opay.services.opay_api import (
    OPayClient,
    OPayHTTPError,
    OPayOrderNotFoundError,
)
from odoo.addons.pos_opay.services.opay_auth import (
    OPayAuth,
    OPayConfigurationError,
    OPayMalformedResponseError,
)
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger


@tagged("post_install", "-at_install")
class TestOPayPaymentAttempt(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.opay_private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=1024
        )
        cls.merchant_private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=1024
        )
        opay_public_key = cls.opay_private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        merchant_private_key = cls.merchant_private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        cls.payment_method = cls.env["pos.payment.method"].create(
            {
                "name": "OPay Phase 6 Terminal",
                "payment_method_type": "terminal",
                "use_payment_terminal": "opay",
                "opay_head_merchant_id": "FAKE-HEAD-MERCHANT",
                "opay_merchant_id": "FAKE-BRANCH-MERCHANT",
                "opay_terminal_sn": "FAKE-PHASE6-TERMINAL",
                "opay_client_auth_key": "FAKE-CLIENT-AUTH-KEY",
                "opay_public_key": opay_public_key,
                "opay_merchant_private_key": merchant_private_key,
                "opay_sub_scene_enum": "FAKE-SUB-SCENE",
            }
        )
        cls.config = cls.env["pos.config"].create(
            {
                "name": "OPay Phase 6 POS",
                "payment_method_ids": [(6, 0, [cls.payment_method.id])],
            }
        )
        cls.session = cls.env["pos.session"].create(
            {
                "name": "OPay Phase 6 Session",
                "config_id": cls.config.id,
                "user_id": cls.env.user.id,
                "state": "opened",
            }
        )

    def test_sql_constraint_rejects_duplicate_payment_reference(self):
        attempt = self._attempt()
        reference = str(uuid4())
        attempt_model = self.env["pos.opay.payment.attempt"]
        duplicate_values = attempt_model._new_attempt_values(
            self.payment_method,
            attempt.payment_reference,
            reference.replace("-", "").upper(),
            "100.00",
            session=self.session,
        )

        with (
            self.assertRaises(IntegrityError),
            mute_logger("odoo.sql_db"),
            self.env.cr.savepoint(),
        ):
            attempt_model.create(duplicate_values)

        self.assertEqual(
            attempt_model.search_count(
                [("payment_reference", "=", attempt.payment_reference)]
            ),
            1,
        )

    def test_attempt_uses_a_merchant_facing_display_name(self):
        attempt = self._attempt()

        self.assertEqual(attempt.display_name, f"OPay Payment #{attempt.id}")
        self.assertNotIn("pos.opay.payment.attempt", attempt.display_name)

    def test_opay_response_message_uses_record_translation_context(self):
        attempt_model = self.env["pos.opay.payment.attempt"]

        with self.assertNoLogs("odoo.tools.translate", level="WARNING"):
            message = attempt_model._append_opay_message(
                "Payment is pending.", "Order accepted"
            )

        self.assertIn("OPay response", message)
        self.assertIn("Order accepted", message)

    def test_sql_constraint_rejects_duplicate_out_order_no(self):
        attempt = self._attempt()
        reference = str(uuid4())
        attempt_model = self.env["pos.opay.payment.attempt"]
        duplicate_values = attempt_model._new_attempt_values(
            self.payment_method,
            reference,
            attempt.out_order_no,
            "100.00",
            session=self.session,
        )

        with (
            self.assertRaises(IntegrityError),
            mute_logger("odoo.sql_db"),
            self.env.cr.savepoint(),
        ):
            attempt_model.create(duplicate_values)

        self.assertEqual(
            attempt_model.search_count(
                [("out_order_no", "=", attempt.out_order_no)]
            ),
            1,
        )

    def test_valid_authenticated_success_webhook_and_duplicate_are_idempotent(self):
        attempt = self._attempt()
        envelope, headers = self._webhook(attempt, "SUCCESS")

        with patch.object(type(self.config), "_notify", autospec=True) as notify:
            processed = self.env[
                "pos.opay.payment.attempt"
            ].process_webhook_notification(
                headers, envelope, now_ms="1700000000000"
            )
            duplicate = self.env[
                "pos.opay.payment.attempt"
            ].process_webhook_notification(
                headers, envelope, now_ms="1700000000000"
            )

        self.assertEqual(processed, attempt)
        self.assertEqual(duplicate, attempt)
        self.assertEqual(attempt.status, "SUCCESS")
        self.assertTrue(attempt.finalized_at)
        notify.assert_called_once()
        notification = notify.call_args.args[2]
        self.assertEqual(notification["reference"], attempt.payment_reference)
        self.assertEqual(notification["out_order_no"], attempt.out_order_no)
        self.assertEqual(notification["order_no"], attempt.order_no)
        self.assertEqual(notification["status"], "SUCCESS")
        self.assertTrue(notification["payment_completed"])

    def test_webhook_controller_returns_documented_acknowledgement(self):
        attempt = self._attempt()
        envelope, headers = self._webhook(
            attempt,
            "SUCCESS",
            timestamp=str(OPayAuth._timestamp_ms()),
        )
        fake_request = Mock()
        fake_request.env = self.env
        fake_request.get_json_data.return_value = envelope
        fake_request.httprequest.headers = {
            "merchantId": headers["merchant_id"],
            "X-Opay-Tranid": headers["transaction_id"],
        }
        fake_request.make_json_response.side_effect = (
            lambda payload, status=200: (payload, status)
        )

        with self.assertLogs(
            "odoo.addons.pos_opay.controllers.main", level="INFO"
        ) as logs, patch(
            "odoo.addons.pos_opay.controllers.main.request", fake_request
        ), patch.object(
            PosOpayController, "_duration_ms", return_value=75
        ):
            response = PosOpayController.notification.original_endpoint(
                PosOpayController()
            )

        self.assertEqual(
            response,
            ({"code": "00000", "message": "SUCCESSFUL"}, 200),
        )
        self.assertEqual(attempt.status, "SUCCESS")
        log_message = logs.output[-1]
        self.assertIn("event=opay_webhook_processing", log_message)
        self.assertIn("outcome=processed", log_message)
        self.assertIn("source=webhook", log_message)
        self.assertIn(f"attempt_id={attempt.id}", log_message)
        self.assertIn(f"reference={attempt.payment_reference}", log_message)
        self.assertIn(f"out_order_no={attempt.out_order_no}", log_message)
        self.assertIn(f"order_no={attempt.order_no}", log_message)
        self.assertIn(f"terminal_sn={attempt.terminal_sn}", log_message)
        self.assertIn("status=SUCCESS", log_message)
        self.assertIn("duration_ms=75", log_message)
        for forbidden_value in (
            envelope["clientAuthKey"],
            envelope["sign"],
            envelope["paramContent"],
            self.payment_method.opay_merchant_private_key,
            self.payment_method.opay_public_key,
        ):
            self.assertNotIn(forbidden_value, log_message)

    def test_webhook_controller_rejects_malformed_json(self):
        fake_request = Mock()
        fake_request.get_json_data.side_effect = ValueError("malformed JSON")
        fake_request.make_json_response.side_effect = (
            lambda payload, status=200: (payload, status)
        )

        with self.assertLogs(
            "odoo.addons.pos_opay.controllers.main", level="WARNING"
        ) as logs, patch(
            "odoo.addons.pos_opay.controllers.main.request", fake_request
        ):
            response = PosOpayController.notification.original_endpoint(
                PosOpayController()
            )

        self.assertEqual(
            response,
            ({"code": "00004", "message": "INVALID REQUEST"}, 400),
        )
        self.assertIn("reason=malformed_json", logs.output[0])
        self.assertIn("duration_ms=", logs.output[0])

    def test_webhook_unexpected_error_does_not_log_payload_or_exception_text(self):
        fake_request = Mock()
        fake_request.env = self.env
        fake_request.get_json_data.return_value = {
            "clientAuthKey": "CLIENT-AUTH-MUST-NOT-BE-LOGGED",
            "sign": "SIGNATURE-MUST-NOT-BE-LOGGED",
            "paramContent": "ENCRYPTED-PAYLOAD-MUST-NOT-BE-LOGGED",
        }
        fake_request.httprequest.headers = {}
        fake_request.make_json_response.side_effect = (
            lambda payload, status=200: (payload, status)
        )
        attempt_model = fake_request.env["pos.opay.payment.attempt"]

        with self.assertLogs(
            "odoo.addons.pos_opay.controllers.main", level="ERROR"
        ) as logs, patch(
            "odoo.addons.pos_opay.controllers.main.request", fake_request
        ), patch.object(
            type(attempt_model),
            "process_webhook_notification",
            side_effect=RuntimeError("DECRYPTED-PAYLOAD-MUST-NOT-BE-LOGGED"),
        ):
            response = PosOpayController.notification.original_endpoint(
                PosOpayController()
            )

        self.assertEqual(
            response,
            ({"code": "11004", "message": "PROCESSING ERROR"}, 500),
        )
        log_message = logs.output[0]
        self.assertIn("event=opay_webhook_processing", log_message)
        self.assertIn("outcome=error", log_message)
        self.assertIn("error_type=RuntimeError", log_message)
        self.assertIn("duration_ms=", log_message)
        for forbidden_value in (
            "CLIENT-AUTH-MUST-NOT-BE-LOGGED",
            "SIGNATURE-MUST-NOT-BE-LOGGED",
            "ENCRYPTED-PAYLOAD-MUST-NOT-BE-LOGGED",
            "DECRYPTED-PAYLOAD-MUST-NOT-BE-LOGGED",
        ):
            self.assertNotIn(forbidden_value, log_message)

    def test_webhook_controller_logs_safe_specific_rejection_reason(self):
        attempt = self._attempt()
        envelope, headers = self._webhook(
            attempt,
            "SUCCESS",
            result_overrides={"amount": "999.00"},
            timestamp=str(OPayAuth._timestamp_ms()),
        )
        fake_request = Mock()
        fake_request.env = self.env
        fake_request.get_json_data.return_value = envelope
        fake_request.httprequest.headers = {
            "merchantId": headers["merchant_id"],
            "X-Opay-Tranid": headers["transaction_id"],
        }
        fake_request.make_json_response.side_effect = (
            lambda payload, status=200: (payload, status)
        )

        with self.assertLogs(
            "odoo.addons.pos_opay.controllers.main", level="WARNING"
        ) as logs, patch(
            "odoo.addons.pos_opay.controllers.main.request", fake_request
        ):
            response = PosOpayController.notification.original_endpoint(
                PosOpayController()
            )

        self.assertEqual(
            response,
            ({"code": "00004", "message": "INVALID REQUEST"}, 400),
        )
        log_message = logs.output[0]
        self.assertIn("reason=amount_mismatch", log_message)
        self.assertIn(f"attempt_id={attempt.id}", log_message)
        self.assertIn(f"out_order_no={attempt.out_order_no}", log_message)
        self.assertIn("duration_ms=", log_message)
        self.assertNotIn("FAKE-CLIENT-AUTH-KEY", log_message)

    def test_failure_close_and_cancel_release_terminal(self):
        for status in ("FAIL", "CLOSE", "CANCEL"):
            with self.subTest(status=status):
                attempt = self._attempt()
                envelope, headers = self._webhook(attempt, status)
                self.env["pos.opay.payment.attempt"].process_webhook_notification(
                    headers, envelope, now_ms="1700000000000"
                )

                self.assertEqual(attempt.status, status)
                self.assertTrue(attempt.frontend_response()["terminal_released"])
                self.assertFalse(attempt.frontend_response()["payment_completed"])

    def test_api_success_message_cannot_make_closed_payment_look_successful(self):
        attempt = self._attempt()
        attempt.write({"status": "CLOSE", "last_status_message": "SUCCESS"})

        response = attempt.frontend_response()

        self.assertEqual(response["status"], "CLOSE")
        self.assertFalse(response["payment_completed"])
        self.assertIn("status-check message", response["message"])
        self.assertIn("does not mean the payment succeeded", response["message"])

    def test_webhook_rejects_invalid_signature_and_expired_timestamp(self):
        attempt = self._attempt()
        envelope, headers = self._webhook(attempt, "SUCCESS")
        envelope["sign"] = base64.b64encode(b"invalid-signature").decode()

        with self.assertRaises(ValidationError) as caught:
            self.env["pos.opay.payment.attempt"].process_webhook_notification(
                headers, envelope, now_ms="1700000000000"
            )
        self.assertEqual(
            caught.exception.reason_code, "signature_verification_failure"
        )

        envelope, headers = self._webhook(attempt, "SUCCESS")
        with self.assertRaises(ValidationError) as caught:
            self.env["pos.opay.payment.attempt"].process_webhook_notification(
                headers, envelope, now_ms="1700000300001"
            )
        self.assertEqual(caught.exception.reason_code, "expired_timestamp")
        self.assertEqual(attempt.status, "waiting")

    def test_webhook_reports_invalid_server_crypto_configuration(self):
        attempt = self._attempt()
        envelope, headers = self._webhook(attempt, "SUCCESS")

        with patch(
            "odoo.addons.pos_opay.models.opay_payment_attempt.OPayAuth",
            side_effect=OPayConfigurationError("Invalid test key."),
        ):
            with self.assertRaises(ValidationError) as caught:
                self.env[
                    "pos.opay.payment.attempt"
                ].process_webhook_notification(
                    headers, envelope, now_ms="1700000000000"
                )

        self.assertEqual(
            caught.exception.reason_code, "invalid_crypto_configuration"
        )
        self.assertEqual(attempt.status, "waiting")

    def test_webhook_rejects_every_correlation_mismatch(self):
        mismatches = {
            "unknown_merchant": (
                {},
                {"merchant_id": "WRONG-BRANCH"},
                "unknown_merchant",
            ),
            "amount": ({"amount": "999.00"}, {}, "amount_mismatch"),
            "currency": ({"currency": "USD"}, {}, "currency_mismatch"),
            "terminal": (
                {"sn": "WRONG-TERMINAL"},
                {},
                "terminal_sn_mismatch",
            ),
            "out_order_no": (
                {"outOrderNo": "WRONG-REFERENCE"},
                {},
                "out_order_no_mismatch",
            ),
            "order_no": (
                {"orderNo": "WRONG-ORDER"},
                {},
                "order_no_mismatch",
            ),
            "status": ({"_webhook_status": "UNKNOWN"}, {}, "invalid_status"),
            "merchant_body": (
                {"merchantId": "WRONG-BRANCH"},
                {},
                "merchant_mismatch",
            ),
            "transaction_header": (
                {},
                {"transaction_id": "WRONG-TRANSACTION"},
                "transaction_header_mismatch",
            ),
        }
        for name, (
            result_overrides,
            header_overrides,
            expected_reason,
        ) in mismatches.items():
            with self.subTest(name=name):
                result_overrides = dict(result_overrides)
                webhook_status = result_overrides.pop(
                    "_webhook_status", "SUCCESS"
                )
                attempt = self._attempt()
                envelope, headers = self._webhook(
                    attempt,
                    webhook_status,
                    result_overrides=result_overrides,
                )
                headers.update(header_overrides)
                with self.assertRaises(ValidationError) as caught:
                    self.env[
                        "pos.opay.payment.attempt"
                    ].process_webhook_notification(
                        headers, envelope, now_ms="1700000000000"
                    )
                self.assertEqual(caught.exception.reason_code, expected_reason)
                self.assertEqual(attempt.status, "waiting")

    def test_late_webhook_for_old_attempt_cannot_complete_new_attempt(self):
        old_attempt = self._attempt(status="CLOSE")
        new_attempt = self._attempt()
        envelope, headers = self._webhook(old_attempt, "CLOSE")

        self.env["pos.opay.payment.attempt"].process_webhook_notification(
            headers, envelope, now_ms="1700000000000"
        )

        self.assertEqual(old_attempt.status, "CLOSE")
        self.assertEqual(new_attempt.status, "waiting")

    def test_query_pending_stays_active_and_success_completes_exact_attempt(self):
        attempt = self._attempt(status="uncertain")
        client = Mock(spec=OPayClient)
        client.query_payment.return_value = self._result(
            attempt,
            "PENDING",
            **{OPayClient.RESPONSE_MESSAGE_KEY: "payment still processing"},
        )

        with patch.object(OPayClient, "from_payment_method", return_value=client):
            pending = attempt.query_opay_status(notify=False)
            client.query_payment.return_value = self._result(attempt, "SUCCESS")
            success = attempt.query_opay_status(notify=False)

        self.assertEqual(pending["status"], "PENDING")
        self.assertFalse(pending["terminal_released"])
        self.assertIn("payment still processing", pending["message"])
        self.assertEqual(success["status"], "SUCCESS")
        self.assertTrue(success["payment_completed"])
        self.assertEqual(client.query_payment.call_count, 2)

    def test_query_technical_failure_preserves_authoritative_status(self):
        attempt = self._attempt(status="PENDING")
        client = Mock(spec=OPayClient)
        client.query_payment.side_effect = OPayHTTPError(500)

        with self.assertLogs(
            "odoo.addons.pos_opay.models.opay_payment_attempt", level="WARNING"
        ) as logs, patch.object(
            OPayClient, "from_payment_method", return_value=client
        ):
            response = attempt.query_opay_status(
                notify=False, reason="manual_check"
            )

        self.assertEqual(attempt.status, "PENDING")
        self.assertEqual(response["status"], "PENDING")
        self.assertTrue(response["ambiguous"])
        self.assertFalse(response["payment_completed"])
        client.query_payment.assert_called_once_with(
            out_order_no=attempt.out_order_no,
            order_no=attempt.order_no,
        )
        client.create_payment.assert_not_called()
        self.assertIn("error_category=http_error", logs.output[0])

    def test_query_logs_safe_reason_for_untrusted_opay_error_response(self):
        attempt = self._attempt(status="uncertain")
        client = Mock(spec=OPayClient)
        client.query_payment.side_effect = OPayMalformedResponseError(
            "UNTRUSTED-RESPONSE-BODY-MUST-NOT-BE-LOGGED",
            reason_code="untrusted_error_response",
            opay_code="50099",
        )

        with self.assertLogs(
            "odoo.addons.pos_opay.models.opay_payment_attempt", level="WARNING"
        ) as logs, patch.object(
            OPayClient, "from_payment_method", return_value=client
        ):
            response = attempt.query_opay_status(
                notify=False,
                reason="admin_manual_check",
            )

        self.assertEqual(attempt.status, "uncertain")
        self.assertTrue(response["ambiguous"])
        self.assertFalse(response["payment_completed"])
        log_message = logs.output[0]
        self.assertIn("outcome=uncertain", log_message)
        self.assertIn("code=50099", log_message)
        self.assertIn("error_category=malformed_response", log_message)
        self.assertIn("error_reason=untrusted_error_response", log_message)
        self.assertNotIn("UNTRUSTED-RESPONSE-BODY", log_message)
        client.query_payment.assert_called_once_with(
            out_order_no=attempt.out_order_no,
            order_no=attempt.order_no,
        )
        client.create_payment.assert_not_called()

    def test_confirmed_order_not_found_releases_existing_attempt(self):
        for code in ("00003", "50002"):
            with self.subTest(code=code):
                attempt = self._attempt(status="uncertain")
                client = Mock(spec=OPayClient)
                client.query_payment.side_effect = OPayOrderNotFoundError(
                    code, "order not exist"
                )

                with patch.object(
                    OPayClient, "from_payment_method", return_value=client
                ), patch.object(type(self.config), "_notify", autospec=True):
                    response = attempt.query_opay_status(
                        notify=True,
                        reason="admin_manual_check",
                    )

                self.assertEqual(attempt.status, "failed")
                self.assertTrue(attempt.finalized_at)
                self.assertEqual(attempt.last_source, "query")
                self.assertEqual(attempt.last_status_message, "order not exist")
                self.assertFalse(response["payment_completed"])
                self.assertFalse(response["ambiguous"])
                self.assertTrue(response["terminal_released"])
                self.assertIn("order not exist", response["message"])
                client.create_payment.assert_not_called()

    def test_order_not_found_cannot_downgrade_success(self):
        attempt = self._attempt(status="SUCCESS")
        finalized_at = attempt.finalized_at
        response = attempt._apply_confirmed_order_not_found(
            OPayOrderNotFoundError("50002", "order not exist"),
            notify=False,
            reason="admin_manual_check",
            started_at=0,
        )

        self.assertEqual(attempt.status, "SUCCESS")
        self.assertEqual(attempt.finalized_at, finalized_at)
        self.assertTrue(response["payment_completed"])

    def test_admin_manual_check_queries_existing_attempt_without_create(self):
        attempt = self._attempt(status="uncertain")
        client = Mock(spec=OPayClient)
        client.query_payment.return_value = self._result(attempt, "CLOSE")

        with patch.object(
            OPayClient, "from_payment_method", return_value=client
        ), patch.object(type(self.config), "_notify", autospec=True):
            action = attempt.action_opay_check_payment_status()

        self.assertEqual(attempt.status, "CLOSE")
        self.assertEqual(action["type"], "ir.actions.client")
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(action["params"]["type"], "danger")
        self.assertTrue(action["params"]["sticky"])
        self.assertEqual(
            action["params"]["next"],
            {
                "type": "ir.actions.client",
                "tag": "soft_reload",
            },
        )
        client.query_payment.assert_called_once_with(
            out_order_no=attempt.out_order_no,
            order_no=attempt.order_no,
        )
        client.create_payment.assert_not_called()

    def test_admin_manual_check_requires_erp_manager(self):
        attempt = self._attempt(status="uncertain")
        ordinary_user = self.env["res.users"].create(
            {
                "name": "OPay Status Viewer",
                "login": "opay-status-viewer",
                "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
            }
        )

        with self.assertRaises(AccessError):
            attempt.with_user(ordinary_user).action_opay_check_payment_status()

    def test_admin_manual_check_can_finalize_without_granting_write_access(self):
        attempt = self._attempt(status="uncertain")
        manager = self.env["res.users"].create(
            {
                "name": "OPay Status Manager",
                "login": "opay-status-manager",
                "company_id": attempt.company_id.id,
                "company_ids": [(6, 0, [attempt.company_id.id])],
                "groups_id": [
                    (
                        6,
                        0,
                        [
                            self.env.ref("base.group_user").id,
                            self.env.ref("base.group_erp_manager").id,
                        ],
                    )
                ],
            }
        )
        client = Mock(spec=OPayClient)
        client.query_payment.side_effect = OPayOrderNotFoundError(
            "50002", "order not exist"
        )

        with patch.object(
            OPayClient, "from_payment_method", return_value=client
        ), patch.object(type(self.config), "_notify", autospec=True):
            action = attempt.with_user(manager).action_opay_check_payment_status()

        self.assertEqual(attempt.status, "failed")
        self.assertTrue(attempt.finalized_at)
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(action["params"]["type"], "danger")
        self.assertIn("order not exist", action["params"]["message"])
        with self.assertRaises(AccessError):
            attempt.with_user(manager).write({"status": "SUCCESS"})
        client.create_payment.assert_not_called()

    def test_query_and_finalization_logs_include_reason_duration_and_no_secrets(self):
        attempt = self._attempt(status="uncertain")
        client = Mock(spec=OPayClient)
        client.query_payment.return_value = self._result(attempt, "PENDING")

        with self.assertLogs(
            "odoo.addons.pos_opay.models.opay_payment_attempt", level="INFO"
        ) as logs, patch.object(
            OPayClient, "from_payment_method", return_value=client
        ), patch.object(
            type(attempt), "_duration_ms", side_effect=[125, 10]
        ):
            attempt.query_opay_status(notify=False, reason="manual_check")

        query_log = next(
            message
            for message in logs.output
            if "event=opay_query_order_api" in message
        )
        finalization_log = next(
            message
            for message in logs.output
            if "event=opay_attempt_finalization" in message
        )
        for log_message in (query_log, finalization_log):
            self.assertIn("source=query", log_message)
            self.assertIn("reason=manual_check", log_message)
            self.assertIn(f"payment_method_id={self.payment_method.id}", log_message)
            self.assertIn(f"attempt_id={attempt.id}", log_message)
            self.assertIn(f"reference={attempt.payment_reference}", log_message)
            self.assertIn(f"out_order_no={attempt.out_order_no}", log_message)
            self.assertIn(f"order_no={attempt.order_no}", log_message)
            self.assertIn(f"terminal_sn={attempt.terminal_sn}", log_message)
            for secret in (
                self.payment_method.opay_client_auth_key,
                self.payment_method.opay_merchant_private_key,
                self.payment_method.opay_public_key,
            ):
                self.assertNotIn(secret, log_message)
        self.assertIn("outcome=authenticated", query_log)
        self.assertIn("status=PENDING", query_log)
        self.assertIn("duration_ms=125", query_log)
        self.assertIn("outcome=applied", finalization_log)
        self.assertIn("status=PENDING", finalization_log)
        self.assertIn("duration_ms=10", finalization_log)

    def test_expiry_does_not_query_or_release_terminal(self):
        stale_attempt = self._attempt(
            expires_at=fields.Datetime.now() - timedelta(seconds=1)
        )
        new_reference = str(uuid4())
        client = Mock(spec=OPayClient)

        with patch.object(
            OPayClient, "from_payment_method", return_value=client
        ) as client_factory:
            response = self.payment_method.opay_create_payment_request(
                {
                    "reference": new_reference,
                    "amount": 50,
                    "session_id": self.session.id,
                }
            )

        self.assertEqual(stale_attempt.status, "waiting")
        self.assertEqual(response["status"], "terminal_blocked")
        self.assertIn("Check Previous Payment", response["message"])
        client_factory.assert_not_called()
        client.query_payment.assert_not_called()
        client.create_payment.assert_not_called()

    def test_expired_uncertain_duplicate_does_not_query_or_create(self):
        attempt = self._attempt(
            status="uncertain",
            expires_at=fields.Datetime.now() - timedelta(seconds=1),
        )
        client = Mock(spec=OPayClient)

        with patch.object(
            OPayClient, "from_payment_method", return_value=client
        ) as client_factory:
            response = self.payment_method.opay_create_payment_request(
                {
                    "reference": attempt.payment_reference,
                    "amount": 100,
                    "session_id": self.session.id,
                }
            )

        self.assertEqual(response["status"], "uncertain")
        self.assertTrue(response["reused"])
        client_factory.assert_not_called()
        client.query_payment.assert_not_called()
        client.create_payment.assert_not_called()

    def test_cashier_can_resolve_previous_payment_without_attempt_write_access(self):
        cashier = self.env["res.users"].create({
            "name": "OPay counter recovery test",
            "login": "opay-counter-recovery-test",
            "groups_id": [(6, 0, [self.env.ref("point_of_sale.group_pos_user").id])],
            "company_id": self.env.company.id,
            "company_ids": [(6, 0, [self.env.company.id])],
        })
        request = {"reference": str(uuid4()), "session_id": self.session.id}
        for status in ("SUCCESS", "FAIL", "CLOSE", "CANCEL"):
            with self.subTest(status=status):
                previous = self._attempt(status="uncertain")
                client = Mock(spec=OPayClient)
                client.query_payment.side_effect = [
                    self._result(previous, status),
                    OPayOrderNotFoundError("50002", "order not exist"),
                ]
                with patch.object(OPayClient, "from_payment_method", return_value=client), patch.object(
                    type(self.config), "_notify", autospec=True
                ) as notify:
                    response = self.payment_method.with_user(cashier).opay_check_previous_payment(request)
                self.assertEqual(response["status"], "terminal_available")
                self.assertEqual(response["reference"], request["reference"])
                self.assertFalse(response["payment_completed"])
                self.assertEqual(response["previous_payment"]["reference"], previous.payment_reference)
                self.assertEqual(previous.status, status)
                self.assertTrue(previous.finalized_at)
                self.assertEqual(notify.call_args.args[2]["reference"], previous.payment_reference)
                self.assertEqual(client.query_payment.call_count, 2)
                client.query_payment.assert_any_call(
                    out_order_no=previous.out_order_no, order_no=previous.order_no
                )
                client.query_payment.assert_any_call(
                    out_order_no=request["reference"].replace("-", "").upper()
                )
                client.create_payment.assert_not_called()
                with self.assertRaises(AccessError):
                    previous.with_user(cashier).write({"status": "CANCEL"})

    def test_previous_pending_and_query_failure_keep_terminal_blocked(self):
        previous = self._attempt(status="PENDING")
        client = Mock(spec=OPayClient)
        client.query_payment.return_value = self._result(previous, "PENDING")
        request = {"reference": str(uuid4()), "session_id": self.session.id}
        with patch.object(OPayClient, "from_payment_method", return_value=client):
            pending = self.payment_method.opay_check_previous_payment(request)
            client.query_payment.side_effect = OPayHTTPError(500)
            uncertain = self.payment_method.opay_check_previous_payment(request)
        for response in (pending, uncertain):
            self.assertEqual(response["status"], "terminal_blocked")
            self.assertFalse(response["payment_completed"])
        self.assertEqual(previous.status, "PENDING")
        self.assertFalse(previous.finalized_at)
        client.create_payment.assert_not_called()

    def test_previous_confirmed_absent_releases_only_original_attempt(self):
        previous = self._attempt(status="uncertain")
        request = {"reference": str(uuid4()), "session_id": self.session.id}
        client = Mock(spec=OPayClient)
        client.query_payment.side_effect = OPayOrderNotFoundError("50002", "order not exist")
        with patch.object(OPayClient, "from_payment_method", return_value=client):
            response = self.payment_method.opay_check_previous_payment(request)
        self.assertEqual(previous.status, "failed")
        self.assertTrue(previous.finalized_at)
        self.assertEqual(response["status"], "terminal_available")
        self.assertFalse(response["payment_completed"])
        self.assertFalse(self.env["pos.opay.payment.attempt"].search([
            ("payment_reference", "=", request["reference"]),
        ]))
        client.create_payment.assert_not_called()

    def test_previous_payment_from_older_session_keeps_original_links(self):
        previous = self._attempt()
        original_session = self.session
        original_session.write({"state": "closed"})
        new_session = self.env["pos.session"].create({
            "config_id": self.config.id, "user_id": self.env.user.id, "state": "opened",
        })
        client = Mock(spec=OPayClient)
        client.query_payment.side_effect = [
            self._result(previous, "SUCCESS"),
            OPayOrderNotFoundError("50002", "order not exist"),
        ]
        with patch.object(OPayClient, "from_payment_method", return_value=client):
            response = self.payment_method.opay_check_previous_payment({
                "reference": str(uuid4()), "session_id": new_session.id,
            })
        self.assertEqual(previous.pos_session_id, original_session)
        self.assertEqual(response["previous_payment"]["pos_session_id"], original_session.id)
        self.assertEqual(response["pos_session_id"], new_session.id)
        self.assertFalse(response["payment_completed"])

    def test_clearing_previous_does_not_release_unknown_current_create(self):
        previous = self._attempt()
        request = {"reference": str(uuid4()), "session_id": self.session.id}
        client = Mock(spec=OPayClient)
        client.query_payment.side_effect = [
            self._result(previous, "CLOSE"), OPayHTTPError(500),
        ]
        with patch.object(OPayClient, "from_payment_method", return_value=client):
            response = self.payment_method.opay_check_previous_payment(request)
        self.assertEqual(previous.status, "CLOSE")
        self.assertTrue(previous.finalized_at)
        self.assertEqual(response["status"], "uncertain")
        self.assertFalse(response["safe_to_retry"])
        self.assertEqual(response["reference"], request["reference"])
        self.assertEqual(client.query_payment.call_count, 2)
        client.create_payment.assert_not_called()

    def test_previous_payment_cannot_be_queried_from_another_config(self):
        previous = self._attempt()
        config = self.env["pos.config"].create({
            "name": "Other counter recovery test",
            "payment_method_ids": [(6, 0, [self.payment_method.id])],
        })
        session = self.env["pos.session"].create({
            "config_id": config.id, "user_id": self.env.user.id, "state": "opened",
        })
        from odoo.exceptions import UserError
        with patch.object(OPayClient, "from_payment_method") as factory:
            with self.assertRaises(UserError):
                self.payment_method.opay_check_previous_payment({
                    "reference": str(uuid4()), "session_id": session.id,
                })
        factory.assert_not_called()
        self.assertEqual(previous.status, "waiting")

    def test_previous_check_preserves_current_success_and_missing_outcome_safety(self):
        current = self._attempt(status="SUCCESS")
        previous = self._attempt()
        with patch.object(OPayClient, "from_payment_method") as factory:
            response = self.payment_method.opay_check_previous_payment({
                "reference": current.payment_reference, "session_id": self.session.id,
            })
        self.assertEqual(response["status"], "SUCCESS")
        self.assertEqual(response["reference"], current.payment_reference)
        self.assertEqual(previous.status, "waiting")
        factory.assert_not_called()
        previous.write({"status": "CLOSE"})
        with patch.object(type(self.payment_method), "_opay_resolve_missing_attempt",
                          return_value={"status": "uncertain", "safe_to_retry": False}) as recover:
            response = self.payment_method.opay_check_previous_payment({
                "reference": str(uuid4()), "session_id": self.session.id,
            })
        recover.assert_called_once()
        self.assertFalse(response["safe_to_retry"])

    def _attempt(self, status="waiting", expires_at=None):
        reference = str(uuid4())
        attempt_model = self.env["pos.opay.payment.attempt"]
        attempt = attempt_model.create(
            {
                **attempt_model._new_attempt_values(
                    self.payment_method,
                    reference,
                    reference.replace("-", "").upper(),
                    "100.00",
                    session=self.session,
                ),
                "order_no": f"OPAY-{reference[:8]}",
                "status": status,
                "expires_at": expires_at
                or fields.Datetime.now() + timedelta(seconds=180),
            }
        )
        return attempt

    def _result(self, attempt, status, **overrides):
        return {
            "outOrderNo": attempt.out_order_no,
            "orderNo": attempt.order_no,
            "status": status,
            "amount": attempt.amount,
            "currency": attempt.currency,
            "headMerchantId": attempt.head_merchant_id,
            "merchantId": attempt.merchant_id,
            "sn": attempt.terminal_sn,
            **overrides,
        }

    def _webhook(
        self,
        attempt,
        status,
        result_overrides=None,
        timestamp="1700000000000",
    ):
        result = self._result(attempt, status, **(result_overrides or {}))
        content = {"data": result}
        encrypted_content = self._encrypt_chunks(
            OPayAuth.canonical_json(content).encode(),
            self.merchant_private_key.public_key(),
        )
        signature = self.opay_private_key.sign(
            f"{encrypted_content}{timestamp}".encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return (
            {
                "clientAuthKey": "FAKE-CLIENT-AUTH-KEY",
                "version": "V1.0.1",
                "bodyFormat": "JSON",
                "timestamp": timestamp,
                "paramContent": encrypted_content,
                "sign": base64.b64encode(signature).decode(),
            },
            {
                "merchant_id": attempt.merchant_id,
                "transaction_id": result["orderNo"],
            },
        )

    @staticmethod
    def _encrypt_chunks(plaintext, public_key):
        block_size = public_key.key_size // 8
        max_chunk_size = block_size - 11
        ciphertext = bytearray()
        for offset in range(0, len(plaintext), max_chunk_size):
            ciphertext.extend(
                public_key.encrypt(
                    plaintext[offset : offset + max_chunk_size],
                    padding.PKCS1v15(),
                )
            )
        return base64.b64encode(ciphertext).decode()
