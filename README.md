# OPay POS Terminal for Odoo 19

`pos_opay` connects Odoo Point of Sale to a configured physical OPay terminal
through OPay's public wireless POS API. When a cashier selects OPay, Odoo sends
the payment amount to the assigned terminal and waits for an authenticated final
status before completing the payment.

This is an Odoo POS payment-terminal addon. It is not an Odoo online payment
provider, an eCommerce checkout integration, an OPay wallet integration, or a
general OPay ERP module.

## Support status

| Area | Current support statement |
| --- | --- |
| Odoo version | Odoo 19.0 only |
| Odoo Community | Verified in the current Odoo 19 Community development environment |
| Odoo Enterprise | Not yet verified |
| Odoo Online (SaaS) | Not supported because the addon contains server-side Python code |
| Odoo.sh | Not yet verified |
| Earlier/later Odoo versions | Not yet verified; no compatibility claim is made |
| Currency | NGN only |
| Market | Nigeria only |
| OPay integration | Physical OPay POS terminal through the public wireless/offline POS API |
| Terminal assignment | One unique terminal serial number per OPay payment method |
| Physical terminal | Required for live payments |
| Terminal model/firmware matrix | Not yet verified; use an OPay-provisioned terminal enabled for the public POS API |
| Create Payment | Live physical-terminal flow verified |
| Query Order | Live cashier-triggered status check verified |
| Webhook implementation | Implemented and covered by automated authentication/correlation tests |
| Live OPay webhook delivery | Not yet verified end to end |

The addon depends only on Odoo's standard `point_of_sale` module. That does not,
by itself, prove Odoo Enterprise compatibility.

## Architecture

```text
Odoo POS browser
-> PaymentOpay (Odoo PaymentInterface)
-> Odoo model RPC
-> pos.payment.method
-> persisted pos.opay.payment.attempt
-> OPayClient and OPayAuth on the Odoo server
-> OPay cloud POS API
-> configured physical OPay terminal
```

The browser sends only the payment-line UUID, amount, and active POS session.
Merchant identifiers, terminal configuration, `clientAuthKey`, and RSA keys are
loaded by the Odoo server from the configured payment method.

Final status can return through either of these paths:

```text
OPay webhook -> Odoo -> POS WebSocket notification -> exact payment line
```

```text
Cashier clicks Check Payment Status -> Odoo Query Order -> exact payment line
```

Query Order is not polled automatically.

## Implemented scope

- Native Odoo `pos.payment.method` terminal registration.
- Odoo `PaymentInterface` implementation registered as `opay`.
- OPay Create Payment and Query Order Details.
- RSA request encryption/signing and response verification/decryption.
- Authenticated webhook processing and exact attempt correlation.
- Persistent payment attempts with stable payment UUID to `outOrderNo` mapping.
- Waiting, uncertain, success, failure, closed, and cancelled behavior.
- Duplicate Create protection and idempotent final-result handling.
- Cashier-triggered Check Payment Status with in-flight protection and cooldown.
- Read-only administrative payment-attempt inspection.
- Multi-company attempt isolation.
- Safe structured operational logging.

The addon does not implement refunds, reversals, split settlement, shared
terminals, automatic Query Order polling, or automatic HTTP retries.

## Requirements

- Odoo 19 with Point of Sale installed.
- An OPay merchant/business enabled for the documented POS API.
- A physical OPay terminal assigned to the merchant/branch.
- The OPay identifiers, authentication key, and RSA keys described in the
  [OPay onboarding guide](docs/OPAY_ONBOARDING.md).
- Outbound HTTPS access from Odoo to `payapi.opayweb.com`.
- A publicly reachable HTTPS Odoo URL if the webhook path will be used.
- An Odoo ERP Manager to enter restricted OPay configuration.

## Installation

1. Place the complete `pos_opay` directory in an Odoo addons directory.
2. Add that directory to Odoo's `addons_path` if it is not already present.
3. Restart Odoo.
4. In Apps, update the Apps List.
5. Search for **OPay POS Terminal** and install it.

For a command-line installation or upgrade, use the normal Odoo command for
your environment, for example:

```text
odoo-bin -c <odoo-config> -d <database> -u pos_opay --stop-after-init
```

Do not store OPay credentials in the Odoo configuration file or in this addon's
source tree.

## Configuration

First obtain all required values from OPay. See
[docs/OPAY_ONBOARDING.md](docs/OPAY_ONBOARDING.md) for their meaning and use.

1. Open **Point of Sale -> Configuration -> Payment Methods**.
2. Create or open the payment method for the physical OPay terminal.
3. Select a **Bank** journal. Without a Bank journal, Odoo may classify the
   method as pay-later and hide the terminal selector.
