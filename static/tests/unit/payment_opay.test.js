import { animationFrame, expect, test, waitUntil } from "@odoo/hoot";
import { Deferred, advanceTime } from "@odoo/hoot-mock";
import { click, queryAll, queryOne } from "@odoo/hoot-dom";
import {
    mountWithCleanup,
    onRpc,
    patchWithCleanup,
} from "@web/../tests/web_test_helpers";
import { PaymentInterface } from "@point_of_sale/app/utils/payment/payment_interface";
import { PosStore } from "@point_of_sale/app/services/pos_store";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { PaymentOpay } from "@pos_opay/app/utils/payment/payment_opay";
import "@pos_opay/app/services/pos_store";
import "@pos_opay/app/screens/payment_screen/payment_lines/payment_lines";
import { definePosModels } from "@point_of_sale/../tests/unit/data/generate_model_definitions";
import { getFilledOrder, setupPosEnv } from "@point_of_sale/../tests/unit/utils";

definePosModels();

test("registers and instantiates the OPay payment interface", async () => {
    const store = await setupPosEnv();
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const nonOpayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => !paymentMethod.use_payment_terminal
    );

    expect(PosStore.prototype.electronic_payment_interfaces.opay).toBe(PaymentOpay);
    expect(opayPaymentMethod.payment_terminal).toBeInstanceOf(PaymentOpay);
    expect(
        PaymentInterface.prototype.isPrototypeOf(opayPaymentMethod.payment_terminal)
    ).toBe(true);
    expect(nonOpayPaymentMethod.payment_terminal).toBe(undefined);
    expect(store.recoverOpayPayments).toBe(undefined);
});

test("accepted backend request never schedules an automatic status query", async () => {
    const notifications = [];
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    let statusCalls = 0;
    patchWithCleanup(store.notification, {
        add(message, options) {
            notifications.push({ message, options });
        },
    });
    onRpc("pos.payment.method", "opay_create_payment_request", ({ args }) => {
        expect(args).toEqual([
            [opayPaymentMethod.id],
            {
                reference: paymentLine.uuid,
                amount: paymentLine.getAmount(),
                session_id: store.session.id,
            },
        ]);
        return {
            success: true,
            status: "waiting",
            message: "OPay accepted the payment request. Waiting for customer payment.",
            reference: paymentLine.uuid,
            out_order_no: "96BE4F0148E441F6B6EBF0638D5D8E6D",
            order_no: "OPAY-ORDER-1",
            payment_completed: false,
            ambiguous: false,
            reused: false,
            recovery_delay_ms: 600000,
        };
    });
    onRpc("pos.payment.method", "opay_get_payment_status", () => {
        statusCalls++;
        throw new Error("An automatic status query must never run");
    });

    const paymentResult = paymentLine.pay();
    await waitUntil(() => paymentLine.getPaymentStatus() === "waitingCard");

    expect(paymentLine.isDone()).toBe(false);
    expect(order.finalized).toBe(false);
    expect(paymentLine.payment_ref_no).toBe("96BE4F0148E441F6B6EBF0638D5D8E6D");
    expect(paymentLine.transaction_id).toBe("OPAY-ORDER-1");
    expect(notifications).toHaveLength(1);
    expect(notifications[0].message).toInclude("Waiting for customer payment");
    expect(notifications[0].options.type).toBe("warning");
    await advanceTime(600000);
    expect(statusCalls).toBe(0);

    opayPaymentMethod.payment_terminal.handleOpayStatusResponse({
        payment_method_id: opayPaymentMethod.id,
        pos_session_id: store.session.id,
        reference: paymentLine.uuid,
        out_order_no: paymentLine.payment_ref_no,
        order_no: paymentLine.transaction_id,
        status: "SUCCESS",
        message: "OPay confirmed the payment successfully.",
        payment_completed: true,
    });
    await expect(paymentResult).resolves.toBe(true);
    expect(paymentLine.getPaymentStatus()).toBe("done");
});

