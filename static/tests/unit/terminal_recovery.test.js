import { expect, test } from "@odoo/hoot";
import { getOpayUiState, PaymentOpay } from "@pos_opay/app/utils/payment/payment_opay";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { PaymentScreenPaymentLines } from "@point_of_sale/app/screens/payment_screen/payment_lines/payment_lines";
import { isQueryableOpayPaymentLine } from "@pos_opay/app/screens/payment_screen/payment_lines/payment_lines";
import "@pos_opay/app/screens/payment_screen/payment_screen";

// Small terminal-interface fixture: all RPCs are test doubles, never OPay HTTP.
function fixture() {
    const lines = [];
    const calls = [];
    const notices = [];
    let response;
    const method = { id: 7, use_payment_terminal: "opay" };
    const pos = {
        env: {},
        session: { id: 12 },
        notification: { add(message, options) { notices.push({ message, options }); } },
        models: { "pos.order": { getAll: () => [{ payment_ids: lines }] } },
        data: {
            async silentCall(model, name, args) {
                calls.push({ name, args });
                return typeof response === "function" ? response() : response;
            },
            async call(model, name, args) {
                calls.push({ name, args });
                return response;
            },
        },
    };
    const terminal = new PaymentOpay(pos, method);
    function line(uuid, status = "retry") {
        const value = {
            uuid, payment_method_id: method, payment_status: status, uiState: {},
            getPaymentStatus() { return this.payment_status; },
            setPaymentStatus(status) { this.payment_status = status; },
            isDone() { return this.payment_status === "done"; },
            handlePaymentResponse(success) { this.payment_status = success ? "done" : "retry"; },
            getAmount() { return 10; },
        };
        lines.push(value);
        return value;
    }
    return { terminal, line, calls, notices, pos, setResponse: (value) => { response = value; } };
}

test("OPay POS toasts use a twelve-second delay", () => {
    const h = fixture();
    h.terminal._showStatus("Payment needs review", "warning");
    expect(h.notices[0].options.autocloseDelay).toBe(12000);
    const notices = [];
    PaymentScreen.prototype.sendForceDone.call({
        notification: { add(message, options) { notices.push({ message, options }); } },
    }, { payment_method_id: { use_payment_terminal: "opay" } });
    expect(notices[0].options.autocloseDelay).toBe(12000);
});

test("blocked Create exposes previous-payment recovery without querying automatically", async () => {
    const h = fixture();
    const current = h.line("current");
    h.setResponse({ status: "terminal_blocked", reference: current.uuid, payment_completed: false });
    expect(await h.terminal.sendPaymentRequest(current.uuid)).toBe(false);
    expect(current.uiState.opayTerminalBlocked).toBe(true);
    expect(current.payment_status).toBe("retry");
    expect(h.calls.map((call) => call.name)).toEqual(["opay_create_payment_request"]);
});

test("restored serialized UI state cannot crash Create response handling", async () => {
    for (const saved of ['{}', '{"selected":true}', 'broken JSON', 'null', '[]', null, false]) {
        const h = fixture();
        const current = h.line("current");
        current.uiState = saved;
        h.setResponse({ status: "terminal_blocked", reference: current.uuid, payment_completed: false });
        expect(await h.terminal.sendPaymentRequest(current.uuid)).toBe(false);
        expect(current.uiState.opayTerminalBlocked).toBe(true);
        expect(current.payment_status).toBe("retry");
        if (saved === '{"selected":true}') {
            expect(current.uiState.selected).toBe(true);
        }
        expect(h.calls.map((call) => call.name)).toEqual(["opay_create_payment_request"]);
    }
});

test("accepted Create with serialized UI state stays waiting until confirmed", async () => {
    const h = fixture();
    const current = h.line("current", "waiting");
    current.uiState = "{}";
    h.setResponse({ status: "waiting", reference: current.uuid, payment_completed: false });
    const request = h.terminal.sendPaymentRequest(current.uuid);
    await Promise.resolve();
    expect(current.payment_status).toBe("waitingCard");
    expect(h.calls.map((call) => call.name)).toEqual(["opay_create_payment_request"]);
    h.terminal.handleOpayStatusResponse({ status: "SUCCESS", reference: current.uuid, payment_completed: true });
    expect(await request).toBe(true);
    expect(current.payment_status).toBe("done");
});

