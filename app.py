#!/usr/bin/env python3
"""
Dockets uploader
================
Staff drop Day Sheet + Tax Invoice PDFs; each file is delivered into a
per-day subfolder (DDMMYYYY) of a configured Google Drive folder.

Secrets (Streamlit Cloud app settings → Secrets, TOML):
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN
      OAuth client + Drive-only refresh token (scope: drive)
  FHA_UPLOADER_PASSCODE   staff passcode (empty/absent = no gate)
  FHA_DRIVE_INBOX_ID      Drive folder id of FHA-Dockets/inbox
"""
import hashlib
import os
import re
from datetime import date, timedelta

import requests
import streamlit as st

DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
MAX_FILE_MB = 50


def secret(name, default=""):
    try:
        return st.secrets[name]
    except (KeyError, FileNotFoundError):
        return os.environ.get(name, default)


@st.cache_data(ttl=3000)  # access tokens last 3600s; refresh with margin
def _access_token():
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": secret("GOOGLE_CLIENT_ID"),
            "client_secret": secret("GOOGLE_CLIENT_SECRET"),
            "refresh_token": secret("GOOGLE_REFRESH_TOKEN"),
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _headers():
    return {"Authorization": f"Bearer {_access_token()}"}


def find_child(parent_id, name, folders_only=False):
    q = f"'{parent_id}' in parents and name = '{name}' and trashed = false"
    if folders_only:
        q += " and mimeType = 'application/vnd.google-apps.folder'"
    r = requests.get(f"{DRIVE}/files", params={"q": q, "fields": "files(id)"},
                     headers=_headers(), timeout=30)
    r.raise_for_status()
    files = r.json().get("files", [])
    return files[0]["id"] if files else None


def ensure_day_folder(ddmmyyyy):
    inbox = secret("FHA_DRIVE_INBOX_ID")
    fid = find_child(inbox, ddmmyyyy, folders_only=True)
    if fid:
        return fid
    r = requests.post(f"{DRIVE}/files",
                      headers={**_headers(), "Content-Type": "application/json"},
                      json={"name": ddmmyyyy,
                            "mimeType": "application/vnd.google-apps.folder",
                            "parents": [inbox]},
                      timeout=30)
    r.raise_for_status()
    return r.json()["id"]


def list_day_files(ddmmyyyy):
    inbox = secret("FHA_DRIVE_INBOX_ID")
    day = find_child(inbox, ddmmyyyy, folders_only=True)
    if not day:
        return []
    r = requests.get(f"{DRIVE}/files",
                     params={"q": f"'{day}' in parents and trashed = false",
                             "fields": "files(name,size)", "orderBy": "name"},
                     headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json().get("files", [])


def upload_pdf(folder_id, name, data: bytes):
    import json as _json
    boundary = "fhauploaderboundary"
    meta = _json.dumps({"name": name, "parents": [folder_id]})
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{meta}\r\n"
        f"--{boundary}\r\nContent-Type: application/pdf\r\n\r\n"
    ).encode() + bytes(data) + f"\r\n--{boundary}--".encode()
    r = requests.post(f"{UPLOAD}/files?uploadType=multipart",
                      headers={**_headers(),
                               "Content-Type": f"multipart/related; boundary={boundary}"},
                      data=body, timeout=120)
    r.raise_for_status()
    return r.json()["id"]


def previous_business_day(today=None):
    d = (today or date.today()) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def safe_name(name):
    base = os.path.basename(name)
    base = re.sub(r"[^\w.\- ()]", "_", base).strip() or "upload.pdf"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base


def folder_md5s(folder_id):
    """md5 of every file already in the day folder — catches re-uploads of
    the same file under a different name (browser '(1)' copies)."""
    r = requests.get(f"{DRIVE}/files",
                     params={"q": f"'{folder_id}' in parents and trashed = false",
                             "fields": "files(md5Checksum)", "pageSize": 500},
                     headers=_headers(), timeout=30)
    r.raise_for_status()
    return {f["md5Checksum"] for f in r.json().get("files", []) if f.get("md5Checksum")}


