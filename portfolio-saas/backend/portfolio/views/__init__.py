"""Portfolio endpoints, split by concern.

Was one 2,871-line module. The seams follow the groupings `urls.py` already
used: `catalog`, `ledger`, `valuation`, `analytics`, `admin_ops`, and `_common`
for what two or more of them share.

Import from the concern module, not from here -- `from .views.ledger import
TradeView`. This package root deliberately exports nothing, so there is exactly
one place each view can be imported from and one place to look for it.

A view belongs in the module its URL prefix belongs to. Put a shared helper in
`_common` only once a second module needs it.
"""
