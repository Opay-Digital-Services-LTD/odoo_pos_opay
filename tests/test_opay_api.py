import base64
import json
from decimal import Decimal
from unittest.mock import Mock

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from odoo.addons.pos_opay.services.opay_api import (
    OPayAPIError,
    OPayClient,
    OPayConnectTimeoutError,
    OPayConnectionError,
    OPayHTTPError,
    OPayOrderNotFoundError,
    OPayReadTimeoutError,
)
from odoo.addons.pos_opay.services.opay_auth import (
    OPayAuth,
    OPayAuthenticationError,
    OPayConfigurationError,
    OPayMalformedResponseError,
)
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestOPayClient(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.opay_private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=1024
        )
        cls.merchant_private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=1024
        )
        cls.opay_public_pem = cls.opay_private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        cls.merchant_private_pem = cls.merchant_private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    def setUp(self):
        super().setUp()
        self.http_session = Mock()
        self.client = OPayClient(
            head_merchant_id="FAKE-BUSINESS-ID",
            merchant_id="FAKE-BRANCH-ID",
            terminal_sn="FAKE-TERMINAL-SN",
            sub_scene_enum="FAKE-SUB-SCENE",
            client_auth_key="FAKE-CLIENT-AUTH-KEY-0000000000",
            opay_public_key=self.opay_public_pem,
            merchant_private_key=self.merchant_private_pem,
            http_session=self.http_session,
            connect_timeout=7,
            read_timeout=11,
        )

    def test_create_payment_constructs_authenticated_request(self):
        self._set_response({"orderNo": "OPAY-ORDER-1"})

        result = self.client.create_payment(
            out_order_no="ODOO-PAYMENT-1",
            amount=Decimal("1234.5"),
            currency="NGN",
        )

        request_call = self.http_session.post.call_args
        self.assertEqual(
            request_call.args[0],
            "https://payapi.opayweb.com/openApi/order/checkout/createOrder",
        )
        self.assertEqual(request_call.kwargs["timeout"], (7, 11))
        self.assertEqual(
            request_call.kwargs["headers"],
            {
                "Content-Type": "application/json",
                "clientAuthKey": "FAKE-CLIENT-AUTH-KEY-0000000000",
                "version": "V1.0.1",
                "bodyFormat": "JSON",
                "timestamp": request_call.kwargs["headers"]["timestamp"],
            },
        )
        request_payload = self._decrypt_request(request_call)
        self.assertEqual(
            request_payload,
            {
                "headMerchantId": "FAKE-BUSINESS-ID",
                "merchantId": "FAKE-BRANCH-ID",
                "outOrderNo": "ODOO-PAYMENT-1",
                "amount": "1234.50",
                "currency": "NGN",
                "orderExpireTime": 180,
                "sceneEnum": "CASH_API",
                "subSceneEnum": "FAKE-SUB-SCENE",
                "sn": "FAKE-TERMINAL-SN",
                "isSplit": "N",
            },
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.status, "created")
        self.assertFalse(result.payment_completed)
        self.assertEqual(result.order_no, "OPAY-ORDER-1")
        self.assertEqual(result.message, "SUCCESSFUL")

    def test_query_payment_constructs_documented_identifiers(self):
        expected_response = {
            "outOrderNo": "ODOO-PAYMENT-1",
            "orderNo": "OPAY-ORDER-1",
            "status": "PENDING",
            "amount": "1234.50",
            "currency": "NGN",
        }
        self._set_response(expected_response)

        result = self.client.query_payment(
            out_order_no="ODOO-PAYMENT-1", order_no="OPAY-ORDER-1"
        )

        request_call = self.http_session.post.call_args
        self.assertEqual(
            request_call.args[0],
            "https://payapi.opayweb.com/openApi/order/checkout/qryOrderDetail",
        )
        self.assertEqual(
            self._decrypt_request(request_call),
            {
                "headMerchantId": "FAKE-BUSINESS-ID",
                "merchantId": "FAKE-BRANCH-ID",
                "outOrderNo": "ODOO-PAYMENT-1",
                "orderNo": "OPAY-ORDER-1",
            },
        )
        self.assertEqual(
            result,
            {
                **expected_response,
                OPayClient.RESPONSE_MESSAGE_KEY: "SUCCESSFUL",
            },
        )

    def test_query_payment_requires_at_least_one_reference(self):
        with self.assertRaises(OPayConfigurationError):
            self.client.query_payment()

    def test_amount_normalization_and_validation(self):
        normalized_amounts = (
            (100, "100.00"),
            ("100.5", "100.50"),
            (Decimal("100.005"), "100.01"),
        )
        for amount, expected in normalized_amounts:
            with self.subTest(amount=amount):
                self.assertEqual(OPayClient.normalize_amount(amount), expected)

        for amount in (False, 0, -1, "NaN", "Infinity", "invalid", Decimal("0.004")):
            with self.subTest(amount=amount), self.assertRaises(
                OPayConfigurationError
            ):
                OPayClient.normalize_amount(amount)

    def test_create_payment_rejects_non_ngn_currency(self):
        with self.assertRaisesRegex(OPayConfigurationError, "require NGN"):
            self.client.create_payment(
                out_order_no="ODOO-PAYMENT-1",
                amount="100.00",
                currency="USD",
                order_expire_time=180,
            )

        self.http_session.post.assert_not_called()

    def test_authenticated_api_rejection_is_raised(self):
        self._set_response(
            {"reason": "invalid request"},
            code="00004",
            message="Invalid request parameters",
        )

        with self.assertRaisesRegex(OPayAPIError, "00004") as error:
            self.client.query_payment(out_order_no="ODOO-PAYMENT-1")

        self.assertEqual(error.exception.code, "00004")
        self.assertEqual(error.exception.category, "authenticated_rejection")

    def test_connect_timeout_is_classified_without_retry(self):
        self.http_session.post.side_effect = requests.ConnectTimeout(
            "secret connect details"
        )

        with self.assertRaises(OPayConnectTimeoutError) as error:
            self.client.query_payment(out_order_no="ODOO-PAYMENT-1")

        self.assertEqual(error.exception.category, "connect_timeout")
        self.assertNotIn("secret", str(error.exception))
        self.http_session.post.assert_called_once()

    def test_read_timeout_is_classified_without_retry(self):
        self.http_session.post.side_effect = requests.ReadTimeout(
            "secret read details"
        )

        with self.assertRaises(OPayReadTimeoutError) as error:
            self.client.query_payment(out_order_no="ODOO-PAYMENT-1")

        self.assertEqual(error.exception.category, "read_timeout")
        self.assertNotIn("secret", str(error.exception))
        self.http_session.post.assert_called_once()

    def test_generic_connection_failure_is_classified_without_retry(self):
        self.http_session.post.side_effect = requests.ConnectionError(
            "secret connection details"
        )

        with self.assertRaises(OPayConnectionError) as error:
            self.client.query_payment(out_order_no="ODOO-PAYMENT-1")

        self.assertEqual(error.exception.category, "connection_error")
        self.assertNotIn("secret", str(error.exception))
        self.http_session.post.assert_called_once()

    def test_malformed_response_and_invalid_signature_are_rejected(self):
        malformed_response = Mock(status_code=200)
        malformed_response.json.return_value = {"code": "00000"}
        self.http_session.post.return_value = malformed_response
        with self.assertRaises(OPayMalformedResponseError):
            self.client.query_payment(out_order_no="ODOO-PAYMENT-1")

        response = self._response_envelope({"status": "PENDING"})
        response["sign"] = base64.b64encode(b"invalid-signature").decode()
        invalid_signature_response = Mock(status_code=200)
        invalid_signature_response.json.return_value = response
        self.http_session.post.return_value = invalid_signature_response
        with self.assertRaises(OPayAuthenticationError):
            self.client.query_payment(out_order_no="ODOO-PAYMENT-1")

    def test_malformed_json_is_classified_before_authentication(self):
        response = Mock(status_code=200)
        response.json.side_effect = ValueError("secret response payload")
        self.http_session.post.return_value = response

        with self.assertRaises(OPayMalformedResponseError) as error:
            self.client.query_payment(out_order_no="ODOO-PAYMENT-1")

        self.assertEqual(error.exception.category, "malformed_response")
        self.assertEqual(error.exception.reason_code, "malformed_json_response")
        self.assertNotIn("secret", str(error.exception))

    def test_live_query_order_not_found_responses_are_recognized(self):
        for code in ("00003", "50002"):
            with self.subTest(code=code):
                response = Mock(status_code=200)
                response.json.return_value = {
                    "code": code,
                    "message": "order not exist",
                    "data": None,
                }
                self.http_session.post.reset_mock()
                self.http_session.post.return_value = response

                with self.assertRaises(OPayOrderNotFoundError) as error:
                    self.client.query_payment(out_order_no="ODOO-PAYMENT-1")

                self.assertEqual(error.exception.category, "order_not_found")
                self.assertEqual(
                    error.exception.reason_code, "confirmed_order_not_found"
                )
                self.assertEqual(error.exception.opay_code, code)
                self.assertEqual(error.exception.opay_message, "order not exist")
                self.http_session.post.assert_called_once()

    def test_order_not_found_exception_is_query_only_and_exact(self):
        near_misses = (
            {
                "code": "50002",
                "message": "order not exist",
                "data": None,
                "unexpected": "field",
            },
            {"code": "50002", "message": "different error", "data": None},
            {"code": "50003", "message": "order not exist", "data": None},
            {"code": "50002", "message": "order not exist", "data": {}},
        )
        for response_body in near_misses:
            with self.subTest(response=response_body):
                response = Mock(status_code=200)
                response.json.return_value = response_body
                self.http_session.post.reset_mock()
                self.http_session.post.return_value = response
                with self.assertRaises(OPayMalformedResponseError):
                    self.client.query_payment(out_order_no="ODOO-PAYMENT-1")

        response = Mock(status_code=200)
        response.json.return_value = {
            "code": "50002",
            "message": "order not exist",
            "data": None,
        }
        self.http_session.post.return_value = response
        with self.assertRaises(OPayMalformedResponseError):
            self.client.create_payment(
                out_order_no="ODOO-PAYMENT-1",
                amount="100.00",
                currency="NGN",
            )

    def test_authenticated_but_malformed_business_data_is_rejected(self):
        self._set_response(["not", "query", "data"])
        with self.assertRaises(OPayMalformedResponseError):
            self.client.query_payment(out_order_no="ODOO-PAYMENT-1")

        self._set_response({"unexpected": "create data"})
        with self.assertRaises(OPayMalformedResponseError):
            self.client.create_payment(
                out_order_no="ODOO-PAYMENT-1",
                amount="100.00",
                currency="NGN",
            )

    def test_http_400_and_500_are_http_errors_without_body_parsing(self):
        for status_code in (400, 500):
            response = Mock(status_code=status_code)
            self.http_session.post.reset_mock()
            self.http_session.post.return_value = response

            with self.subTest(status_code=status_code), self.assertRaises(
                OPayHTTPError
            ) as error:
                self.client.query_payment(out_order_no="ODOO-PAYMENT-1")

            self.assertEqual(error.exception.status_code, status_code)
            self.assertEqual(error.exception.category, "http_error")
            response.json.assert_not_called()
            self.http_session.post.assert_called_once()

    def test_separate_timeout_values_must_be_positive(self):
        common = {
            "head_merchant_id": "FAKE-BUSINESS-ID",
            "merchant_id": "FAKE-BRANCH-ID",
            "terminal_sn": "FAKE-TERMINAL-SN",
            "sub_scene_enum": "FAKE-SUB-SCENE",
            "client_auth_key": "FAKE-CLIENT-AUTH-KEY-0000000000",
            "opay_public_key": self.opay_public_pem,
            "merchant_private_key": self.merchant_private_pem,
        }
        for timeout_values in (
            {"connect_timeout": 0},
            {"read_timeout": 0},
            {"connect_timeout": True},
            {"read_timeout": "10"},
        ):
            with self.subTest(timeout_values=timeout_values), self.assertRaises(
                OPayConfigurationError
            ):
                OPayClient(**common, **timeout_values)

    def test_legacy_scalar_timeout_sets_connect_and_read_timeouts(self):
        common = {
            "head_merchant_id": "FAKE-BUSINESS-ID",
            "merchant_id": "FAKE-BRANCH-ID",
            "terminal_sn": "FAKE-TERMINAL-SN",
            "sub_scene_enum": "FAKE-SUB-SCENE",
            "client_auth_key": "FAKE-CLIENT-AUTH-KEY-0000000000",
            "opay_public_key": self.opay_public_pem,
            "merchant_private_key": self.merchant_private_pem,
        }

        client = OPayClient(**common, timeout=13)

        self.assertEqual(client.connect_timeout, 13)
        self.assertEqual(client.read_timeout, 13)
        with self.assertRaises(OPayConfigurationError):
            OPayClient(**common, timeout=13, connect_timeout=7)

    def test_payment_method_configuration_builds_server_side_client(self):
        payment_method = self.env["pos.payment.method"].create(
            {
                "name": "OPay Phase 4 Client",
                "payment_method_type": "terminal",
                "use_payment_terminal": "opay",
                "opay_head_merchant_id": "FAKE-BUSINESS-ID",
                "opay_merchant_id": "FAKE-BRANCH-ID",
                "opay_terminal_sn": "FAKE-PHASE4-TERMINAL",
                "opay_client_auth_key": "FAKE-CLIENT-AUTH-KEY-0000000000",
                "opay_public_key": self.opay_public_pem,
                "opay_merchant_private_key": self.merchant_private_pem,
                "opay_sub_scene_enum": "FAKE-SUB-SCENE",
            }
        )

        client = OPayClient.from_payment_method(
            payment_method, http_session=self.http_session
        )

        self.assertEqual(client.head_merchant_id, "FAKE-BUSINESS-ID")
        self.assertEqual(client.merchant_id, "FAKE-BRANCH-ID")
        self.assertEqual(client.terminal_sn, "FAKE-PHASE4-TERMINAL")
        self.assertEqual(client.connect_timeout, 10)
        self.assertEqual(client.read_timeout, 10)

    def _set_response(self, data, code="00000", message="SUCCESSFUL"):
        response = Mock(status_code=200)
        response.json.return_value = self._response_envelope(data, code, message)
        self.http_session.post.return_value = response

    def _response_envelope(self, data, code="00000", message="SUCCESSFUL"):
        timestamp = "1700000000000"
        encrypted_data = self._encrypt_chunks(
            OPayAuth.canonical_json(data).encode(),
            self.merchant_private_key.public_key(),
        )
        response = {
            "code": code,
            "message": message,
            "data": encrypted_data,
            "timestamp": timestamp,
        }
        signature_content = OPayAuth.response_signature_content(response)
        signature = self.opay_private_key.sign(
            signature_content.encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        response["sign"] = base64.b64encode(signature).decode()
        return response

    def _decrypt_request(self, request_call):
        request_body = request_call.kwargs["json"]
        timestamp = request_call.kwargs["headers"]["timestamp"]
        self.merchant_private_key.public_key().verify(
            base64.b64decode(request_body["sign"]),
            f'{request_body["paramContent"]}{timestamp}'.encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        plaintext = self._decrypt_chunks(
            request_body["paramContent"], self.opay_private_key
        )
        return json.loads(plaintext)

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

    @staticmethod
    def _decrypt_chunks(ciphertext, private_key):
        encrypted = base64.b64decode(ciphertext)
        block_size = private_key.key_size // 8
        plaintext = bytearray()
        for offset in range(0, len(encrypted), block_size):
            plaintext.extend(
                private_key.decrypt(
                    encrypted[offset : offset + block_size],
                    padding.PKCS1v15(),
                )
            )
        return bytes(plaintext)