4. Set **Integration** to **Terminal**.
5. Set **Integrate with** (or **Use a Payment Terminal**) to **OPay**.
6. Enter:
   - OPay Business ID (`headMerchantId`)
   - OPay Branch ID (`merchantId`)
   - OPay Terminal Serial Number (`sn`)
   - OPay Sub Scene (`subSceneEnum`)
   - OPay Client Auth Key (`clientAuthKey`)
   - OPay Public Key
   - Merchant Private Key
7. Save the payment method.
8. Add the payment method to the intended Point of Sale configuration.

The terminal serial number must be unique across OPay payment methods,
including payment methods in other Odoo companies. Configure a separate payment
method for each physical terminal.

## Payment flow

1. The cashier creates an order and opens the Payment screen.
2. The cashier selects the configured OPay payment method.
3. Odoo creates a persistent attempt using the payment-line UUID and a stable
   `outOrderNo`.
4. The Odoo server validates and normalizes the amount as NGN, encrypts and
   signs the request, and sends Create Payment to OPay.
5. The configured terminal serial number determines which physical terminal
   receives the amount.
6. An authenticated Create Payment acceptance stores OPay's `orderNo` and
   leaves the Odoo payment line waiting.
7. The customer completes the transaction on the physical terminal.
8. An authenticated webhook or an explicit cashier Check Payment Status action
   supplies the final status.
9. Odoo completes the payment only when the correlated status is `SUCCESS`.

Create Payment acceptance is not proof that the customer paid. HTTP success,
OPay response code `00000`, or an `orderNo` alone never completes the order.

## Webhook behavior

The addon exposes this HTTP POST route:

```text
https://<public-odoo-host>/pos_opay/notification
```

The OPay Webhook URL shown on the payment method is read-only and is generated
from Odoo's base URL. Configure the public Odoo HTTPS origin correctly before
copying that URL into OPay. OPay's current POS documentation instructs a Super
Admin to submit it under **OPay Business Dashboard -> Developer Tool -> POS &
Others**. `localhost`, a private IP address, or an internal hostname cannot
receive OPay cloud callbacks.

The route validates the documented OPay headers and encrypted envelope,
including authentication key, timestamp, signature, and decrypted content. It
then correlates the result to one exact stored attempt using merchant, terminal,
business order, OPay order, amount, currency, payment method, and POS session
data where available. Invalid or mismatched callbacks fail closed.

For a valid final callback, Odoo persists the status and publishes an
`OPAY_PAYMENT_STATUS` notification through Odoo's native POS notification
channel. A callback for another session, payment line, terminal, amount, or
order cannot complete the active line.

Live OPay webhook delivery has not yet been verified end to end. Until it is,
the cashier-triggered Check Payment Status action is the verified way to obtain
the final result when a callback does not arrive.

## Check Payment Status

While an OPay line is waiting or uncertain, the Payment screen displays
**Check Payment Status**. The button:

- queries the already-existing attempt through Odoo's backend;
- uses server-side stored OPay references and credentials;
- never sends another Create Payment request;
- disables itself during the request;
- applies a four-second client-side cooldown;
- completes only the exact line when authenticated Query Order returns
  `SUCCESS`.

`PENDING` or an uncertain/unavailable response keeps the line waiting. A Query
transport or authentication problem does not manufacture a failed payment and
does not release the line for a new payment.

There is no automatic Query Order timer. The reliability order is:

1. authenticated webhook, when delivered;
2. explicit cashier Check Payment Status.

## Payment statuses

| Status | Meaning in the addon | POS behavior |
| --- | --- | --- |
| `creating` | Local attempt is being submitted | Not paid; creation is protected against duplicates |
| `waiting` | Create Payment was accepted | Wait for webhook or Check Payment Status |
| `uncertain` | Communication outcome is not authoritative | Remain waiting; do not create another payment |
| `PENDING` | OPay confirms the transaction is still pending | Remain waiting |
| `SUCCESS` | Authenticated, correlated OPay success | Complete the exact payment line |
| `failed` | Create Payment was explicitly rejected or configuration failed | Retry behavior is allowed |
| `FAIL` | OPay confirms failure | Fail/retry and release the terminal attempt |
| `CLOSE` | OPay confirms the transaction is closed/expired | Fail/retry and release the terminal attempt |
| `CANCEL` | OPay confirms cancellation | Fail/retry and release the terminal attempt |

Clicking Cancel on an unresolved waiting, pending, or uncertain line does not
claim that the remote transaction was cancelled. Use Check Payment Status after
the terminal reaches a final state. No server-side OPay cancellation endpoint is
implemented.

## Payment-attempt inspection