test("backend rejection moves the payment to retry and informs the cashier", async () => {
    const notifications = [];
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    patchWithCleanup(store.notification, {
        add(message, options) {
            notifications.push({ message, options });
        },
    });
    onRpc("pos.payment.method", "opay_create_payment_request", () => ({
        success: false,
        status: "failed",
        message: "OPay rejected the payment request.",
        reference: paymentLine.uuid,
        out_order_no: "96BE4F0148E441F6B6EBF0638D5D8E6D",
        order_no: false,
        payment_completed: false,
        ambiguous: false,
        reused: false,
    }));

    const result = await paymentLine.pay();

    expect(result).toBe(false);
    expect(paymentLine.getPaymentStatus()).toBe("retry");
    expect(notifications).toHaveLength(1);
    expect(notifications[0].message).toBe("OPay rejected the payment request.");
    expect(notifications[0].options.type).toBe("danger");
});

test("ambiguous Create Payment outcome remains waiting and cannot complete the order", async () => {
    const notifications = [];
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    patchWithCleanup(store.notification, {
        add(message, options) {
            notifications.push({ message, options });
        },
    });
    onRpc("pos.payment.method", "opay_create_payment_request", () => ({
        success: false,
        status: "uncertain",
        message:
            "OPay may have received this payment request. Keep it waiting and do not resend it.",
        reference: paymentLine.uuid,
        out_order_no: "96BE4F0148E441F6B6EBF0638D5D8E6D",
        order_no: false,
        payment_completed: false,
        ambiguous: true,
        reused: false,
        recovery_delay_ms: 600000,
    }));

    const paymentResult = paymentLine.pay();
    await waitUntil(() => paymentLine.getPaymentStatus() === "waitingCard");

    expect(paymentLine.isDone()).toBe(false);
    expect(order.finalized).toBe(false);
    expect(paymentLine.payment_ref_no).toBe("96BE4F0148E441F6B6EBF0638D5D8E6D");
    expect(notifications).toHaveLength(1);
    expect(notifications[0].message).toInclude("do not resend");
    expect(notifications[0].options.type).toBe("warning");

    opayPaymentMethod.payment_terminal.handleOpayStatusResponse({
        payment_method_id: opayPaymentMethod.id,
        pos_session_id: store.session.id,
        reference: paymentLine.uuid,
        out_order_no: paymentLine.payment_ref_no,
        order_no: false,
        status: "CLOSE",
        message: "The OPay payment expired.",
        payment_completed: false,
    });
    await expect(paymentResult).resolves.toBe(false);
    expect(paymentLine.getPaymentStatus()).toBe("retry");
});

test("missing local attempt remains uncertain when OPay absence is not authoritative", async () => {
    const notifications = [];
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    let recoveryCalls = 0;
    patchWithCleanup(store.notification, {
        add(message, options) {
            notifications.push({ message, options });
        },
    });
    onRpc("pos.payment.method", "opay_create_payment_request", () => {
        throw new Error("internal details must not reach the cashier");
    });
    onRpc("pos.payment.method", "opay_resolve_create_outcome", ({ args }) => {
        recoveryCalls++;
        expect(args).toEqual([
            [opayPaymentMethod.id],
            { reference: paymentLine.uuid, session_id: store.session.id },
        ]);
        return {
            success: false,
            status: "uncertain",
            message:
                "The OPay payment could not be verified. Do not start another OPay payment.",
            payment_method_id: opayPaymentMethod.id,
            pos_session_id: store.session.id,
            reference: paymentLine.uuid,
            out_order_no: "96BE4F0148E441F6B6EBF0638D5D8E6D",
            order_no: false,
            payment_completed: false,
            safe_to_retry: false,
            local_attempt_missing: true,
            recovery_state: "opay_query_uncertain",
            recovery_delay_ms: 600000,
        };
    });

    const paymentResult = paymentLine.pay();
    await waitUntil(() => paymentLine.getPaymentStatus() === "waitingCard");
    await animationFrame();

    expect(recoveryCalls).toBe(0);
    expect(paymentLine.payment_ref_no).toBeFalsy();
    expect(paymentLine.getPaymentStatus()).toBe("waitingCard");
    expect(paymentLine.isDone()).toBe(false);
    expect(notifications).toHaveLength(1);
    expect(notifications[0].message).toInclude("Do not start another OPay payment");
    expect(notifications[0].message).not.toInclude("internal details");
    expect(notifications[0].options.type).toBe("warning");
    await opayPaymentMethod.payment_terminal.checkPaymentStatus(paymentLine.uuid);
    expect(recoveryCalls).toBe(1);
    expect(paymentLine.payment_ref_no).toBe(
        "96BE4F0148E441F6B6EBF0638D5D8E6D"
    );
    opayPaymentMethod.payment_terminal.handleOpayStatusResponse({
        payment_method_id: opayPaymentMethod.id,
        pos_session_id: store.session.id,
        reference: paymentLine.uuid,
        out_order_no: paymentLine.payment_ref_no,
        status: "CANCEL",
        message: "Test cleanup",
        payment_completed: false,
    });
    await expect(paymentResult).resolves.toBe(false);
});

