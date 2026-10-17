"""v2 reader plugin: ao3. The v1 implementation behind the v2 contract.

Only the import is added here - Ao3 is the proven v1 object, unchanged. The
plugin tree is what the host discovers; nothing above host/pluginhost.py imports
a plugin by name.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sources.ao3 import Ao3 as _Impl


class Reader(_Impl):
    pass