def unique_name(folder_id, name):
    stem, ext = os.path.splitext(name)
    candidate, n = name, 0
    while find_child(folder_id, candidate):
        n += 1
        candidate = f"{stem}-{n}{ext}"
    return candidate


st.set_page_config(page_title="FHA Dockets upload", page_icon="📄")
st.title("FHA Dockets — Day Sheets & Invoices")

PASSCODE = secret("FHA_UPLOADER_PASSCODE")
if PASSCODE:
    if st.session_state.get("authed") is not True:
        entered = st.text_input("Passcode", type="password")
        if entered and entered == PASSCODE:
            st.session_state["authed"] = True
            st.rerun()
        elif entered:
            st.error("Wrong passcode.")
        st.stop()

st.caption(
    "Upload the Day Sheets and Tax Invoices for jobs completed on the day "
    "below. Any bundling is fine — multi-page PDFs, one file per docket, "
    "or one big scan. Files go straight through to the docket pipeline."
)

job_day = st.date_input(
    "Job completion day",
    value=previous_business_day(),
    max_value=date.today(),
    format="DD/MM/YYYY",
)
folder_name = job_day.strftime("%d%m%Y")

if job_day.weekday() >= 5:
    st.warning("That's a weekend — dockets are normally for the previous "
               "business day. Double-check before uploading.")

uploads = st.file_uploader(
    "PDF files", type=["pdf"], accept_multiple_files=True,
    key=st.session_state.get("uploader_key", "uploader-0"),
)

if uploads and st.button(f"Send {len(uploads)} file(s) for {folder_name}",
                         type="primary"):
    saved, errors = [], []
    with st.spinner("Uploading…"):
        try:
            day_folder = ensure_day_folder(folder_name)
        except Exception as e:
            day_folder = None
            errors.append(f"Upload service unreachable ({e}) — nothing sent. "
                          "Try again or call Nathan.")
        if day_folder:
            try:
                existing_md5 = folder_md5s(day_folder)
            except Exception:
                existing_md5 = set()
            skipped_dupes = []
            for up in uploads:
                data = up.getbuffer()
                if len(data) > MAX_FILE_MB * 1024 * 1024:
                    errors.append(f"{up.name}: over {MAX_FILE_MB} MB, skipped")
                    continue
                if not bytes(data[:5]) == b"%PDF-":
                    errors.append(f"{up.name}: not a valid PDF, skipped")
                    continue
                digest = hashlib.md5(bytes(data)).hexdigest()
                if digest in existing_md5:
                    skipped_dupes.append(up.name)
                    continue
                try:
                    name = unique_name(day_folder, safe_name(up.name))
                    upload_pdf(day_folder, name, data)
                    saved.append(name)
                    existing_md5.add(digest)
                except Exception:
                    errors.append(f"{up.name}: upload failed — try again or "
                                  "call Nathan.")
            if skipped_dupes:
                st.info("Already uploaded (identical file), skipped: "
                        + ", ".join(skipped_dupes))
    if saved:
        st.success(f"Sent for {folder_name}: " + ", ".join(saved))
    for e in errors:
        st.error(e)
    key = st.session_state.get("uploader_key", "uploader-0")
    st.session_state["uploader_key"] = f"uploader-{int(key.split('-')[1]) + 1}"
    st.rerun()

st.divider()
st.subheader(f"Already uploaded for {folder_name}")
try:
    existing = list_day_files(folder_name)
except Exception:
    existing = None
if existing is None:
    st.caption("Couldn't check — uploads may still work.")
elif not existing:
    st.caption("Nothing yet.")
else:
    for f in existing:
        size = f" — {int(f['size']) / 1024:.0f} KB" if f.get("size") else ""
        st.write(f"{f['name']}{size}")
st.caption("Wrong file? Call or message Nathan — he can remove it before "
           "the pack is built.")
