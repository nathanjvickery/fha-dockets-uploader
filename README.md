# Dockets uploader

Streamlit app for uploading daily docket PDFs (day sheets + invoices).
Uploads are delivered to a Google Drive folder via a scoped OAuth token.

All configuration (OAuth credentials, target folder id, passcode) is
supplied via Streamlit secrets — see the docstring in `app.py`. Nothing
sensitive lives in this repository.
