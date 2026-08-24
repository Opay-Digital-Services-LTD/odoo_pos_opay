from unittest.mock import Mock, patch

from lxml import etree

from odoo.addons.pos_opay.services.opay_api import (
    OPayAPIError,
    OPayClient,
    OPayConnectionError,
    OPayCreatePaymentResult,
    OPayHTTPError,
    OPayOrderNotFoundError,
)
from odoo.addons.pos_opay.services.opay_auth import OPayConfigurationError
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPosPaymentMethod(TransactionCase):
    payment_uuid = "96be4f01-48e4-41f6-b6eb-f0638d5d8e6d"
    out_order_no = "96BE4F0148E441F6B6EBF0638D5D8E6D"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.payment_method_model = cls.env["pos.payment.method"]
        cls.opay_values = {
            "name": "OPay Test Terminal",
            "payment_method_type": "terminal",
            "use_payment_terminal": "opay",
            "opay_head_merchant_id": "FAKE-HEAD-MERCHANT",
            "opay_merchant_id": "FAKE-BRANCH-MERCHANT",
            "opay_terminal_sn": "FAKE-TERMINAL-001",
            "opay_client_auth_key": "FAKE-CLIENT-AUTH-KEY",
            "opay_public_key": "FAKE-OPAY-PUBLIC-KEY",
            "opay_merchant_private_key": "FAKE-MERCHANT-PRIVATE-KEY",
            "opay_sub_scene_enum": "FAKE-SUB-SCENE",
        }

    def test_opay_is_registered_as_payment_terminal(self):
        self.assertIn(
            ("opay", "OPay"),
            self.payment_method_model._get_payment_terminal_selection(),
        )

    def test_opay_configuration_can_be_stored(self):
        payment_method = self.payment_method_model.create(dict(self.opay_values))

        self.assertRecordValues(payment_method, [self.opay_values])

    def test_opay_fields_are_conditional_in_payment_method_form(self):
        form_view = self.payment_method_model.get_view(
            self.env.ref("point_of_sale.pos_payment_method_view_form").id,
            "form",
        )
        form_arch = etree.fromstring(form_view["arch"])
        opay_field_names = (
            "opay_head_merchant_id",
            "opay_merchant_id",
            "opay_terminal_sn",
            "opay_sub_scene_enum",
            "opay_client_auth_key",
            "opay_public_key",
            "opay_merchant_private_key",
        )

        for field_name in opay_field_names:
            field_node = form_arch.xpath(f"//field[@name='{field_name}']")
            self.assertEqual(len(field_node), 1)
            self.assertEqual(
                field_node[0].get("invisible"),
                "use_payment_terminal != 'opay'",
            )
            self.assertEqual(
                field_node[0].get("required"),
                "use_payment_terminal == 'opay'",
            )

        for field_name in (
            "opay_client_auth_key",
            "opay_public_key",
            "opay_merchant_private_key",
        ):
            self.assertEqual(
                form_arch.xpath(f"//field[@name='{field_name}']")[0].get("widget"),
                "password",
            )
        journal_hint = form_arch.xpath("//div[@name='opay_bank_journal_hint']")
        self.assertEqual(len(journal_hint), 1)
        self.assertEqual(
            journal_hint[0].get("invisible"),
            "payment_method_type != 'terminal' or type == 'bank'",
        )
        self.assertIn("select a Bank journal", " ".join(journal_hint[0].itertext()))
        service_disclosure = form_arch.xpath(
            "//div[@name='opay_external_service_disclosure']"
        )
        self.assertEqual(len(service_disclosure), 1)
        self.assertEqual(
            service_disclosure[0].get("invisible"),
            "use_payment_terminal != 'opay'",
        )
        disclosure_text = " ".join(service_disclosure[0].itertext())
        self.assertIn("external OPay cloud POS service", disclosure_text)
        self.assertIn("does not send customer card", disclosure_text)
        for section in (
            "OPay Merchant and Terminal",
            "OPay Authentication",
            "OPay Webhook",
        ):
            self.assertEqual(
                len(form_arch.xpath(f"//separator[@string='{section}']")), 1
            )

    def test_opay_configuration_metadata_matches_onboarding_terms(self):
        expected_terms = {
            "opay_head_merchant_id": ("headMerchantId", "Head Merchant ID"),
            "opay_merchant_id": ("merchantId", "branch"),
            "opay_terminal_sn": ("(sn)", "physical OPay terminal"),
            "opay_sub_scene_enum": ("subSceneEnum", "do not guess"),
            "opay_client_auth_key": ("clientAuthKey", "never sent to the cashier POS"),
            "opay_public_key": ("OPay Public Key", "merchant public key"),
            "opay_merchant_private_key": ("Merchant Private Key", "registered with OPay"),
            "opay_event_url": ("OPay Webhook URL", "public HTTPS URL"),
        }

        for field_name, (label_term, help_term) in expected_terms.items():
            with self.subTest(field_name=field_name):
                field = self.payment_method_model._fields[field_name]
                self.assertIn(label_term, field.string)
                self.assertIn(help_term, field.help)
        for field_name in (
            "opay_client_auth_key",
            "opay_public_key",
            "opay_merchant_private_key",
        ):
            self.assertEqual(
                self.payment_method_model._fields[field_name].groups,
                "base.group_erp_manager",
            )

    def test_opay_requires_complete_configuration(self):
        incomplete_values = dict(self.opay_values, opay_client_auth_key=False)

        with self.assertRaisesRegex(
            ValidationError,
            "OPay Test Terminal.*OPay Client Auth Key \\(clientAuthKey\\)",
        ):
            self.payment_method_model.create(incomplete_values)

    def test_opay_terminal_must_be_unique(self):
        self.payment_method_model.create(dict(self.opay_values))
        duplicate_values = dict(self.opay_values, name="Duplicate OPay Terminal")

        with self.assertRaisesRegex(ValidationError, "FAKE-TERMINAL-001"):
            self.payment_method_model.create(duplicate_values)

    def test_opay_terminal_must_be_unique_across_companies(self):
        other_company = self.env["res.company"].create(
            {"name": "Other OPay Test Company"}
        )
        other_company_values = dict(
            self.opay_values,
            name="Other Company OPay Terminal",
            company_id=other_company.id,
        )
        other_company_model = self.payment_method_model.with_context(
            allowed_company_ids=[self.env.company.id, other_company.id]
        ).with_company(other_company)
        other_company_model.create(other_company_values)

        with self.assertRaisesRegex(ValidationError, "Other OPay Test Company"):
            self.payment_method_model.create(dict(self.opay_values))

    def test_non_opay_payment_method_is_not_affected(self):
        payment_method = self.payment_method_model.create(
            {
                "name": "Manual Bank Payment",
                "payment_method_type": "none",
            }
        )

        self.assertFalse(payment_method.use_payment_terminal)

    def test_opay_configuration_is_not_loaded_in_pos_data(self):
        loaded_fields = self.payment_method_model._load_pos_data_fields(False)

        for field_name in self.opay_values:
            if field_name.startswith("opay_"):
                with self.subTest(field_name=field_name):
                    self.assertNotIn(field_name, loaded_fields)

    def _runtime_records(self):
        payment_method = self.payment_method_model.create(dict(self.opay_values))
        config = self.env["pos.config"].create(
            {
                "name": "OPay Runtime POS",
                "payment_method_ids": [(6, 0, [payment_method.id])],
            }
        )
        session = self.env["pos.session"].create(
            {
                "name": "OPay Runtime Session",
                "config_id": config.id,
                "user_id": self.env.user.id,
                "state": "opened",
            }
        )
        return payment_method, session

    def _runtime_request(self, session, **overrides):
        return {
            "reference": self.payment_uuid,
            "amount": 1250.0,
            "session_id": session.id,
            **overrides,
        }

    def test_create_payment_request_returns_waiting_result(self):
        payment_method, session = self._runtime_records()
        client = Mock(spec=OPayClient)
        client.create_payment.return_value = OPayCreatePaymentResult(
            out_order_no=self.out_order_no,
            order_no="OPAY-ORDER-1",
            message="payment request accepted",
        )

        with patch.object(
            OPayClient, "from_payment_method", return_value=client
        ) as client_factory:
            result = payment_method.opay_create_payment_request(
                self._runtime_request(session)
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["reference"], self.payment_uuid)
        self.assertEqual(result["out_order_no"], self.out_order_no)
        self.assertEqual(result["order_no"], "OPAY-ORDER-1")
        self.assertFalse(result["payment_completed"])
        self.assertFalse(result["ambiguous"])
        self.assertFalse(result["terminal_released"])
        self.assertFalse(result["reused"])
        self.assertIn("payment request accepted", result["message"])
        client_factory.assert_called_once()
        self.assertEqual(client_factory.call_args.args[0], payment_method)
        client.create_payment.assert_called_once_with(
            out_order_no=self.out_order_no,
            amount="1250.00",
            currency="NGN",
        )
        self.assertRecordValues(
            payment_method,
            [
                {
                    "opay_latest_payment_reference": self.payment_uuid,
                    "opay_latest_out_order_no": self.out_order_no,
                    "opay_latest_order_no": "OPAY-ORDER-1",
                    "opay_latest_amount": "1250.00",
                    "opay_latest_status": "waiting",
                }
            ],
        )
        attempt = self.env["pos.opay.payment.attempt"].search(
            [("out_order_no", "=", self.out_order_no)]
        )
        self.assertRecordValues(
            attempt,
            [
                {
                    "payment_method_id": payment_method.id,
                    "pos_session_id": session.id,
                    "pos_config_id": session.config_id.id,
                    "payment_reference": self.payment_uuid,
                    "amount": "1250.00",
                    "status": "waiting",
                }
            ],
        )
        self.assertFalse(
            set(result)
            & {
                "opay_client_auth_key",
                "opay_merchant_private_key",
                "opay_public_key",
            }
        )

    def test_create_payment_logs_safe_identifiers_and_duration(self):
        payment_method, session = self._runtime_records()
        client = Mock(spec=OPayClient)
        client.create_payment.return_value = OPayCreatePaymentResult(
            out_order_no=self.out_order_no,
            order_no="OPAY-OBSERVABILITY-1",
        )

        with self.assertLogs(
            "odoo.addons.pos_opay.models.pos_payment_method", level="INFO"
        ) as logs, patch.object(
            OPayClient, "from_payment_method", return_value=client
        ), patch.object(
            type(payment_method), "_opay_duration_ms", return_value=125
        ):
            payment_method.opay_create_payment_request(
                self._runtime_request(session)
            )

        log_message = logs.output[-1]
        self.assertIn("event=opay_create_payment_api", log_message)
        self.assertIn("outcome=accepted", log_message)
        self.assertIn("source=create", log_message)
        self.assertIn(f"payment_method_id={payment_method.id}", log_message)
        attempt = self.env["pos.opay.payment.attempt"].search(
            [("out_order_no", "=", self.out_order_no)]
        )
        self.assertIn(f"attempt_id={attempt.id}", log_message)
        self.assertIn(f"reference={self.payment_uuid}", log_message)
        self.assertIn(f"out_order_no={self.out_order_no}", log_message)
        self.assertIn("order_no=OPAY-OBSERVABILITY-1", log_message)
        self.assertIn(
            f"terminal_sn={payment_method.opay_terminal_sn}", log_message
        )
        self.assertIn("status=waiting", log_message)
        self.assertIn("duration_ms=125", log_message)
        for secret in (
            payment_method.opay_client_auth_key,
            payment_method.opay_public_key,
            payment_method.opay_merchant_private_key,
        ):
            self.assertNotIn(secret, log_message)

    def test_duplicate_request_reuses_accepted_attempt(self):
        payment_method, session = self._runtime_records()
        client = Mock(spec=OPayClient)
        client.create_payment.return_value = OPayCreatePaymentResult(
            out_order_no=self.out_order_no,
            order_no="OPAY-ORDER-1",
        )
        request = self._runtime_request(session)

        with patch.object(OPayClient, "from_payment_method", return_value=client):
            first_result = payment_method.opay_create_payment_request(request)
            second_result = payment_method.opay_create_payment_request(request)

        self.assertFalse(first_result["reused"])
        self.assertTrue(second_result["reused"])
        self.assertEqual(second_result["out_order_no"], self.out_order_no)
        self.assertEqual(second_result["order_no"], "OPAY-ORDER-1")
        self.assertFalse(second_result["payment_completed"])
        client.create_payment.assert_called_once()

    def test_uncertain_request_is_not_created_twice(self):
        payment_method, session = self._runtime_records()
        client = Mock(spec=OPayClient)
        client.create_payment.side_effect = OPayConnectionError(
            "The OPay request timed out."
        )
        client.query_payment.side_effect = OPayConnectionError(
            "The OPay query timed out."
        )
        request = self._runtime_request(session)

        with patch.object(
            OPayClient, "from_payment_method", return_value=client
        ) as client_factory:
            first_result = payment_method.opay_create_payment_request(request)
            second_result = payment_method.opay_create_payment_request(request)

        self.assertEqual(first_result["status"], "uncertain")
        self.assertTrue(first_result["ambiguous"])
        self.assertEqual(first_result["out_order_no"], self.out_order_no)
        self.assertTrue(second_result["reused"])
        self.assertEqual(second_result["out_order_no"], self.out_order_no)
        self.assertEqual(client_factory.call_count, 1)
        client.create_payment.assert_called_once()
        client.query_payment.assert_not_called()
        self.assertEqual(payment_method.opay_latest_status, "uncertain")

    def test_create_http_failure_is_uncertain_and_never_retried(self):
        payment_method, session = self._runtime_records()
        client = Mock(spec=OPayClient)
        client.create_payment.side_effect = OPayHTTPError(500)
        request = self._runtime_request(session)

        with self.assertLogs(
            "odoo.addons.pos_opay.models.pos_payment_method", level="WARNING"
        ) as logs, patch.object(
            OPayClient, "from_payment_method", return_value=client
        ):
            first_result = payment_method.opay_create_payment_request(request)
            second_result = payment_method.opay_create_payment_request(request)

        self.assertEqual(first_result["status"], "uncertain")
        self.assertTrue(first_result["ambiguous"])
        self.assertFalse(first_result["terminal_released"])
        self.assertFalse(first_result["payment_completed"])
        self.assertTrue(second_result["reused"])
        client.create_payment.assert_called_once()
        client.query_payment.assert_not_called()
        self.assertIn("error_category=http_error", logs.output[0])

    def test_authenticated_rejection_is_failed_and_not_recreated(self):
        payment_method, session = self._runtime_records()
        client = Mock(spec=OPayClient)
        client.create_payment.side_effect = OPayAPIError(
            "00004", "Invalid request parameters"
        )
        request = self._runtime_request(session)

        with patch.object(OPayClient, "from_payment_method", return_value=client):
            first_result = payment_method.opay_create_payment_request(request)
            second_result = payment_method.opay_create_payment_request(request)

        self.assertEqual(first_result["status"], "failed")
        self.assertFalse(first_result["ambiguous"])
        self.assertTrue(second_result["reused"])
        client.create_payment.assert_called_once()
        self.assertIn("Invalid request parameters", first_result["message"])

    def test_duplicate_business_order_response_is_uncertain(self):
        payment_method, session = self._runtime_records()
        client = Mock(spec=OPayClient)
        client.create_payment.side_effect = OPayAPIError(
            "00005", "Duplicate business order number"
        )
        client.query_payment.side_effect = OPayConnectionError(
            "The OPay query timed out."
        )
        request = self._runtime_request(session)

        with patch.object(OPayClient, "from_payment_method", return_value=client):
            first_result = payment_method.opay_create_payment_request(request)
            second_result = payment_method.opay_create_payment_request(request)

        self.assertEqual(first_result["status"], "uncertain")
        self.assertTrue(first_result["ambiguous"])
        self.assertEqual(first_result["out_order_no"], self.out_order_no)
        self.assertTrue(second_result["reused"])
        client.create_payment.assert_called_once()
        client.query_payment.assert_not_called()

    def test_invalid_server_configuration_returns_safe_failure(self):
        payment_method, session = self._runtime_records()

        with patch.object(
            OPayClient,
            "from_payment_method",
            side_effect=OPayConfigurationError("secret key details"),
        ):
            result = payment_method.opay_create_payment_request(
                self._runtime_request(session)
            )

        self.assertEqual(result["status"], "failed")
        self.assertNotIn("secret", result["message"])

    def test_create_payment_request_rejects_non_opay_method(self):
        payment_method = self.payment_method_model.create(
            {"name": "Manual Payment", "payment_method_type": "none"}
        )
        config = self.env["pos.config"].create(
            {
                "name": "Manual POS",
                "payment_method_ids": [(6, 0, [payment_method.id])],
            }
        )
        session = self.env["pos.session"].create(
            {"name": "Manual Session", "config_id": config.id, "state": "opened"}
        )

        with self.assertRaisesRegex(UserError, "require an OPay payment method"):
            payment_method.opay_create_payment_request(
                self._runtime_request(session)
            )

    def test_create_payment_request_rejects_invalid_payloads(self):
        payment_method, session = self._runtime_records()
        invalid_requests = (
            False,
            {},
            self._runtime_request(session, reference=""),
            self._runtime_request(session, reference="not-a-uuid"),
            self._runtime_request(session, amount=0),
            self._runtime_request(session, session_id=False),
            {
                "reference": self.payment_uuid,
                "amount": 1250.0,
                "session_id": session.id,
                "opay_client_auth_key": "MUST-NOT-CROSS-THE-RPC-BOUNDARY",
            },
        )

        for request in invalid_requests:
            with self.subTest(request=request), self.assertRaises(UserError):
                payment_method.opay_create_payment_request(request)

    def test_missing_local_attempt_and_query_rejection_remain_uncertain(self):
        payment_method, session = self._runtime_records()
        client = Mock(spec=OPayClient)
        client.query_payment.side_effect = OPayAPIError(
            "00004", "Order not found"
        )

        with patch.object(OPayClient, "from_payment_method", return_value=client):
            result = payment_method.opay_resolve_create_outcome(
                {
                    "reference": self.payment_uuid,
                    "session_id": session.id,
                }
            )

        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["recovery_state"], "opay_query_uncertain")
        self.assertTrue(result["local_attempt_missing"])
        self.assertFalse(result["safe_to_retry"])
        self.assertFalse(result["terminal_released"])
        self.assertFalse(result["payment_completed"])
        self.assertEqual(result["reference"], self.payment_uuid)
        self.assertEqual(result["payment_method_id"], payment_method.id)
        self.assertEqual(result["pos_session_id"], session.id)
        self.assertEqual(result["out_order_no"], self.out_order_no)
        self.assertFalse(result["order_no"])
        client.query_payment.assert_called_once_with(out_order_no=self.out_order_no)
        client.create_payment.assert_not_called()

    def test_missing_local_attempt_exact_remote_absence_allows_retry(self):
        payment_method, session = self._runtime_records()
        client = Mock(spec=OPayClient)
        client.query_payment.side_effect = OPayOrderNotFoundError(
            "50002", "order not exist"
        )

        with patch.object(OPayClient, "from_payment_method", return_value=client):
            result = payment_method.opay_resolve_create_outcome(
                {
                    "reference": self.payment_uuid,
                    "session_id": session.id,
                }
            )

        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["recovery_state"], "opay_order_confirmed_absent")
        self.assertTrue(result["local_attempt_missing"])
        self.assertTrue(result["safe_to_retry"])
        self.assertTrue(result["terminal_released"])
        self.assertFalse(result["payment_completed"])
        self.assertFalse(result["ambiguous"])
        self.assertIn("order not exist", result["message"])
        client.query_payment.assert_called_once_with(out_order_no=self.out_order_no)
        client.create_payment.assert_not_called()

    def test_remote_order_survives_missing_local_attempt_without_allowing_retry(self):
        payment_method, session = self._runtime_records()
        client = Mock(spec=OPayClient)
        client.head_merchant_id = payment_method.opay_head_merchant_id
        client.merchant_id = payment_method.opay_merchant_id
        client.terminal_sn = payment_method.opay_terminal_sn
        client.query_payment.return_value = {
            "outOrderNo": self.out_order_no,
            "orderNo": "OPAY-ORDER-ROLLED-BACK-LOCALLY",
            "status": "SUCCESS",
            "amount": "1250.00",
            "currency": "NGN",
            "headMerchantId": payment_method.opay_head_merchant_id,
            "merchantId": payment_method.opay_merchant_id,
            "sn": payment_method.opay_terminal_sn,
        }

        with patch.object(OPayClient, "from_payment_method", return_value=client):
            result = payment_method.opay_resolve_create_outcome(
                {
                    "reference": self.payment_uuid,
                    "session_id": session.id,
                }
            )

        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["opay_status"], "SUCCESS")
        self.assertEqual(result["recovery_state"], "opay_order_confirmed")
        self.assertTrue(result["local_attempt_missing"])
        self.assertFalse(result["safe_to_retry"])
        self.assertFalse(result["payment_completed"])
        self.assertEqual(result["out_order_no"], self.out_order_no)
        self.assertEqual(result["order_no"], "OPAY-ORDER-ROLLED-BACK-LOCALLY")
        client.query_payment.assert_called_once_with(out_order_no=self.out_order_no)
        client.create_payment.assert_not_called()

    def test_missing_local_attempt_allows_retry_after_remote_final_negative(self):
        payment_method, session = self._runtime_records()
        client = Mock(spec=OPayClient)
        client.head_merchant_id = payment_method.opay_head_merchant_id
        client.merchant_id = payment_method.opay_merchant_id
        client.terminal_sn = payment_method.opay_terminal_sn
        client.query_payment.return_value = {
            "outOrderNo": self.out_order_no,
            "orderNo": "OPAY-ORDER-CLOSED",
            "status": "CLOSE",
            "amount": "1250.00",
            "currency": "NGN",
            "headMerchantId": payment_method.opay_head_merchant_id,
            "merchantId": payment_method.opay_merchant_id,
            "sn": payment_method.opay_terminal_sn,
        }

        with patch.object(OPayClient, "from_payment_method", return_value=client):
            result = payment_method.opay_resolve_create_outcome(
                {
                    "reference": self.payment_uuid,
                    "session_id": session.id,
                }
            )

        self.assertEqual(result["status"], "CLOSE")
        self.assertEqual(result["recovery_state"], "opay_order_confirmed")
        self.assertTrue(result["safe_to_retry"])
        self.assertTrue(result["terminal_released"])
        self.assertFalse(result["payment_completed"])
        client.create_payment.assert_not_called()

    def test_resolve_create_outcome_queries_existing_attempt_without_create(self):
        payment_method, session = self._runtime_records()
        attempt_model = self.env["pos.opay.payment.attempt"]
        attempt = attempt_model.create(
            {
                **attempt_model._new_attempt_values(
                    payment_method,
                    self.payment_uuid,
                    self.out_order_no,
                    "1250.00",
                    session=session,
                ),
                "order_no": "OPAY-ORDER-1",
                "status": "uncertain",
            }
        )
        client = Mock(spec=OPayClient)
        client.query_payment.return_value = {
            "outOrderNo": attempt.out_order_no,
            "orderNo": attempt.order_no,
            "status": "PENDING",
            "amount": attempt.amount,
            "currency": attempt.currency,
            "headMerchantId": attempt.head_merchant_id,
            "merchantId": attempt.merchant_id,
            "sn": attempt.terminal_sn,
        }

        with self.assertLogs(
            "odoo.addons.pos_opay.models.opay_payment_attempt", level="INFO"
        ) as logs, patch.object(
            OPayClient, "from_payment_method", return_value=client
        ):
            result = payment_method.opay_resolve_create_outcome(
                {
                    "reference": self.payment_uuid,
                    "session_id": session.id,
                }
            )

        self.assertEqual(result["status"], "PENDING")
        self.assertFalse(result["safe_to_retry"])
        self.assertEqual(result["out_order_no"], self.out_order_no)
        self.assertEqual(result["order_no"], "OPAY-ORDER-1")
        client.query_payment.assert_called_once_with(
            out_order_no=self.out_order_no,
            order_no="OPAY-ORDER-1",
        )
        client.create_payment.assert_not_called()
        self.assertTrue(
            any(
                "source=query reason=manual_create_recovery" in message
                for message in logs.output
            )
        )

    def test_resolve_create_outcome_allows_retry_after_final_negative(self):
        payment_method, session = self._runtime_records()
        attempt_model = self.env["pos.opay.payment.attempt"]
        attempt = attempt_model.create(
            {
                **attempt_model._new_attempt_values(
                    payment_method,
                    self.payment_uuid,
                    self.out_order_no,
                    "1250.00",
                    session=session,
                ),
                "order_no": "OPAY-ORDER-1",
                "status": "CLOSE",
            }
        )

        with patch.object(OPayClient, "from_payment_method") as client_factory:
            result = payment_method.opay_resolve_create_outcome(
                {
                    "reference": self.payment_uuid,
                    "session_id": session.id,
                }
            )

        self.assertEqual(result["status"], "CLOSE")
        self.assertTrue(result["safe_to_retry"])
        self.assertFalse(result["payment_completed"])
        client_factory.assert_not_called()

    def test_resolve_create_outcome_rejects_authoritative_browser_data(self):
        payment_method, session = self._runtime_records()

        with self.assertRaisesRegex(UserError, "Invalid OPay status request"):
            payment_method.opay_resolve_create_outcome(
                {
                    "reference": self.payment_uuid,
                    "session_id": session.id,
                    "out_order_no": self.out_order_no,
                    "opay_client_auth_key": "MUST-NOT-CROSS-THE-RPC-BOUNDARY",
                }
            )