test("serialized blocked UI state is read safely and preserved on failed checks", async () => {
    const h = fixture();
    const current = h.line("current");
    current.uiState = '{"opayTerminalBlocked":true,"selected":true}';
    expect(getOpayUiState(current).opayTerminalBlocked).toBe(true);
    h.setResponse(() => { throw new Error("offline"); });
    expect(await h.terminal.checkPaymentStatus(current.uuid)).toBe(false);
    expect(getOpayUiState(current).opayTerminalBlocked).toBe(true);
});

test("previous release and negative finalization normalize restored UI state", async () => {
    const h = fixture();
    const current = h.line("current");
    current.uiState = '{"opayTerminalBlocked":true,"selected":true}';
    h.setResponse({ status: "terminal_available", reference: current.uuid, payment_completed: false });
    await h.terminal.checkPaymentStatus(current.uuid);
    expect(current.uiState.opayTerminalBlocked).toBe(false);
    expect(current.uiState.selected).toBe(true);
    current.uiState = '{"opayTerminalBlocked":true}';
    h.terminal.handleOpayStatusResponse({ status: "CLOSE", reference: current.uuid, payment_completed: false });
    expect(current.uiState.opayTerminalBlocked).toBe(false);
    expect(current.payment_status).toBe("retry");
});

test("confirmed closed payment retries with a new POS line, never the old UUID", async () => {
    const h = fixture();
    const oldLine = h.line("old-uuid");
    h.terminal.handleOpayStatusResponse({
        status: "CLOSE", reference: oldLine.uuid, payment_completed: false,
        terminal_released: true,
    });
    expect(getOpayUiState(oldLine).opayRetrySafe).toBe(true);
    h.setResponse({ status: "waiting", reference: "new-uuid", payment_completed: false });
    const screen = {
        paymentLines: [oldLine],
        notification: { add() {} },
        retryOpayPayment: PaymentScreen.prototype.retryOpayPayment,
        deletePaymentLine(uuid) {
            this.paymentLines = this.paymentLines.filter((line) => line.uuid !== uuid);
        },
        async addNewPaymentLine(method) {
            expect(method).toBe(oldLine.payment_method_id);
            const replacement = h.line("new-uuid", "pending");
            this.paymentLines.push(replacement);
            h.terminal.sendPaymentRequest(replacement.uuid);
            return true;
        },
    };
    expect(await PaymentScreen.prototype.sendPaymentRequest.call(screen, oldLine)).toBe(true);
    expect(screen.paymentLines.map((line) => line.uuid)).toEqual(["new-uuid"]);
    expect(h.calls.map((call) => call.name)).toEqual(["opay_create_payment_request"]);
    expect(h.calls[0].args[1].reference).toBe("new-uuid");
});

test("unverified retry never removes a line or sends Create", async () => {
    const h = fixture();
    const oldLine = h.line("old-uuid");
    const notices = [];
    const screen = {
        paymentLines: [oldLine],
        notification: { add(message) { notices.push(message); } },
        retryOpayPayment: PaymentScreen.prototype.retryOpayPayment,
        deletePaymentLine() { throw new Error("must retain unresolved line"); },
    };
    expect(await PaymentScreen.prototype.sendPaymentRequest.call(screen, oldLine)).toBe(false);
    expect(screen.paymentLines.map((line) => line.uuid)).toEqual(["old-uuid"]);
    expect(notices.length).toBe(1);
    expect(h.calls.length).toBe(0);
});

test("rapid retry clicks cannot create two replacement lines", async () => {
    const h = fixture();
    const oldLine = h.line("old-uuid");
    oldLine.uiState = { opayRetrySafe: true };
    let finish;
    let additions = 0;
    const screen = {
        paymentLines: [oldLine],
        notification: { add() {} },
        retryOpayPayment: PaymentScreen.prototype.retryOpayPayment,
        deletePaymentLine(uuid) {
            this.paymentLines = this.paymentLines.filter((line) => line.uuid !== uuid);
        },
        addNewPaymentLine() {
            additions++;
            return new Promise((resolve) => { finish = resolve; });
        },
    };
    const first = PaymentScreen.prototype.sendPaymentRequest.call(screen, oldLine);
    expect(await PaymentScreen.prototype.sendPaymentRequest.call(screen, oldLine)).toBe(false);
    finish(true);
    expect(await first).toBe(true);
    expect(additions).toBe(1);
    expect(h.calls.length).toBe(0);
});

