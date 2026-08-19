import { patch } from "@web/core/utils/patch";
import { PosStore } from "@point_of_sale/app/services/pos_store";

patch(PosStore.prototype, {
    async setup() {
        await super.setup(...arguments);
        this.data.connectWebSocket("OPAY_PAYMENT_STATUS", (notification) => {
            this.handleOpayPaymentStatus(notification);
        });
    },

    handleOpayPaymentStatus(notification) {
        if (notification?.pos_session_id !== this.session.id) {
            return false;
        }
        const paymentLine = this.findOpayPaymentLine(notification.reference);
        if (
            !paymentLine ||
            paymentLine.payment_method_id.id !== notification.payment_method_id
        ) {
            return false;
        }
        return paymentLine.payment_method_id.payment_terminal.handleOpayStatusResponse(
            notification
        );
    },

    findOpayPaymentLine(reference) {
        for (const order of this.models["pos.order"].getAll()) {
            const paymentLine = order.payment_ids.find(
                (line) =>
                    line.uuid === reference &&
                    line.payment_method_id.use_payment_terminal === "opay"
            );
            if (paymentLine) {
                return paymentLine;
            }
        }
        return false;
    },
});
