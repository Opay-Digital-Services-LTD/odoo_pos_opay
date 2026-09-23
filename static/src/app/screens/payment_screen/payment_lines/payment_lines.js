import { onWillUnmount, useState } from "@odoo/owl";
import { patch } from "@web/core/utils/patch";
import { PaymentScreenPaymentLines } from "@point_of_sale/app/screens/payment_screen/payment_lines/payment_lines";
import { getOpayUiState, MANUAL_STATUS_CHECK_COOLDOWN_MS } from "@pos_opay/app/utils/payment/payment_opay";

const QUERYABLE_PAYMENT_STATUSES = new Set([
    "waitingCard",
    "waiting",
    "waitingCancel",
    "timeout",
    "retry",
    "force_done",
    "waitingCapture",
]);

export function isQueryableOpayPaymentLine(line) {
    return Boolean(
        line?.payment_method_id?.use_payment_terminal === "opay" &&
            !line.is_done() &&
            QUERYABLE_PAYMENT_STATUSES.has(line.get_payment_status())
    );
}

patch(PaymentScreenPaymentLines.prototype, {
    setup() {
        super.setup(...arguments);
        this.opayStatusChecks = useState({});
        this.opayStatusCooldownTimers = {};
        onWillUnmount(() => {
            for (const timer of Object.values(this.opayStatusCooldownTimers)) {
                clearTimeout(timer);
            }
        });
    },

    canCheckOpayStatus(line) {
        return isQueryableOpayPaymentLine(line);
    },

    isOpayTerminalBlocked(line) {
        return line?.payment_method_id?.use_payment_terminal === "opay" &&
            getOpayUiState(line).opayTerminalBlocked === true;
    },

    isOpayRetrySafe(line) {
        return getOpayUiState(line).opayRetrySafe === true;
    },

    isCheckingOpayStatus(line) {
        return Boolean(this.opayStatusChecks[line.uuid]?.inProgress);
    },

    isOpayStatusCheckDisabled(line) {
        const state = this.opayStatusChecks[line.uuid];
        return Boolean(state?.inProgress || state?.coolingDown);
    },

    async checkOpayPaymentStatus(line) {
        if (
            !this.canCheckOpayStatus(line) ||
            this.isOpayStatusCheckDisabled(line)
        ) {
            return false;
        }

        this.opayStatusChecks[line.uuid] = {
            inProgress: true,
            coolingDown: true,
        };
        // Mutate the reactive proxy, not the raw object assigned above.
        const state = this.opayStatusChecks[line.uuid];
        this.opayStatusCooldownTimers[line.uuid] = setTimeout(() => {
            state.coolingDown = false;
            delete this.opayStatusCooldownTimers[line.uuid];
        }, MANUAL_STATUS_CHECK_COOLDOWN_MS);

        try {
            return await line.payment_method_id.payment_terminal.checkPaymentStatus(
                line.uuid
            );
        } finally {
            state.inProgress = false;
        }
    },
});
