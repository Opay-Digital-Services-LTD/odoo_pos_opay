import base64
import binascii
import hmac
import json
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class OPayError(Exception):
    """Base exception for the OPay server-side protocol layer."""

    category = "opay_error"

    def __init__(self, message="", *, reason_code=None):
        self.reason_code = reason_code
        super().__init__(message)


class OPayConfigurationError(OPayError):
    """Raised when OPay cryptographic configuration is invalid."""

    category = "configuration_error"


class OPayAuthenticationError(OPayError):
    """Raised when authenticated OPay content cannot be trusted."""

    category = "authentication_error"


class OPayMalformedResponseError(OPayError):
    """Raised when an OPay response does not match the documented envelope."""

    category = "malformed_response"


class OPayAuth:
    """Implement OPay's documented RSA request/response authentication."""

    RESPONSE_FIELDS = ("code", "message", "data", "timestamp")
    WEBHOOK_FIELDS = (
        "clientAuthKey",
        "version",
        "bodyFormat",
        "timestamp",
        "paramContent",
        "sign",
    )
    WEBHOOK_MAX_AGE_MS = 5 * 60 * 1000

    def __init__(self, opay_public_key, merchant_private_key, timestamp_factory=None):
        self._opay_public_key = self._load_public_key(opay_public_key)
        self._merchant_private_key = self._load_private_key(merchant_private_key)
        self._timestamp_factory = timestamp_factory or self._timestamp_ms

    @staticmethod
    def _timestamp_ms():
        return str(time.time_ns() // 1_000_000)

    @staticmethod
    def canonical_json(payload):
        try:
            return json.dumps(payload, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise OPayConfigurationError(
                "OPay request content is not valid JSON data."
            ) from error

    @staticmethod
    def response_signature_content(response):
        if not isinstance(response, dict):
            raise OPayMalformedResponseError("OPay returned an invalid response envelope.")

        content = []
        for field_name in sorted(set(response) - {"sign"}):
            value = response[field_name]
            if value is not None:
                content.append(f"{field_name}={value}")
        return "&".join(content)

    def build_request(self, payload, timestamp=None):
        timestamp = str(
            self._timestamp_factory() if timestamp is None else timestamp
        )
        if not timestamp.isdigit() or len(timestamp) > 15:
            raise OPayConfigurationError("OPay requires a millisecond timestamp.")

        encrypted_content = self.encrypt_request(payload)
        signature = self.sign(f"{encrypted_content}{timestamp}")
        return timestamp, {
            "paramContent": encrypted_content,
            "sign": signature,
        }

    def encrypt_request(self, payload):
        plaintext = self.canonical_json(payload).encode("utf-8")
        key_bytes = self._opay_public_key.key_size // 8
        max_chunk_size = key_bytes - 11
        ciphertext = bytearray()
        for offset in range(0, len(plaintext), max_chunk_size):
            ciphertext.extend(
                self._opay_public_key.encrypt(
                    plaintext[offset : offset + max_chunk_size],
                    padding.PKCS1v15(),
                )
            )
        return base64.b64encode(ciphertext).decode("ascii")

    def sign(self, content):
        signature = self._merchant_private_key.sign(
            content.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def verify_response(self, response):
        if not isinstance(response, dict) or not isinstance(response.get("sign"), str):
            raise OPayMalformedResponseError("OPay returned an invalid response signature.")

        signature_content = self.response_signature_content(response)
        signature = self._decode_base64(
            response["sign"], "OPay returned an invalid response signature."
        )
        try:
            self._verify_signature(signature, signature_content)
        except InvalidSignature as error:
            raise OPayAuthenticationError(
                "OPay response signature verification failed."
            ) from error
        except ValueError as error:
            raise OPayMalformedResponseError(
                "OPay returned an invalid response signature."
            ) from error

    def process_webhook(self, envelope, expected_client_auth_key, now_ms=None):
        """Verify and decrypt OPay's documented POS webhook envelope."""
        self._validate_webhook_envelope(envelope)
        if not isinstance(expected_client_auth_key, str) or not hmac.compare_digest(
            envelope["clientAuthKey"], expected_client_auth_key
        ):
            raise OPayAuthenticationError(
                "OPay webhook authentication failed.",
                reason_code="client_auth_key_mismatch",
            )

        self._validate_webhook_timestamp(envelope["timestamp"], now_ms=now_ms)
        try:
            signature = self._decode_base64(
                envelope["sign"], "OPay returned an invalid webhook signature."
            )
        except OPayMalformedResponseError as error:
            raise OPayMalformedResponseError(
                "OPay returned an invalid webhook signature.",
                reason_code="malformed_signature",
            ) from error
        try:
            self._verify_signature(
                signature,
                f'{envelope["paramContent"]}{envelope["timestamp"]}',
            )
        except InvalidSignature as error:
            raise OPayAuthenticationError(
                "OPay webhook signature verification failed.",
                reason_code="signature_verification_failure",
            ) from error
        except ValueError as error:
            raise OPayMalformedResponseError(
                "OPay returned an invalid webhook signature.",
                reason_code="malformed_signature",
            ) from error

        try:
            content = self.decrypt_response(envelope["paramContent"])
        except OPayMalformedResponseError as error:
            raise OPayMalformedResponseError(
                "OPay returned malformed encrypted webhook content.",
                reason_code="malformed_param_content",
            ) from error
        except OPayAuthenticationError as error:
            raise OPayAuthenticationError(
                "OPay webhook decryption failed.",
                reason_code="decryption_failure",
            ) from error
        if not isinstance(content, dict):
            raise OPayMalformedResponseError(
                "OPay returned invalid webhook content.",
                reason_code="malformed_decrypted_content",
            )
        return content

    def _verify_signature(self, signature, content):
        self._opay_public_key.verify(
            signature,
            content.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

    def decrypt_response(self, encrypted_data):
        encrypted_bytes = self._decode_base64(
            encrypted_data, "OPay returned invalid encrypted response data."
        )
        block_size = self._merchant_private_key.key_size // 8
        if not encrypted_bytes or len(encrypted_bytes) % block_size:
            raise OPayMalformedResponseError(
                "OPay returned invalid encrypted response data."
            )

        plaintext = bytearray()
        try:
            for offset in range(0, len(encrypted_bytes), block_size):
                plaintext.extend(
                    self._merchant_private_key.decrypt(
                        encrypted_bytes[offset : offset + block_size],
                        padding.PKCS1v15(),
                    )
                )
            return json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise OPayAuthenticationError(
                "OPay response decryption failed."
            ) from error

    def process_response(self, response):
        self._validate_response_envelope(response)
        self.verify_response(response)
        return {
            "code": response["code"],
            "message": response["message"],
            "timestamp": response["timestamp"],
            "data": self.decrypt_response(response["data"]),
        }

    @staticmethod
    def _validate_response_envelope(response):
        if not isinstance(response, dict):
            raise OPayMalformedResponseError("OPay returned an invalid response envelope.")
        required_fields = set(OPayAuth.RESPONSE_FIELDS) | {"sign"}
        if required_fields - set(response):
            raise OPayMalformedResponseError(
                "OPay returned an incomplete response envelope."
            )
        if any(
            not isinstance(response[field_name], str)
            for field_name in required_fields
        ):
            raise OPayMalformedResponseError(
                "OPay returned an invalid response envelope."
            )
        if any(not response[field_name] for field_name in ("code", "data", "sign", "timestamp")):
            raise OPayMalformedResponseError(
                "OPay returned an invalid response envelope."
            )
        timestamp = response["timestamp"]
        if not timestamp.isdigit() or len(timestamp) > 15:
            raise OPayMalformedResponseError(
                "OPay returned an invalid response timestamp."
            )

    @staticmethod
    def _validate_webhook_envelope(envelope):
        if not isinstance(envelope, dict):
            raise OPayMalformedResponseError(
                "OPay returned an invalid webhook envelope.",
                reason_code="malformed_envelope",
            )
        required_fields = set(OPayAuth.WEBHOOK_FIELDS)
        if required_fields - set(envelope):
            raise OPayMalformedResponseError(
                "OPay returned an incomplete webhook envelope.",
                reason_code="incomplete_envelope",
            )
        if any(
            not isinstance(envelope[field_name], str) or not envelope[field_name]
            for field_name in required_fields
        ):
            raise OPayMalformedResponseError(
                "OPay returned an invalid webhook envelope.",
                reason_code="malformed_envelope",
            )
        if envelope["version"] != "V1.0.1" or envelope["bodyFormat"] != "JSON":
            raise OPayMalformedResponseError(
                "OPay returned an unsupported webhook envelope.",
                reason_code="unsupported_envelope",
            )

    @classmethod
    def _validate_webhook_timestamp(cls, timestamp, now_ms=None):
        if not timestamp.isdigit() or len(timestamp) > 15:
            raise OPayMalformedResponseError(
                "OPay returned an invalid webhook timestamp.",
                reason_code="invalid_timestamp",
            )
        current_ms = cls._timestamp_ms() if now_ms is None else str(now_ms)
        if not current_ms.isdigit():
            raise OPayConfigurationError(
                "The current timestamp is invalid.",
                reason_code="invalid_server_timestamp",
            )
        if abs(int(current_ms) - int(timestamp)) > cls.WEBHOOK_MAX_AGE_MS:
            raise OPayAuthenticationError(
                "OPay webhook timestamp has expired.",
                reason_code="expired_timestamp",
            )

    @staticmethod
    def _load_public_key(value):
        key_bytes = OPayAuth._key_bytes(value, "OPay public key")
        try:
            if key_bytes.startswith(b"-----BEGIN"):
                key = serialization.load_pem_public_key(key_bytes)
            else:
                key = serialization.load_der_public_key(key_bytes)
        except (TypeError, ValueError) as error:
            raise OPayConfigurationError("The OPay public key is invalid.") from error
        if not isinstance(key, rsa.RSAPublicKey):
            raise OPayConfigurationError("The OPay public key must be an RSA key.")
        return key

    @staticmethod
    def _load_private_key(value):
        key_bytes = OPayAuth._key_bytes(value, "merchant private key")
        try:
            if key_bytes.startswith(b"-----BEGIN"):
                key = serialization.load_pem_private_key(key_bytes, password=None)
            else:
                key = serialization.load_der_private_key(key_bytes, password=None)
        except (TypeError, ValueError) as error:
            raise OPayConfigurationError(
                "The merchant private key is invalid."
            ) from error
        if not isinstance(key, rsa.RSAPrivateKey):
            raise OPayConfigurationError("The merchant private key must be an RSA key.")
        return key

    @staticmethod
    def _key_bytes(value, label):
        if not isinstance(value, (str, bytes)) or not value:
            raise OPayConfigurationError(f"The {label} is required.")
        try:
            raw_value = value.encode("ascii") if isinstance(value, str) else value
        except UnicodeEncodeError as error:
            raise OPayConfigurationError(f"The {label} is invalid.") from error
        raw_value = raw_value.strip()
        if raw_value.startswith(b"-----BEGIN"):
            return raw_value
        try:
            return OPayAuth._decode_base64(raw_value, f"The {label} is invalid.")
        except OPayMalformedResponseError as error:
            raise OPayConfigurationError(f"The {label} is invalid.") from error

    @staticmethod
    def _decode_base64(value, error_message):
        if not isinstance(value, (str, bytes)):
            raise OPayMalformedResponseError(error_message)
        try:
            encoded = value.encode("ascii") if isinstance(value, str) else value
            encoded = b"".join(encoded.split())
            return base64.b64decode(encoded, validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as error:
            raise OPayMalformedResponseError(error_message) from error