test("Create RPC failure recovers an existing pending attempt without another Create", async () => {
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    let createCalls = 0;
    let recoveryCalls = 0;
    onRpc("pos.payment.method", "opay_create_payment_request", () => {
        createCalls++;
        throw new Error("browser response lost");
    });
    onRpc("pos.payment.method", "opay_resolve_create_outcome", ({ args }) => {
        recoveryCalls++;
        expect(args[1]).toEqual({
            reference: paymentLine.uuid,
            session_id: store.session.id,
        });
        return {
            success: false,
            status: "PENDING",
            message: "The OPay payment is still pending on the terminal.",
            payment_method_id: opayPaymentMethod.id,
            pos_session_id: store.session.id,
            reference: paymentLine.uuid,
            out_order_no: "EXISTING-OUT-ORDER",
            order_no: "EXISTING-OPAY-ORDER",
            payment_completed: false,
            safe_to_retry: false,
            recovery_delay_ms: 600000,
        };
    });

    const paymentResult = paymentLine.pay();
    await waitUntil(() => paymentLine.getPaymentStatus() === "waitingCard");
    await animationFrame();

    expect(createCalls).toBe(1);
    expect(recoveryCalls).toBe(0);
    expect(paymentLine.payment_ref_no).toBeFalsy();
    expect(paymentLine.getPaymentStatus()).toBe("waitingCard");
    expect(paymentLine.isDone()).toBe(false);
    await opayPaymentMethod.payment_terminal.checkPaymentStatus(paymentLine.uuid);
    expect(recoveryCalls).toBe(1);
    expect(paymentLine.payment_ref_no).toBe("EXISTING-OUT-ORDER");
    expect(createCalls).toBe(1);
    opayPaymentMethod.payment_terminal.handleOpayStatusResponse({
        payment_method_id: opayPaymentMethod.id,
        pos_session_id: store.session.id,
        reference: paymentLine.uuid,
        out_order_no: "EXISTING-OUT-ORDER",
        order_no: "EXISTING-OPAY-ORDER",
        status: "CANCEL",
        message: "The OPay payment was cancelled.",
        payment_completed: false,
    });
    await expect(paymentResult).resolves.toBe(false);
});

test("Create RPC failure can recover SUCCESS for the exact payment line", async () => {
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    onRpc("pos.payment.method", "opay_create_payment_request", () => {
        throw new Error("browser response lost");
    });
    onRpc("pos.payment.method", "opay_resolve_create_outcome", () => ({
        success: true,
        status: "SUCCESS",
        message: "OPay confirmed the payment successfully.",
        payment_method_id: opayPaymentMethod.id,
        pos_session_id: store.session.id,
        reference: paymentLine.uuid,
        out_order_no: "EXISTING-OUT-ORDER",
        order_no: "EXISTING-OPAY-ORDER",
        payment_completed: true,
        safe_to_retry: false,
    }));

    const paymentResult = paymentLine.pay();
    await waitUntil(() => paymentLine.getPaymentStatus() === "waitingCard");
    await opayPaymentMethod.payment_terminal.checkPaymentStatus(paymentLine.uuid);
    await expect(paymentResult).resolves.toBe(true);
    expect(paymentLine.getPaymentStatus()).toBe("done");
    expect(paymentLine.payment_ref_no).toBe("EXISTING-OUT-ORDER");
});

