"""v2 speech plugin: minimax. The v1 implementation behind the v2 contract.

The proven v1 object is unchanged, plus the default quote/report and an adapter
that coerces the v1 Audio (.bytes) into a host.contracts.Audio (.data). The
plugin tree is what the host discovers; nothing above host/pluginhost.py imports
a plugin by name.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from host.contracts import to_audio
from providers.minimax import MiniMax as _Impl
from providers._accounting import AccountingMixin


class Speech(_Impl, AccountingMixin):
    def synth(self, text, voice, opts, cred):
        return to_audio(_Impl.synth(self, text, voice, opts, cred))
