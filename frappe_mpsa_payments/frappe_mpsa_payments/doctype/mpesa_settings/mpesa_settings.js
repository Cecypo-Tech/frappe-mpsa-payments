// Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Mpesa Settings", {
	onload_post_render: function (frm) {
		frm.events.setup_account_balance_html(frm);
	},

	refresh: function (frm) {
		frappe.realtime.on("refresh_form", function () {
			frm.reload_doc();
		});
		frappe.realtime.on("refresh_mpesa_dashboard", function () {
			frm.reload_doc();
			frm.events.setup_account_balance_html(frm);
		});
		frm.fields_dict["exchange_rates"].grid.get_field("currency").get_query = function () {
			return {
				filters: {
					name: ["!=", "KES"],
				},
			};
		};

		// Register the transaction status listener once per form load,
		// removing any prior instance first so clicks never stack listeners.
		if (frm._mpesa_status_handler) {
			frappe.realtime.off("mpesa_transaction_status_update", frm._mpesa_status_handler);
		}
		frm._mpesa_status_handler = (data) => {
			frappe.hide_progress();
			if (frm._mpesa_status_timeout) {
				clearTimeout(frm._mpesa_status_timeout);
				frm._mpesa_status_timeout = null;
			}
			frappe.msgprint({
				message: __(data.message),
				title: __(data.title),
				indicator:
					data.status === "error"
						? "red"
						: data.status === "warning"
						? "orange"
						: "green",
			});
			if (data.document_name) {
				frappe.show_alert({
					message: __("View transaction: {0}", [data.document_name]),
					indicator: "green",
				});
			}
		};
		frappe.realtime.on("mpesa_transaction_status_update", frm._mpesa_status_handler);
	},

	get_account_balance: function (frm) {
		if (!frm.doc.initiator_name && !frm.doc.security_credential) {
			frappe.throw(__("Please set the initiator name and the security credential"));
		}
		frappe.call({
			method: "get_account_balance_info",
			doc: frm.doc,
		});
	},

	setup_account_balance_html: function (frm) {
		if (!frm.doc.account_balance) return;

		let text = frm.doc.account_balance;
		let rows = text.split("&");

		let data = {};

		rows.forEach((row) => {
			let parts = row.split("|");
			if (parts.length >= 6) {
				let [
					account_type,
					currency,
					current_balance,
					available_balance,
					reserved_balance,
					uncleared_balance,
				] = parts;

				data[account_type] = {
					currency,
					current_balance,
					available_balance,
					reserved_balance,
					uncleared_balance,
				};
			}
		});

		$("div").remove(".form-dashboard-section.custom");

		frm.dashboard.add_section(frappe.render_template("account_balance", { data: data }));

		frm.dashboard.show();
	},

	check_transaction_status: function (frm) {
		if (!frm.doc.initiator_name && !frm.doc.security_credential) {
			frappe.throw(__("Please set the initiator name and the security credential"));
		}
		frappe.clear_cache;
		frappe.prompt(
			[
				{
					label: "Transaction ID",
					fieldname: "transaction_id",
					fieldtype: "Data",
					reqd: 1,
				},
				{
					label: "Remarks",
					fieldname: "remarks",
					fieldtype: "Small Text",
				},
			],
			(values) => {
				frappe.call({
					method: "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings.trigger_transaction_status",
					args: {
						mpesa_settings: frm.doc.name,
						transaction_id: values.transaction_id,
						remarks: values.remarks,
					},
					callback: (r) => {
						if (r.message) {
							if (r.message.status === "error") {
								frappe.hide_progress();
								frappe.msgprint({
									message: __(r.message.message),
									title: __("Error"),
									indicator: "red",
								});
							} else {
								frappe.show_progress(
									__("Processing"),
									50,
									100,
									__("Waiting for M-Pesa callback...")
								);
								// Auto-cancel progress if Safaricom never calls back
								frm._mpesa_status_timeout = setTimeout(() => {
									frappe.hide_progress();
									frappe.show_alert({
										message: __(
											"No callback received from M-Pesa. Check Error Logs."
										),
										indicator: "orange",
									});
								}, 60000);
							}
						}
					},
					error: (err) => {
						frappe.msgprint({
							message: __("An error occurred: {0}", [err.message]),
							title: "Error",
							indicator: "red",
						});
					},
				});
			},
			__("Transaction Status Query"),
			__("Submit")
		);
	},

	update_match_field_options: function (frm, cdt, cdn) {
		const row = frappe.get_doc(cdt, cdn);
		const target_doctype = row.target_doctype;

		if (!target_doctype) {
			frappe.utils.filter_dict(
				frm.fields_dict["reconciliation_order"].grid.grid_rows_by_docname[cdn].docfields,
				{ fieldname: "match_field" }
			)[0].options = [];
			frm.refresh();
			return;
		}

		frappe.call({
			method: "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings.get_doctype_fields",
			args: { doctype: target_doctype },
			callback: function (r) {
				if (r && r.message) {
					let fields = ["", "name", ...r.message];

					frappe.utils.filter_dict(
						frm.fields_dict["reconciliation_order"].grid.grid_rows_by_docname[cdn]
							.docfields,
						{ fieldname: "match_field" }
					)[0].options = fields;

					frm.refresh();
				} else {
					frappe.msgprint(
						__("Could not load field information for DocType: {0}", [target_doctype])
					);
				}
			},
		});
	},
});

frappe.ui.form.on("Mpesa Reconciliation Priority", {
	target_doctype: function (frm, cdt, cdn) {
		frm.events.update_match_field_options(frm, cdt, cdn);
	},

	refresh: function (frm, cdt, cdn) {
		frm.events.update_match_field_options(frm, cdt, cdn);
	},
});

frappe.ui.form.on("Mpesa Conversion Rate", {
	currency: function (frm, cdt, cdn) {
		validate_kes_currency(frm, cdt, cdn);
	},

	exchange_rates_add: function (frm, cdt, cdn) {
		validate_kes_currency(frm, cdt, cdn);
	},
});

function validate_kes_currency(frm, cdt, cdn) {
	const row = locals[cdt][cdn];

	if (row.currency === "KES") {
		row.currency = null;
		frm.refresh_field("exchange_rates");
	}
}