test("failed Create-outcome recovery remains unresolved and never permits retry", async () => {
    const notifications = [];
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    let recoveryCalls = 0;
    patchWithCleanup(store.notification, {
        add(message, options) {
            notifications.push({ message, options });
        },
    });
    onRpc("pos.payment.method", "opay_create_payment_request", () => {
        throw new Error("browser response lost");
    });
    onRpc("pos.payment.method", "opay_resolve_create_outcome", () => {
        recoveryCalls++;
        throw new Error("Odoo remains unreachable");
    });

    const paymentResult = paymentLine.pay();
    await waitUntil(() => paymentLine.getPaymentStatus() === "waitingCard");
    await animationFrame();

    expect(recoveryCalls).toBe(0);
    expect(paymentLine.getPaymentStatus()).toBe("waitingCard");
    expect(paymentLine.isDone()).toBe(false);
    expect(notifications).toHaveLength(1);
    expect(notifications[0].message).toInclude("Do not start another OPay payment");
    await opayPaymentMethod.payment_terminal.checkPaymentStatus(paymentLine.uuid);
    expect(recoveryCalls).toBe(1);
    expect(paymentLine.getPaymentStatus()).toBe("waitingCard");
    opayPaymentMethod.payment_terminal.handleOpayStatusResponse({
        payment_method_id: opayPaymentMethod.id,
        pos_session_id: store.session.id,
        reference: paymentLine.uuid,
        status: "CANCEL",
        message: "Test cleanup",
        payment_completed: false,
    });
    await expect(paymentResult).resolves.toBe(false);
});

test("uncertain Create-outcome recovery remains waiting", async () => {
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    onRpc("pos.payment.method", "opay_create_payment_request", () => {
        throw new Error("browser response lost");
    });
    onRpc("pos.payment.method", "opay_resolve_create_outcome", () => ({
        success: false,
        status: "uncertain",
        message: "The existing OPay payment could not be verified.",
        payment_method_id: opayPaymentMethod.id,
        pos_session_id: store.session.id,
        reference: paymentLine.uuid,
        out_order_no: "EXISTING-OUT-ORDER",
        order_no: false,
        payment_completed: false,
        safe_to_retry: false,
        recovery_delay_ms: 600000,
    }));

    const paymentResult = paymentLine.pay();
    await waitUntil(() => paymentLine.getPaymentStatus() === "waitingCard");
    expect(paymentLine.payment_ref_no).toBeFalsy();
    await opayPaymentMethod.payment_terminal.checkPaymentStatus(paymentLine.uuid);

    expect(paymentLine.payment_ref_no).toBe("EXISTING-OUT-ORDER");
    expect(paymentLine.getPaymentStatus()).toBe("waitingCard");
    expect(paymentLine.isDone()).toBe(false);
    opayPaymentMethod.payment_terminal.handleOpayStatusResponse({
        payment_method_id: opayPaymentMethod.id,
        pos_session_id: store.session.id,
        reference: paymentLine.uuid,
        out_order_no: "EXISTING-OUT-ORDER",
        status: "FAIL",
        message: "Test cleanup",
        payment_completed: false,
    });
    await expect(paymentResult).resolves.toBe(false);
});

test("Create-outcome recovery reuses final negative handling", async () => {
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    const terminal = opayPaymentMethod.payment_terminal;
    let recoveredStatus = "FAIL";
    onRpc("pos.payment.method", "opay_resolve_create_outcome", () => ({
        success: false,
        status: recoveredStatus,
        message: `OPay returned ${recoveredStatus}.`,
        payment_method_id: opayPaymentMethod.id,
        pos_session_id: store.session.id,
        reference: paymentLine.uuid,
        out_order_no: "EXISTING-OUT-ORDER",
        order_no: "EXISTING-OPAY-ORDER",
        payment_completed: false,
        safe_to_retry: true,
    }));

    for (const status of ["FAIL", "CLOSE", "CANCEL"]) {
        recoveredStatus = status;
        paymentLine.setPaymentStatus("waitingCard");
        paymentLine.payment_ref_no = false;
        paymentLine.transaction_id = false;
        terminal.manualStatusCheckCooldowns[paymentLine.uuid] = 0;
        await terminal.checkPaymentStatus(paymentLine.uuid);
        expect(paymentLine.getPaymentStatus()).toBe("retry");
        expect(paymentLine.isDone()).toBe(false);
    }
});

