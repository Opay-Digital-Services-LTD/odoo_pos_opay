import { _t } from "@web/core/l10n/translation";
import { useState } from "@odoo/owl";
import { patch } from "@web/core/utils/patch";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { getOpayUiState, OPAY_NOTIFICATION_DELAY_MS } from "@pos_opay/app/utils/payment/payment_opay";

patch(PaymentScreen.prototype, {
    setup() {
        super.setup(...arguments);
        this.opayReview = useState({ open: false, loading: false, checking: null, payments: [] });
    },

    get opayPaymentMethods() {
        return this.payment_methods_from_config.filter(
            (method) => method.use_payment_terminal === "opay"
        );
    },

    async toggleOpayReview() {
        this.opayReview.open = !this.opayReview.open;
        if (this.opayReview.open) {
            await this.loadOpayPaymentsToReview();
        }
    },

    async loadOpayPaymentsToReview() {
        if (this.opayReview.loading) {
            return;
        }
        this.opayReview.loading = true;
        try {
            const results = await Promise.all(this.opayPaymentMethods.map(async (method) => {
                const payments = await this.pos.data.silentCall(
                    "pos.payment.method", "opay_list_payments_to_review", [
                        [method.id], { session_id: this.pos.session.id },
                    ]
                );
                return payments.map((payment) => ({ ...payment, methodName: method.name }));
            }));
            this.opayReview.payments = results.flat();
        } catch {
            this.notification.add(
                _t("Earlier OPay payments could not be loaded. Please try again."),
                { title: _t("OPay Payment"), type: "warning", autocloseDelay: OPAY_NOTIFICATION_DELAY_MS }
            );
        } finally {
            this.opayReview.loading = false;
        }
    },

    async checkOpayPaymentToReview(payment) {
        if (this.opayReview.checking !== null) {
            return;
        }
        this.opayReview.checking = payment.id;
        try {
            const response = await this.pos.data.silentCall(
                "pos.payment.method", "opay_check_payment_to_review", [
                    [payment.payment_method_id],
                    { session_id: this.pos.session.id, attempt_id: payment.id },
                ]
            );
            if (
                response.payment_method_id !== payment.payment_method_id ||
                response.out_order_no !== payment.out_order_no
            ) {
                throw new Error("Mismatched OPay review result");
            }
            const method = this.opayPaymentMethods.find(
                (candidate) => candidate.id === payment.payment_method_id
            );
            method?.payment_terminal?.handleOpayStatusResponse(response);
            this.notification.add(response.message, {
                title: _t("Earlier OPay Payment"),
                type: response.status === "SUCCESS" ? "danger" :
                    ["FAIL", "CLOSE", "CANCEL", "failed"].includes(response.status) ? "success" : "warning",
                sticky: response.status === "SUCCESS",
                autocloseDelay: OPAY_NOTIFICATION_DELAY_MS,
            });
            await this.loadOpayPaymentsToReview();
        } catch {
            this.notification.add(
                _t("The earlier payment could not be checked. It has not been marked paid or cancelled."),
                { title: _t("OPay Payment"), type: "warning", autocloseDelay: OPAY_NOTIFICATION_DELAY_MS }
            );
        } finally {
            this.opayReview.checking = null;
        }
    },

    async sendPaymentRequest(line) {
        if (line?.payment_method_id?.use_payment_terminal !== "opay") {
            return super.sendPaymentRequest(...arguments);
        }
        if (line.getPaymentStatus() === "retry") {
            return this.retryOpayPayment(line);
        }
        try {
            return await super.sendPaymentRequest(...arguments);
        } catch {
            // A browser-side exception is not evidence that OPay rejected Create.
            // Keep the exact line recoverable instead of exposing an RPC traceback
            // or allowing another request.
            const confirmed = line.getPaymentStatus() === "done";
            if (!confirmed) {
                line.setPaymentStatus("waitingCard");
            }
            this.notification.add(
                confirmed
                    ? _t("OPay confirmed the payment. Checkout could not finish; please try Validate again.")
                    : _t("The payment status could not be displayed. Check this sale's payment status before retrying it."),
                { title: _t("OPay Payment"), type: "warning", sticky: true }
            );
            return false;
        } finally {
            this.pos.paymentTerminalInProgress = false;
        }
    },

    async retryOpayPayment(line) {
        const uuid = line?.uuid;
        if (
            !uuid ||
            line.payment_method_id?.use_payment_terminal !== "opay" ||
            line.getPaymentStatus() !== "retry" ||
            line.isDone() ||
            !this.paymentLines.some((payment) => payment.uuid === uuid)
        ) {
            return false;
        }
        if (!getOpayUiState(line).opayRetrySafe) {
            this.notification.add(
                _t("Check Payment Status before retrying this OPay payment."),
                { title: _t("OPay Payment"), type: "warning", autocloseDelay: OPAY_NOTIFICATION_DELAY_MS }
            );
            return false;
        }
        if (!this.opayRetriesInProgress) {
            this.opayRetriesInProgress = new Set();
        }
        if (this.opayRetriesInProgress.has(uuid)) {
            return false;
        }
        this.opayRetriesInProgress.add(uuid);
        try {
            const paymentMethod = line.payment_method_id;
            // Retain the previous line's stable UUID in its historical attempt.
            // A fresh POS line receives a fresh UUID and therefore a fresh
            // server-derived outOrderNo; never resend Create for the old UUID.
            this.deletePaymentLine(uuid);
            if (this.paymentLines.some((payment) => payment.uuid === uuid)) {
                return false;
            }
            const added = await this.addNewPaymentLine(paymentMethod);
            if (!added) {
                this.notification.add(
                    _t("A new OPay payment could not be started. Select OPay again when ready."),
                    { title: _t("OPay Payment"), type: "warning", autocloseDelay: OPAY_NOTIFICATION_DELAY_MS }
                );
            }
            return added;
        } catch {
            this.notification.add(
                _t("A new OPay payment could not be started. Check this sale before trying again."),
                { title: _t("OPay Payment"), type: "warning", autocloseDelay: OPAY_NOTIFICATION_DELAY_MS }
            );
            return false;
        } finally {
            this.opayRetriesInProgress.delete(uuid);
        }
    },

    async addNewPaymentLine(paymentMethod) {
        const existingOpayLine = this.paymentLines.find(
            (line) =>
                line.payment_method_id?.use_payment_terminal === "opay" && !line.isDone()
        );
        if (existingOpayLine) {
            this.notification.add(
                existingOpayLine.getPaymentStatus() === "retry" &&
                    getOpayUiState(existingOpayLine).opayRetrySafe
                    ? _t("Use Retry OPay Payment on the existing payment line.")
                    : _t("An OPay payment is unresolved. Use Check Payment Status before adding another payment."),
                { title: _t("OPay Payment"), type: "warning", autocloseDelay: OPAY_NOTIFICATION_DELAY_MS }
            );
            return false;
        }
        return super.addNewPaymentLine(...arguments);
    },

    sendForceDone(line) {
        if (line?.payment_method_id?.use_payment_terminal === "opay") {
            this.notification.add(
                _t("Only a confirmed OPay payment can be completed. Use Check Payment Status."),
                { title: _t("OPay Payment"), type: "warning", autocloseDelay: OPAY_NOTIFICATION_DELAY_MS }
            );
            return false;
        }
        return super.sendForceDone(...arguments);
    },
});
