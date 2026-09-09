"""oomtm — shared library for MTM OpenOrchestrator processes.

Public modules:

- ``oomtm.sharepoint`` — SharePoint Online cert auth + folder/file ops + name sanitization
- ``oomtm.f2``         — F2REST (cBrain ESDH): hypermedia client, PEO create, journalisation
- ``oomtm.go``         — GO API NTLM session, metadata, chunked download, PDF conversion
- ``oomtm.nova``       — KMD Nova OAuth2 + DigiCert-intermediate-aware HTTP + document API

``oomtm.f2`` is a **verbatim copy** of ``app/integrations/f2_rest.py`` in the
KontAKT repo, where it is developed and tested (``tests/test_f2_rest.py``, plus
``F2-TESTLOG.md`` recording every measurement it is built on). Edit it there and
copy it out again; ``tests/test_copies.py`` fails when the two drift apart. It is
here because a robot cannot import from KontAKT, and installing the library is
better than each robot keeping its own copy.

``oomtm.go`` is on its way out: F2 replaces GetOrganized as the journal, and the
KontAKT robots have moved. It stays for the older Aktbob processes.

Each module is independently importable; nothing here is re-exported eagerly
so that one missing optional dep (e.g. Office365-REST-Python-Client) does not
break processes that only use a subset of the lib.
"""

__version__ = "0.1.0"
