# oomtm

Shared library for MTM (Aarhus Kommune) OpenOrchestrator processes.

Centralises the gnarly integration code we used to copy-paste across robots:

| Module | Covers |
|---|---|
| `oomtm.sharepoint` | SharePoint Online cert auth, folder create/delete, chunked upload/download, share-link generation, plus filename sanitization and path-length truncation tailored to SharePoint Online's 400-char URL limit. |
| `oomtm.go` | GO API: NTLM session, document metadata, chunked download with the `ows_EncodedAbsUrl` fallback for files >10 MB, server-side PDF conversion. |
| `oomtm.nova` | KMD Nova: DigiCert-intermediate-aware TLS wrapper (`nova_request`), bearer-token refresh with 90-min cache helper, document list + sub-document expansion, file download. |
| `oomtm.convert` | **One way to PDF for every robot**: `can_convert(name)` says whether a file can become a PDF, `convert(src, out_dir)` makes it and returns a `Result` (`ok`, `pdf`, `note` in Danish for the caseworker, a mail's real `attachments`, `warnings` such as tracked changes or hidden sheets). Mails, HTML, text and CSV are laid out by **WeasyPrint**, Office files by **LibreOffice**, images by Pillow, PDFs are left as they are. All free and open source; many robots can run at once. Install with the `[pdf]` extra. |
| `oomtm.mail` | `.eml`/`.msg` read into one HTML page: Danish header (Fra, Sendt in Danish time, Til, Cc, Emne, Vedhæftet), the body with its inline images, everything active removed and remote images never fetched. |
| `oomtm.engines` | The engines behind `convert`: WeasyPrint in a reusable child process (killed and restarted if a page hangs), LibreOffice with reused, locked profiles. Both installed without admin, from [oomtm_setup](https://github.com/mtm-aarhus/oomtm_setup) first and the original address second, SHA-256 checked. |
| `oomtm.pdf` | Installing LibreOffice and Tesseract without admin (`ensure_libreoffice()`, `ensure_tesseract()`): LibreOffice is unpacked from its MSI and Tesseract from its installer into the user's AppData. `image_to_pdf` (every page of a multi-page TIFF). The old entry points `convert_to_pdf`/`office_to_pdf` now call `oomtm.convert`, so old callers get the same PDFs. |

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

## Robot binary bootstrap

Windows robot users do not need admin rights for the LibreOffice/WeasyPrint/Tesseract bootstrap paths. Defaults land under `%LOCALAPPDATA%\oomtm`; override these when using an internal mirror or a fixed robot tools folder:

- `LIBREOFFICE_MSI_URL`, `OOMTM_LIBREOFFICE_DIR`, `OOMTM_LO_PROFILE_DIR`
- `OOMTM_WEASYPRINT_ZIP_URL`, `OOMTM_WEASYPRINT_DIR`
- `OOMTM_TESSERACT_EXE_URL`, `OOMTM_TESSERACT_DIR`
- `OOMTM_7ZIP_MSI_URL`, `OOMTM_TOOLS_DIR`
- `OOMTM_TESSDATA_DIR`, `OOMTM_TESSDATA_BASE_URL`

Existing installs still win when `LIBREOFFICE_PATH` or `TESSERACT_PATH` points at a binary.

## Tests

Tests live in `tests/`, and each file runs as a plain script (and under pytest). Use a venv in `tests/.venv` (git-ignored) with the **same packages as the robots** (the `pdf` extra), so a test proves what the robots will do. The conversion test also needs python-docx, openpyxl, python-pptx (to make its test files) and pymupdf (to read the PDFs back):

```
python -m venv tests/.venv
tests/.venv/Scripts/python.exe -m pip install -e ".[pdf]" python-docx openpyxl python-pptx pymupdf
tests/.venv/Scripts/python.exe tests/test_pdf_images.py
tests/.venv/Scripts/python.exe tests/test_konvertering.py
```

- `tests/test_pdf_images.py`: `image_to_pdf` makes one page per image, and one page per page of a multi-page TIFF (a scanned letter). Before 2026-11-17 it kept only the first page.
- `tests/test_konvertering.py`: makes its own test files (Word, Excel, PowerPoint, RTF, text in three character sets, CSV, HTML, mails with inline images, attachments and emoji, images, broken files) and converts them with the real engines, installed without admin like on a robot machine. It checks the text (æ, ø, å, €, ½ and no wrongly decoded characters), A4, embedded fonts, inline images, a mail's attachments and the caseworker's note for files that can't become a PDF. It stops a page that hangs, runs 8 robots at once, and saves a picture of each PDF's first pages in `tests/ud/index.html`. Real mails put in `tests/egne_filer/` (never in git: personal data) are converted too. The first run on a machine downloads the engines and takes a few minutes.

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