test("WebSocket success completes the exact line after its resolver was lost", async () => {
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    paymentLine.setPaymentStatus("waitingCard");
    paymentLine.payment_ref_no = "EXACT-OUT-ORDER";
    paymentLine.transaction_id = "EXACT-OPAY-ORDER";

    const handled = store.handleOpayPaymentStatus({
        payment_method_id: opayPaymentMethod.id,
        pos_session_id: store.session.id,
        reference: paymentLine.uuid,
        out_order_no: "EXACT-OUT-ORDER",
        order_no: "EXACT-OPAY-ORDER",
        status: "SUCCESS",
        message: "OPay confirmed the payment successfully.",
        payment_completed: true,
    });

    expect(handled).toBe(true);
    expect(paymentLine.getPaymentStatus()).toBe("done");
    expect(paymentLine.isDone()).toBe(true);
});

test("a final notification arriving before the Create response is not lost", async () => {
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    onRpc("pos.payment.method", "opay_create_payment_request", () => {
        store.handleOpayPaymentStatus({
            payment_method_id: opayPaymentMethod.id,
            pos_session_id: store.session.id,
            reference: paymentLine.uuid,
            out_order_no: "FAST-OUT-ORDER",
            order_no: "FAST-OPAY-ORDER",
            status: "SUCCESS",
            message: "OPay confirmed the payment successfully.",
            payment_completed: true,
        });
        return {
            success: true,
            status: "waiting",
            message: "Waiting for customer payment.",
            reference: paymentLine.uuid,
            out_order_no: "FAST-OUT-ORDER",
            order_no: "FAST-OPAY-ORDER",
            payment_completed: false,
            recovery_delay_ms: 180000,
        };
    });

    expect(await paymentLine.pay()).toBe(true);
    expect(paymentLine.getPaymentStatus()).toBe("done");
    expect(paymentLine.isDone()).toBe(true);
});

test("a notification for another session or reference cannot complete the line", async () => {
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    paymentLine.setPaymentStatus("waitingCard");

    const baseNotification = {
        payment_method_id: opayPaymentMethod.id,
        pos_session_id: store.session.id,
        reference: paymentLine.uuid,
        out_order_no: "EXACT-OUT-ORDER",
        order_no: "EXACT-OPAY-ORDER",
        status: "SUCCESS",
        payment_completed: true,
    };

    expect(
        store.handleOpayPaymentStatus({
            ...baseNotification,
            pos_session_id: store.session.id + 1,
        })
    ).toBe(false);
    expect(
        store.handleOpayPaymentStatus({
            ...baseNotification,
            reference: "00000000-0000-0000-0000-000000000000",
        })
    ).toBe(false);
    expect(paymentLine.getPaymentStatus()).toBe("waitingCard");
    expect(paymentLine.isDone()).toBe(false);
});

test("cancellation does not query OPay and refuses to remove an unresolved payment", async () => {
    const notifications = [];
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    paymentLine.setPaymentStatus("waitingCard");
    paymentLine.payment_ref_no = "EXACT-OUT-ORDER";
    let statusCalls = 0;
    patchWithCleanup(store.notification, {
        add(message, options) {
            notifications.push({ message, options });
        },
    });
    onRpc("pos.payment.method", "opay_get_payment_status", ({ args }) => {
        statusCalls++;
        expect(args).toEqual([
            [opayPaymentMethod.id],
            { reference: paymentLine.uuid, session_id: store.session.id },
        ]);
        return {
            success: false,
            status: "CANCEL",
            message: "The OPay payment was cancelled by the operator.",
            reference: paymentLine.uuid,
            out_order_no: "EXACT-OUT-ORDER",
            order_no: "EXACT-OPAY-ORDER",
            payment_completed: false,
            recovery_delay_ms: 15000,
        };
    });

    expect(
        await opayPaymentMethod.payment_terminal.sendPaymentCancel(
            order,
            paymentLine.uuid
        )
    ).toBe(false);
    expect(paymentLine.getPaymentStatus()).toBe("waitingCard");
    expect(paymentLine.isDone()).toBe(false);
    expect(statusCalls).toBe(0);
    expect(notifications.at(-1).message).toInclude("Check Payment Status");

    let numberBufferResets = 0;
    PaymentScreen.prototype.deletePaymentLine.call(
        {
            paymentLines: order.payment_ids,
            currentOrder: order,
            numberBuffer: {
                reset() {
                    numberBufferResets++;
                },
            },
        },
        paymentLine.uuid
    );
    await waitUntil(() => paymentLine.getPaymentStatus() === "waitingCard");
    expect(order.payment_ids.includes(paymentLine)).toBe(true);
    expect(numberBufferResets).toBe(0);
    expect(statusCalls).toBe(0);

    await opayPaymentMethod.payment_terminal.checkPaymentStatus(paymentLine.uuid);
    expect(statusCalls).toBe(1);
    expect(paymentLine.getPaymentStatus()).toBe("retry");
    expect(notifications.at(-1).message).toInclude("cancelled");
    expect(
        await opayPaymentMethod.payment_terminal.sendPaymentCancel(
            order,
            paymentLine.uuid
        )
    ).toBe(true);
    expect(statusCalls).toBe(1);
});

