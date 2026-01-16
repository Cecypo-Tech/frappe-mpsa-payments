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
		$("div").remove(".form-dashboard-section.custom");
		frm.dashboard.add_section(
			frappe.render_template("account_balance", {
				data: JSON.parse(frm.doc.account_balance),
			})
		);
		frm.dashboard.show();
	},

	check_transaction_status: function (frm) {
		if (!frm.doc.initiator_name && !frm.doc.security_credential) {
			frappe.throw(__("Please set the initiator name and the security credential"));
			return;
		}

		frappe.realtime.on("mpesa_transaction_status_update", (data) => {
			frappe.hide_progress();

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
		});

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
					default: "OK",
					hidden: 1,
				},
			],
			(values) => {
				frappe.call({
					method: "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings.trigger_transaction_status",
					args: {
						mpesa_settings: frm.doc.name,
						transaction_id: values.transaction_id,
						remarks: values.remarks || "OK",
					},
					freeze: true,
					freeze_message: __("Checking transaction status..."),
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
							}
						}
					},
					error: (err) => {
						frappe.hide_progress();
						frappe.msgprint({
							message: __("An error occurred: {0}", [
								err.message || "Unknown error",
							]),
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
});
