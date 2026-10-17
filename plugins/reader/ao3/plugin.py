"""v2 reader plugin: ao3. The v1 implementation behind the v2 contract.

The proven v1 object is unchanged; the adapter only coerces the v1 dict shape
into a host.contracts.Book so the host sees one book shape from every reader.
Nothing above host/pluginhost.py imports a plugin by name.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from host.contracts import to_book
from sources.ao3 import Ao3 as _Impl


class Reader(_Impl):
    def read(self, req, fetch, cred=None):
        return to_book(_Impl.read(self, req, fetch, cred))
