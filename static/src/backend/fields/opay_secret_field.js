import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useInputField } from "@web/views/fields/input_field_hook";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

import { Component, useState } from "@odoo/owl";

export class OPaySecretField extends Component {
    static template = "pos_opay.OPaySecretField";
    static props = {
        ...standardFieldProps,
        placeholder: { type: String, optional: true },
    };

    setup() {
        this.state = useState({ isRevealed: false });
        useInputField({
            getValue: () => this.props.record.data[this.props.name] || "",
        });
    }

    get buttonTitle() {
        return this.state.isRevealed ? _t("Hide value") : _t("Reveal value");
    }

    get displayedValue() {
        return this.props.record.data[this.props.name] || "";
    }

    onToggleReveal() {
        this.state.isRevealed = !this.state.isRevealed;
    }
}

export const opaySecretField = {
    component: OPaySecretField,
    displayName: _t("Protected OPay value"),
    supportedTypes: ["char", "text"],
    extractProps: ({ attrs }) => ({ placeholder: attrs.placeholder }),
};

registry.category("fields").add("opay_secret", opaySecretField);
