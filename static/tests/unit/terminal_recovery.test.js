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
    let response;
    const method = { id: 7, use_payment_terminal: "opay" };
    const pos = {
        env: {},
        session: { id: 12 },
        notification: { add() {} },
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
            get_payment_status() { return this.payment_status; },
            set_payment_status(status) { this.payment_status = status; },
            is_done() { return this.payment_status === "done"; },
            handle_payment_response(success) { this.payment_status = success ? "done" : "retry"; },
            get_amount() { return 10; },
        };
        lines.push(value);
        return value;
    }
    return { terminal, line, calls, setResponse: (value) => { response = value; } };
}

test("blocked Create exposes previous-payment recovery without querying automatically", async () => {
    const h = fixture();
    const current = h.line("current");
    h.setResponse({ status: "terminal_blocked", reference: current.uuid, payment_completed: false });
    expect(await h.terminal.send_payment_request(current.uuid)).toBe(false);
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
        expect(await h.terminal.send_payment_request(current.uuid)).toBe(false);
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
    const request = h.terminal.send_payment_request(current.uuid);
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
            h.terminal.send_payment_request(replacement.uuid);
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

test("previous SUCCESS updates only original line; current sale remains unpaid", async () => {
    const h = fixture();
    const current = h.line("current");
    const previous = h.line("previous", "waitingCard");
    h.setResponse({
        status: "terminal_available", reference: current.uuid, payment_completed: false,
        previous_payment: {
            reference: previous.uuid, status: "SUCCESS", payment_completed: true,
            pos_session_id: 12, payment_method_id: 7,
        },
    });
    await h.terminal.checkPaymentStatus(current.uuid);
    expect(current.payment_status).toBe("retry");
    expect(previous.payment_status).toBe("done");
    expect(current.transaction_id).toBe(undefined);
    expect(h.calls.map((call) => call.name)).toEqual(["opay_check_previous_payment"]);
    expect(h.calls[0].args).toEqual([[7], { reference: "current", session_id: 12 }]);
});

test("previous result from another session cannot complete a local line", async () => {
    const h = fixture();
    const current = h.line("current");
    const previous = h.line("previous", "waitingCard");
    h.setResponse({
        status: "terminal_available", reference: current.uuid, payment_completed: false,
        previous_payment: {
            reference: previous.uuid, status: "SUCCESS", payment_completed: true,
            pos_session_id: 99, payment_method_id: 7,
        },
    });
    await h.terminal.checkPaymentStatus(current.uuid);
    expect(previous.payment_status).toBe("waitingCard");
    expect(current.payment_status).toBe("retry");
});

test("pending previous payment remains blocked and rapid clicks are deduplicated", async () => {
    const h = fixture();
    const current = h.line("current");
    let finish;
    h.setResponse(() => new Promise((resolve) => { finish = resolve; }));
    const first = h.terminal.checkPaymentStatus(current.uuid);
    expect(await h.terminal.checkPaymentStatus(current.uuid)).toBe(false);
    finish({ status: "terminal_blocked", reference: current.uuid, payment_completed: false });
    await first;
    expect(await h.terminal.checkPaymentStatus(current.uuid)).toBe(false);
    expect(current.uiState.opayTerminalBlocked).toBe(true);
    expect(h.calls.length).toBe(1);
});

test("previous Query network failure preserves blocked state and never creates", async () => {
    const h = fixture();
    const current = h.line("current");
    current.uiState.opayTerminalBlocked = true;
    h.setResponse(() => { throw new Error("offline"); });
    expect(await h.terminal.checkPaymentStatus(current.uuid)).toBe(false);
    expect(current.uiState.opayTerminalBlocked).toBe(true);
    expect(current.payment_status).toBe("retry");
    expect(h.calls.map((call) => call.name)).toEqual(["opay_check_previous_payment"]);
});

test("negative previous finals release only the previous line without automatic Create", async () => {
    for (const status of ["FAIL", "CLOSE", "CANCEL", "failed"]) {
        const h = fixture();
        const current = h.line("current");
        current.uiState.opayTerminalBlocked = true;
        const previous = h.line("previous", "waitingCard");
        h.setResponse({
            status: "terminal_available", reference: current.uuid, payment_completed: false,
            previous_payment: {
                reference: previous.uuid, status, payment_completed: false,
                pos_session_id: 12, payment_method_id: 7,
            },
        });
        await h.terminal.checkPaymentStatus(current.uuid);
        expect(current.uiState.opayTerminalBlocked).toBe(false);
        expect(current.payment_status).toBe("retry");
        expect(previous.payment_status).toBe("retry");
        expect(h.calls.map((call) => call.name)).toEqual(["opay_check_previous_payment"]);
    }
});

test("old retry line with its own uncertain payment returns to waiting, not safe retry", async () => {
    const h = fixture();
    const current = h.line("current");
    h.setResponse({ status: "uncertain", reference: current.uuid, payment_completed: false });
    await h.terminal.checkPaymentStatus(current.uuid);
    expect(current.payment_status).toBe("waitingCard");
    expect(h.calls.map((call) => call.name)).toEqual(["opay_check_previous_payment"]);
});
