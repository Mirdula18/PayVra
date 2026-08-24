"""Razorpay integration: Payment Links, webhooks, reconciliation inputs.

Never parses a webhook body before verifying the signature over the raw bytes. Never uses live
keys. Never stores card data. HTTP calls are confined to razorpay/client.py.
"""
