frappe.ui.form.on("Payment Entry", {
	refresh(frm) {
		if (
			frm.doc.docstatus === 0 &&
			!frm.is_new() &&
			frm.doc.payment_type === "Receive" &&
			frm.doc.party_type === "Customer" &&
			frm.doc.custom_payment_gateway_account
		) {
			frm.add_custom_button(__("Initiate M-Pesa STK Push"), async () => {
				if (frm.is_dirty()) {
					await frm.save();
				}
				frappe.call({
					method: "frappe_mpsa_payments.frappe_mpsa_payments.api.payment_entry.initiate_payment_entry_stk",
					args: { payment_entry: frm.doc.name },
					freeze: true,
					freeze_message: __("Sending STK Push..."),
					callback: (r) => {
						if (!r.exc) {
							frappe.show_alert({
								message: __(
									"STK Push sent. Ask the customer to enter their M-Pesa PIN."
								),
								indicator: "green",
							});

							const handler = (data) => {
								if (
									data.reference_doctype === "Payment Entry" &&
									data.reference_name === frm.doc.name
								) {
									frappe.realtime.off("mpesa_stk_payment_completed", handler);
									frappe.show_alert({
										message: __("Payment received. Submitting..."),
										indicator: "green",
									});
									frm.reload_doc();
								}
							};
							frappe.realtime.on("mpesa_stk_payment_completed", handler);
						}
					},
				});
			});
		}
	},
});