test("cancellation preserves unresolved and successful states and only releases final negatives", async () => {
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    const terminal = opayPaymentMethod.payment_terminal;
    let statusCalls = 0;
    onRpc("pos.payment.method", "opay_get_payment_status", () => {
        statusCalls++;
        throw new Error("Cancel must not query OPay");
    });

    for (const status of ["waiting", "PENDING", "uncertain"]) {
        paymentLine.setPaymentStatus("waitingCard");
        terminal.handleOpayStatusResponse({
            payment_method_id: opayPaymentMethod.id,
            pos_session_id: store.session.id,
            reference: paymentLine.uuid,
            status,
            payment_completed: false,
        });
        // Odoo changes the line to waitingCancel immediately before invoking
        // the terminal interface.
        paymentLine.setPaymentStatus("waitingCancel");
        expect(await terminal.sendPaymentCancel(order, paymentLine.uuid)).toBe(false);
        expect(paymentLine.getPaymentStatus()).toBe("waitingCard");
        expect(paymentLine.isDone()).toBe(false);
    }

    // A lost Create response has no provider reference, but cancellation must
    // still preserve the unresolved line.
    paymentLine.payment_ref_no = false;
    paymentLine.transaction_id = false;
    paymentLine.setPaymentStatus("waitingCancel");
    expect(await terminal.sendPaymentCancel(order, paymentLine.uuid)).toBe(false);
    expect(paymentLine.getPaymentStatus()).toBe("waitingCard");

    terminal.handleOpayStatusResponse({
        payment_method_id: opayPaymentMethod.id,
        pos_session_id: store.session.id,
        reference: paymentLine.uuid,
        status: "SUCCESS",
        payment_completed: true,
    });
    expect(await terminal.sendPaymentCancel(order, paymentLine.uuid)).toBe(false);
    expect(paymentLine.getPaymentStatus()).toBe("done");
    expect(paymentLine.isDone()).toBe(true);

    for (const status of ["FAIL", "CLOSE", "CANCEL"]) {
        paymentLine.setPaymentStatus("waitingCard");
        terminal.handleOpayStatusResponse({
            payment_method_id: opayPaymentMethod.id,
            pos_session_id: store.session.id,
            reference: paymentLine.uuid,
            status,
            payment_completed: false,
        });
        expect(paymentLine.getPaymentStatus()).toBe("retry");
        expect(await terminal.sendPaymentCancel(order, paymentLine.uuid)).toBe(true);
        expect(paymentLine.getPaymentStatus()).toBe("retry");
    }
    expect(statusCalls).toBe(0);
});

test("Check Payment Status appears only for a waiting OPay attempt", async () => {
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    paymentLine.setPaymentStatus("waitingCard");
    paymentLine.payment_ref_no = "EXACT-OUT-ORDER";

    await mountWithCleanup(PaymentScreen, {
        props: { orderUuid: order.uuid },
    });
    await animationFrame();
    expect(queryAll(".o_pos_opay_check_status")).toHaveLength(1);

    paymentLine.setPaymentStatus("done");
    await animationFrame();
    expect(queryAll(".o_pos_opay_check_status")).toHaveLength(0);

    paymentLine.setPaymentStatus("waitingCard");
    paymentLine.payment_ref_no = false;
    await animationFrame();
    expect(queryAll(".o_pos_opay_check_status")).toHaveLength(1);
});

