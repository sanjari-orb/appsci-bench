"""DB-access tools for the invoice-review agent.

One tool per file. The agent calls these instead of writing ad-hoc SQL, so all
database access is parameterised and lives in a single, testable layer.
"""
