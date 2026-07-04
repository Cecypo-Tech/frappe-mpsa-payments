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

		// Pull transaction completion listener — same off/on pattern
		if (frm._mpesa_pull_handler) {
			frappe.realtime.off("mpesa_pull_transaction_complete", frm._mpesa_pull_handler);
		}
		frm._mpesa_pull_handler = (data) => {
			frappe.hide_progress();
			if (frm._mpesa_pull_timeout) {
				clearTimeout(frm._mpesa_pull_timeout);
				frm._mpesa_pull_timeout = null;
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
		};
		frappe.realtime.on("mpesa_pull_transaction_complete", frm._mpesa_pull_handler);
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
								// Auto-cancel progress if Safaricom never calls back
								frm._mpesa_status_timeout = setTimeout(() => {
									frappe.hide_progress();
									frappe.show_alert({
										message: __("No callback received from M-Pesa. Check Error Logs."),
										indicator: "orange",
									});
								}, 60000);
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

	register_pull_transaction: function (frm) {
		if (!frm.doc.pull_transaction_nominated_number) {
			frappe.throw(__("Please set the Pull Transaction Nominated Number first."));
			return;
		}
		frappe.confirm(
			__("Register nominated number {0} with Safaricom for Pull Transaction?", [
				frm.doc.pull_transaction_nominated_number,
			]),
			() => {
				frappe.call({
					method: "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings.register_pull_transaction",
					args: { mpesa_settings: frm.doc.name },
					freeze: true,
					freeze_message: __("Registering with Safaricom..."),
					callback: (r) => {
						if (r.message && r.message.status === "success") {
							frappe.show_alert({
								message: __("Pull Transaction registered successfully."),
								indicator: "green",
							});
						} else {
							frappe.msgprint({
								message: __(
									r.message?.message || __("Registration failed. Check Error Logs.")
								),
								title: __("Registration Error"),
								indicator: "red",
							});
						}
					},
					error: (err) => {
						frappe.msgprint({
							message: __("An error occurred: {0}", [err.message || "Unknown error"]),
							title: __("Error"),
							indicator: "red",
						});
					},
				});
			}
		);
	},

	pull_transactions: function (frm) {
		if (!frm.doc.pull_transaction_nominated_number) {
			frappe.throw(
				__("Please set the Pull Transaction Nominated Number and register before pulling.")
			);
			return;
		}

		frappe.prompt(
			[
				{
					label: __("Start Date"),
					fieldname: "start_date",
					fieldtype: "Datetime",
					reqd: 1,
				},
				{
					label: __("End Date"),
					fieldname: "end_date",
					fieldtype: "Datetime",
					reqd: 1,
				},
				{
					label: __("Offset"),
					fieldname: "offset",
					fieldtype: "Int",
					default: 0,
					description: __("Page offset for pagination (0 = first page)"),
				},
			],
			(values) => {
				frappe.call({
					method: "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.pull_transactions",
					args: {
						mpesa_settings: frm.doc.name,
						start_date: values.start_date,
						end_date: values.end_date,
						offset: values.offset || 0,
					},
					freeze: true,
					freeze_message: __("Pulling transactions from M-Pesa..."),
					callback: (r) => {
						if (r.message) {
							if (r.message.status === "error") {
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
									__("Importing transactions...")
								);
								frm._mpesa_pull_timeout = setTimeout(() => {
									frappe.hide_progress();
									frappe.show_alert({
										message: __("Import is taking longer than expected. Check Error Logs."),
										indicator: "orange",
									});
								}, 60000);
							}
						}
					},
					error: (err) => {
						frappe.msgprint({
							message: __("An error occurred: {0}", [err.message || "Unknown error"]),
							title: __("Error"),
							indicator: "red",
						});
					},
				});
			},
			__("Pull Transactions"),
			__("Pull")
		);
	},
});
