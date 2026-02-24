frappe.provide("mpesa_qp");

const MPESA_FORM_CONFIG = {
	auto_save_invoice: true,
	auto_submit_invoice: false,
	merge_payments: true,
};

frappe.ui.form.on("POS Invoice", {
	refresh(frm) {
		if (
			!frm.is_new() &&
			frm.doc.is_pos &&
			frm.doc.docstatus === 0 &&
			frm.doc.outstanding_amount > 0
		) {
			frappe.call({
				method: "frappe_mpsa_payments.frappe_mpsa_payments.api.mpesa_quick_pay.pos_quick_pay_mpesa_process",
				args: {
					action: "check_mpesa_available",
					doctype: frm.doctype,
					company: frm.doc.company,
				},
				callback(r) {
					if (r.message && r.message.available) {
						frm.add_custom_button(
							__("Quick Pay - Mpesa"),
							() => {
								mpesa_qp.show_dialog(
									frm,
									"invoice",
									mpesa_process_payments,
									frm.doc.outstanding_amount,
									MPESA_FORM_CONFIG,
									[],
								);
							},
							__("Mpesa Actions"),
						);
					}
				},
			});
		}
	},
});

function mpesa_process_payments(frm, dialog, outstanding) {
	if (!dialog.selected.length) {
		frappe.msgprint(__("Please select at least one Mpesa payment"));
		return;
	}

	frappe.call({
		method: "frappe_mpsa_payments.frappe_mpsa_payments.api.mpesa_quick_pay.pos_quick_pay_mpesa_process",
		args: {
			action: "process_mpesa",
			doctype: frm.doctype,
			invoice_name: frm.doc.name,
			customer: frm.doc.customer,
			mpesa_payments: dialog.selected.map((p) => p.name).join(","),
			outstanding_amount: outstanding,
			auto_save: 1, // always auto save
			auto_submit: dialog.auto_submit ? 1 : 0,
			merge_payments: dialog.merge_payments ? 1 : 0,
		},
		freeze: true,
		freeze_message: __("Processing Mpesa Payments…"),
		callback(r) {
			if (!r.message || !r.message.success) return;
			dialog.hide();

			let msg = `<p><strong>${__("Mpesa Payments Added Successfully")}</strong></p><ul>`;
			(r.message.payments_added || []).forEach((p) => {
				msg += `<li>${p.mode_of_payment}: ${format_currency(p.amount, frm.doc.currency)} – ${p.reference}</li>`;
			});
			msg += "</ul>";
			if (r.message.saved)
				msg += `<p class="text-success"><i class="fa fa-check"></i> ${__("Invoice saved")}</p>`;
			if (r.message.submitted)
				msg += `<p class="text-success"><i class="fa fa-check"></i> ${__("Invoice submitted")}</p>`;
			if (r.message.error)
				msg += `<p class="text-danger"><i class="fa fa-exclamation-triangle"></i> ${r.message.error}</p>`;

			frappe.msgprint({ title: __("Payment Successful"), message: msg, indicator: "green" });
			frm.reload_doc();
		},
		error(r) {
			frappe.msgprint({
				title: __("Error"),
				message: r.message || __("Failed to process Mpesa payments"),
				indicator: "red",
			});
		},
	});
}
