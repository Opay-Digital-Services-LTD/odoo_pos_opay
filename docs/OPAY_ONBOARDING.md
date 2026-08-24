# OPay Onboarding and Configuration Guide

This guide describes the values required by `pos_opay`, where they are entered,
and how the addon uses them. Obtain production values through your authorized
OPay business onboarding channel. Do not copy credentials from another merchant,
branch, or terminal.

Do not put real credentials, merchant identifiers, terminal serial numbers, or
key material in source control, screenshots, tickets, chat messages, or example
configuration files.

## Supported integration profile

`pos_opay` currently targets:

- Odoo 19.0 Point of Sale;
- Nigeria;
- NGN;
- OPay's physical wireless POS terminal API;
- `sceneEnum = CASH_API`;
- `isSplit = N`;
- one Odoo OPay payment method for one unique physical terminal serial number.

Odoo 19 Community has been verified in the current environment.

## Values to request from OPay

| Odoo field | OPay/API name | Where the addon uses it |
| --- | --- | --- |
| OPay Business ID (`headMerchantId`) | `headMerchantId` | Create Payment and Query Order payloads; webhook/query correlation when returned |
| OPay Branch ID (`merchantId`) | `merchantId` | Create Payment and Query Order payloads; webhook merchant lookup and correlation |
| OPay Terminal Serial Number (`sn`) | `sn` | Routes Create Payment to the physical terminal; validates webhook/query results |
| OPay Sub Scene (`subSceneEnum`) | `subSceneEnum` | Create Payment payload |
| OPay Client Auth Key (`clientAuthKey`) | `clientAuthKey` | Authenticated OPay request header and webhook authentication |
| OPay Public Key | OPay RSA public key | Encrypts request content and verifies OPay API/webhook signatures |
| Merchant Private Key | merchant RSA private key | Signs requests and decrypts authenticated OPay API/webhook content |
| OPay Webhook URL | payment notification/callback URL | Allows OPay to send a final status to the Odoo server |

The Business ID and Branch ID are different identifiers. Do not substitute one
for the other. Confirm the correct `subSceneEnum` with OPay rather than guessing
or copying a value from another integration.

## Credential and key preparation

### OPay Business ID / `headMerchantId`

This identifies the head merchant/business. The Odoo server reads it from the
payment method and includes it in Create Payment and Query Order requests. It is
also retained on each internal payment attempt for exact result correlation.

### OPay Branch ID / `merchantId`

This identifies the OPay branch merchant. The server includes it in Create
Payment and Query Order. The public webhook route uses the documented merchant
header to locate candidate OPay payment methods before authentication and exact
attempt correlation.

### Terminal serial number / `sn`

Enter the serial number of the physical terminal that must receive this payment
method's amounts. The addon prevents the same serial number from being saved on
another OPay payment method, including in another Odoo company.

Do not reuse one configured payment method for a different physical terminal.
Create a separate OPay payment method for each terminal.

### `subSceneEnum`

Enter the exact value assigned or confirmed by OPay for this POS integration.
The addon sends it unchanged in Create Payment. It does not infer or manufacture
a default value.

### `clientAuthKey`

Enter the exact authentication value provided through OPay onboarding or the
authorized OPay business configuration channel. The addon sends it from the
Odoo server and checks it on incoming webhook envelopes.

This value is sensitive. It is restricted to ERP Managers, is not loaded into
the cashier POS, and must not be included in logs or support material.

### OPay public key

Enter OPay's RSA public key, not the merchant's public key. The addon accepts a
PEM-encoded RSA public key or the Base64 DER form supported by its protocol
layer. It uses this key to:

- encrypt request `paramContent`;
- verify authenticated OPay API response signatures;
- verify OPay webhook signatures.

Confirm the production key and its environment with OPay. Do not use an
unverified key obtained from a public post or another merchant.

### Merchant private key

Enter the RSA private key corresponding to the merchant authentication setup
registered with OPay. OPay's current authentication documentation instructs the
merchant to generate a merchant RSA key pair and provide/register the merchant
public key with OPay. Keep the corresponding private key in Odoo; do not enter
the merchant public key in the **OPay Public Key** field.

The addon accepts an unencrypted PEM private key or the supported Base64 DER
representation. It uses this key only on the Odoo server to:

- sign encrypted request content and timestamp;
- decrypt authenticated OPay API response data;
- decrypt authenticated webhook content.

The key must not be password-encrypted because the current server loader does
not accept a private-key passphrase. Restrict access to the Odoo database,
backups, ERP Manager accounts, and configuration exports accordingly.

Password-style Odoo widgets mask the client authentication key, OPay public key,
and merchant private key on screen; this masking is not encryption at rest.

## Configure Odoo

1. Sign in as an authorized Odoo ERP Manager.
2. Open **Point of Sale -> Configuration -> Payment Methods**.
3. Create one payment method for the intended physical terminal.
4. Select a **Bank** journal.
5. Set **Integration** to **Terminal**.
6. Set **Integrate with** (or **Use a Payment Terminal**) to **OPay**.
7. Enter every OPay field listed above.
8. Save the payment method.
9. Add the payment method to the intended Point of Sale configuration.
10. Confirm that the payment method and POS configuration belong to the same
    Odoo company.

If no Bank journal is selected, Odoo may classify the method as pay-later and
hide the terminal integration selector.

## Configure the webhook URL

The payment method displays a read-only URL with this path:

```text
https://<public-odoo-host>/pos_opay/notification
```

The URL is computed from Odoo's base URL and is not an independent per-terminal
text setting.