test("Check Payment Status is absent for non-OPay payment lines", async () => {
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const nonOpayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal !== "opay"
    );
    const paymentLine = order.addPaymentline(nonOpayPaymentMethod).data;
    paymentLine.setPaymentStatus("waitingCard");
    paymentLine.payment_ref_no = "NON-OPAY-REFERENCE";

    await mountWithCleanup(PaymentScreen, {
        props: { orderUuid: order.uuid },
    });
    await animationFrame();

    expect(queryAll(".o_pos_opay_check_status")).toHaveLength(0);
});

test("manual status button queries once, shows loading, and applies cooldown", async () => {
    const notifications = [];
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    paymentLine.setPaymentStatus("waitingCard");
    paymentLine.payment_ref_no = "EXACT-OUT-ORDER";
    paymentLine.transaction_id = "EXACT-OPAY-ORDER";
    const statusResponse = new Deferred();
    let statusCalls = 0;
    let createCalls = 0;
    patchWithCleanup(store.notification, {
        add(message, options) {
            notifications.push({ message, options });
        },
    });
    onRpc("pos.payment.method", "opay_create_payment_request", () => {
        createCalls++;
        throw new Error("Check Status must never create a payment");
    });
    onRpc("pos.payment.method", "opay_get_payment_status", ({ args }) => {
        statusCalls++;
        expect(args).toEqual([
            [opayPaymentMethod.id],
            { reference: paymentLine.uuid, session_id: store.session.id },
        ]);
        return statusResponse;
    });

    await mountWithCleanup(PaymentScreen, {
        props: { orderUuid: order.uuid },
    });
    await animationFrame();
    const firstClick = click(".o_pos_opay_check_status");
    await waitUntil(() => statusCalls === 1);

    expect(queryOne(".o_pos_opay_check_status").disabled).toBe(true);
    expect(queryOne(".o_pos_opay_check_status").textContent).toInclude("Checking");
    queryOne(".o_pos_opay_check_status").click();
    expect(statusCalls).toBe(1);
    expect(createCalls).toBe(0);

    statusResponse.resolve({
        success: false,
        status: "PENDING",
        message: "The OPay payment is still pending on the terminal.",
        payment_method_id: opayPaymentMethod.id,
        pos_session_id: store.session.id,
        reference: paymentLine.uuid,
        out_order_no: paymentLine.payment_ref_no,
        order_no: paymentLine.transaction_id,
        payment_completed: false,
        recovery_delay_ms: 15000,
    });
    await firstClick;
    await waitUntil(
        () => !queryOne(".o_pos_opay_check_status").textContent.includes("Checking")
    );

    expect(paymentLine.getPaymentStatus()).toBe("waitingCard");
    expect(paymentLine.isDone()).toBe(false);
    expect(notifications.at(-1).message).toInclude("still pending");
    expect(queryOne(".o_pos_opay_check_status").disabled).toBe(true);
    expect(statusCalls).toBe(1);
    await advanceTime(4000);
    await animationFrame();
    expect(queryOne(".o_pos_opay_check_status").disabled).toBe(false);
});

test("manual status SUCCESS completes the exact payment line", async () => {
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    paymentLine.setPaymentStatus("waitingCard");
    paymentLine.payment_ref_no = "EXACT-OUT-ORDER";
    paymentLine.transaction_id = "EXACT-OPAY-ORDER";
    onRpc("pos.payment.method", "opay_get_payment_status", () => ({
        success: true,
        status: "SUCCESS",
        message: "OPay confirmed the payment successfully.",
        payment_method_id: opayPaymentMethod.id,
        pos_session_id: store.session.id,
        reference: paymentLine.uuid,
        out_order_no: paymentLine.payment_ref_no,
        order_no: paymentLine.transaction_id,
        payment_completed: true,
    }));

    const response = await opayPaymentMethod.payment_terminal.checkPaymentStatus(
        paymentLine.uuid
    );

    expect(response.status).toBe("SUCCESS");
    expect(paymentLine.getPaymentStatus()).toBe("done");
    expect(paymentLine.isDone()).toBe(true);
});