ERP Managers can open **Point of Sale -> OPay Payment Attempts** to inspect safe
operational details such as status, amount, currency, references, terminal,
payment method, POS session, source, and timestamps. The views and access rights
are read-only, relational navigation is disabled, and the existing company rule
limits records to the user's allowed companies.

This screen does not provide manual status changes or an administrative Query
Order action.

## Troubleshooting

### OPay is missing from the terminal selector

- Confirm the addon is installed or upgraded.
- Select a Bank journal on the payment method.
- Set Integration to Terminal before looking for OPay.
- Restart Odoo and rebuild/refresh POS assets after upgrading the addon.

### The amount does not appear on the terminal

- Confirm the payment method is assigned to the active POS.
- Confirm all OPay fields are complete.
- Recheck the Business ID, Branch ID, terminal serial number, and `subSceneEnum`
  with OPay.
- Confirm the order currency is NGN.
- Confirm the Odoo server has outbound HTTPS access to OPay.
- Search the Odoo log for `event=opay_create_payment_api` using the safe
  `attempt_id`, `reference`, or `out_order_no` fields.

### The payment remains waiting

- Ask the cashier to click **Check Payment Status** after the physical terminal
  has reached a result.
- Do not remove the line or start a second OPay payment while the result is
  waiting, pending, or uncertain.
- Query Order is deliberately not polled automatically.

### The webhook does not arrive

- Confirm the displayed webhook URL uses a public HTTPS hostname, not localhost.
- Confirm Odoo's `web.base.url` represents the externally reachable origin.
- Confirm DNS, TLS, firewall, reverse proxy, and tunnel rules allow HTTP POST to
  `/pos_opay/notification`.
- Confirm the exact generated URL is registered with OPay for payment
  notifications.
- Search the Odoo log for `event=opay_webhook_processing`.
- A `400` response with `outcome=rejected` means Odoo received the callback but
  rejected its authentication, envelope, or transaction correlation. Inspect
  the safe `reason` code; do not bypass verification.

### A Create response was lost or timed out

Treat the attempt as uncertain. The addon does not automatically retry Create
Payment because OPay may already have accepted the original request. Use Check
Payment Status to reconcile the stable `outOrderNo`.

### Cancel does not remove a waiting line

This is intentional. Local cancellation cannot prove that a remote payment is
cancelled. Check the status and release the attempt only after OPay confirms
`FAIL`, `CLOSE`, or `CANCEL`.

### A payment method cannot be deleted

Odoo prevents deletion when historical POS payment records reference that
payment method. Keep/archive the historical method and create a new configured
payment method rather than deleting referenced accounting/POS history.

## Security notes

- `clientAuthKey`, the merchant private key, and the OPay public key stay on the
  Odoo server and are not loaded into POS cashier JavaScript.
- Restricted key fields require the Odoo ERP Manager group.
- The client authentication key and merchant private key use password-style
  administrative widgets. A password widget masks display only; it does not
  provide database encryption at rest.
- Protect the Odoo database, backups, administrator accounts, configuration
  exports, and server filesystem according to your organization's secret policy.
- The POS browser never calls OPay directly.
- OPay responses and webhooks are signature-verified and decrypted before their
  content is trusted.
- Invalid signatures, expired webhook timestamps, mismatched amounts, currencies,
  merchants, terminals, and references fail closed.
- HTTPS certificate verification remains enabled.
- Logs contain operational identifiers and timing, but do not intentionally log
  authentication keys, RSA key material, signatures, encrypted/decrypted
  envelopes, or customer/card/account payloads.

## Known limitations

- Odoo 19 Enterprise compatibility is not yet verified.
- Live OPay webhook delivery and end-to-end callback interoperability are not yet
  verified.
- Refunds and reversals are not implemented because no supported contract has
  been adopted by this addon.
- Only NGN and the Nigeria OPay POS market are supported.
- Split payments and OPay split settlement are not supported (`isSplit = N`).
- Shared-terminal operation is not supported.
- No automatic Query Order polling or automatic HTTP retry is performed.
- The release package has been validated for direct addon installation, but the
  Odoo Apps listing visuals, submission account setup, official certification,
  and an external support SLA are not yet provided or verified.

## Further documentation

- [OPay onboarding and configuration](docs/OPAY_ONBOARDING.md)
- [OPay POS API documentation](https://documentation.opayweb.com/doc/offline/pos-api.html)
- [OPay authentication documentation](https://documentation.opayweb.com/doc/offline/authentication.html)
- [OPay Query Order documentation](https://documentation.opayweb.com/doc/offline/query-order-detail.html)
- [OPay webhook documentation](https://documentation.opayweb.com/doc/offline/general-payment-webhook.html)
