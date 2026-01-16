frappe.listview_settings["Mpesa C2B Payment Register"] = {
	onload: function (listview) {
		// Add a custom button to the page actions (top bar)
		listview.page.add_inner_button(__("Check Transaction Status"), function () {
			frappe.prompt(
				[
					{
						label: "Mpesa Settings",
						fieldname: "mpesa_settings",
						fieldtype: "Link",
						options: "Mpesa Settings",
						reqd: 1,
					},
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
					frappe.db.get_value(
						"Mpesa Settings",
						values.mpesa_settings,
						["initiator_name", "security_credential"],
						(settings) => {
							if (
								!settings ||
								(!settings.initiator_name && !settings.security_credential)
							) {
								frappe.throw(
									__(
										"Please set the initiator name and security credential in the selected Mpesa Settings"
									)
								);
								return;
							}

							frappe.call({
								method: "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings.trigger_transaction_status",
								args: {
									mpesa_settings: values.mpesa_settings,
									transaction_id: values.transaction_id,
									remarks: values.remarks || "OK",
								},
								freeze: true,
								freeze_message: __("Checking transaction status..."),
								callback: (r) => {
									if (r.message) {
										frappe.msgprint({
											message: __(r.message.message),
											title:
												r.message.status === "error" ? "Error" : "Success",
											indicator:
												r.message.status === "error" ? "red" : "green",
										});

										if (r.message.status === "success") {
											listview.refresh();
										}
									}
								},
								error: (err) => {
									frappe.msgprint({
										message: __("An error occurred: {0}", [
											err.message || "Unknown error",
										]),
										title: "Error",
										indicator: "red",
									});
								},
							});
						}
					);
				},
				__("Transaction Status Query"),
				__("Submit")
			);
		});
	},
};