test("recovery controls cover all unresolved states including restored Request sent", () => {
    const h = fixture();
    const current = h.line("current");
    const component = Object.create(PaymentScreenPaymentLines.prototype);
    current.uiState = '{"opayTerminalBlocked":true}';
    expect(component.isOpayTerminalBlocked(current)).toBe(true);
    for (const status of ["waiting", "waitingCard", "waitingCancel", "waitingCapture", "force_done", "timeout", "retry"]) {
        current.payment_status = status;
        expect(isQueryableOpayPaymentLine(current)).toBe(true);
    }
    current.payment_status = "done";
    expect(isQueryableOpayPaymentLine(current)).toBe(false);
    current.payment_status = "waiting";
    current.payment_method_id = { use_payment_terminal: "other" };
    expect(isQueryableOpayPaymentLine(current)).toBe(false);
    expect(component.isOpayTerminalBlocked(current)).toBe(false);
});

test("checkout exception keeps OPay recoverable without a technical modal or automatic Query", async () => {
    for (const initial of ["waiting", "done"]) {
        const h = fixture();
        const current = h.line("current", initial);
        const messages = [];
        const screen = {
            pos: {}, paymentLines: [current], numberBuffer: { capture() {} },
            notification: { add(message) { messages.push(message); } },
        };
        current.pay = async () => { throw new Error("technical test detail"); };
        expect(await PaymentScreen.prototype.sendPaymentRequest.call(screen, current)).toBe(false);
        expect(screen.pos.paymentTerminalInProgress).toBe(false);
        expect(current.payment_status).toBe(initial === "done" ? "done" : "waitingCard");
        expect(messages.length).toBe(1);
        expect(messages[0].includes("technical test detail")).toBe(false);
        expect(h.calls.length).toBe(0);
    }
});

test("non-OPay checkout exceptions retain native handling", async () => {
    const h = fixture();
    const current = h.line("current", "waiting");
    current.payment_method_id = { use_payment_terminal: "other" };
    const failure = new Error("native terminal error");
    current.pay = async () => { throw failure; };
    const screen = {
        pos: {}, paymentLines: [current], numberBuffer: { capture() {} },
        notification: { add() { throw new Error("OPay must not intercept another terminal"); } },
    };
    let caught;
    try {
        await PaymentScreen.prototype.sendPaymentRequest.call(screen, current);
    } catch (error) {
        caught = error;
    }
    expect(caught).toBe(failure);
    expect(current.payment_status).toBe("waiting");
});

test("retry-line status check resolves only that line, never a previous order", async () => {
    const h = fixture();
    const current = h.line("current");
    current.uiState.opayTerminalBlocked = true;
    h.setResponse({
        status: "not_found", reference: current.uuid, payment_completed: false,
        out_order_no: "CURRENT", safe_to_retry: true, terminal_released: true,
    });
    await h.terminal.checkPaymentStatus(current.uuid);
    expect(current.payment_status).toBe("retry");
    expect(getOpayUiState(current).opayTerminalBlocked).toBe(false);
    expect(getOpayUiState(current).opayRetrySafe).toBe(true);
    expect(h.calls.map((call) => call.name)).toEqual(["opay_resolve_create_outcome"]);
});

test("retry-line uncertainty remains unresolved without automatic Create", async () => {
    const h = fixture();
    const current = h.line("current");
    h.setResponse({ status: "uncertain", reference: current.uuid, payment_completed: false });
    await h.terminal.checkPaymentStatus(current.uuid);
    expect(current.payment_status).toBe("waitingCard");
    expect(h.calls.map((call) => call.name)).toEqual(["opay_resolve_create_outcome"]);
});

test("review list is cashier initiated and does not query OPay automatically", async () => {
    const h = fixture();
    const screen = {
        opayReview: { open: false, loading: false, checking: null, payments: [] },
        opayPaymentMethods: [{ id: 7, name: "OPay" }],
        pos: h.pos,
        notification: { add() {} },
        loadOpayPaymentsToReview: PaymentScreen.prototype.loadOpayPaymentsToReview,
    };
    expect(h.calls.length).toBe(0);
    h.setResponse([{ id: 42, payment_method_id: 7, status: "PENDING",
        amount: "10.00", currency: "NGN", out_order_no: "OLD-REFERENCE" }]);
    await PaymentScreen.prototype.toggleOpayReview.call({
        ...screen, toggleOpayReview: PaymentScreen.prototype.toggleOpayReview,
    });
    // The only call lists local records; it does not invoke Query Order.
    expect(h.calls.map((call) => call.name)).toEqual(["opay_list_payments_to_review"]);
    expect(screen.opayReview.payments.length).toBe(1);
});

