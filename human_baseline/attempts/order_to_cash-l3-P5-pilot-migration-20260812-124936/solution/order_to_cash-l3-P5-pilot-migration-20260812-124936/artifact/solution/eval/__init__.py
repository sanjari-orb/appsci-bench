"""Evaluation harness for the invoice-review agent.

`run_eval` reads the labelled examples, calls `respond()` (which looks up each
invoice live in Postgres), and reports the flag metrics AP cares about.
"""
