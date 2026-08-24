import { _t } from "@web/core/l10n/translation";
import { PaymentInterface } from "@point_of_sale/app/utils/payment/payment_interface";
import { register_payment_method } from "@point_of_sale/app/services/pos_store";

const WAITING_STATUSES = new Set(["waiting", "uncertain", "PENDING"]);
const FAILED_STATUSES = new Set([
    "failed",
    "not_found",
    "FAIL",
    "CLOSE",
    "CANCEL",
]);
export const MANUAL_STATUS_CHECK_COOLDOWN_MS = 4000;

export class PaymentOpay extends PaymentInterface {
    setup() {
        super.setup(...arguments);
        this.paymentLineResolvers = {};
        this.recoveryRequests = {};
        this.createOutcomeRecoveryRequests = {};
        this.manualStatusCheckRequests = {};
        this.manualStatusCheckCooldowns = {};
    }

    async sendPaymentRequest(uuid) {
        super.sendPaymentRequest(uuid);
        const paymentLine = this._findPaymentLine(uuid);
        if (!paymentLine) {
            this._showFailure(_t("The OPay payment line could not be found."));
            return false;
        }
        // Install the resolver before the RPC. A terminal can complete quickly
        // enough for its WebSocket notification to beat the Create response.
        const paymentConfirmation = this._waitForPaymentConfirmation(uuid);

        let response;
        try {
            response = await this.pos.data.call(
                "pos.payment.method",
                "opay_create_payment_request",
                [
                    [this.payment_method_id.id],
                    {
                        reference: uuid,
                        amount: paymentLine.getAmount(),
                        session_id: this.pos.session.id,
                    },
                ]
            );
        } catch {
            paymentLine.setPaymentStatus("waitingCard");
            this._showStatus(
                _t(
                    "The OPay payment could not be verified. Do not start another " +
                        "OPay payment until the current attempt is resolved."
                ),
                "warning"
            );
            return paymentConfirmation;
        }

        if (!this._isCorrelated(paymentLine, response)) {
            delete this.paymentLineResolvers[uuid];
            paymentLine.setPaymentStatus("retry");
            this._showFailure(_t("Odoo returned a mismatched OPay payment response."));
            return false;
        }
        this._storeReferences(paymentLine, response);

        // A correlated WebSocket notification may have settled the attempt
        // while the Create RPC was still returning.
        if (!this.paymentLineResolvers[uuid]) {
            return paymentLine.isDone();
        }

        if (response?.status === "SUCCESS" && response.payment_completed === true) {
            delete this.paymentLineResolvers[uuid];
            this._showStatus(response.message, "success");
            return true;
        }
        if (WAITING_STATUSES.has(response?.status)) {
            paymentLine.setPaymentStatus("waitingCard");
            this._showStatus(
                response.message ||
                    _t("OPay accepted the payment request. Waiting for customer payment."),
                "warning"
            );
            return paymentConfirmation;
        }

        delete this.paymentLineResolvers[uuid];
        paymentLine.setPaymentStatus("retry");
        this._showFailure(
            response?.message || _t("The OPay payment request was rejected by Odoo.")
        );
        return false;
    }

    async sendPaymentCancel(order, uuid) {
        super.sendPaymentCancel(order, uuid);
        const paymentLine = this._findPaymentLine(uuid);
        if (!paymentLine) {
            return false;
        }
        if (paymentLine.isDone()) {
            return false;
        }
        if (paymentLine.getPaymentStatus() === "retry") {
            return true;
        }

        paymentLine.setPaymentStatus("waitingCard");
        this._showStatus(
            _t(
                "The OPay payment remains active. Use Check Payment Status after " +
                    "the terminal reaches a final state."
            ),
            "warning"
        );
        return false;
    }

    async recoverPayment(uuid) {
        if (this.recoveryRequests[uuid]) {
            return this.recoveryRequests[uuid];
        }
        const paymentLine = this._findPaymentLine(uuid);
        if (!paymentLine) {
            return false;
        }

        this.recoveryRequests[uuid] = this.pos.data
            .silentCall("pos.payment.method", "opay_get_payment_status", [
                [this.payment_method_id.id],
                {
                    reference: uuid,
                    session_id: this.pos.session.id,
                },
            ])
            .then((response) => {
                this.handleOpayStatusResponse(response);
                return response;
            })
            .catch(() => {
                paymentLine.setPaymentStatus("waitingCard");
                return false;
            })
            .finally(() => {
                delete this.recoveryRequests[uuid];
            });
        return this.recoveryRequests[uuid];
    }