1. Deploy Odoo behind a stable, publicly reachable HTTPS hostname.
2. Configure Odoo's `web.base.url` to that external origin. When a reverse proxy
   is used, configure Odoo/proxy mode and forwarded host/protocol handling using
   the normal Odoo deployment guidance.
3. Reopen the payment method and confirm the displayed OPay Webhook URL uses the
   correct public HTTPS origin.
4. Using the OPay Business Dashboard **Super Admin** role, open **Developer
   Tool -> POS & Others** and submit that exact URL as the payment webhook. This
   is the location stated in OPay's current POS documentation; contact OPay if
   it is unavailable for your merchant account.
5. Ensure the firewall and reverse proxy allow unauthenticated HTTP POST routing
   to the path. The payload itself is authenticated cryptographically by the
   addon.
6. Keep the Odoo server clock synchronized. Webhook timestamps outside the
   addon's five-minute validation window are rejected.

Do not register a URL containing `localhost`, `127.0.0.1`, a private address, or
an internal-only hostname. OPay's cloud cannot reach those addresses.

The route expects OPay's documented ordinary HTTP JSON webhook contract. It
validates the `merchantId` and `X-Opay-Tranid` headers plus the encrypted
envelope fields. Do not wrap the notification in Odoo JSON-RPC and do not create
a separate custom controller URL.

Live OPay webhook delivery has not yet been verified end to end for this addon.
During rollout, keep the cashier-triggered **Check Payment Status** action
available and compare Odoo's safe webhook logs with the current OPay onboarding
material. Never bypass signature, timestamp, or correlation checks to make an
incompatible callback appear successful.

## How the values remain isolated

The browser request contains only transaction data that the active POS already
knows:

```text
payment-line UUID
amount
POS session ID
```

The browser does not supply authoritative merchant IDs, terminal serial number,
`subSceneEnum`, `clientAuthKey`, or RSA keys. The Odoo backend reads those values
from the configured `pos.payment.method`.

The following fields are restricted to ERP Managers and are not included in POS
frontend payment-method data:

- OPay Client Auth Key;
- OPay Public Key;
- Merchant Private Key.

Merchant IDs, terminal configuration, and `subSceneEnum` also remain server-side
for runtime API construction and are not exposed to the POS loader by this addon.

## Pre-production validation checklist

- [ ] The payment method uses a Bank journal and OPay terminal integration.
- [ ] Business ID and Branch ID were confirmed by OPay.
- [ ] The terminal serial number matches the intended physical terminal.
- [ ] The terminal serial number is not configured on another OPay payment method.
- [ ] `subSceneEnum` was confirmed by OPay.
- [ ] `clientAuthKey` was entered only in the restricted Odoo field.
- [ ] The OPay public key and merchant private key form the expected production setup.
- [ ] The Odoo company, POS configuration, session, and payment method match.
- [ ] The POS and Odoo company currency are NGN.
- [ ] Odoo can make outbound HTTPS connections to OPay.
- [ ] The displayed webhook URL is public HTTPS and is registered with OPay.
- [ ] Odoo's system clock is synchronized.
- [ ] A small controlled payment reaches only the configured physical terminal.
- [ ] Create Payment acceptance leaves Odoo waiting rather than paid.
- [ ] Check Payment Status resolves the existing attempt without another Create.
- [ ] `SUCCESS` completes only the exact correlated payment line.
- [ ] `FAIL`, `CLOSE`, and `CANCEL` release the attempt without completing payment.
- [ ] `PENDING` remains waiting.
- [ ] Odoo logs contain no credentials or key material.

## Operational references

Each attempt stores safe correlation identifiers for support:

- payment-line UUID/reference;
- deterministic `outOrderNo`;
- OPay `orderNo` when returned;
- payment method and terminal serial number;
- amount and NGN currency;
- POS configuration/session/company;
- status, source, created time, expiry time, and finalization time.

ERP Managers can inspect these values through **Point of Sale -> OPay Payment
Attempts**. The screen is read-only and does not expose credentials or allow a
status to be forced. For an active attempt, **Check Payment Status** queries the
already-existing OPay order and refreshes the read-only status and timestamps;
it never creates another payment.

For server logs, search for:

```text
event=opay_create_payment_api
event=opay_query_order_api
event=opay_webhook_processing
event=opay_attempt_finalization
```

Use `attempt_id`, `reference`, `out_order_no`, or `order_no` to correlate events.
Do not paste complete log files into public channels without reviewing them for
other application data outside this addon's logging boundary.

## Status resolution policy

Final payment status comes only from:

1. an authenticated OPay webhook; or
2. an explicit cashier **Check Payment Status** action.

The addon does not automatically poll Query Order. It also does not retry Create
Payment after a timeout, lost browser response, HTTP error, malformed response,
or authentication failure. Such outcomes remain uncertain until the existing
attempt is authoritatively reconciled.

## Go-live claims still requiring verification

Before making broader public support claims, verify and record:

- live OPay webhook delivery, response acknowledgement, and POS notification;
- any OPay certification or production approval requirements;
- vendor support contact and service-level expectations.

Until those checks are complete, describe each item as **Not yet verified**.

## References

- [Addon README](../README.md)
- [OPay POS API](https://documentation.opayweb.com/doc/offline/pos-api.html)
- [OPay authentication](https://documentation.opayweb.com/doc/offline/authentication.html)
- [OPay Query Order Details](https://documentation.opayweb.com/doc/offline/query-order-detail.html)
- [OPay general payment webhook](https://documentation.opayweb.com/doc/offline/general-payment-webhook.html)