test("manual status terminal failures reuse retry handling", async () => {
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    const terminal = opayPaymentMethod.payment_terminal;
    paymentLine.payment_ref_no = "EXACT-OUT-ORDER";
    paymentLine.transaction_id = "EXACT-OPAY-ORDER";
    let queriedStatus = "FAIL";
    onRpc("pos.payment.method", "opay_get_payment_status", () => ({
        success: false,
        status: queriedStatus,
        message: `OPay returned ${queriedStatus}.`,
        payment_method_id: opayPaymentMethod.id,
        pos_session_id: store.session.id,
        reference: paymentLine.uuid,
        out_order_no: paymentLine.payment_ref_no,
        order_no: paymentLine.transaction_id,
        payment_completed: false,
    }));

    for (const status of ["FAIL", "CLOSE", "CANCEL"]) {
        queriedStatus = status;
        paymentLine.setPaymentStatus("waitingCard");
        terminal.manualStatusCheckCooldowns[paymentLine.uuid] = 0;
        const response = await terminal.checkPaymentStatus(paymentLine.uuid);
        expect(response.status).toBe(status);
        expect(paymentLine.getPaymentStatus()).toBe("retry");
        expect(paymentLine.isDone()).toBe(false);
    }
});

test("manual status RPC failure remains waiting and shows a safe message", async () => {
    const notifications = [];
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    paymentLine.setPaymentStatus("waitingCard");
    paymentLine.payment_ref_no = "EXACT-OUT-ORDER";
    patchWithCleanup(store.notification, {
        add(message, options) {
            notifications.push({ message, options });
        },
    });
    onRpc("pos.payment.method", "opay_get_payment_status", () => {
        throw new Error("private backend details");
    });

    const response = await opayPaymentMethod.payment_terminal.checkPaymentStatus(
        paymentLine.uuid
    );

    expect(response).toBe(false);
    expect(paymentLine.getPaymentStatus()).toBe("waitingCard");
    expect(paymentLine.isDone()).toBe(false);
    expect(notifications.at(-1).message).toBe(
        "Unable to verify the payment status. Please try again."
    );
    expect(notifications.at(-1).message).not.toInclude("private backend details");
});

test("manual status uncertain response remains waiting and shows safe feedback", async () => {
    const notifications = [];
    const store = await setupPosEnv();
    const order = await getFilledOrder(store);
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );
    const paymentLine = order.addPaymentline(opayPaymentMethod).data;
    paymentLine.setPaymentStatus("waitingCard");
    paymentLine.payment_ref_no = "EXACT-OUT-ORDER";
    patchWithCleanup(store.notification, {
        add(message, options) {
            notifications.push({ message, options });
        },
    });
    onRpc("pos.payment.method", "opay_get_payment_status", () => ({
        success: false,
        status: "uncertain",
        message: "The provider result could not be verified.",
        payment_method_id: opayPaymentMethod.id,
        pos_session_id: store.session.id,
        reference: paymentLine.uuid,
        out_order_no: paymentLine.payment_ref_no,
        order_no: false,
        payment_completed: false,
        ambiguous: true,
        recovery_delay_ms: 15000,
    }));

    const response = await opayPaymentMethod.payment_terminal.checkPaymentStatus(
        paymentLine.uuid
    );

    expect(response.status).toBe("uncertain");
    expect(paymentLine.getPaymentStatus()).toBe("waitingCard");
    expect(paymentLine.isDone()).toBe(false);
    expect(notifications.at(-1).message).toBe(
        "Unable to verify the payment status. Please try again."
    );
});

test("OPay configuration is absent from frontend payment-method data", async () => {
    const store = await setupPosEnv();
    const opayPaymentMethod = store.models["pos.payment.method"].find(
        (paymentMethod) => paymentMethod.use_payment_terminal === "opay"
    );

    const serverOnlyFields = [
        "opay_head_merchant_id",
        "opay_merchant_id",
        "opay_terminal_sn",
        "opay_client_auth_key",
        "opay_public_key",
        "opay_merchant_private_key",
        "opay_sub_scene_enum",
    ];
    for (const fieldName of serverOnlyFields) {
        expect(opayPaymentMethod[fieldName]).toBe(undefined);
    }
});
