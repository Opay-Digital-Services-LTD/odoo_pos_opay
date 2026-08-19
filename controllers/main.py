import logging
import time

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request
from werkzeug.exceptions import BadRequest


_logger = logging.getLogger(__name__)


class PosOpayController(http.Controller):
    @http.route(
        "/pos_opay/notification",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def notification(self):
        started_at = time.monotonic()
        try:
            envelope = request.get_json_data()
        except (BadRequest, TypeError, ValueError):
            self._log_rejection(
                reason="malformed_json",
                duration_ms=self._duration_ms(started_at),
            )
            return self._invalid_response()

        headers = {
            "merchant_id": request.httprequest.headers.get("merchantId"),
            "transaction_id": request.httprequest.headers.get("X-Opay-Tranid"),
        }
        try:
            attempt = (
                request.env["pos.opay.payment.attempt"]
                .sudo()
                .process_webhook_notification(headers, envelope)
            )
        except ValidationError as error:
            self._log_rejection(
                reason=getattr(error, "reason_code", "unexpected_validation_error"),
                payment_method_id=getattr(error, "payment_method_id", None),
                attempt_id=getattr(error, "attempt_id", None),
                out_order_no=getattr(error, "out_order_no", None),
                duration_ms=self._duration_ms(started_at),
            )
            return self._invalid_response()
        except Exception as error:  # keep the public response safe and retryable
            # Do not include exception text or a traceback here: third-party
            # parsing/crypto exceptions can embed webhook payload fragments.
            _logger.error(
                "event=opay_webhook_processing outcome=error source=webhook "
                "reason=callback payment_method_id=- attempt_id=- reference=- "
                "out_order_no=- order_no=- terminal_sn=- status=- "
                "error_type=%s duration_ms=%s",
                type(error).__name__,
                self._duration_ms(started_at),
            )
            return request.make_json_response(
                {"code": "11004", "message": "PROCESSING ERROR"}, status=500
            )

        _logger.info(
            "event=opay_webhook_processing outcome=processed source=webhook "
            "reason=callback payment_method_id=%s attempt_id=%s reference=%s "
            "out_order_no=%s order_no=%s terminal_sn=%s status=%s duration_ms=%s",
            attempt.payment_method_id.id,
            attempt.id,
            attempt.payment_reference,
            attempt.out_order_no,
            attempt.order_no or "-",
            attempt.terminal_sn,
            attempt.status,
            self._duration_ms(started_at),
        )
        return request.make_json_response(
            {"code": "00000", "message": "SUCCESSFUL"}
        )

    @staticmethod
    def _log_rejection(
        *,
        reason,
        payment_method_id=None,
        attempt_id=None,
        out_order_no=None,
        duration_ms=0,
    ):
        _logger.warning(
            "event=opay_webhook_processing outcome=rejected source=webhook "
            "reason=%s payment_method_id=%s attempt_id=%s reference=- "
            "out_order_no=%s order_no=- terminal_sn=- status=- duration_ms=%s",
            reason,
            payment_method_id or "-",
            attempt_id or "-",
            out_order_no or "-",
            duration_ms,
        )

    @staticmethod
    def _duration_ms(started_at):
        return max(0, round((time.monotonic() - started_at) * 1000))

    @staticmethod
    def _invalid_response():
        return request.make_json_response(
            {"code": "00004", "message": "INVALID REQUEST"}, status=400
        )
