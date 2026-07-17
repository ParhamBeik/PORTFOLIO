"""Renderers.

M5: DRF's default JSONEncoder turns Decimal into float, which loses precision on
large Toman values. We render Decimal as a string instead. Clients coerce with
Number() (the frontend's fmtNum already does), so this is wire-compatible for
this app while staying exact end-to-end.
"""
from decimal import Decimal

from rest_framework.renderers import JSONRenderer
from rest_framework.utils.encoders import JSONEncoder


class DecimalAsStringEncoder(JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


class DecimalStringJSONRenderer(JSONRenderer):
    encoder_class = DecimalAsStringEncoder
