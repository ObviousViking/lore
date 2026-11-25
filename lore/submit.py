"""Anonymous upload of the collected hashes to the Lore submission Worker (a Cloudflare Worker).

The upload is the same CSV as Export, gzipped and sent in parts of up to PART_BYTES. Nothing
else is sent: no account names, IDs or machine details. Parts of one submission share a random
ID so they can be put back together.
"""

import os
import shutil
import urllib.error
import urllib.request
import uuid

from . import __version__, ui
from .export import EXPORT_STORES, LABELS, write_gzip_parts
from .paths import files_dir

# Set to the deployed Worker's URL. LORE_SUBMIT_URL overrides it for testing.
SUBMIT_URL = "https://lore-submit.datahoarder.co.uk"
PART_BYTES = 40 * 1024 * 1024  # Cloudflare's free plan accepts up to 100 MB per request


def submit_url():
    return os.environ.get("LORE_SUBMIT_URL") or SUBMIT_URL


def _post(url, data, headers):
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status
    except urllib.error.HTTPError as e:
        detail = e.read()[:200].decode(errors="replace")
        raise RuntimeError(f"upload refused (HTTP {e.code}): {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"couldn't reach the submission server: {e.reason}") from None


def submit(epic_gog_db):
    url = submit_url()
    if not url:
        ui.warn("Submitting isn't set up in this build yet. Use Export CSV instead.")
        return

    tmp = files_dir() / "submit_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        with ui.console.status("Preparing your hashes...", spinner_style=ui.TEAL):
            parts, counts = write_gzip_parts(tmp, EXPORT_STORES, epic_gog_db, PART_BYTES)
        total = sum(counts.values())
        if not total:
            ui.warn("Nothing collected yet. Choose Collect hashes first.")
            return
        size_mb = sum(p.stat().st_size for p in parts) / 1024 / 1024
        for store, n in counts.items():
            if n:
                ui.info(f"{LABELS[store]}: {n:,} hashes")
        ui.info(f"{size_mb:.1f} MB to upload. Only the hash list is sent, nothing that "
                "identifies you.")
        if not ui.confirm("Upload now?"):
            return

        sub_id = uuid.uuid4().hex
        with ui.Bar("Uploading", len(parts)) as bar:
            for i, part in enumerate(parts, 1):
                bar.status(f"part {i} of {len(parts)}")
                _post(url.rstrip("/") + "/submit", part.read_bytes(), {
                    "Content-Type": "application/gzip",
                    "User-Agent": f"lore/{__version__}",
                    "X-Lore-Version": __version__,
                    "X-Lore-Submission": sub_id,
                    "X-Lore-Part": str(i),
                    "X-Lore-Parts": str(len(parts)),
                })
                bar.advance()
        ui.ok(f"Submitted {total:,} hashes. Thank you! Reference: {sub_id[:8]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
