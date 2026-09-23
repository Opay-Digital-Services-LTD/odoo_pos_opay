# Changelog

All notable changes to `pos_opay` are documented in this file. Versions follow
Odoo's five-part addon versioning convention: the Odoo series followed by the
addon release number.

## [19.0.1.0.3]

### Fixed

- Made cashier Retry create a new POS payment line and a new server-derived OPay
  business order reference, instead of resending Create for the old line.
- Kept unresolved or unverified attempts on Check Payment Status; only a
  confirmed final-negative or confirmed-absent result permits a fresh request.
- Clarified that an OPay API `SUCCESS` message on a closed payment describes
  the status check, not a successful payment.
- Prevented rapid repeated Retry clicks from opening multiple replacement
  payment lines.

### Safety

- The previous attempt and its stable reference remain in the audit history.
  No automatic Query polling or blind Create retry was introduced.

## [19.0.1.0.2]

### Fixed

- Gave OPay response-message translation access to the active Odoo environment,
  eliminating missing-language warnings.
- Kept the real concurrency and error-path tests while preventing their expected
  serialization conflicts and rejection logs from incorrectly marking Odoo.sh
  builds as failed or warning builds.

### Safety

- Payment locking, duplicate-Create prevention, final-status conflict handling,
  and production operational logging are unchanged.

## [19.0.1.0.1]

### Changed

- Added safe backend **Check Payment Status** for active read-only payment
  attempts, with a controller-level refresh that keeps the result notification
  visible.
- Recognized the two exact live OPay Query Order `order not exist` response
  contracts observed for missing business/OPay order references; near-matching
  responses continue to fail closed.
- Included sanitized OPay response messages in cashier and administrator
  feedback without exposing protocol payloads or credentials.
- Masked all three RSA/authentication configuration fields in the Odoo backend.
- Added explicit external-service/data-transfer disclosure and aligned release
  documentation with current Odoo Apps publication requirements.

### Safety

- A confirmed missing OPay order may be released for retry, while uncertain or
  unauthenticated near-matches remain blocked.
- Delayed conflicting frontend results cannot downgrade an already successful
  OPay payment line.

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

### Distribution

- The Odoo Apps listing is free under the LGPL-3 license.
