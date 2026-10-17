"""v2 speech plugin: minimax. The v1 implementation behind the v2 contract.

Only the import is added here - the proven v1 object is unchanged, plus the
default quote/report so the accounting contract is answered. The plugin tree is
what the host discovers; nothing above host/pluginhost.py imports a plugin by
name.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from providers.minimax import MiniMax as _Impl
from providers._accounting import AccountingMixin


class Speech(_Impl, AccountingMixin):
    pass