    async resolveCreateOutcome(uuid) {
        if (this.createOutcomeRecoveryRequests[uuid]) {
            return this.createOutcomeRecoveryRequests[uuid];
        }
        const paymentLine = this._findPaymentLine(uuid);
        if (!paymentLine) {
            return false;
        }

        this.createOutcomeRecoveryRequests[uuid] = this.pos.data
            .silentCall("pos.payment.method", "opay_resolve_create_outcome", [
                [this.payment_method_id.id],
                {
                    reference: uuid,
                    session_id: this.pos.session.id,
                },
            ])
            .then((response) => {
                if (!this._isCorrelated(paymentLine, response)) {
                    paymentLine.setPaymentStatus("waitingCard");
                    return false;
                }
                this.handleOpayStatusResponse(response);
                return response;
            })
            .catch(() => {
                paymentLine.setPaymentStatus("waitingCard");
                return false;
            })
            .finally(() => {
                delete this.createOutcomeRecoveryRequests[uuid];
            });
        return this.createOutcomeRecoveryRequests[uuid];
    }

    async checkPaymentStatus(uuid) {
        const now = Date.now();
        if (
            this.manualStatusCheckRequests[uuid] ||
            now < (this.manualStatusCheckCooldowns[uuid] || 0)
        ) {
            return false;
        }
        this.manualStatusCheckCooldowns[uuid] =
            now + MANUAL_STATUS_CHECK_COOLDOWN_MS;

        const request = this._checkPaymentStatus(uuid);
        this.manualStatusCheckRequests[uuid] = request;
        try {
            return await request;
        } finally {
            delete this.manualStatusCheckRequests[uuid];
        }
    }

    async _checkPaymentStatus(uuid) {
        const paymentLine = this._findPaymentLine(uuid);
        if (!paymentLine) {
            return false;
        }
        // A lost Create RPC may leave the browser without OPay references.
        // Manual recovery reconstructs/correlates them on the server; it never
        // issues another Create Payment request.
        const response = paymentLine.payment_ref_no
            ? await this.recoverPayment(uuid)
            : await this.resolveCreateOutcome(uuid);
        if (!response || response.status === "uncertain") {
            this._showFailure(
                _t("Unable to verify the payment status. Please try again.")
            );
            return response;
        }
        if (WAITING_STATUSES.has(response.status)) {
            this._showStatus(
                response.message || _t("OPay payment is still pending."),
                "warning"
            );
        }
        return response;
    }

    handleOpayStatusResponse(response) {
        const paymentLine = this._findPaymentLine(response?.reference);
        if (!paymentLine || !this._isCorrelated(paymentLine, response)) {
            return false;
        }
        this._storeReferences(paymentLine, response);

        // Backend finalization is authoritative and first-final-wins. Keep the
        // browser equally defensive if a delayed/conflicting event arrives.
        if (paymentLine.isDone()) {
            return response.status === "SUCCESS" && response.payment_completed === true;
        }

        if (response.status === "SUCCESS" && response.payment_completed === true) {
            this._showStatus(response.message, "success");
            this._settlePaymentLine(paymentLine, true);
            return true;
        }
        if (FAILED_STATUSES.has(response.status)) {
            this._showFailure(response.message || _t("The OPay payment failed."));
            this._settlePaymentLine(paymentLine, false);
            return false;
        }
        if (WAITING_STATUSES.has(response.status)) {
            paymentLine.setPaymentStatus("waitingCard");
        }
        return false;
    }

    _waitForPaymentConfirmation(uuid) {
        return new Promise((resolve) => {
            this.paymentLineResolvers[uuid] = resolve;
        });
    }

    _settlePaymentLine(paymentLine, successful) {
        const resolver = this.paymentLineResolvers[paymentLine.uuid];
        paymentLine.handlePaymentResponse(successful);
        if (resolver) {
            delete this.paymentLineResolvers[paymentLine.uuid];
            resolver(successful);
        }
    }

    _findPaymentLine(uuid) {
        if (!uuid) {
            return false;
        }
        for (const order of this.pos.models["pos.order"].getAll()) {
            const paymentLine = order.payment_ids.find((line) => line.uuid === uuid);
            if (paymentLine) {
                return paymentLine;
            }
        }
        return false;
    }

    _isCorrelated(paymentLine, response) {
        if (!response || response.reference !== paymentLine.uuid) {
            return false;
        }
        if (
            response.payment_method_id &&
            response.payment_method_id !== this.payment_method_id.id
        ) {
            return false;
        }
        if (response.pos_session_id && response.pos_session_id !== this.pos.session.id) {
            return false;
        }
        if (
            paymentLine.payment_ref_no &&
            response.out_order_no &&
            paymentLine.payment_ref_no !== response.out_order_no
        ) {
            return false;
        }
        return !(
            paymentLine.transaction_id &&
            response.order_no &&
            paymentLine.transaction_id !== response.order_no
        );
    }

    _storeReferences(paymentLine, response) {
        if (response?.out_order_no) {
            paymentLine.payment_ref_no = response.out_order_no;
        }
        if (response?.order_no) {
            paymentLine.transaction_id = response.order_no;
        }
    }

    _showStatus(message, type) {
        if (!message) {
            return;
        }
        this.pos.notification.add(message, {
            title: _t("OPay Payment"),
            type,
        });
    }

    _showFailure(message) {
        this._showStatus(message, "danger");
    }
}

register_payment_method("opay", PaymentOpay);
