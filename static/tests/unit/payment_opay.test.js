import { expect, test } from "@odoo/hoot";
import { advanceTime, Deferred } from "@odoo/hoot-mock";
import { PaymentInterface } from "@point_of_sale/app/payment/payment_interface";
import { PosPayment } from "@point_of_sale/app/models/pos_payment";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { PaymentScreenPaymentLines } from "@point_of_sale/app/screens/payment_screen/payment_lines/payment_lines";
import { PosStore } from "@point_of_sale/app/store/pos_store";
import { PaymentOpay } from "@pos_opay/app/utils/payment/payment_opay";
import { isQueryableOpayPaymentLine } from "@pos_opay/app/screens/payment_screen/payment_lines/payment_lines";
import "@pos_opay/app/screens/payment_screen/payment_screen";
import "@pos_opay/app/services/pos_store";
import {
    correlatedResponse,
    makeOpayHarness,
} from "@pos_opay/../tests/unit/data/pos_payment_method.data";

const tick = () => Promise.resolve();

function makePaymentLinesComponent() {
    const component = Object.create(PaymentScreenPaymentLines.prototype);
    component.opayStatusChecks = {};
    component.opayStatusCooldownTimers = {};
    return component;
}

function makePaymentScreen(harness, resetCounter = { count: 0 }) {
    return {
        paymentLines: harness.order.payment_ids,
        currentOrder: harness.order,
        numberBuffer: { reset: () => resetCounter.count++ },
    };
}

test("registers OPay through the native Odoo 18 PaymentInterface registry", () => {
    const harness = makeOpayHarness();
    expect(PosStore.prototype.electronic_payment_interfaces.opay).toBe(PaymentOpay);
    expect(harness.paymentMethod.payment_terminal).toBeInstanceOf(PaymentOpay);
    expect(PaymentInterface.prototype.isPrototypeOf(PaymentOpay.prototype)).toBe(true);
    expect(PaymentOpay.prototype.send_payment_request).toBeDefined();
    expect(PaymentOpay.prototype.send_payment_cancel).toBeDefined();
});

test("native PosPayment.pay invokes send_payment_request with the exact UUID", async () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    harness.rpcHandlers.opay_create_payment_request = (args) => {
        expect(args).toEqual([
            [harness.paymentMethod.id],
            { reference: line.uuid, amount: line.get_amount(), session_id: harness.pos.session.id },
        ]);
        return correlatedResponse(harness, line, { success: true, status: "waiting" });
    };

    const payment = PosPayment.prototype.pay.call(line);
    await tick();
    expect(harness.calls).toHaveLength(1);
    expect(line.get_payment_status()).toBe("waitingCard");
    expect(line.is_done()).toBe(false);
    expect(line.payment_ref_no).toBe("EXACT-OUT-ORDER");
    harness.paymentMethod.payment_terminal.handleOpayStatusResponse(
        correlatedResponse(harness, line, { success: true, status: "SUCCESS", payment_completed: true })
    );
    await expect(payment).resolves.toBe(true);
});

test("Create acceptance and orderNo remain waiting rather than paid", async () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    harness.rpcHandlers.opay_create_payment_request = () =>
        correlatedResponse(harness, line, {
            success: true,
            status: "waiting",
            order_no: "OPAY-CREATED-NOT-PAID",
        });

    const payment = line.pay();
    await tick();
    expect(line.transaction_id).toBe("OPAY-CREATED-NOT-PAID");
    expect(line.get_payment_status()).toBe("waitingCard");
    expect(line.is_done()).toBe(false);
    expect(harness.calls.filter((call) => call.method === "opay_get_payment_status")).toHaveLength(0);
    harness.paymentMethod.payment_terminal.handleOpayStatusResponse(
        correlatedResponse(harness, line, { status: "CANCEL" })
    );
    await expect(payment).resolves.toBe(false);
});

test("zero amount is rejected locally without Create or Query", async () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine(harness.paymentMethod, 0);
    expect(await line.pay()).toBe(false);
    expect(line.get_payment_status()).toBe("retry");
    expect(harness.calls).toHaveLength(0);
    expect(harness.notifications.at(-1).message).toInclude("greater than zero");
});

