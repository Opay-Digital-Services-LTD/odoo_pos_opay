import { patch } from "@web/core/utils/patch";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";

const UNRESOLVED_OPAY_STATUSES = new Set(["waiting", "waitingCard", "timeout"]);

patch(PaymentScreen.prototype, {
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
