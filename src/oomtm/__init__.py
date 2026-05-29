"""oomtm — shared library for MTM OpenOrchestrator processes.

Public modules:

- ``oomtm.sharepoint`` — SharePoint Online cert auth + folder/file ops + name sanitization
- ``oomtm.go``         — GO API NTLM session, metadata, chunked download, PDF conversion
- ``oomtm.nova``       — KMD Nova OAuth2 + DigiCert-intermediate-aware HTTP + document API

Each module is independently importable; nothing here is re-exported eagerly
so that one missing optional dep (e.g. Office365-REST-Python-Client) does not
break processes that only use a subset of the lib.
"""

__version__ = "0.1.0"