test("a correlated Create rejection becomes retryable", async () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    harness.rpcHandlers.opay_create_payment_request = () =>
        correlatedResponse(harness, line, { status: "failed", message: "OPay rejected." });
    expect(await line.pay()).toBe(false);
    expect(line.get_payment_status()).toBe("retry");
    expect(harness.notifications.at(-1).options.type).toBe("danger");
});

test("a known invalid-amount rejection never becomes an uncertain payment", async () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    harness.rpcHandlers.opay_create_payment_request = () =>
        correlatedResponse(harness, line, {
            status: "failed",
            message: "Enter a payment amount greater than zero before sending to OPay.",
        });
    expect(await line.pay()).toBe(false);
    expect(line.get_payment_status()).toBe("retry");
    expect(harness.calls.filter((call) => call.method === "opay_get_payment_status")).toHaveLength(0);
    expect(harness.calls.filter((call) => call.method === "opay_resolve_create_outcome")).toHaveLength(0);
});

test("technical Create uncertainty remains unresolved with no automatic Query or retry", async () => {
    for (const failure of ["connection failure", "connect timeout", "read timeout", "lost RPC response"]) {
        const harness = makeOpayHarness();
        const line = harness.addPaymentLine();
        harness.rpcHandlers.opay_create_payment_request = () => {
            throw new Error(failure);
        };
        line.pay();
        await tick();
        expect(line.get_payment_status()).toBe("waitingCard");
        expect(line.is_done()).toBe(false);
        await advanceTime(600000);
        expect(harness.calls).toHaveLength(1);
        expect(harness.calls[0].method).toBe("opay_create_payment_request");
    }
});

test("POS reload and reconnect expose no automatic OPay recovery hook", async () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    line.set_payment_status("waitingCard");
    line.payment_ref_no = "EXACT-OUT-ORDER";

    expect(harness.pos.recoverOpayPayments).toBe(undefined);
    expect(harness.paymentMethod.payment_terminal._scheduleRecovery).toBe(undefined);
    await advanceTime(600000);
    expect(harness.calls).toHaveLength(0);
});

test("explicit Check Status resolves a lost Create without issuing Create again", async () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    harness.rpcHandlers.opay_create_payment_request = () => {
        throw new Error("response lost");
    };
    harness.rpcHandlers.opay_resolve_create_outcome = (args) => {
        expect(args).toEqual([
            [harness.paymentMethod.id],
            { reference: line.uuid, session_id: harness.pos.session.id },
        ]);
        return correlatedResponse(harness, line, {
            status: "PENDING",
            out_order_no: "RECONSTRUCTED-STABLE-REFERENCE",
        });
    };
    line.pay();
    await tick();
    await harness.paymentMethod.payment_terminal.checkPaymentStatus(line.uuid);
    expect(harness.calls.filter((call) => call.method === "opay_create_payment_request")).toHaveLength(1);
    expect(harness.calls.filter((call) => call.method === "opay_resolve_create_outcome")).toHaveLength(1);
    expect(line.payment_ref_no).toBe("RECONSTRUCTED-STABLE-REFERENCE");
    expect(line.get_payment_status()).toBe("waitingCard");
});

test("exact Query not-found releases a lost Create without issuing Create again", async () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    harness.rpcHandlers.opay_create_payment_request = () => {
        throw new Error("response lost");
    };
    harness.rpcHandlers.opay_resolve_create_outcome = () =>
        correlatedResponse(harness, line, {
            status: "not_found",
            safe_to_retry: true,
            terminal_released: true,
            message: "OPay response: order not exist",
        });

    line.pay();
    await tick();
    await harness.paymentMethod.payment_terminal.checkPaymentStatus(line.uuid);

    expect(line.get_payment_status()).toBe("retry");
    expect(harness.notifications.at(-1).message).toBe(
        "OPay response: order not exist"
    );
    expect(
        harness.calls.filter((call) => call.method === "opay_create_payment_request")
    ).toHaveLength(1);
    expect(
        harness.calls.filter((call) => call.method === "opay_resolve_create_outcome")
    ).toHaveLength(1);
});