test("reviewing an older CANCEL updates only its exact payment line", async () => {
    const h = fixture();
    const current = h.line("current", "waitingCard");
    const previous = h.line("previous", "waitingCard");
    const notices = [];
    let reloads = 0;
    const screen = {
        opayReview: { checking: null },
        opayPaymentMethods: [{ id: 7, name: "OPay", payment_terminal: h.terminal }],
        pos: h.pos,
        notification: { add(message) { notices.push(message); } },
        async loadOpayPaymentsToReview() { reloads++; },
    };
    const summary = { id: 42, payment_method_id: 7, out_order_no: "OLD" };
    h.setResponse({
        status: "CANCEL", reference: previous.uuid, out_order_no: "OLD",
        payment_completed: false, terminal_released: true,
        pos_session_id: 12, payment_method_id: 7, message: "The earlier payment was cancelled.",
    });
    await PaymentScreen.prototype.checkOpayPaymentToReview.call(screen, summary);
    expect(h.calls.map((call) => call.name)).toEqual(["opay_check_payment_to_review"]);
    expect(previous.payment_status).toBe("retry");
    expect(current.payment_status).toBe("waitingCard");
    expect(reloads).toBe(1);
    expect(notices.length).toBe(1);
});

test("reviewed SUCCESS from another session cannot pay the current sale", async () => {
    const h = fixture();
    const current = h.line("current", "waitingCard");
    const previous = h.line("previous", "waitingCard");
    const screen = {
        opayReview: { checking: null },
        opayPaymentMethods: [{ id: 7, name: "OPay", payment_terminal: h.terminal }],
        pos: h.pos,
        notification: { add() {} },
        async loadOpayPaymentsToReview() {},
    };
    h.setResponse({
        status: "SUCCESS", reference: previous.uuid, out_order_no: "OLD",
        payment_completed: true, pos_session_id: 99, payment_method_id: 7,
        message: "Review the earlier sale.",
    });
    await PaymentScreen.prototype.checkOpayPaymentToReview.call(
        screen, { id: 42, payment_method_id: 7, out_order_no: "OLD" }
    );
    expect(current.payment_status).toBe("waitingCard");
    expect(previous.payment_status).toBe("waitingCard");
});

test("reviewed SUCCESS in this session completes only its original line", async () => {
    const h = fixture();
    const current = h.line("current", "waitingCard");
    const previous = h.line("previous", "waitingCard");
    const screen = {
        opayReview: { checking: null },
        opayPaymentMethods: [{ id: 7, name: "OPay", payment_terminal: h.terminal }],
        pos: h.pos,
        notification: { add() {} },
        async loadOpayPaymentsToReview() {},
    };
    h.setResponse({
        status: "SUCCESS", reference: previous.uuid, out_order_no: "OLD",
        payment_completed: true, pos_session_id: 12, payment_method_id: 7,
        message: "Review the earlier sale.",
    });
    await PaymentScreen.prototype.checkOpayPaymentToReview.call(
        screen, { id: 42, payment_method_id: 7, out_order_no: "OLD" }
    );
    expect(previous.payment_status).toBe("done");
    expect(current.payment_status).toBe("waitingCard");
    expect(h.calls.map((call) => call.name)).toEqual(["opay_check_payment_to_review"]);
});

test("failed review does not fabricate a final status or create a payment", async () => {
    const h = fixture();
    const current = h.line("current", "waitingCard");
    const notices = [];
    const screen = {
        opayReview: { checking: null },
        opayPaymentMethods: [{ id: 7, name: "OPay", payment_terminal: h.terminal }],
        pos: h.pos,
        notification: { add(message) { notices.push(message); } },
        async loadOpayPaymentsToReview() { throw new Error("must not reload"); },
    };
    h.setResponse(() => { throw new Error("offline"); });
    await PaymentScreen.prototype.checkOpayPaymentToReview.call(
        screen, { id: 42, payment_method_id: 7, out_order_no: "OLD" }
    );
    expect(current.payment_status).toBe("waitingCard");
    expect(h.calls.map((call) => call.name)).toEqual(["opay_check_payment_to_review"]);
    expect(notices.length).toBe(1);
});
