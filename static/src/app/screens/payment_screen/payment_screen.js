import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { getOpayUiState } from "@pos_opay/app/utils/payment/payment_opay";

const UNRESOLVED_OPAY_STATUSES = new Set(["waiting", "waitingCard", "timeout"]);

patch(PaymentScreen.prototype, {
    async sendPaymentRequest(line) {
        if (line?.payment_method_id?.use_payment_terminal !== "opay") {
            return super.sendPaymentRequest(...arguments);
        }
        if (line.get_payment_status() === "retry") {
            return this.retryOpayPayment(line);
        }
        try {
            return await super.sendPaymentRequest(...arguments);
        } catch {
            // A browser-side exception is not evidence that OPay rejected Create.
            // Keep the exact line recoverable instead of exposing an RPC traceback
            // or allowing another request.
            const confirmed = line.get_payment_status() === "done";
            if (!confirmed) {
                line.set_payment_status("waitingCard");
            }
            this.notification.add(
                confirmed
                    ? _t("OPay confirmed the payment. Checkout could not finish; please try Validate again.")
                    : _t("The payment status could not be displayed. Use Check Payment Status before trying another payment."),
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
            line.get_payment_status() !== "retry" ||
            line.is_done() ||
            !this.paymentLines.some((payment) => payment.uuid === uuid)
        ) {
            return false;
        }
        if (!getOpayUiState(line).opayRetrySafe) {
            this.notification.add(
                _t("Check Payment Status before retrying this OPay payment."),
                { title: _t("OPay Payment"), type: "warning" }
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
            // The historical attempt keeps its UUID and outOrderNo. Odoo
            // gives the replacement POS line a new UUID for a fresh request.
            this.deletePaymentLine(uuid);
            if (this.paymentLines.some((payment) => payment.uuid === uuid)) {
                return false;
            }
            const added = await this.addNewPaymentLine(paymentMethod);
            if (!added) {
                this.notification.add(
                    _t("A new OPay payment could not be started. Select OPay again when ready."),
                    { title: _t("OPay Payment"), type: "warning" }
                );
            }
            return added;
        } catch {
            this.notification.add(
                _t("A new OPay payment could not be started. Check this sale before trying again."),
                { title: _t("OPay Payment"), type: "warning" }
            );
            return false;
        } finally {
            this.opayRetriesInProgress.delete(uuid);
        }
    },

    async addNewPaymentLine(paymentMethod) {
        const existingOpayLine = this.paymentLines.find(
            (line) =>
                line.payment_method_id?.use_payment_terminal === "opay" && !line.is_done()
        );
        if (existingOpayLine) {
            this.notification.add(
                existingOpayLine.get_payment_status() === "retry" &&
                    getOpayUiState(existingOpayLine).opayRetrySafe
                    ? _t("Use Retry OPay Payment on the existing payment line.")
                    : _t("An OPay payment is unresolved. Use Check Payment Status before adding another payment."),
                { title: _t("OPay Payment"), type: "warning" }
            );
            return false;
        }
        return super.addNewPaymentLine(...arguments);
    },

    sendForceDone(line) {
        if (line?.payment_method_id?.use_payment_terminal === "opay") {
            this.notification.add(
                _t("Only a confirmed OPay payment can be completed. Use Check Payment Status."),
                { title: _t("OPay Payment"), type: "warning" }
            );
            return false;
        }
        return super.sendForceDone(...arguments);
    },

    deletePaymentLine(uuid) {
        const paymentLine = this.paymentLines.find((line) => line.uuid === uuid);
        const isOpay =
            paymentLine?.payment_method_id?.use_payment_terminal === "opay";
        if (isOpay && paymentLine.is_done()) {
            return;
        }
        if (isOpay && UNRESOLVED_OPAY_STATUSES.has(paymentLine.get_payment_status())) {
            paymentLine.set_payment_status("waitingCancel");
            Promise.resolve()
                .then(() =>
                    paymentLine.payment_method_id.payment_terminal.send_payment_cancel(
                        this.currentOrder,
                        uuid
                    )
                )
                .then((cancelled) => {
                    if (cancelled) {
                        this.currentOrder.remove_paymentline(paymentLine);
                        this.numberBuffer.reset();
                    }
                })
                .catch(() => {
                    paymentLine.set_payment_status("waitingCard");
                });
            return;
        }
        return super.deletePaymentLine(...arguments);
    },
});