test("manual status checks deduplicate in-flight requests and enforce cooldown", async () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    line.set_payment_status("waitingCard");
    line.payment_ref_no = "EXACT-OUT-ORDER";
    const response = new Deferred();
    harness.rpcHandlers.opay_get_payment_status = () => response;

    const first = harness.paymentMethod.payment_terminal.checkPaymentStatus(line.uuid);
    expect(await harness.paymentMethod.payment_terminal.checkPaymentStatus(line.uuid)).toBe(false);
    expect(harness.calls).toHaveLength(1);
    response.resolve(correlatedResponse(harness, line));
    await first;
    expect(await harness.paymentMethod.payment_terminal.checkPaymentStatus(line.uuid)).toBe(false);
    await advanceTime(4000);
    harness.rpcHandlers.opay_get_payment_status = () => correlatedResponse(harness, line);
    await harness.paymentMethod.payment_terminal.checkPaymentStatus(line.uuid);
    expect(harness.calls).toHaveLength(2);
});

test("manual PENDING and Query technical failure both preserve waiting", async () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    line.set_payment_status("waitingCard");
    line.payment_ref_no = "EXACT-OUT-ORDER";
    harness.rpcHandlers.opay_get_payment_status = () => correlatedResponse(harness, line);
    await harness.paymentMethod.payment_terminal.checkPaymentStatus(line.uuid);
    expect(line.get_payment_status()).toBe("waitingCard");
    await advanceTime(4000);
    harness.rpcHandlers.opay_get_payment_status = () => {
        throw new Error("network failure");
    };
    await harness.paymentMethod.payment_terminal.checkPaymentStatus(line.uuid);
    expect(line.get_payment_status()).toBe("waitingCard");
    expect(harness.notifications.at(-1).message).toBe("Unable to verify the payment status. Please try again.");
});

test("uncertain Query shows the backend's safe explanation", async () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    line.set_payment_status("waitingCard");
    line.payment_ref_no = "EXACT-OUT-ORDER";
    harness.rpcHandlers.opay_get_payment_status = () =>
        correlatedResponse(harness, line, {
            status: "uncertain",
            message: "OPay could not authenticate the status response. Payment remains unresolved.",
        });
    await harness.paymentMethod.payment_terminal.checkPaymentStatus(line.uuid);
    expect(line.get_payment_status()).toBe("waitingCard");
    expect(harness.notifications.at(-1).message).toInclude("Payment remains unresolved");
});

test("manual SUCCESS completes while final negatives become retryable", async () => {
    for (const status of ["SUCCESS", "FAIL", "CLOSE", "CANCEL"]) {
        const harness = makeOpayHarness();
        const line = harness.addPaymentLine();
        line.set_payment_status("waitingCard");
        line.payment_ref_no = "EXACT-OUT-ORDER";
        harness.rpcHandlers.opay_get_payment_status = () =>
            correlatedResponse(harness, line, {
                success: status === "SUCCESS",
                status,
                payment_completed: status === "SUCCESS",
            });
        await harness.paymentMethod.payment_terminal.checkPaymentStatus(line.uuid);
        expect(line.get_payment_status()).toBe(status === "SUCCESS" ? "done" : "retry");
    }
});

test("Check Status visibility is restricted to unresolved OPay lines", () => {
    const harness = makeOpayHarness();
    const opayLine = harness.addPaymentLine();
    const otherLine = harness.addPaymentLine(harness.nonOpayPaymentMethod);
    for (const status of ["waitingCard", "waiting", "waitingCancel", "timeout", "retry"]) {
        opayLine.set_payment_status(status);
        expect(isQueryableOpayPaymentLine(opayLine)).toBe(true);
    }
    for (const status of ["done", "reversed", undefined]) {
        opayLine.set_payment_status(status);
        expect(isQueryableOpayPaymentLine(opayLine)).toBe(false);
    }
    otherLine.set_payment_status("waitingCard");
    expect(isQueryableOpayPaymentLine(otherLine)).toBe(false);
});

