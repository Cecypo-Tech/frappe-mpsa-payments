frappe.listview_settings['Mpesa C2B Payment Register'] = {
    onload: function(listview) {
        // Add a custom button to the page actions (top bar)
        listview.page.add_inner_button(__("Check Transaction Status"), function() {
            frappe.prompt(
                [
                    {
                        label: "Mpesa Settings",
                        fieldname: "mpesa_settings",
                        fieldtype: "Link",
                        options: "Mpesa Settings",
                        reqd: 1
                    },
                    {
                        label: "Transaction ID",
                        fieldname: "transaction_id",
                        fieldtype: "Data",
                        reqd: 1
                    },
                    {
                        label: "Remarks",
                        fieldname: "remarks",
                        fieldtype: "Small Text"
                    }
                ],
                (values) => {
                    frappe.db.get_value("Mpesa Settings", values.mpesa_settings, ["initiator_name", "security_credential"], (settings) => {
                        if (!settings || (!settings.initiator_name && !settings.security_credential)) {
                            frappe.throw(__("Please set the initiator name and security credential in the selected Mpesa Settings"));
                        }

                        frappe.call({
                            method: "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings.trigger_transaction_status",
                            args: {
                                mpesa_settings: values.mpesa_settings,
                                transaction_id: values.transaction_id,
                                remarks: values.remarks
                            },
                            callback: (r) => {
                                if (r.message && r.message.status === "queued") {
                                    // Wait for the transaction status to be processed
                                } else {
                                    frappe.msgprint({
                                        message: __(r.message.message),
                                        title: r.message.status === "error" ? "Error" : "Success",
                                        indicator: r.message.status === "error" ? "red" : "green"
                                    });
                                }
                            },
                            error: (err) => {
                                frappe.msgprint({
                                    message: __("An error occurred: {0}", [err.message]),
                                    title: "Error",
                                    indicator: "red"
                                });
                            }
                        });
                    });
                },
                __("Transaction Status Query"),
                __("Submit")
            );
        });

        // Same pull as Mpesa Settings > Pull Transactions, just asking which
        // account to use instead of taking it from the open document.
        listview.page.add_inner_button(__("Pull Transactions"), function() {
            const DATE_FORMAT = "YYYY-MM-DD HH:mm:ss";
            const PULL_WINDOW_HOURS = 48;

            frappe.prompt(
                [
                    {
                        label: "Mpesa Settings",
                        fieldname: "mpesa_settings",
                        fieldtype: "Link",
                        options: "Mpesa Settings",
                        reqd: 1
                    },
                    {
                        label: "End Date",
                        fieldname: "end_date",
                        fieldtype: "Datetime",
                        reqd: 1,
                        default: frappe.datetime.now_datetime(),
                        description: __("Pulls the 48 hours ending at this time. Transactions already recorded are skipped.")
                    }
                ],
                (values) => {
                    frappe.db.get_value("Mpesa Settings", values.mpesa_settings, "pull_transaction_nominated_number", (settings) => {
                        if (!settings || !settings.pull_transaction_nominated_number) {
                            frappe.throw(__("Please set the Pull Transaction Nominated Number on the selected Mpesa Settings and register before pulling."));
                        }

                        const start_date = moment(values.end_date, DATE_FORMAT)
                            .subtract(PULL_WINDOW_HOURS, "hours")
                            .format(DATE_FORMAT);

                        frappe.call({
                            method: "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.pull_transactions",
                            args: {
                                mpesa_settings: values.mpesa_settings,
                                start_date: start_date,
                                end_date: values.end_date
                            },
                            freeze: true,
                            freeze_message: __("Pulling transactions from M-Pesa..."),
                            callback: (r) => {
                                if (!r.message) {
                                    return;
                                }
                                if (r.message.status === "error") {
                                    frappe.msgprint({
                                        message: __(r.message.message),
                                        title: __("Error"),
                                        indicator: "red"
                                    });
                                } else {
                                    frappe.show_alert({
                                        message: __(r.message.message),
                                        indicator: "blue"
                                    });
                                }
                            },
                            error: (err) => {
                                frappe.msgprint({
                                    message: __("An error occurred: {0}", [err.message || "Unknown error"]),
                                    title: __("Error"),
                                    indicator: "red"
                                });
                            }
                        });
                    });
                },
                __("Pull Transactions"),
                __("Pull")
            );
        });
    },

    refresh: function(listview) {
        frappe.realtime.on("mpesa_transaction_status", function(data) {
            frappe.msgprint({
                message: __(data.message),
                title: data.status === "success" ? "Success" : "Error",
                indicator: data.status === "success" ? "green" : "red"
            });
            if (data.status === "success" && data.doc_name) {
                listview.refresh();
            }
        });

        // The pull is a background job, so its result arrives here rather than
        // in the button callback. Drop any prior handler so repeated refreshes
        // don't stack listeners and report the same pull several times.
        if (listview._mpesa_pull_handler) {
            frappe.realtime.off("mpesa_pull_transaction_complete", listview._mpesa_pull_handler);
        }
        listview._mpesa_pull_handler = function(data) {
            frappe.msgprint({
                message: __(data.message),
                title: __(data.title),
                indicator: data.status === "error" ? "red" : data.status === "warning" ? "orange" : "green"
            });
            if (data.count) {
                listview.refresh();
            }
        };
        frappe.realtime.on("mpesa_pull_transaction_complete", listview._mpesa_pull_handler);
    }
};