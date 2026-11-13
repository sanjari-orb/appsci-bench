"""The reason-generation layer for the agent.

`build_reason` turns a Decision + InvoiceRecord into a plain-language reason for
AP. It uses Claude when a key + SDK are available, and otherwise falls back to a
deterministic template so the pipeline runs fully offline.
"""
