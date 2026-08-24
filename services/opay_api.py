from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests

from .opay_auth import (
    OPayAuth,
    OPayConfigurationError,
    OPayError,
    OPayMalformedResponseError,
)


class OPayAPIError(OPayError):
    """Raised for an authenticated rejection returned by OPay."""

    category = "authenticated_rejection"

    def __init__(self, code, message):
        self.code = code
        safe_message = self._safe_protocol_message(message) or "Unspecified OPay error"
        super().__init__(
            f"OPay rejected the request with code {code}: {safe_message}",
            opay_code=code,
            opay_message=safe_message,
        )


class OPayConnectionError(OPayError):
    """Raised for an OPay connection/network failure other than a known timeout."""

    category = "connection_error"


class OPayConnectTimeoutError(OPayConnectionError):
    """Raised when a connection to OPay cannot be established in time."""

    category = "connect_timeout"


class OPayReadTimeoutError(OPayConnectionError):
    """Raised when OPay does not return a response in time."""

    category = "read_timeout"


class OPayHTTPError(OPayError):
    """Raised when OPay responds with a non-successful HTTP status."""

    category = "http_error"

    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"OPay returned HTTP status {status_code}.")


class OPayOrderNotFoundError(OPayError):
    """Exact live Query Order response confirming no OPay order exists."""

    category = "order_not_found"

    def __init__(self, code, message):
        self.code = code
        safe_message = self._safe_protocol_message(message) or "Order not found"
        super().__init__(
            f"OPay did not find the queried order (code {code}).",
            reason_code="confirmed_order_not_found",
            opay_code=code,
            opay_message=safe_message,
        )


@dataclass(frozen=True)
class OPayCreatePaymentResult:
    out_order_no: str
    order_no: str
    message: str = ""
    accepted: bool = True
    status: str = "created"
    payment_completed: bool = False


