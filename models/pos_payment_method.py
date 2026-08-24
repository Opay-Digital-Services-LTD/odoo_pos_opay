import logging
import time
from datetime import timedelta
from uuid import UUID

from odoo import _, api, fields, models
from odoo.exceptions import AccessDenied, UserError, ValidationError

from ..services.opay_api import (
    OPayAPIError,
    OPayClient,
    OPayConnectionError,
    OPayOrderNotFoundError,
)
from ..services.opay_auth import (
    OPayAuthenticationError,
    OPayConfigurationError,
    OPayError,
    OPayMalformedResponseError,
)


_logger = logging.getLogger(__name__)


class PosPaymentMethod(models.Model):
    _inherit = "pos.payment.method"

    _OPAY_RUNTIME_FIELDS = {
        "opay_latest_payment_reference",
        "opay_latest_out_order_no",
        "opay_latest_order_no",
        "opay_latest_amount",
        "opay_latest_status",
    }
    _OPAY_ACTIVE_STATUSES = {"creating", "waiting", "uncertain"}

    def _get_payment_terminal_selection(self):
        return super()._get_payment_terminal_selection() + [
            ("opay", "OPay"),
        ]

    opay_head_merchant_id = fields.Char(
        string="OPay Business ID (headMerchantId)",
        copy=False,
        help=(
            "The OPay Business ID, also called the Head Merchant ID "
            "(headMerchantId), supplied by OPay."
        ),
    )
    opay_merchant_id = fields.Char(
        string="OPay Branch ID (merchantId)",
        copy=False,
        help=(
            "The OPay Branch ID (merchantId) supplied by OPay for the branch "
            "that owns this terminal."
        ),
    )
    opay_terminal_sn = fields.Char(
        string="OPay Terminal Serial Number (sn)",
        copy=False,
        help=(
            "The serial number (sn) of the physical OPay terminal that should "
            "receive payment amounts from this payment method."
        ),
    )
    opay_client_auth_key = fields.Char(
        string="OPay Client Auth Key (clientAuthKey)",
        copy=False,
        groups="base.group_erp_manager",
        help=(
            "The sensitive clientAuthKey from the OPay Business Dashboard "
            "Developer Tool. It stays on the Odoo server and is never sent to "
            "the cashier POS."
        ),
    )
    opay_public_key = fields.Text(
        string="OPay Public Key",
        copy=False,
        groups="base.group_erp_manager",
        help=(
            "OPay's RSA public key, used on the Odoo server to encrypt requests "
            "and verify OPay signatures. Do not enter the merchant public key."
        ),
    )
    opay_merchant_private_key = fields.Text(
        string="Merchant Private Key",
        copy=False,
        groups="base.group_erp_manager",
        help=(
            "The sensitive merchant RSA private key whose public key is "
            "registered with OPay. It is used on the Odoo server to sign "
            "requests and decrypt responses, and is never sent to the cashier POS."
        ),
    )
    opay_sub_scene_enum = fields.Char(
        string="OPay Sub Scene (subSceneEnum)",
        copy=False,
        help=(
            "The exact subSceneEnum value assigned by OPay for this POS "
            "integration. Contact OPay for this value; do not guess it."
        ),
    )
    opay_event_url = fields.Char(
        string="OPay Webhook URL",
        compute="_compute_opay_event_url",
        readonly=True,
        help=(
            "Register this public HTTPS URL in the OPay Business Dashboard under "
            "Developer Tool > POS & Others. It is generated from Odoo's web base URL."
        ),
    )
    opay_latest_payment_reference = fields.Char(
        copy=False,
        readonly=True,
        groups="base.group_erp_manager",
    )
    opay_latest_out_order_no = fields.Char(
        copy=False,
        readonly=True,
        groups="base.group_erp_manager",
    )
    opay_latest_order_no = fields.Char(
        copy=False,
        readonly=True,
        groups="base.group_erp_manager",
    )
    opay_latest_amount = fields.Char(
        copy=False,
        readonly=True,
        groups="base.group_erp_manager",
    )
    opay_latest_status = fields.Char(
        copy=False,
        readonly=True,
        groups="base.group_erp_manager",
    )

    @api.constrains(
        "use_payment_terminal",
        "opay_head_merchant_id",
        "opay_merchant_id",
        "opay_terminal_sn",
        "opay_client_auth_key",
        "opay_public_key",
        "opay_merchant_private_key",
        "opay_sub_scene_enum",
    )
    def _check_opay_configuration(self):
        opay_field_names = (
            "opay_head_merchant_id",
            "opay_merchant_id",
            "opay_terminal_sn",
            "opay_client_auth_key",
            "opay_public_key",
            "opay_merchant_private_key",
            "opay_sub_scene_enum",
        )
        for payment_method in self.filtered(
            lambda method: method.use_payment_terminal == "opay"
        ):
            missing_labels = [
                payment_method._fields[field_name].string
                for field_name in opay_field_names
                if not (payment_method[field_name] or "").strip()
            ]
            if missing_labels:
                raise ValidationError(
                    _(
                        "OPay payment method %(payment_method)s is missing required "
                        "configuration: %(fields)s. Enter the values supplied by "
                        "OPay before saving.",
                        payment_method=payment_method.display_name,
                        fields=", ".join(missing_labels),
                    )
                )

    @api.constrains("use_payment_terminal", "opay_terminal_sn")
    def _check_opay_terminal_sn(self):
        for payment_method in self.filtered(
            lambda method: method.use_payment_terminal == "opay"
            and method.opay_terminal_sn
        ):
            existing_payment_method = self.sudo().search(
                [
                    ("id", "!=", payment_method.id),
                    ("use_payment_terminal", "=", "opay"),
                    ("opay_terminal_sn", "=", payment_method.opay_terminal_sn),
                ],
                limit=1,
            )
            if not existing_payment_method:
                continue
            if existing_payment_method.company_id == payment_method.company_id:
                raise ValidationError(
                    _(
                        "OPay terminal %(terminal)s is already used on payment "
                        "method %(payment_method)s.",
                        terminal=payment_method.opay_terminal_sn,
                        payment_method=existing_payment_method.display_name,
                    )
                )
            raise ValidationError(
                _(
                    "OPay terminal %(terminal)s is already used in company "
                    "%(company)s on payment method %(payment_method)s.",
                    terminal=payment_method.opay_terminal_sn,
                    company=existing_payment_method.company_id.name,
                    payment_method=existing_payment_method.display_name,
                )
            )

    def _is_write_forbidden(self, field_names):
        return super()._is_write_forbidden(field_names - self._OPAY_RUNTIME_FIELDS)

    def _compute_opay_event_url(self):
        event_url = f"{self.get_base_url()}/pos_opay/notification"
        for payment_method in self:
            payment_method.opay_event_url = event_url

    def opay_create_payment_request(self, request):
        """Create one OPay order, or safely recover the existing terminal attempt."""
        self.ensure_one()
        self._opay_check_pos_user()
        if self.use_payment_terminal != "opay":
            raise UserError(
                _("OPay payment requests require an OPay payment method.")
            )
        if not isinstance(request, dict) or set(request) != {
            "reference",
            "amount",
            "session_id",
        }:
            raise UserError(_("Invalid OPay payment request."))

        reference, out_order_no = self._opay_attempt_reference(request["reference"])
        session = self._opay_validate_session(request["session_id"])
        try:
            normalized_amount = OPayClient.normalize_amount(request["amount"])
        except OPayConfigurationError as error:
            raise UserError(_("The OPay payment amount is invalid.")) from error

        configured_method = self.sudo()
        try:
            configured_method._check_opay_configuration()
        except ValidationError:
            return self._opay_failed_response(
                reference,
                out_order_no,
                _("The OPay terminal configuration is incomplete."),
            )

        # A configured payment method represents one physical terminal. Serializing
        # requests on its row prevents concurrent double-clicks from issuing two
        # Create Payment calls for the same payment-line UUID.
        self.env.cr.execute(
            "SELECT id FROM pos_payment_method WHERE id = %s FOR UPDATE",
            [self.id],
        )
        configured_method.invalidate_recordset(self._OPAY_RUNTIME_FIELDS)
        configured_method._opay_import_legacy_attempt()
        attempt_model = self.env["pos.opay.payment.attempt"].sudo()
        attempt = attempt_model.search(
            [
                ("payment_method_id", "=", self.id),
                ("payment_reference", "=", reference),
            ],
            limit=1,
        )
        if attempt:
            if attempt.amount != normalized_amount:
                return self._opay_failed_response(
                    reference,
                    out_order_no,
                    _(
                        "This OPay payment attempt already uses a different amount. "
                        "Start a new payment line."
                    ),
                    reused=True,
                )
            attempt._opay_attach_session(session)
            return attempt.frontend_response(reused=True)

        active_attempt = attempt_model.search(
            [
                ("payment_method_id", "=", self.id),
                ("status", "in", list(attempt_model.ACTIVE_STATUSES)),
            ],
            limit=1,
        )
        if active_attempt:
            if active_attempt.status == "SUCCESS":
                return self._opay_failed_response(
                    reference,
                    out_order_no,
                    _(
                        "A previous OPay payment was confirmed successful. Resolve "
                        "that payment before starting another attempt."
                    ),
                )
            if active_attempt.is_active():
                return self._opay_failed_response(
                    reference,
                    out_order_no,
                    _(
                        "The configured OPay terminal already has a payment awaiting "
                        "confirmation. Use Check Payment Status before creating "
                        "another payment."
                    ),
                )

        attempt = attempt_model.create(
            attempt_model._new_attempt_values(
                configured_method,
                reference,
                out_order_no,
                normalized_amount,
                session=session,
            )
        )

        configured_method.write(
            {
                "opay_latest_payment_reference": reference,
                "opay_latest_out_order_no": out_order_no,
                "opay_latest_order_no": False,
                "opay_latest_amount": normalized_amount,
                "opay_latest_status": "creating",
            }
        )

        started_at = time.monotonic()
        try:
            client = OPayClient.from_payment_method(configured_method)
            result = client.create_payment(
                out_order_no=out_order_no,
                amount=normalized_amount,
                currency=OPayClient.CURRENCY,
            )
        except OPayAPIError as error:
            if error.code == "00005":
                attempt.write(
                    {
                        "status": "uncertain",
                        "last_status_message": attempt._safe_message(
                            error.opay_message
                        ),
                    }
                )
                configured_method.opay_latest_status = attempt.status
                configured_method._opay_log_create_api(
                    attempt,
                    outcome="duplicate",
                    started_at=started_at,
                    level=logging.WARNING,
                    code=error.code,
                    error_type=type(error).__name__,
                    error_category=error.category,
                )
                return attempt.frontend_response(ambiguous=True)
            attempt.write(
                {
                    "status": "failed",
                    "last_status_message": attempt._safe_message(error.opay_message),
                }
            )
            configured_method.opay_latest_status = attempt.status
            configured_method._opay_log_create_api(
                attempt,
                outcome="rejected",
                started_at=started_at,
                level=logging.INFO,
                code=error.code,
                error_type=type(error).__name__,
                error_category=error.category,
            )
            return attempt.frontend_response(
                message=_(
                    "OPay rejected the payment request. OPay response: %(message)s",
                    message=attempt.last_status_message,
                )
            )
        except OPayConfigurationError as error:
            attempt.status = "failed"
            configured_method.opay_latest_status = attempt.status
            configured_method._opay_log_create_api(
                attempt,
                outcome="configuration_error",
                started_at=started_at,
                level=logging.WARNING,
                error_type=type(error).__name__,
                error_category=error.category,
            )
            return attempt.frontend_response(
                message=_("The OPay terminal configuration is invalid.")
            )
        except (
            OPayConnectionError,
            OPayAuthenticationError,
            OPayMalformedResponseError,
            OPayError,
        ) as error:
            attempt.write(
                {
                    "status": "uncertain",
                    "last_status_message": attempt._safe_message(
                        error.opay_message
                    ),
                }
            )
            configured_method.opay_latest_status = attempt.status
            configured_method._opay_log_create_api(
                attempt,
                outcome="uncertain",
                started_at=started_at,
                level=logging.WARNING,
                error_type=type(error).__name__,
                error_category=error.category,
                error_reason=error.reason_code or "-",
                code=error.opay_code or "-",
            )
            return attempt.frontend_response(
                message=attempt._append_opay_message(
                    _(
                        "OPay may have received this payment request. Its status "
                        "must be checked before another payment is created."
                    ),
                    error.opay_message,
                    verified=False,
                ),
                ambiguous=True,
            )

        attempt.write(
            {
                "order_no": result.order_no,
                "status": "waiting",
                "last_source": "create",
                "last_status_message": attempt._safe_message(result.message),
            }
        )
        configured_method.write(
            {
                "opay_latest_order_no": result.order_no,
                "opay_latest_status": "waiting",
            }
        )
        configured_method._opay_log_create_api(
            attempt,
            outcome="accepted",
            started_at=started_at,
            level=logging.INFO,
        )
        return attempt.frontend_response(
            message=attempt._append_opay_message(
                _(
                    "OPay accepted the payment request. Waiting for customer payment."
                ),
                result.message,
            )
        )

    def opay_get_payment_status(self, request):
        """Return/query the exact stored attempt identified by the payment UUID."""
        self.ensure_one()
        self._opay_check_pos_user()
        if self.use_payment_terminal != "opay":
            raise UserError(_("OPay status requests require an OPay payment method."))
        _reference, _out_order_no, session, attempt = (
            self._opay_find_status_attempt(request)
        )
        if not attempt:
            raise UserError(_("The OPay payment attempt could not be found."))
        attempt._opay_attach_session(session)
        return attempt.query_opay_status(reason="manual_check")

    def opay_resolve_create_outcome(self, request):
        """Resolve an interrupted Create RPC without ever creating a new order."""
        self.ensure_one()
        self._opay_check_pos_user()
        if self.use_payment_terminal != "opay":
            raise UserError(_("OPay status requests require an OPay payment method."))

        reference, out_order_no, session, attempt = self._opay_find_status_attempt(
            request
        )
        if not attempt:
            return self._opay_resolve_missing_attempt(
                reference, out_order_no, session
            )

        attempt._opay_attach_session(session)
        response = (
            attempt.frontend_response(reused=True)
            if attempt.status == "failed"
            else attempt.query_opay_status(reason="manual_create_recovery")
        )
        safe_retry_statuses = (attempt.FINAL_STATUSES - {"SUCCESS"}) | {"failed"}
        return {
            **response,
            "payment_method_id": self.id,
            "pos_session_id": session.id,
            "safe_to_retry": response["status"] in safe_retry_statuses,
            "local_attempt_missing": False,
            "recovery_state": "local_attempt_found",
        }

    def _opay_find_status_attempt(self, request):
        """Find one exact attempt after serializing against Create Payment."""
        self.ensure_one()
        if not isinstance(request, dict) or set(request) != {
            "reference",
            "session_id",
        }:
            raise UserError(_("Invalid OPay status request."))

        reference, out_order_no = self._opay_attempt_reference(request["reference"])
        session = self._opay_validate_session(request["session_id"])
        # Create Payment holds this same row lock until its transaction commits
        # or rolls back. This prevents an in-Odoo race, but a missing record after
        # rollback is not proof that OPay did not accept the external request.
        self.env.cr.execute(
            "SELECT id FROM pos_payment_method WHERE id = %s FOR UPDATE",
            [self.id],
        )
        configured_method = self.sudo()
        configured_method.invalidate_recordset(self._OPAY_RUNTIME_FIELDS)
        configured_method._opay_import_legacy_attempt()
        attempt = self.env["pos.opay.payment.attempt"].sudo().search(
            [
                ("payment_method_id", "=", self.id),
                ("payment_reference", "=", reference),
            ],
            limit=1,
        )
        return reference, out_order_no, session, attempt

    def _opay_resolve_missing_attempt(self, reference, out_order_no, session):
        """Query the deterministic OPay reference without assuming local absence."""
        self.ensure_one()
        configured_method = self.sudo()
        started_at = time.monotonic()
        try:
            client = OPayClient.from_payment_method(configured_method)
            result = client.query_payment(out_order_no=out_order_no)
            result = self._opay_validate_missing_attempt_query(
                result, out_order_no, client
            )
        except OPayOrderNotFoundError as error:
            configured_method._opay_log_missing_attempt_query(
                reference=reference,
                out_order_no=out_order_no,
                outcome="confirmed_absent",
                status="not_found",
                started_at=started_at,
                level=logging.INFO,
                code=error.code,
                error_type=type(error).__name__,
                error_category=error.category,
                error_reason=error.reason_code,
            )
            return self._opay_missing_attempt_not_found_response(
                reference,
                out_order_no,
                session,
                error.opay_message,
            )
        except OPayAPIError as error:
            # OPay's POS documentation does not define an authoritative
            # order-not-found response. No API rejection code/message can be
            # promoted to proof that creating another order is safe.
            configured_method._opay_log_missing_attempt_query(
                reference=reference,
                out_order_no=out_order_no,
                outcome="rejected",
                status="uncertain",
                started_at=started_at,
                level=logging.INFO,
                code=error.code,
                error_type=type(error).__name__,
                error_category=error.category,
            )
            return self._opay_missing_attempt_uncertain_response(
                reference,
                out_order_no,
                session,
                opay_message=error.opay_message,
                verified=True,
            )
        except (
            OPayConnectionError,
            OPayAuthenticationError,
            OPayConfigurationError,
            OPayMalformedResponseError,
            OPayError,
        ) as error:
            configured_method._opay_log_missing_attempt_query(
                reference=reference,
                out_order_no=out_order_no,
                outcome="uncertain",
                status="uncertain",
                started_at=started_at,
                level=logging.WARNING,
                error_type=type(error).__name__,
                error_category=error.category,
                error_reason=error.reason_code or "-",
                code=error.opay_code or "-",
            )
            return self._opay_missing_attempt_uncertain_response(
                reference,
                out_order_no,
                session,
                opay_message=error.opay_message,
                verified=False,
            )

        opay_status = result["status"]
        final_negative = opay_status in {"FAIL", "CLOSE", "CANCEL"}
        frontend_status = "uncertain" if opay_status == "SUCCESS" else opay_status
        messages = {
            "PENDING": _("The recovered OPay payment is still pending."),
            "SUCCESS": _(
                "OPay confirmed this payment order, but Odoo's local attempt "
                "record is unavailable. Do not create another payment."
            ),
            "FAIL": _("OPay confirmed that the recovered payment failed."),
            "CLOSE": _("OPay confirmed that the recovered payment was closed."),
            "CANCEL": _("OPay confirmed that the recovered payment was cancelled."),
        }
        configured_method._opay_log_missing_attempt_query(
            reference=reference,
            out_order_no=out_order_no,
            outcome="authenticated",
            status=opay_status,
            started_at=started_at,
            level=logging.INFO,
            order_no=result["orderNo"],
        )
        response_message = self.env["pos.opay.payment.attempt"]._append_opay_message(
            messages[opay_status],
            result.get(OPayClient.RESPONSE_MESSAGE_KEY),
        )
        return {
            "success": False,
            "status": frontend_status,
            "opay_status": opay_status,
            "message": response_message,
            "payment_method_id": self.id,
            "pos_session_id": session.id,
            "reference": reference,
            "out_order_no": out_order_no,
            "order_no": result["orderNo"],
            # A recovered SUCCESS cannot complete the Odoo payment because the
            # rolled-back local record contained the expected amount/correlation.
            "payment_completed": False,
            "ambiguous": not final_negative,
            "terminal_released": final_negative,
            "reused": True,
            "safe_to_retry": final_negative,
            "local_attempt_missing": True,
            "recovery_state": "opay_order_confirmed",
        }

    def _opay_validate_missing_attempt_query(
        self, result, out_order_no, client
    ):
        """Validate provider-owned fields available without a local attempt."""
        self.ensure_one()
        if not isinstance(result, dict):
            raise OPayMalformedResponseError("OPay returned invalid Query Order data.")
        expected_values = {
            "outOrderNo": out_order_no,
            "headMerchantId": client.head_merchant_id,
            "merchantId": client.merchant_id,
            "currency": OPayClient.CURRENCY,
            "sn": client.terminal_sn,
        }
        for field_name, expected_value in expected_values.items():
            value = result.get(field_name)
            if not isinstance(value, str) or value.strip() != expected_value:
                raise OPayMalformedResponseError(
                    f"OPay returned mismatched {field_name} data."
                )
        order_no = result.get("orderNo")
        status = result.get("status")
        if not isinstance(order_no, str) or not order_no.strip():
            raise OPayMalformedResponseError(
                "OPay Query Order did not return an order number."
            )
        status = status.strip() if isinstance(status, str) else status
        if status not in self.env["pos.opay.payment.attempt"].OPAY_STATUSES:
            raise OPayMalformedResponseError(
                "OPay Query Order returned an invalid payment status."
            )
        try:
            OPayClient.normalize_amount(result.get("amount"))
        except OPayConfigurationError as error:
            raise OPayMalformedResponseError(
                "OPay Query Order returned an invalid amount."
            ) from error
        return {
            **result,
            "orderNo": order_no.strip(),
            "status": status,
        }

    def _opay_missing_attempt_uncertain_response(
        self,
        reference,
        out_order_no,
        session,
        opay_message=None,
        verified=False,
    ):
        message = _(
            "The OPay payment could not be verified. Do not start another "
            "OPay payment until the current attempt is resolved."
        )
        message = self.env["pos.opay.payment.attempt"]._append_opay_message(
            message,
            opay_message,
            verified=verified,
        )
        return {
            "success": False,
            "status": "uncertain",
            "opay_status": False,
            "message": message,
            "payment_method_id": self.id,
            "pos_session_id": session.id,
            "reference": reference,
            "out_order_no": out_order_no,
            "order_no": False,
            "payment_completed": False,
            "ambiguous": True,
            "terminal_released": False,
            "reused": True,
            "safe_to_retry": False,
            "local_attempt_missing": True,
            "recovery_state": "opay_query_uncertain",
        }

    def _opay_missing_attempt_not_found_response(
        self,
        reference,
        out_order_no,
        session,
        opay_message,
    ):
        message = self.env["pos.opay.payment.attempt"]._append_opay_message(
            _(
                "OPay confirmed that this payment order does not exist. "
                "The payment may be tried again."
            ),
            opay_message,
        )
        return {
            "success": False,
            "status": "not_found",
            "opay_status": False,
            "message": message,
            "payment_method_id": self.id,
            "pos_session_id": session.id,
            "reference": reference,
            "out_order_no": out_order_no,
            "order_no": False,
            "payment_completed": False,
            "ambiguous": False,
            "terminal_released": True,
            "reused": True,
            "safe_to_retry": True,
            "local_attempt_missing": True,
            "recovery_state": "opay_order_confirmed_absent",
        }

    def _opay_attempt_reference(self, reference):
        if not isinstance(reference, str) or not reference.strip():
            raise UserError(_("The OPay payment reference is required."))
        try:
            payment_uuid = UUID(reference.strip())
        except (AttributeError, TypeError, ValueError) as error:
            raise UserError(_("The OPay payment reference must be a valid UUID.")) from error
        return str(payment_uuid), payment_uuid.hex.upper()

    def _opay_check_pos_user(self):
        self.ensure_one()
        if not self.env.su and not self.env.user.has_group(
            "point_of_sale.group_pos_user"
        ):
            raise AccessDenied()

    def _opay_validate_session(self, session_id):
        self.ensure_one()
        if isinstance(session_id, bool) or not isinstance(session_id, int):
            raise UserError(_("A valid POS session is required for OPay payments."))
        session = self.env["pos.session"].browse(session_id).exists()
        if not session:
            raise UserError(_("The POS session could not be found."))
        session.check_access("read")
        session_sudo = session.sudo()
        if (
            session_sudo.state not in {"opening_control", "opened"}
            or self.sudo() not in session_sudo.payment_method_ids
            or session_sudo.company_id != self.company_id
        ):
            raise UserError(
                _("This OPay payment method is not available in the POS session.")
            )
        return session_sudo

    def _opay_import_legacy_attempt(self):
        """Lazily preserve an active Phase-5 attempt during a module upgrade."""
        self.ensure_one()
        if not self.opay_latest_payment_reference or not self.opay_latest_out_order_no:
            return self.env["pos.opay.payment.attempt"]
        attempt_model = self.env["pos.opay.payment.attempt"].sudo()
        existing = attempt_model.search(
            [("out_order_no", "=", self.opay_latest_out_order_no)], limit=1
        )
        if existing:
            return existing
        try:
            amount = OPayClient.normalize_amount(self.opay_latest_amount)
        except OPayConfigurationError:
            return attempt_model
        status = self.opay_latest_status
        valid_statuses = attempt_model.ACTIVE_STATUSES | attempt_model.FINAL_STATUSES | {
            "failed"
        }
        if status not in valid_statuses:
            status = "uncertain"
        base_time = self.write_date or fields.Datetime.now()
        return attempt_model.create(
            {
                **attempt_model._new_attempt_values(
                    self,
                    self.opay_latest_payment_reference,
                    self.opay_latest_out_order_no,
                    amount,
                ),
                "order_no": self.opay_latest_order_no,
                "status": status,
                "expires_at": base_time
                + timedelta(seconds=OPayClient.DEFAULT_ORDER_EXPIRE_TIME),
            }
        )

    def _opay_clear_latest_attempt(self, attempt):
        self.ensure_one()
        if self.sudo().opay_latest_out_order_no != attempt.out_order_no:
            return
        self.sudo().write(
            {
                "opay_latest_payment_reference": False,
                "opay_latest_out_order_no": False,
                "opay_latest_order_no": False,
                "opay_latest_amount": False,
                "opay_latest_status": False,
            }
        )

    def _opay_log_create_api(
        self,
        attempt,
        *,
        outcome,
        started_at,
        level,
        code="-",
        error_type="-",
        error_category="-",
        error_reason="-",
    ):
        self.ensure_one()
        _logger.log(
            level,
            "event=opay_create_payment_api outcome=%s source=create "
            "reason=payment_request payment_method_id=%s attempt_id=%s "
            "reference=%s out_order_no=%s order_no=%s terminal_sn=%s "
            "status=%s error_type=%s code=%s error_category=%s error_reason=%s "
            "duration_ms=%s",
            outcome,
            self.id,
            attempt.id,
            attempt.payment_reference,
            attempt.out_order_no,
            attempt.order_no or "-",
            self.opay_terminal_sn,
            attempt.status,
            error_type,
            code,
            error_category,
            error_reason,
            self._opay_duration_ms(started_at),
        )

    def _opay_log_missing_attempt_query(
        self,
        *,
        reference,
        out_order_no,
        outcome,
        status,
        started_at,
        level,
        order_no="-",
        code="-",
        error_type="-",
        error_category="-",
        error_reason="-",
    ):
        self.ensure_one()
        _logger.log(
            level,
            "event=opay_query_order_api outcome=%s source=query "
            "reason=manual_create_recovery payment_method_id=%s attempt_id=- "
            "reference=%s out_order_no=%s order_no=%s terminal_sn=%s "
            "status=%s error_type=%s code=%s error_category=%s error_reason=%s "
            "duration_ms=%s",
            outcome,
            self.id,
            reference,
            out_order_no,
            order_no,
            self.opay_terminal_sn,
            status,
            error_type,
            code,
            error_category,
            error_reason,
            self._opay_duration_ms(started_at),
        )

    @staticmethod
    def _opay_duration_ms(started_at):
        return max(0, round((time.monotonic() - started_at) * 1000))

    @staticmethod
    def _opay_failed_response(reference, out_order_no, message, reused=False):
        return {
            "success": False,
            "status": "failed",
            "message": message,
            "reference": reference,
            "out_order_no": out_order_no,
            "order_no": False,
            "payment_completed": False,
            "ambiguous": False,
            "reused": reused,
        }
