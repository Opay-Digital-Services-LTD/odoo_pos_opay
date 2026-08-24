from ast import literal_eval

from lxml import etree

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestOPayPaymentAttemptViews(TransactionCase):
    SAFE_VIEW_FIELDS = {
        "status",
        "amount",
        "currency",
        "payment_reference",
        "out_order_no",
        "order_no",
        "payment_method_id",
        "terminal_sn",
        "pos_config_id",
        "pos_session_id",
        "company_id",
        "last_source",
        "create_date",
        "expires_at",
        "finalized_at",
    }
    RESTRICTED_FIELDS = {
        "head_merchant_id",
        "merchant_id",
        "last_status_message",
        "opay_client_auth_key",
        "opay_merchant_private_key",
        "opay_public_key",
        "sign",
        "paramContent",
    }

    def _view_root(self, xml_id, view_type):
        view = self.env.ref(xml_id)
        result = self.env["pos.opay.payment.attempt"].get_view(
            view_id=view.id, view_type=view_type
        )
        return etree.fromstring(result["arch"])

    def test_action_menu_and_views_load(self):
        action = self.env.ref("pos_opay.action_pos_opay_payment_attempt")
        menu = self.env.ref("pos_opay.menu_pos_opay_payment_attempt")

        self.assertEqual(action.res_model, "pos.opay.payment.attempt")
        self.assertEqual(action.view_mode, "list,form")
        self.assertEqual(
            action.search_view_id,
            self.env.ref("pos_opay.pos_opay_payment_attempt_view_search"),
        )
        self.assertEqual(menu.action, action)
        self.assertEqual(menu.parent_id, self.env.ref("point_of_sale.menu_point_of_sale"))
        self.assertEqual(menu.group_ids, self.env.ref("base.group_erp_manager"))

        self._view_root("pos_opay.pos_opay_payment_attempt_view_list", "list")
        self._view_root("pos_opay.pos_opay_payment_attempt_view_form", "form")
        self._view_root("pos_opay.pos_opay_payment_attempt_view_search", "search")

    def test_list_and_form_are_read_only_and_expose_only_safe_fields(self):
        for xml_id, view_type in (
            ("pos_opay.pos_opay_payment_attempt_view_list", "list"),
            ("pos_opay.pos_opay_payment_attempt_view_form", "form"),
        ):
            root = self._view_root(xml_id, view_type)
            self.assertEqual(root.get("create"), "0")
            self.assertEqual(root.get("edit"), "0")
            self.assertEqual(root.get("delete"), "0")
            field_names = {field.get("name") for field in root.xpath(".//field")}
            self.assertTrue(field_names <= self.SAFE_VIEW_FIELDS)
            self.assertFalse(field_names & self.RESTRICTED_FIELDS)

        search_root = self._view_root(
            "pos_opay.pos_opay_payment_attempt_view_search", "search"
        )
        search_fields = {
            field.get("name") for field in search_root.xpath(".//field")
        }
        self.assertTrue(search_fields <= self.SAFE_VIEW_FIELDS)
        self.assertFalse(search_fields & self.RESTRICTED_FIELDS)

    def test_attempt_acl_remains_read_only(self):
        access = self.env.ref("pos_opay.access_pos_opay_payment_attempt_manager")

        self.assertTrue(access.perm_read)
        self.assertFalse(access.perm_create)
        self.assertFalse(access.perm_write)
        self.assertFalse(access.perm_unlink)

        attempt_model = self.env["pos.opay.payment.attempt"].with_user(
            self.env.ref("base.user_admin")
        )
        attempt_model.check_access("read")
        with self.assertRaises(AccessError):
            attempt_model.check_access("create")
        with self.assertRaises(AccessError):
            attempt_model.check_access("write")
        with self.assertRaises(AccessError):
            attempt_model.check_access("unlink")

    def test_active_attempt_form_has_manager_status_check_action(self):
        root = self._view_root(
            "pos_opay.pos_opay_payment_attempt_view_form", "form"
        )
        buttons = root.xpath(
            ".//button[@name='action_opay_check_payment_status']"
        )

        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].get("type"), "object")
        self.assertIn("uncertain", buttons[0].get("invisible"))

        raw_root = etree.fromstring(
            self.env.ref(
                "pos_opay.pos_opay_payment_attempt_view_form"
            ).arch_db
        )
        raw_button = raw_root.xpath(
            ".//button[@name='action_opay_check_payment_status']"
        )[0]
        self.assertEqual(raw_button.get("groups"), "base.group_erp_manager")

    def test_operational_relations_cannot_open_configuration_forms(self):
        relation_fields = {"payment_method_id", "pos_config_id", "pos_session_id"}

        for xml_id, view_type in (
            ("pos_opay.pos_opay_payment_attempt_view_list", "list"),
            ("pos_opay.pos_opay_payment_attempt_view_form", "form"),
        ):
            root = self._view_root(xml_id, view_type)
            for field_name in relation_fields:
                field = root.xpath(f".//field[@name='{field_name}']")
                self.assertEqual(len(field), 1)
                self.assertTrue(literal_eval(field[0].get("options"))["no_open"])

    def test_company_record_rule_remains_global(self):
        rule = self.env.ref("pos_opay.pos_opay_payment_attempt_company_rule")

        self.assertFalse(rule.groups)
        self.assertEqual(rule.domain_force, "[('company_id', 'in', company_ids)]")
