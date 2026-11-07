"""Tiny billing service used as the shared, tightly coupled codebase for Antibody.

Dependency chain: money -> line_items -> tax -> totals -> invoices -> reports.
All data is fake.
"""
