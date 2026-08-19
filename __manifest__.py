{
    "name": "OPay POS Terminal",
    "version": "19.0.1.0.0",
    "summary": "OPay payment terminal integration for Odoo Point of Sale",
    "category": "Sales/Point of Sale",
    "license": "LGPL-3",
    "author": "OPay",

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
            "pos_opay/static/src/**/*",
        ],
        "web.assets_unit_tests": [
            "pos_opay/static/tests/unit/data/**/*",
            "pos_opay/static/tests/unit/**/*.test.js",
        ],
    },

    "installable": True,
    "application": False,
}
