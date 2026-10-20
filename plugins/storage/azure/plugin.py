"""v2 storage plugin: azure. Azure Blob behind the v2 contract.

Same shape as the local plugin - the backend object is the real one, imported
unchanged. The plugin tree is what the host discovers; nothing above
host/pluginhost.py imports a plugin by name.

Reads its config from the environment (FIRESIDE_AZURE_*). With no config it
still loads, it just reports itself unconfigured; the host only selects it when
told to.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from storage.azure_blob import AzureBlobBackend as _Impl


class Storage(_Impl):
    pass
