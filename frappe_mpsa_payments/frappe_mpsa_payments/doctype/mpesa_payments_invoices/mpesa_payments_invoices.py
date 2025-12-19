# Copyright (c) 2024, Navari Limited and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class MpesaPaymentsInvoices(Document):
    def db_insert(self, *args, **kwargs):
        pass

    def load_from_db(self):
        pass

    def db_update(self):
        pass

    def delete(self):
        pass

    @staticmethod
    def get_list(filters=None, **kwargs):
        pass

    @staticmethod
    def get_count(filters=None, **kwargs):
        pass

    @staticmethod
    def get_stats(filters=None, **kwargs):
        pass