class OPayClient:
    BASE_URL = "https://payapi.opayweb.com"
    CREATE_PAYMENT_PATH = "/openApi/order/checkout/createOrder"
    QUERY_PAYMENT_PATH = "/openApi/order/checkout/qryOrderDetail"
    API_VERSION = "V1.0.1"
    BODY_FORMAT = "JSON"
    CURRENCY = "NGN"
    SCENE = "CASH_API"
    IS_SPLIT = "N"
    DEFAULT_CONNECT_TIMEOUT = 10
    DEFAULT_READ_TIMEOUT = 10
    DEFAULT_ORDER_EXPIRE_TIME = 180
    NGN_QUANTUM = Decimal("0.01")
    RESPONSE_MESSAGE_KEY = "_opay_response_message"

    def __init__(
        self,
        *,
        head_merchant_id,
        merchant_id,
        terminal_sn,
        sub_scene_enum,
        client_auth_key,
        opay_public_key,
        merchant_private_key,
        http_session=None,
        timeout=None,
        connect_timeout=None,
        read_timeout=None,
    ):
        self.head_merchant_id = self._required_text(
            head_merchant_id, "OPay Business ID"
        )
        self.merchant_id = self._required_text(merchant_id, "OPay Branch ID")
        self.terminal_sn = self._required_text(terminal_sn, "OPay terminal serial number")
        self.sub_scene_enum = self._required_text(
            sub_scene_enum, "OPay subSceneEnum"
        )
        self.client_auth_key = self._required_text(
            client_auth_key, "OPay clientAuthKey"
        )
        if timeout is not None:
            if connect_timeout is not None or read_timeout is not None:
                raise OPayConfigurationError(
                    "The legacy OPay HTTP timeout cannot be combined with separate "
                    "connect or read timeouts."
                )
            connect_timeout = timeout
            read_timeout = timeout
        if connect_timeout is None:
            connect_timeout = self.DEFAULT_CONNECT_TIMEOUT
        if read_timeout is None:
            read_timeout = self.DEFAULT_READ_TIMEOUT
        self.connect_timeout = self._positive_timeout(
            connect_timeout, "connect timeout"
        )
        self.read_timeout = self._positive_timeout(read_timeout, "read timeout")
        self.auth = OPayAuth(opay_public_key, merchant_private_key)
        self.http_session = http_session or requests.Session()

    @classmethod
    def from_payment_method(cls, payment_method, **kwargs):
        payment_method.ensure_one()
        if payment_method.use_payment_terminal != "opay":
            raise OPayConfigurationError(
                "An OPay API client requires an OPay payment method."
            )
        configured_method = payment_method.sudo()
        configured_method._check_opay_configuration()
        return cls(
            head_merchant_id=configured_method.opay_head_merchant_id,
            merchant_id=configured_method.opay_merchant_id,
            terminal_sn=configured_method.opay_terminal_sn,
            sub_scene_enum=configured_method.opay_sub_scene_enum,
            client_auth_key=configured_method.opay_client_auth_key,
            opay_public_key=configured_method.opay_public_key,
            merchant_private_key=configured_method.opay_merchant_private_key,
            **kwargs,
        )

    def create_payment(
        self,
        *,
        out_order_no,
        amount,
        currency,
        order_expire_time=DEFAULT_ORDER_EXPIRE_TIME,
    ):
        out_order_no = self._required_text(out_order_no, "OPay business order number")
        currency = self._required_text(currency, "payment currency")
        if currency != self.CURRENCY:
            raise OPayConfigurationError("OPay POS payments require NGN currency.")
        if (
            isinstance(order_expire_time, bool)
            or not isinstance(order_expire_time, int)
            or order_expire_time <= 0
        ):
            raise OPayConfigurationError(
                "The OPay order expiry time must be a positive number of seconds."
            )

        payload = {
            "headMerchantId": self.head_merchant_id,
            "merchantId": self.merchant_id,
            "outOrderNo": out_order_no,
            "amount": self.normalize_amount(amount),
            "currency": self.CURRENCY,
            "orderExpireTime": order_expire_time,
            "sceneEnum": self.SCENE,
            "subSceneEnum": self.sub_scene_enum,
            "sn": self.terminal_sn,
            "isSplit": self.IS_SPLIT,
        }
        response = self._post(self.CREATE_PAYMENT_PATH, payload)
        response_data = response["data"]
        if not isinstance(response_data, dict):
            raise OPayMalformedResponseError(
                "OPay returned invalid Create Payment data.",
                reason_code="invalid_create_payment_data",
            )
        order_no = response_data.get("orderNo")
        if not isinstance(order_no, str) or not order_no.strip():
            raise OPayMalformedResponseError(
                "OPay Create Payment did not return an order number.",
                reason_code="missing_create_order_no",
            )
        return OPayCreatePaymentResult(
            out_order_no=out_order_no,
            order_no=order_no.strip(),
            message=OPayError._safe_protocol_message(response["message"]) or "",
        )

    def query_payment(self, *, out_order_no=None, order_no=None):
        out_order_no = self._optional_text(out_order_no)
        order_no = self._optional_text(order_no)
        if not out_order_no and not order_no:
            raise OPayConfigurationError(
                "OPay Query Order requires outOrderNo or orderNo."
            )

        payload = {
            "headMerchantId": self.head_merchant_id,
            "merchantId": self.merchant_id,
        }
        if out_order_no:
            payload["outOrderNo"] = out_order_no
        if order_no:
            payload["orderNo"] = order_no

        response = self._post(self.QUERY_PAYMENT_PATH, payload)
        response_data = response["data"]
        if not isinstance(response_data, dict):
            raise OPayMalformedResponseError(
                "OPay returned invalid Query Order data.",
                reason_code="invalid_query_order_data",
            )
        return {
            **response_data,
            self.RESPONSE_MESSAGE_KEY: (
                OPayError._safe_protocol_message(response["message"]) or ""
            ),
        }

    @classmethod
    def normalize_amount(cls, amount):
        if isinstance(amount, bool):
            raise OPayConfigurationError("The OPay payment amount is invalid.")
        try:
            normalized = Decimal(str(amount))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise OPayConfigurationError("The OPay payment amount is invalid.") from error
        if not normalized.is_finite() or normalized <= 0:
            raise OPayConfigurationError(
                "The OPay payment amount must be greater than zero."
            )
        normalized = normalized.quantize(cls.NGN_QUANTUM, rounding=ROUND_HALF_UP)
        if normalized <= 0:
            raise OPayConfigurationError(
                "The OPay payment amount must be at least 0.01 NGN."
            )
        return format(normalized, ".2f")

    def _post(self, path, payload):
        timestamp, request_body = self.auth.build_request(payload)
        headers = {
            "Content-Type": "application/json",
            "clientAuthKey": self.client_auth_key,
            "version": self.API_VERSION,
            "bodyFormat": self.BODY_FORMAT,
            "timestamp": timestamp,
        }
        try:
            response = self.http_session.post(
                f"{self.BASE_URL}{path}",
                json=request_body,
                headers=headers,
                timeout=(self.connect_timeout, self.read_timeout),
            )
        except requests.ConnectTimeout as error:
            raise OPayConnectTimeoutError(
                "The connection to OPay timed out."
            ) from error
        except requests.ReadTimeout as error:
            raise OPayReadTimeoutError(
                "OPay did not return a response before the read timeout."
            ) from error
        except requests.Timeout as error:
            raise OPayConnectionError("The OPay request timed out.") from error
        except requests.RequestException as error:
            raise OPayConnectionError("Unable to contact OPay.") from error

        if not 200 <= response.status_code < 300:
            raise OPayHTTPError(response.status_code)
        try:
            response_envelope = response.json()
        except (requests.JSONDecodeError, ValueError) as error:
            raise OPayMalformedResponseError(
                "OPay returned a malformed JSON response.",
                reason_code="malformed_json_response",
            ) from error

        if path == self.QUERY_PAYMENT_PATH and self._is_order_not_found_response(
            response_envelope
        ):
            raise OPayOrderNotFoundError(
                response_envelope["code"],
                response_envelope["message"],
            )

        authenticated_response = self.auth.process_response(response_envelope)
        if authenticated_response["code"] != "00000":
            raise OPayAPIError(
                authenticated_response["code"],
                authenticated_response["message"],
            )
        return authenticated_response

    @staticmethod
    def _is_order_not_found_response(response):
        if not isinstance(response, dict):
            return False
        if set(response) != {"code", "message", "data"}:
            return False
        message = OPayError._safe_protocol_message(response.get("message"))
        return (
            response.get("code") in {"00003", "50002"}
            and message is not None
            and message.casefold() == "order not exist"
            and response.get("data") is None
        )

    @staticmethod
    def _required_text(value, label):
        normalized = OPayClient._optional_text(value)
        if not normalized:
            raise OPayConfigurationError(f"The {label} is required.")
        return normalized

    @staticmethod
    def _optional_text(value):
        return value.strip() if isinstance(value, str) else None

    @staticmethod
    def _positive_timeout(value, label):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
        ):
            raise OPayConfigurationError(f"The OPay HTTP {label} must be positive.")
        return value
