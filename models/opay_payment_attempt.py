import hmac
import logging
import time
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.opay_api import (
    OPayAPIError,
    OPayClient,
    OPayConnectionError,
    OPayOrderNotFoundError,
)
from ..services.opay_auth import (
    OPayAuth,
    OPayAuthenticationError,
    OPayConfigurationError,
    OPayError,
    OPayMalformedResponseError,
)


_logger = logging.getLogger(__name__)


class OPayWebhookValidationError(ValidationError):
    """Fail-closed validation carrying only development-safe diagnostics."""

    def __init__(
        self,
        reason_code,
        message,
        *,
        attempt=None,
        out_order_no=None,
        payment_method_id=None,
    ):
        self.reason_code = reason_code
        self.attempt_id = attempt.id if attempt else None
        self.payment_method_id = (
            payment_method_id
            or (attempt.payment_method_id.id if attempt else None)
        )
        self.out_order_no = self._safe_identifier(
            out_order_no or (attempt.out_order_no if attempt else None)
        )
        super().__init__(message)

    @staticmethod
    def _safe_identifier(value):
        if not isinstance(value, str):
            return None
        return "".join(
            character
            for character in value.strip()
            if character.isalnum() or character in {"-", "_", "."}
        )[:128] or None


class PosOpayPaymentAttempt(models.Model):
    _name = "pos.opay.payment.attempt"
    _description = "OPay POS Payment Attempt"
    _order = "id desc"
    _rec_names_search = ["payment_reference", "out_order_no", "order_no"]
    _check_company_auto = True

    ACTIVE_STATUSES = {"creating", "waiting", "uncertain", "PENDING"}
    FINAL_STATUSES = {"SUCCESS", "FAIL", "CLOSE", "CANCEL"}
    OPAY_STATUSES = FINAL_STATUSES | {"PENDING"}

    payment_method_id = fields.Many2one(
        "pos.payment.method",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    company_id = fields.Many2one(
        related="payment_method_id.company_id", store=True, index=True
    )
    pos_session_id = fields.Many2one(
        "pos.session", index=True, ondelete="restrict", check_company=True
    )
    pos_config_id = fields.Many2one(
        "pos.config", index=True, ondelete="restrict", check_company=True
    )
    payment_reference = fields.Char(required=True, index=True)
    out_order_no = fields.Char(required=True, index=True)
    order_no = fields.Char(index=True)
    amount = fields.Char(required=True)
    currency = fields.Char(required=True, default=OPayClient.CURRENCY)
    head_merchant_id = fields.Char(required=True)
    merchant_id = fields.Char(required=True, index=True)
    terminal_sn = fields.Char(required=True, index=True)
    status = fields.Selection(
        [
            ("creating", "Creating"),
            ("waiting", "Waiting"),
            ("uncertain", "Uncertain"),
            ("failed", "Create Failed"),
            ("PENDING", "Pending"),
            ("SUCCESS", "Success"),
            ("FAIL", "Failed"),
            ("CLOSE", "Closed"),
            ("CANCEL", "Cancelled"),
        ],
        required=True,
        default="creating",
        index=True,
    )
    expires_at = fields.Datetime(required=True, index=True)
    finalized_at = fields.Datetime(readonly=True)
    last_source = fields.Selection(
        [("create", "Create"), ("query", "Query"), ("webhook", "Webhook")]
    )
    last_status_message = fields.Char(readonly=True)

    _unique_payment_reference = models.Constraint(
        "unique (payment_reference)",
        "An OPay payment reference must identify exactly one attempt.",
    )
    _unique_out_order_no = models.Constraint(
        "unique (out_order_no)",
        "An OPay business order number must identify exactly one attempt.",
    )

    @api.depends("payment_reference")
    def _compute_display_name(self):
        for attempt in self:
            attempt.display_name = _(
                "OPay Payment #%(id)s",
                id=attempt.id,
            )

    @api.model
    def _new_attempt_values(
        self, payment_method, reference, out_order_no, amount, session=None
    ):
        configured_method = payment_method.sudo()
        return {
            "payment_method_id": configured_method.id,
            "pos_session_id": session.id if session else False,
            "pos_config_id": session.config_id.id if session else False,
            "payment_reference": reference,
            "out_order_no": out_order_no,
            "amount": amount,
            "currency": OPayClient.CURRENCY,
            "head_merchant_id": configured_method.opay_head_merchant_id,
            "merchant_id": configured_method.opay_merchant_id,
            "terminal_sn": configured_method.opay_terminal_sn,
            "status": "creating",
            "expires_at": fields.Datetime.now()
            + timedelta(seconds=OPayClient.DEFAULT_ORDER_EXPIRE_TIME),
            "last_source": "create",
        }

    def is_active(self):
        self.ensure_one()
        return self.status in self.ACTIVE_STATUSES

    def is_stale(self):
        self.ensure_one()
        return self.is_active() and self.expires_at <= fields.Datetime.now()

    def _opay_attach_session(self, session):
        """Attach legacy attempts, while never moving a known attempt to another POS."""
        self.ensure_one()
        if self.pos_session_id and self.pos_session_id != session:
            raise ValidationError(
                _("This OPay payment attempt belongs to another POS session.")
            )
        if self.pos_config_id and self.pos_config_id != session.config_id:
            raise ValidationError(
                _("This OPay payment attempt belongs to another Point of Sale.")
            )
        if not self.pos_session_id:
            self.write(
                {
                    "pos_session_id": session.id,
                    "pos_config_id": session.config_id.id,
                }
            )

    def query_opay_status(self, notify=True, reason="unspecified"):
        """Query the existing OPay order; never create a replacement order."""
        self.ensure_one()
        if self.status in self.FINAL_STATUSES:
            return self.frontend_response(reused=True)

        started_at = time.monotonic()
        try:
            client = OPayClient.from_payment_method(self.payment_method_id)
            result = client.query_payment(
                out_order_no=self.out_order_no,
                order_no=self.order_no or None,
            )
        except OPayOrderNotFoundError as error:
            return self._apply_confirmed_order_not_found(
                error,
                notify=notify,
                reason=reason,
                started_at=started_at,
            )
        except OPayAPIError as error:
            self._log_query_event(
                outcome="rejected",
                reason=reason,
                duration_ms=self._duration_ms(started_at),
                level=logging.INFO,
                error_type=type(error).__name__,
                error_category=error.category,
                code=error.code,
            )
            return self.frontend_response(
                message=_(
                    "Unable to confirm the OPay payment status. OPay response: "
                    "%(message)s",
                    message=self._safe_message(error.opay_message),
                ),
                ambiguous=True,
                reused=True,
            )
        except (
            OPayConnectionError,
            OPayAuthenticationError,
            OPayConfigurationError,
            OPayMalformedResponseError,
            OPayError,
        ) as error:
            response_message = _(
                "Unable to confirm the OPay payment status. The existing "
                "payment remains active and was not resent."
            )
            response_message = self._append_opay_message(
                response_message,
                error.opay_message,
                verified=False,
            )
            self._log_query_event(
                outcome="uncertain",
                reason=reason,
                duration_ms=self._duration_ms(started_at),
                level=logging.WARNING,
                error_type=type(error).__name__,
                error_category=error.category,
                error_reason=error.reason_code or "-",
                code=error.opay_code or "-",
            )
            return self.frontend_response(
                message=response_message,
                ambiguous=True,
                reused=True,
            )
        api_duration_ms = self._duration_ms(started_at)
        try:
            self.apply_authenticated_result(
                result,
                source="query",
                reason=reason,
                notify=notify,
            )
        except OPayWebhookValidationError as error:
            self._log_query_event(
                outcome="invalid_result",
                reason=reason,
                duration_ms=api_duration_ms,
                level=logging.WARNING,
                error_type=type(error).__name__,
                error_category="validation_error",
                code=error.reason_code,
            )
            raise
        self._log_query_event(
            outcome="authenticated",
            reason=reason,
            duration_ms=api_duration_ms,
            level=logging.INFO,
        )
        return self.frontend_response(reused=True)

    def _apply_confirmed_order_not_found(
        self,
        error,
        *,
        notify,
        reason,
        started_at,
    ):
        """Release only the exact live OPay Query Order not-found response."""
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM pos_payment_method WHERE id = %s FOR UPDATE",
            [self.payment_method_id.id],
        )
        self.env.cr.execute(
            "SELECT id FROM pos_opay_payment_attempt WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset()

        if self.status in self.FINAL_STATUSES:
            return self.frontend_response(reused=True)

        self.write(
            {
                "status": "failed",
                "last_source": "query",
                "last_status_message": self._safe_message(error.opay_message),
                "finalized_at": fields.Datetime.now(),
            }
        )
        self.payment_method_id._opay_clear_latest_attempt(self)
        if notify:
            self._notify_pos()
        self._log_query_event(
            outcome="confirmed_absent",
            reason=reason,
            duration_ms=self._duration_ms(started_at),
            level=logging.INFO,
            error_type=type(error).__name__,
            error_category=error.category,
            error_reason=error.reason_code,
            code=error.code,
        )
        return self.frontend_response(
            message=self._append_opay_message(
                _(
                    "OPay confirmed that this payment order does not exist. "
                    "The payment has been released and may be tried again."
                ),
                error.opay_message,
            ),
            ambiguous=False,
            reused=True,
        )

    def action_opay_check_payment_status(self):
        """Let an authorized backend manager query an orphaned active attempt."""
        self.ensure_one()
        if not self.env.user.has_group("base.group_erp_manager"):
            raise AccessError(
                _("Only an authorized Odoo administrator can check this payment status.")
            )
        self.check_access("read")
        attempt = self.sudo()

        if attempt.status in attempt.ACTIVE_STATUSES:
            attempt.env.cr.execute(
                "SELECT id FROM pos_payment_method WHERE id = %s FOR UPDATE",
                [attempt.payment_method_id.id],
            )
            attempt.invalidate_recordset()
            response = attempt.query_opay_status(
                notify=True,
                reason="admin_manual_check",
            )
        else:
            response = attempt.frontend_response(reused=True)

        status = response["status"]
        notification_type = (
            "success"
            if status == "SUCCESS"
            else "danger"
            if status in self.FINAL_STATUSES | {"failed"}
            else "warning"
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("OPay Payment Status"),
                "message": response["message"],
                "type": notification_type,
                "sticky": True,
                "next": {
                    "type": "ir.actions.client",
                    "tag": "soft_reload",
                },
            },
        }

    def apply_authenticated_result(
        self,
        result,
        *,
        source,
        reason="unspecified",
        notify=True,
        headers=None,
    ):
        self.ensure_one()
        started_at = time.monotonic()
        # Create, recovery, and Query already lock the terminal's payment method
        # before touching an attempt. Webhooks must use that same lock order:
        # taking the attempt first and later clearing payment-method runtime fields
        # can deadlock against a concurrent Query (payment method -> attempt).
        self.env.cr.execute(
            "SELECT id FROM pos_payment_method WHERE id = %s FOR UPDATE",
            [self.payment_method_id.id],
        )
        self.env.cr.execute(
            "SELECT id FROM pos_opay_payment_attempt WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset()
        values = self._validated_result(result, headers=headers)
        incoming_status = values["status"]

        if self.status in self.FINAL_STATUSES:
            if self.status != incoming_status:
                self._log_status_event(
                    outcome="conflict",
                    source=source,
                    reason=reason,
                    status=incoming_status,
                    duration_ms=self._duration_ms(started_at),
                    level=logging.WARNING,
                )
                raise OPayWebhookValidationError(
                    "conflicting_final_status",
                    _("OPay returned a conflicting final payment status."),
                    attempt=self,
                )
            self._log_status_event(
                outcome="idempotent",
                source=source,
                reason=reason,
                status=incoming_status,
                duration_ms=self._duration_ms(started_at),
                level=logging.INFO,
            )
            return False

        write_values = {
            "status": incoming_status,
            "order_no": values["order_no"],
            "last_source": source,
            "last_status_message": values["message"],
        }
        if incoming_status in self.FINAL_STATUSES:
            write_values["finalized_at"] = fields.Datetime.now()
        self.write(write_values)

        if incoming_status in self.FINAL_STATUSES:
            self.payment_method_id._opay_clear_latest_attempt(self)

        if notify:
            self._notify_pos()
        self._log_status_event(
            outcome="applied",
            source=source,
            reason=reason,
            status=self.status,
            duration_ms=self._duration_ms(started_at),
            level=logging.INFO,
        )
        return True

    def _log_query_event(
        self,
        *,
        outcome,
        reason,
        duration_ms,
        level,
        error_type="-",
        error_category="-",
        error_reason="-",
        code="-",
    ):
        self.ensure_one()
        _logger.log(
            level,
            "event=opay_query_order_api outcome=%s source=query reason=%s "
            "payment_method_id=%s attempt_id=%s reference=%s out_order_no=%s "
            "order_no=%s terminal_sn=%s status=%s error_type=%s code=%s "
            "error_category=%s error_reason=%s duration_ms=%s",
            outcome,
            reason,
            self.payment_method_id.id,
            self.id,
            self.payment_reference,
            self.out_order_no,
            self.order_no or "-",
            self.terminal_sn,
            self.status,
            error_type,
            code,
            error_category,
            error_reason,
            duration_ms,
        )

    def _log_status_event(
        self, outcome, source, reason, status, duration_ms, level
    ):
        self.ensure_one()
        _logger.log(
            level,
            "event=opay_attempt_finalization outcome=%s source=%s reason=%s "
            "payment_method_id=%s attempt_id=%s reference=%s out_order_no=%s "
            "order_no=%s terminal_sn=%s status=%s duration_ms=%s",
            outcome,
            source,
            reason,
            self.payment_method_id.id,
            self.id,
            self.payment_reference,
            self.out_order_no,
            self.order_no or "-",
            self.terminal_sn,
            status,
            duration_ms,
        )

    @staticmethod
    def _duration_ms(started_at):
        return max(0, round((time.monotonic() - started_at) * 1000))

    def _validated_result(self, result, headers=None):
        self.ensure_one()
        if not isinstance(result, dict):
            raise OPayWebhookValidationError(
                "malformed_payment_data",
                _("OPay returned invalid payment status data."),
                attempt=self,
            )

        required = ("outOrderNo", "orderNo", "status", "amount", "currency")
        if any(
            not isinstance(result.get(name), str) or not result[name].strip()
            for name in required
        ):
            raise OPayWebhookValidationError(
                "incomplete_payment_data",
                _("OPay returned incomplete payment status data."),
                attempt=self,
            )

        out_order_no = result["outOrderNo"].strip()
        order_no = result["orderNo"].strip()
        status = result["status"].strip().upper()
        currency = result["currency"].strip().upper()
        try:
            amount = OPayClient.normalize_amount(result["amount"])
        except OPayConfigurationError as error:
            raise OPayWebhookValidationError(
                "invalid_amount",
                _("OPay returned an invalid payment amount."),
                attempt=self,
            ) from error

        if out_order_no != self.out_order_no:
            raise OPayWebhookValidationError(
                "out_order_no_mismatch",
                _("OPay returned a mismatched business order number."),
                attempt=self,
                out_order_no=out_order_no,
            )
        if self.order_no and order_no != self.order_no:
            raise OPayWebhookValidationError(
                "order_no_mismatch",
                _("OPay returned a mismatched payment order number."),
                attempt=self,
            )
        if status not in self.OPAY_STATUSES:
            raise OPayWebhookValidationError(
                "invalid_status",
                _("OPay returned an unsupported payment status."),
                attempt=self,
            )
        if amount != self.amount:
            raise OPayWebhookValidationError(
                "amount_mismatch",
                _("OPay returned a mismatched payment amount."),
                attempt=self,
            )
        if currency != self.currency:
            raise OPayWebhookValidationError(
                "currency_mismatch",
                _("OPay returned a mismatched payment currency."),
                attempt=self,
            )

        terminal_sn = result.get("sn")
        if terminal_sn is not None and str(terminal_sn).strip() != self.terminal_sn:
            raise OPayWebhookValidationError(
                "terminal_sn_mismatch",
                _("OPay returned a mismatched terminal serial number."),
                attempt=self,
            )
        merchant_id = result.get("merchantId")
        if merchant_id is not None and str(merchant_id).strip() != self.merchant_id:
            raise OPayWebhookValidationError(
                "merchant_mismatch",
                _("OPay returned a mismatched branch merchant."),
                attempt=self,
            )
        head_merchant_id = result.get("headMerchantId")
        if (
            head_merchant_id is not None
            and str(head_merchant_id).strip() != self.head_merchant_id
        ):
            raise OPayWebhookValidationError(
                "head_merchant_mismatch",
                _("OPay returned a mismatched business merchant."),
                attempt=self,
            )

        if headers:
            if headers.get("merchant_id") != self.merchant_id:
                raise OPayWebhookValidationError(
                    "merchant_header_mismatch",
                    _("OPay returned a mismatched webhook merchant."),
                    attempt=self,
                )
            transaction_id = headers.get("transaction_id")
            valid_transaction_ids = {order_no}
            if result.get("payNo"):
                valid_transaction_ids.add(str(result["payNo"]).strip())
            if transaction_id not in valid_transaction_ids:
                raise OPayWebhookValidationError(
                    "transaction_header_mismatch",
                    _("OPay returned a mismatched webhook transaction identifier."),
                    attempt=self,
                )
            if not terminal_sn:
                raise OPayWebhookValidationError(
                    "missing_terminal_sn",
                    _("OPay webhook data did not identify the expected terminal."),
                    attempt=self,
                )

        response_message = self._safe_message(
            result.get(OPayClient.RESPONSE_MESSAGE_KEY)
        )
        payment_message = self._safe_message(result.get("errorMsg"))
        messages = []
        for message in (response_message, payment_message):
            if message and message not in messages:
                messages.append(message)

        return {
            "status": status,
            "order_no": order_no,
            "message": "; ".join(messages) or False,
        }

    @api.model
    def process_webhook_notification(self, headers, envelope, now_ms=None):
        if not isinstance(headers, dict):
            raise OPayWebhookValidationError(
                "malformed_headers", _("Invalid OPay webhook headers.")
            )
        merchant_id = headers.get("merchant_id")
        transaction_id = headers.get("transaction_id")
        if not merchant_id:
            raise OPayWebhookValidationError(
                "missing_merchant_header", _("Incomplete OPay webhook headers.")
            )
        if not transaction_id:
            raise OPayWebhookValidationError(
                "missing_transaction_header", _("Incomplete OPay webhook headers.")
            )
        if not isinstance(envelope, dict):
            raise OPayWebhookValidationError(
                "malformed_envelope", _("Invalid OPay webhook request.")
            )

        candidates = self.env["pos.payment.method"].sudo().search(
            [
                ("use_payment_terminal", "=", "opay"),
                ("opay_merchant_id", "=", merchant_id),
            ]
        )
        supplied_auth_key = envelope.get("clientAuthKey")
        if not isinstance(supplied_auth_key, str):
            raise OPayWebhookValidationError(
                "malformed_client_auth_key",
                _("Invalid OPay webhook authentication."),
            )

        if not candidates:
            raise OPayWebhookValidationError(
                "unknown_merchant", _("OPay webhook merchant is not configured.")
            )

        auth_key_matched = False
        last_protocol_error = None
        last_payment_method_id = None
        for payment_method in candidates:
            configured_auth_key = payment_method.opay_client_auth_key or ""
            if not hmac.compare_digest(supplied_auth_key, configured_auth_key):
                continue
            auth_key_matched = True
            try:
                content = OPayAuth(
                    payment_method.opay_public_key,
                    payment_method.opay_merchant_private_key,
                ).process_webhook(
                    envelope,
                    configured_auth_key,
                    now_ms=now_ms,
                )
            except (
                OPayAuthenticationError,
                OPayConfigurationError,
                OPayMalformedResponseError,
            ) as error:
                last_protocol_error = error
                last_payment_method_id = payment_method.id
                continue

            result = content.get("data") if set(content) == {"data"} else content
            if not isinstance(result, dict) or not isinstance(
                result.get("outOrderNo"), str
            ):
                raise OPayWebhookValidationError(
                    "incomplete_payment_data",
                    _("OPay returned incomplete payment status data."),
                    payment_method_id=payment_method.id,
                )
            out_order_no = result["outOrderNo"].strip()
            attempt = self.sudo().search(
                [
                    ("payment_method_id", "=", payment_method.id),
                    ("out_order_no", "=", out_order_no),
                ],
                limit=1,
            )
            if not attempt:
                order_no = result.get("orderNo")
                attempt_by_order = (
                    self.sudo().search(
                        [
                            ("payment_method_id", "=", payment_method.id),
                            ("order_no", "=", order_no.strip()),
                        ],
                        limit=1,
                    )
                    if isinstance(order_no, str) and order_no.strip()
                    else self.browse()
                )
                if attempt_by_order:
                    raise OPayWebhookValidationError(
                        "out_order_no_mismatch",
                        _("OPay returned a mismatched business order number."),
                        attempt=attempt_by_order,
                        out_order_no=out_order_no,
                    )
                attempt_for_other_method = self.sudo().search(
                    [("out_order_no", "=", out_order_no)], limit=1
                )
                if attempt_for_other_method:
                    raise OPayWebhookValidationError(
                        "payment_method_attempt_mismatch",
                        _("OPay webhook matched a different payment method."),
                        attempt=attempt_for_other_method,
                        out_order_no=out_order_no,
                        payment_method_id=payment_method.id,
                    )
                raise OPayWebhookValidationError(
                    "unknown_out_order_no",
                    _("OPay webhook did not match a known payment attempt."),
                    out_order_no=out_order_no,
                    payment_method_id=payment_method.id,
                )
            attempt.apply_authenticated_result(
                result,
                source="webhook",
                reason="callback",
                notify=True,
                headers=headers,
            )
            return attempt

        if last_protocol_error:
            fallback_reason = {
                OPayConfigurationError: "invalid_crypto_configuration",
                OPayAuthenticationError: "authentication_failure",
                OPayMalformedResponseError: "malformed_protocol_data",
            }
            raise OPayWebhookValidationError(
                last_protocol_error.reason_code
                or fallback_reason.get(
                    type(last_protocol_error), "protocol_validation_failure"
                ),
                _("OPay webhook protocol validation failed."),
                payment_method_id=last_payment_method_id,
            ) from last_protocol_error
        if not auth_key_matched:
            raise OPayWebhookValidationError(
                "client_auth_key_mismatch",
                _("OPay webhook authentication failed."),
            )
        raise OPayWebhookValidationError(
            "authentication_or_correlation_failure",
            _("OPay webhook authentication or correlation failed."),
        )

    def frontend_response(
        self, *, message=None, ambiguous=None, reused=False
    ):
        self.ensure_one()
        status_messages = {
            "creating": _("The OPay payment request is being created."),
            "waiting": _("Waiting for customer payment on the OPay terminal."),
            "uncertain": _(
                "OPay may have received this payment request. Its status must be "
                "checked before another payment is created."
            ),
            "failed": _("OPay rejected the payment request."),
            "PENDING": _("The OPay payment is still pending on the terminal."),
            "SUCCESS": _("OPay confirmed the payment successfully."),
            "FAIL": _("OPay reported that the payment failed."),
            "CLOSE": _("The OPay payment expired or was closed."),
            "CANCEL": _("The OPay payment was cancelled by the operator."),
        }
        if self.last_status_message:
            status_messages[self.status] = self._append_opay_message(
                status_messages[self.status],
                self.last_status_message,
            )
        is_success = self.status == "SUCCESS"
        return {
            "success": is_success or self.status == "waiting",
            "status": self.status,
            "message": message or status_messages[self.status],
            "reference": self.payment_reference,
            "out_order_no": self.out_order_no,
            "order_no": self.order_no or False,
            "payment_completed": is_success,
            "ambiguous": self.status == "uncertain" if ambiguous is None else ambiguous,
            "terminal_released": self.status in self.FINAL_STATUSES | {"failed"},
            "reused": reused,
        }

    def _notify_pos(self):
        self.ensure_one()
        if not self.pos_config_id or not self.pos_session_id:
            return
        self.pos_config_id._notify(
            "OPAY_PAYMENT_STATUS",
            {
                "payment_method_id": self.payment_method_id.id,
                "pos_session_id": self.pos_session_id.id,
                **self.frontend_response(),
            },
        )

    @staticmethod
    def _safe_message(message):
        if not isinstance(message, str):
            return False
        return " ".join(message.split())[:256] or False

    @classmethod
    def _append_opay_message(cls, message, opay_message, verified=True):
        safe_message = cls._safe_message(opay_message)
        if not safe_message:
            return message
        label = _("OPay response") if verified else _("Unverified OPay response")
        return _(
            "%(message)s %(label)s: %(opay_message)s",
            message=message,
            label=label,
            opay_message=safe_message,
        )
