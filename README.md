# oomtm

Shared library for MTM (Aarhus Kommune) OpenOrchestrator processes.

Centralises the gnarly integration code we used to copy-paste across robots:

| Module | Covers |
|---|---|
| `oomtm.sharepoint` | SharePoint Online cert auth, folder create/delete, chunked upload/download, share-link generation, plus filename sanitization and path-length truncation tailored to SharePoint Online's 400-char URL limit. |
| `oomtm.go` | GO API: NTLM session, document metadata, chunked download with the `ows_EncodedAbsUrl` fallback for files >10 MB, server-side PDF conversion. |
| `oomtm.nova` | KMD Nova: DigiCert-intermediate-aware TLS wrapper (`nova_request`), bearer-token refresh with 90-min cache helper, document list + sub-document expansion, file download. |
| `oomtm.pdf` *(planned)* | LibreOffice headless conversion, photo→PDF, Tesseract OCR (dan+eng), PyMuPDF true redaction, metadata scrubbing. |

## Design

- **Credentials never live in the lib.** Each OO process pulls credentials from `OrchestratorConnection` and passes them in as arguments. The lib has no dependency on OpenOrchestrator itself.
- **Functions take primitives, not OO objects.** Makes the lib unit-testable and reusable outside OO.
- **One source of truth.** Bug fixes in one place propagate to every robot.

## Install

In a consuming process's `pyproject.toml`:

```toml
dependencies = [
    "oomtm @ git+https://github.com/mtm-aarhus/oomtm.git@main",
    # ...other deps
]
```

Pin to a tag for production stability:

```toml
"oomtm @ git+https://github.com/mtm-aarhus/oomtm.git@v0.1.0",
```

## Usage examples

### SharePoint upload

```python
from oomtm import sharepoint as sp

ctx = sp.connect(
    site_url=oo.get_constant("KontAKTSharePoint").value,
    tenant=oo.get_credential("SharePointCert").username,
    client_id=...,
    thumbprint=...,
    cert_path=...,
)

case_folder = f"{case_id} - {sp.sanitize_title(case_title)[:80]}"
ref_folder  = f"{external_id} - {sp.sanitize_title(ref_title)[:80]}"
path = sp.build_server_relative_path(site_url, "Delte dokumenter", case_folder, ref_folder)
sp.ensure_folder(ctx, path)
sp.upload_file(ctx, path, "C:/tmp/dok.pdf")
```

### GO download

```python
from oomtm import go

session = go.session(oo.get_credential("GOAktApiUser").username,
                     oo.get_credential("GOAktApiUser").password)
go_url  = oo.get_constant("GOApiURL").value

meta = go.fetch_metadata(session, go_url, dok_id)
go.download_file(session, go_url, dok_id, local_path=f"C:/tmp/{dok_id}.{meta['ext']}")

# Built-in PDF conversion (GO docs only)
pdf_bytes = go.pdf_convert(
    username=oo.get_credential("GOAktApiUser").username,
    password=oo.get_credential("GOAktApiUser").password,
    base_url=go_url,
    dok_id=dok_id,
    version_ui=meta["version_ui"],
)
```

### Nova download

```python
from oomtm import nova

token, ts, refreshed = nova.get_token(
    current_token=oo.get_credential("KMDAccessToken").password,
    current_timestamp_str=oo.get_constant("KMDTokenTimestamp").value,
    token_url=oo.get_credential("KMDAccessToken").username,
    client_id="aarhus_kommune",
    client_secret=oo.get_credential("KMDClientSecret").password,
)
if refreshed:
    oo.update_credential("KMDAccessToken",
                         oo.get_credential("KMDAccessToken").username, token)
    oo.update_constant("KMDTokenTimestamp", ts.strftime("%d-%m-%Y %H:%M:%S"))

nova.download_file(token, base_url=oo.get_constant("KMDNovaURL").value,
                   document_uuid=doc_uuid, local_path=f"C:/tmp/{doc_uuid}.pdf")
```
