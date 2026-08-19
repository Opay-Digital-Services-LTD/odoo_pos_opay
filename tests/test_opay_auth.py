import base64
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from odoo.addons.pos_opay.services.opay_auth import (
    OPayAuth,
    OPayAuthenticationError,
    OPayConfigurationError,
    OPayMalformedResponseError,
)
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestOPayAuth(TransactionCase):
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
        cls.auth = OPayAuth(
            cls.opay_public_pem,
            cls.merchant_private_pem,
            timestamp_factory=lambda: "1700000000000",
        )

    def test_request_encryption_signing_and_timestamp_binding(self):
        payload = {
            "merchantId": "BRANCH-1",
            "headMerchantId": "BUSINESS-1",
            "nested": {"z": 2, "a": 1},
        }

        timestamp, request = self.auth.build_request(payload)
        plaintext = self._decrypt_chunks(
            request["paramContent"], self.opay_private_key
        )

        self.assertEqual(timestamp, "1700000000000")
        self.assertEqual(
            plaintext.decode(),
            '{"headMerchantId":"BUSINESS-1","merchantId":"BRANCH-1",'
            '"nested":{"a":1,"z":2}}',
        )
        self.merchant_private_key.public_key().verify(
            base64.b64decode(request["sign"]),
            f'{request["paramContent"]}{timestamp}'.encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        with self.assertRaises(InvalidSignature):
            self.merchant_private_key.public_key().verify(
                base64.b64decode(request["sign"]),
                f'{request["paramContent"]}{int(timestamp) + 1}'.encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )

    def test_response_signature_verification_and_decryption(self):
        response = self._response_envelope(
            {"orderNo": "OPAY-ORDER-1", "status": "PENDING"}
        )

        self.assertEqual(
            OPayAuth.response_signature_content(
                {
                    "timestamp": "1700000000000",
                    "message": "SUCCESSFUL",
                    "data": "encrypted-data",
                    "code": "00000",
                    "sign": "excluded-signature",
                }
            ),
            "code=00000&data=encrypted-data&message=SUCCESSFUL&timestamp=1700000000000",
        )

        processed = self.auth.process_response(response)

        self.assertEqual(processed["code"], "00000")
        self.assertEqual(
            processed["data"],
            {"orderNo": "OPAY-ORDER-1", "status": "PENDING"},
        )

    def test_response_signature_tampering_fails_closed(self):
        response = self._response_envelope({"orderNo": "OPAY-ORDER-1"})
        response["timestamp"] = "1700000000001"

        with self.assertRaises(OPayAuthenticationError):
            self.auth.process_response(response)

    def test_malformed_keys_and_base64_are_rejected(self):
        invalid_configurations = (
            ("not-a-public-key", self.merchant_private_pem),
            (self.opay_public_pem, "not-a-private-key"),
        )
        for public_key, private_key in invalid_configurations:
            with self.subTest(public_key=public_key[:12]), self.assertRaises(
                OPayConfigurationError
            ):
                OPayAuth(public_key, private_key)

        response = self._response_envelope({"orderNo": "OPAY-ORDER-1"})
        response["data"] = "not-valid-base64!"
        response["sign"] = self._sign_response(response)
        with self.assertRaises(OPayMalformedResponseError):
            self.auth.process_response(response)

    def test_base64_der_keys_from_official_python_format_are_supported(self):
        opay_public_der = base64.b64encode(
            self.opay_private_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode()
        merchant_private_der = base64.b64encode(
            self.merchant_private_key.private_bytes(
                serialization.Encoding.DER,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        ).decode()
        auth = OPayAuth(opay_public_der, merchant_private_der)

        timestamp, request = auth.build_request(
            {"test": "official-key-format"}, timestamp="1700000000000"
        )

        self.assertEqual(timestamp, "1700000000000")
        self.assertEqual(
            json.loads(
                self._decrypt_chunks(
                    request["paramContent"], self.opay_private_key
                )
            ),
            {"test": "official-key-format"},
        )

    def test_malformed_response_envelope_is_rejected(self):
        invalid_timestamp_response = self._response_envelope(
            {"orderNo": "OPAY-ORDER-1"}, timestamp="not-a-timestamp"
        )
        for response in (None, {}, {"code": "00000"}, invalid_timestamp_response):
            with self.subTest(response=response), self.assertRaises(
                OPayMalformedResponseError
            ):
                self.auth.process_response(response)

    def test_webhook_signature_timestamp_and_decryption(self):
        content = {
            "data": {
                "outOrderNo": "ODOO-1",
                "orderNo": "OPAY-1",
                "status": "SUCCESS",
            }
        }
        envelope = self._webhook_envelope(content)

        self.assertEqual(
            self.auth.process_webhook(
                envelope,
                "FAKE-CLIENT-AUTH-KEY",
                now_ms="1700000000000",
            ),
            content,
        )

        envelope["paramContent"] += "tampered"
        with self.assertRaises(OPayAuthenticationError) as caught:
            self.auth.process_webhook(
                envelope,
                "FAKE-CLIENT-AUTH-KEY",
                now_ms="1700000000000",
            )
        self.assertEqual(caught.exception.reason_code, "signature_verification_failure")

    def test_webhook_expired_timestamp_and_wrong_auth_key_fail_closed(self):
        envelope = self._webhook_envelope({"data": {"status": "PENDING"}})

        with self.assertRaisesRegex(OPayAuthenticationError, "expired") as caught:
            self.auth.process_webhook(
                envelope,
                "FAKE-CLIENT-AUTH-KEY",
                now_ms="1700000300001",
            )
        self.assertEqual(caught.exception.reason_code, "expired_timestamp")
        with self.assertRaises(OPayAuthenticationError) as caught:
            self.auth.process_webhook(
                envelope,
                "WRONG-AUTH-KEY",
                now_ms="1700000000000",
            )
        self.assertEqual(caught.exception.reason_code, "client_auth_key_mismatch")

    def _webhook_envelope(self, content, timestamp="1700000000000"):
        encrypted_content = self._encrypt_chunks(
            OPayAuth.canonical_json(content).encode(),
            self.merchant_private_key.public_key(),
        )
        signature = self.opay_private_key.sign(
            f"{encrypted_content}{timestamp}".encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return {
            "clientAuthKey": "FAKE-CLIENT-AUTH-KEY",
            "version": "V1.0.1",
            "bodyFormat": "JSON",
            "timestamp": timestamp,
            "paramContent": encrypted_content,
            "sign": base64.b64encode(signature).decode(),
        }

    def _response_envelope(
        self, data, code="00000", message="SUCCESSFUL", timestamp="1700000000000"
    ):
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
        response["sign"] = self._sign_response(response)
        return response

    def _sign_response(self, response):
        content = OPayAuth.response_signature_content(response)
        signature = self.opay_private_key.sign(
            content.encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        return base64.b64encode(signature).decode()

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