test("payment-lines button handler runs one manual Query and never Create", async () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    line.set_payment_status("waitingCard");
    line.payment_ref_no = "EXACT-OUT-ORDER";
    const component = makePaymentLinesComponent();
    const response = new Deferred();
    harness.rpcHandlers.opay_get_payment_status = () => response;
    const first = component.checkOpayPaymentStatus(line);
    expect(component.isCheckingOpayStatus(line)).toBe(true);
    expect(await component.checkOpayPaymentStatus(line)).toBe(false);
    response.resolve(correlatedResponse(harness, line));
    await first;
    expect(harness.calls.filter((call) => call.method === "opay_get_payment_status")).toHaveLength(1);
    expect(harness.calls.filter((call) => call.method === "opay_create_payment_request")).toHaveLength(0);
    await advanceTime(4000);
    expect(component.isOpayStatusCheckDisabled(line)).toBe(false);
});

test("cancel refuses unresolved states without Query and protects SUCCESS", async () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    const terminal = harness.paymentMethod.payment_terminal;
    for (const status of ["waiting", "PENDING", "uncertain"]) {
        line.set_payment_status("waitingCancel");
        expect(await terminal.send_payment_cancel(harness.order, line.uuid)).toBe(false);
        expect(line.get_payment_status()).toBe("waitingCard");
    }
    line.set_payment_status("done");
    expect(await terminal.send_payment_cancel(harness.order, line.uuid)).toBe(false);
    expect(line.get_payment_status()).toBe("done");
    expect(harness.calls.filter((call) => call.method === "opay_get_payment_status")).toHaveLength(0);
});

test("Force done cannot complete an unresolved OPay line", () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    line.set_payment_status("waitingCard");
    const screen = makePaymentScreen(harness);
    screen.notification = harness.pos.notification;
    expect(PaymentScreen.prototype.sendForceDone.call(screen, line)).toBe(false);
    expect(line.get_payment_status()).toBe("waitingCard");
    expect(harness.notifications.at(-1).message).toInclude("confirmed OPay payment");
});

test("adding another payment explains the unresolved OPay blocker", async () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    line.set_payment_status("waitingCard");
    const screen = makePaymentScreen(harness);
    screen.notification = harness.pos.notification;
    expect(await PaymentScreen.prototype.addNewPaymentLine.call(screen, harness.nonOpayPaymentMethod)).toBe(false);
    expect(harness.order.payment_ids).toHaveLength(1);
    expect(harness.notifications.at(-1).message).toInclude("Check Payment Status");
});

test("cancel releases only final-negative lines", async () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    const terminal = harness.paymentMethod.payment_terminal;
    for (const status of ["FAIL", "CLOSE", "CANCEL"]) {
        line.set_payment_status("waitingCard");
        terminal.handleOpayStatusResponse(correlatedResponse(harness, line, { status }));
        expect(line.get_payment_status()).toBe("retry");
        expect(await terminal.send_payment_cancel(harness.order, line.uuid)).toBe(true);
    }
});

test("OPay deletion guard keeps unresolved lines when cancellation returns false", async () => {
    for (const status of ["waiting", "waitingCard", "timeout"]) {
        const harness = makeOpayHarness();
        const line = harness.addPaymentLine();
        line.set_payment_status(status);
        const reset = { count: 0 };
        harness.paymentMethod.payment_terminal.send_payment_cancel = () => Promise.resolve(false);
        PaymentScreen.prototype.deletePaymentLine.call(makePaymentScreen(harness, reset), line.uuid);
        await tick();
        expect(harness.order.payment_ids).toInclude(line);
        expect(reset.count).toBe(0);
    }
});

test("OPay deletion guard preserves SUCCESS defensively", async () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    line.set_payment_status("done");
    const reset = { count: 0 };
    PaymentScreen.prototype.deletePaymentLine.call(makePaymentScreen(harness, reset), line.uuid);
    await tick();
    expect(harness.order.payment_ids).toInclude(line);
    expect(line.get_payment_status()).toBe("done");
    expect(reset.count).toBe(0);
});

test("OPay deletion guard keeps an unresolved line when cancellation rejects", async () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    line.set_payment_status("waitingCard");
    const reset = { count: 0 };
    harness.paymentMethod.payment_terminal.send_payment_cancel = () => Promise.reject(new Error("cancel RPC failed"));
    PaymentScreen.prototype.deletePaymentLine.call(makePaymentScreen(harness, reset), line.uuid);
    await tick();
    await tick();
    expect(harness.order.payment_ids).toInclude(line);
    expect(line.get_payment_status()).toBe("waitingCard");
    expect(reset.count).toBe(0);
});

