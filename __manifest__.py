{
    "name": "OPay POS Terminal",
    "version": "18.0.1.0.1",
    "summary": "OPay payment terminal integration for Odoo Point of Sale",
    "description": """
Connect Odoo Point of Sale to OPay physical POS terminals in Nigeria.

The addon provides server-side authenticated Create Payment and Query Order
requests, authenticated webhook processing, cashier-triggered payment-status
checks, and auditable payment-attempt records.

External service disclosure: after an administrator deliberately configures
OPay and a cashier starts or checks an OPay payment, the Odoo server sends OPay
the configured merchant and terminal identifiers, payment reference, amount,
NGN currency, expiry, scene/sub-scene, split indicator, and authenticated
protocol envelope. Odoo receives OPay order references, transaction statuses,
response messages, and authenticated webhook/query data. This addon does not
send customer card, PIN, bank-account, or wallet details in these requests.

Requires an OPay-enabled physical terminal, Nigeria/NGN operation, and an Odoo
deployment that permits third-party Python addons. Odoo Online (SaaS) is not
supported. Query Order is cashier-triggered only; there is no automatic Query
polling, blind Create retry, or refund/reversal implementation.
""",
    "category": "Sales/Point of Sale",
    "license": "LGPL-3",
    "author": "Opay Digital Services Limited",

    "depends": [
        "point_of_sale",
    ],

    "data": [
        "security/pos_opay_security.xml",
        "security/ir.model.access.csv",
        "views/pos_payment_method_views.xml",
        "views/opay_payment_attempt_views.xml",
    ],

    "assets": {
        "point_of_sale._assets_pos": [
            "pos_opay/static/src/app/**/*",
        ],
        "web.assets_backend": [
            "pos_opay/static/src/backend/fields/opay_secret_field.js",
            "pos_opay/static/src/backend/fields/opay_secret_field.xml",
        ],
        "web.assets_unit_tests": [
            ("include", "point_of_sale._assets_pos"),
            "pos_opay/static/tests/unit/data/**/*",
            "pos_opay/static/tests/unit/**/*.test.js",
        ],
    },

    "installable": True,
    "application": False,
}
