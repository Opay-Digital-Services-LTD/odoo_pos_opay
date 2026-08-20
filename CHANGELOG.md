# Changelog

All notable changes to `pos_opay` are documented in this file. Versions follow
Odoo's five-part addon versioning convention: the Odoo series followed by the
addon release number.

## [19.0.1.0.0]

### Added

- Native OPay physical-terminal registration in Odoo 19 Point of Sale.
- OPay payment-method configuration for Business ID, Branch ID, terminal serial
  number, `subSceneEnum`, `clientAuthKey`, OPay public key, and merchant private
  key.
- Odoo POS `PaymentInterface` implementation for starting terminal payments and
  handling asynchronous results.
- Server-side OPay authentication using RSA encryption, request signing,
  response verification, and response decryption.
- Create Payment support for NGN transactions sent to one configured physical
  OPay terminal.
- Query Order support through the cashier's **Check Payment Status** action.
- Authenticated OPay webhook endpoint with exact payment-attempt correlation and
  idempotent final-status processing.
- POS notification handling for `SUCCESS`, `FAIL`, `CLOSE`, `CANCEL`, and
  `PENDING` results.
- Persistent payment-attempt records with stable references, duplicate Create
  protection, multi-company isolation, and read-only administrative views.
- Safe handling of uncertain Create Payment outcomes without blind retries.
- Concurrency protection for payment creation, recovery, query, and webhook
  finalization races.
- Separate connect/read HTTP timeouts and precise network, HTTP, malformed
  response, authentication, and business-rejection error handling.
- Structured operational logging with safe correlation identifiers and API
  duration measurements.
- Installation, operation, security, troubleshooting, and OPay onboarding
  documentation.

### Security

- OPay credentials and cryptographic keys remain server-side and are excluded
  from POS data and operational logs.
- Webhook results are authenticated, decrypted, and correlated by persisted
  transaction attributes before they can change a payment state.
- Multi-company consistency checks and record rules isolate payment attempts by
  the user's allowed companies.
- Read-only payment-attempt views prevent editing, deletion, or relational
  navigation into sensitive payment-method configuration.

### Operational behavior

- Create Payment acceptance leaves the payment waiting; it never proves that
  the customer paid.
- Final payment status comes from an authenticated webhook or an explicit
  cashier **Check Payment Status** action.
- Query Order is not polled automatically, and Create Payment is never retried
  automatically.
- Communication failures preserve an uncertain payment state until an
  authoritative result is available.

### Known limitations

- Only Odoo 19 Community has been verified; Enterprise compatibility is not yet
  verified.
- Live payment operation is limited to NGN in Nigeria using an OPay-provisioned
  physical terminal enabled for the public wireless/offline POS API.
- Live Create Payment and cashier-triggered Query Order have been verified;
  end-to-end live OPay webhook delivery is not yet verified.
- One OPay payment method maps to one unique terminal serial number. Shared
  terminals and split payments are not supported.
- Refunds and reversals are not implemented because a supported OPay contract
  has not been confirmed.
