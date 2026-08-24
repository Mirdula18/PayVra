"""Reconciliation: settle invoices and stop outreach.

When an invoice settles, every pending Action for it must be revoked in the same transaction —
the most important line of code in the product.
"""
