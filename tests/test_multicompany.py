from datetime import timedelta
from uuid import uuid4

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestOPayMultiCompany(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env.company
        cls.company_b = cls.env["res.company"].create(
            {"name": "OPay Phase 7.1 Company B"}
        )
        cls.payment_method_a = cls._create_payment_method(cls.company_a, "A")
        cls.payment_method_b = cls._create_payment_method(cls.company_b, "B")
        cls.config_a, cls.session_a = cls._create_pos(cls.company_a, "A")
        cls.config_b, cls.session_b = cls._create_pos(cls.company_b, "B")
        cls.attempt_a = cls._create_attempt(
            cls.payment_method_a, cls.session_a, "A"
        )
        cls.attempt_b = cls._create_attempt(
            cls.payment_method_b, cls.session_b, "B"
        )
        cls.manager = cls.env.ref("base.user_admin")
        cls.manager.write(
            {
                "company_id": cls.company_a.id,
                "company_ids": [
                    Command.set((cls.company_a | cls.company_b).ids)
                ],
            }
        )

    @classmethod
    def _create_payment_method(cls, company, suffix):
        return cls.env["pos.payment.method"].with_company(company).create(
            {
                "name": f"OPay Phase 7.1 Method {suffix}",
                "company_id": company.id,
            }
        )

    @classmethod
    def _create_pos(cls, company, suffix):
        config = cls.env["pos.config"].with_company(company).create(
            {
                "name": f"OPay Phase 7.1 POS {suffix}",
                "company_id": company.id,
            }
        )
        session = cls.env["pos.session"].with_company(company).create(
            {
                "name": f"OPay Phase 7.1 Session {suffix}",
                "config_id": config.id,
                "user_id": cls.env.user.id,
                "state": "opened",
            }
        )
        return config, session

    @classmethod
    def _create_attempt(cls, payment_method, session, suffix):
        return cls.env["pos.opay.payment.attempt"].create(
            cls._attempt_values(payment_method, session, suffix)
        )

    @classmethod
    def _attempt_values(cls, payment_method, session, suffix):
        reference = str(uuid4())
        return {
            "payment_method_id": payment_method.id,
            "pos_session_id": session.id,
            "pos_config_id": session.config_id.id,
            "payment_reference": reference,
            "out_order_no": reference.replace("-", "").upper(),
            "order_no": f"OPAY-PHASE71-{suffix}",
            "amount": "100.00",
            "currency": "NGN",
            "head_merchant_id": f"FAKE-HEAD-{suffix}",
            "merchant_id": f"FAKE-MERCHANT-{suffix}",
            "terminal_sn": f"FAKE-TERMINAL-{suffix}",
            "status": "waiting",
            "expires_at": fields.Datetime.now() + timedelta(minutes=3),
            "last_source": "create",
        }

    def _attempt_model_for(self, companies):
        return (
            self.env["pos.opay.payment.attempt"]
            .with_user(self.manager)
            .with_context(allowed_company_ids=companies.ids)
        )

    def _create_bank_journal(self, company, suffix):
        return self.env["account.journal"].with_company(company).create(
            {
                "name": f"OPay Odoo 18 Bank {suffix}",
                "code": f"O{suffix}18",
                "type": "bank",
                "company_id": company.id,
            }
        )

    def _opay_payment_method_values(
        self, company, suffix, *, journal=None, configs=None
    ):
        return {
            "name": f"OPay Odoo 18 Company Method {suffix}",
            "company_id": company.id,
            "payment_method_type": "terminal",
            "use_payment_terminal": "opay",
            "journal_id": journal.id if journal else False,
            "config_ids": [Command.set(configs.ids)] if configs else False,
            "opay_head_merchant_id": f"FAKE-HEAD-18-{suffix}",
            "opay_merchant_id": f"FAKE-MERCHANT-18-{suffix}",
            "opay_terminal_sn": f"FAKE-TERMINAL-18-{suffix}",
            "opay_client_auth_key": f"FAKE-AUTH-18-{suffix}",
            "opay_public_key": f"FAKE-PUBLIC-18-{suffix}",
            "opay_merchant_private_key": f"FAKE-PRIVATE-18-{suffix}",
            "opay_sub_scene_enum": f"FAKE-SCENE-18-{suffix}",
        }

    def test_opay_same_company_journal_and_config_succeed(self):
        journal_a = self._create_bank_journal(self.company_a, "A")

        payment_method = self.env["pos.payment.method"].create(
            self._opay_payment_method_values(
                self.company_a,
                "VALID",
                journal=journal_a,
                configs=self.config_a,
            )
        )

        self.assertEqual(payment_method.company_id, self.company_a)
        self.assertEqual(payment_method.journal_id.company_id, self.company_a)
        self.assertEqual(payment_method.config_ids, self.config_a)

    def test_opay_cross_company_journal_is_rejected(self):
        journal_b = self._create_bank_journal(self.company_b, "B")

        with self.assertRaisesRegex(ValidationError, "journal.*company"):
            self.env["pos.payment.method"].create(
                self._opay_payment_method_values(
                    self.company_a,
                    "WRONG-JOURNAL",
                    journal=journal_b,
                )
            )

    def test_opay_cross_company_config_is_rejected(self):
        journal_a = self._create_bank_journal(self.company_a, "A")

        with self.assertRaisesRegex(ValidationError, "Every Point of Sale"):
            self.env["pos.payment.method"].create(
                self._opay_payment_method_values(
                    self.company_a,
                    "WRONG-CONFIG",
                    journal=journal_a,
                    configs=self.config_b,
                )
            )

    def test_company_consistent_attempt_succeeds(self):
        attempt_model = self.env["pos.opay.payment.attempt"]
        self.assertTrue(attempt_model._check_company_auto)
        for field_name in (
            "payment_method_id",
            "pos_session_id",
            "pos_config_id",
        ):
            self.assertTrue(attempt_model._fields[field_name].check_company)
        self.assertEqual(self.attempt_a.company_id, self.company_a)
        self.assertEqual(self.attempt_a.payment_method_id, self.payment_method_a)
        self.assertEqual(self.attempt_a.pos_session_id, self.session_a)
        self.assertEqual(self.attempt_a.pos_config_id, self.config_a)

    def test_cross_company_session_and_config_are_rejected(self):
        values = {
            **self._attempt_values(self.payment_method_a, self.session_b, "CROSS"),
            "pos_config_id": self.config_b.id,
        }
        with self.assertRaises(UserError):
            self.env["pos.opay.payment.attempt"].create(values)

    def test_one_active_company_only_exposes_its_attempts(self):
        attempts = self._attempt_model_for(self.company_a)
        visible = attempts.search(
            [("id", "in", (self.attempt_a | self.attempt_b).ids)]
        )

        self.assertEqual(visible.ids, self.attempt_a.ids)
        self.assertEqual(
            attempts.browse(self.attempt_a.id).read(["company_id"])[0][
                "company_id"
            ][0],
            self.company_a.id,
        )

    def test_both_active_companies_expose_both_attempts(self):
        companies = self.company_a | self.company_b
        attempts = self._attempt_model_for(companies)
        visible = attempts.search(
            [("id", "in", (self.attempt_a | self.attempt_b).ids)]
        )

        self.assertEqual(set(visible.ids), set((self.attempt_a | self.attempt_b).ids))
        attempts.browse(self.attempt_b.id).check_access("read")

    def test_direct_cross_company_access_is_blocked(self):
        attempt_b = self._attempt_model_for(self.company_a).browse(self.attempt_b.id)

        with self.assertRaises(AccessError):
            attempt_b.read(["status"])