test("OPay final-negative deletion and non-OPay native deletion both proceed", async () => {
    const opay = makeOpayHarness();
    const opayLine = opay.addPaymentLine();
    opayLine.set_payment_status("retry");
    const opayReset = { count: 0 };
    PaymentScreen.prototype.deletePaymentLine.call(makePaymentScreen(opay, opayReset), opayLine.uuid);
    expect(opay.order.payment_ids).not.toInclude(opayLine);
    expect(opayReset.count).toBe(1);

    const other = makeOpayHarness();
    const otherLine = other.addPaymentLine(other.nonOpayPaymentMethod);
    otherLine.set_payment_status("waitingCard");
    const otherReset = { count: 0 };
    other.nonOpayPaymentMethod.payment_terminal.send_payment_cancel = () => Promise.resolve(false);
    PaymentScreen.prototype.deletePaymentLine.call(makePaymentScreen(other, otherReset), otherLine.uuid);
    await tick();
    expect(other.order.payment_ids).not.toInclude(otherLine);
    expect(otherReset.count).toBe(1);
});

test("matching WebSocket event updates only the exact OPay line", () => {
    const harness = makeOpayHarness();
    const target = harness.addPaymentLine();
    const other = harness.addPaymentLine();
    target.set_payment_status("waitingCard");
    other.set_payment_status("waitingCard");
    target.payment_ref_no = "TARGET-OUT";
    other.payment_ref_no = "OTHER-OUT";
    expect(harness.pos.handleOpayPaymentStatus(correlatedResponse(harness, target, {
        success: true, status: "SUCCESS", out_order_no: "TARGET-OUT", payment_completed: true,
    }))).toBe(true);
    expect(target.get_payment_status()).toBe("done");
    expect(other.get_payment_status()).toBe("waitingCard");
});

test("WebSocket rejects another session, payment method, or reference", () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    line.set_payment_status("waitingCard");
    const base = correlatedResponse(harness, line, { success: true, status: "SUCCESS", payment_completed: true });
    expect(harness.pos.handleOpayPaymentStatus({ ...base, pos_session_id: 999 })).toBe(false);
    expect(harness.pos.handleOpayPaymentStatus({ ...base, payment_method_id: 999 })).toBe(false);
    expect(harness.pos.handleOpayPaymentStatus({ ...base, reference: "other-reference" })).toBe(false);
    expect(line.get_payment_status()).toBe("waitingCard");
});

test("duplicate SUCCESS is idempotent and conflicting finals cannot downgrade it", () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    line.set_payment_status("waitingCard");
    const success = correlatedResponse(harness, line, { success: true, status: "SUCCESS", payment_completed: true });
    expect(harness.paymentMethod.payment_terminal.handleOpayStatusResponse(success)).toBe(true);
    expect(harness.paymentMethod.payment_terminal.handleOpayStatusResponse(success)).toBe(true);
    for (const status of ["FAIL", "CLOSE", "CANCEL"]) {
        expect(harness.paymentMethod.payment_terminal.handleOpayStatusResponse({
            ...success, success: false, status, payment_completed: false,
        })).toBe(false);
        expect(line.get_payment_status()).toBe("done");
    }
});

test("PENDING followed by SUCCESS completes and PENDING alone never completes", () => {
    const harness = makeOpayHarness();
    const line = harness.addPaymentLine();
    line.set_payment_status("waitingCard");
    harness.paymentMethod.payment_terminal.handleOpayStatusResponse(correlatedResponse(harness, line));
    expect(line.get_payment_status()).toBe("waitingCard");
    expect(line.is_done()).toBe(false);
    harness.paymentMethod.payment_terminal.handleOpayStatusResponse(
        correlatedResponse(harness, line, { success: true, status: "SUCCESS", payment_completed: true })
    );
    expect(line.get_payment_status()).toBe("done");
});

test("frontend payment-method data contains no OPay credentials", () => {
    const harness = makeOpayHarness();
    for (const field of [
        "opay_head_merchant_id", "opay_merchant_id", "opay_terminal_sn",
        "opay_client_auth_key", "opay_public_key", "opay_merchant_private_key",
        "opay_sub_scene_enum",
    ]) {
        expect(harness.paymentMethod[field]).toBe(undefined);
    }
});
