import { PosPayment } from "@point_of_sale/app/models/pos_payment";
import { PosStore } from "@point_of_sale/app/store/pos_store";
import { PaymentOpay } from "@pos_opay/app/utils/payment/payment_opay";

let nextReference = 1;

export function makeOpayHarness() {
    const notifications = [];
    const calls = [];
    const rpcHandlers = {};
    const orders = [];
    const pos = {
        env: {},
        session: { id: 18 },
        notification: {
            add(message, options) {
                notifications.push({ message, options });
            },
        },
        data: {
            call(model, method, args) {
                calls.push({ mode: "call", model, method, args });
                return rpcHandlers[method]?.(args);
            },
            silentCall(model, method, args) {
                calls.push({ mode: "silentCall", model, method, args });
                return Promise.resolve().then(() => rpcHandlers[method]?.(args));
            },
            connectWebSocket() {},
        },
        models: {
            "pos.order": {
                getAll() {
                    return orders;
                },
            },
        },
    };
    Object.setPrototypeOf(pos, PosStore.prototype);
    const paymentMethod = {
        id: 4,
        name: "OPay",
        payment_method_type: "terminal",
        use_payment_terminal: "opay",
    };
    const nonOpayPaymentMethod = {
        id: 5,
        name: "Other Terminal",
        payment_method_type: "terminal",
        use_payment_terminal: "other",
        payment_terminal: {
            send_payment_cancel() {
                return Promise.resolve(false);
            },
        },
    };
    paymentMethod.payment_terminal = new PaymentOpay(pos, paymentMethod);

    function addPaymentLine(method = paymentMethod, amount = 12.5) {
        const line = {
            uuid: `00000000-0000-4000-8000-${String(nextReference++).padStart(12, "0")}`,
            amount,
            payment_status: undefined,
            payment_ref_no: false,
            transaction_id: false,
            payment_method_id: method,
            update(values) {
                Object.assign(this, values);
            },
            set_payment_status: PosPayment.prototype.set_payment_status,
            get_payment_status: PosPayment.prototype.get_payment_status,
            get_amount: PosPayment.prototype.get_amount,
            is_done: PosPayment.prototype.is_done,
            handle_payment_response: PosPayment.prototype.handle_payment_response,
            pay: PosPayment.prototype.pay,
        };
        order.payment_ids.push(line);
        return line;
    }

    const order = {
        payment_ids: [],
        remove_paymentline(line) {
            const index = this.payment_ids.indexOf(line);
            if (index >= 0) {
                this.payment_ids.splice(index, 1);
            }
        },
    };
    orders.push(order);

    return {
        pos,
        order,
        paymentMethod,
        nonOpayPaymentMethod,
        notifications,
        calls,
        rpcHandlers,
        addPaymentLine,
    };
}

export function correlatedResponse(harness, line, values = {}) {
    return {
        success: false,
        status: "PENDING",
        message: "The OPay payment is still pending.",
        payment_method_id: harness.paymentMethod.id,
        pos_session_id: harness.pos.session.id,
        reference: line.uuid,
        out_order_no: line.payment_ref_no || "EXACT-OUT-ORDER",
        order_no: line.transaction_id || "EXACT-OPAY-ORDER",
        payment_completed: false,
        safe_to_retry: false,
        ...values,
    };
}
