"""Update Trader to the newest version on GitHub.

Usage:
    python -m trader.update

Downloads the latest code and replaces the program files. Your own files are
never touched: .env, watchlist.txt, config.local.toml and the .state folder.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
DOWNLOAD_URL = "https://github.com/akhs001/Trader/archive/HEAD.zip"

# Files that belong to the user. An update never writes or deletes these.
USER_FILES = {".env", "watchlist.txt", "config.local.toml"}
USER_FOLDERS = {".state", ".git"}


def download(url: str = DOWNLOAD_URL) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "trader-updater"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def apply_update(archive: bytes, root: Path) -> list[str]:
    """Install the files from a GitHub ZIP archive into `root`.

    Returns lines describing what happened, for showing to the user.
    """
    record_path = root / ".state" / "installed.json"
    record = _read_record(record_path)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        version = bundle.comment.decode("ascii", "replace").strip()  # the commit id
        incoming = _files_in(bundle)
    if not incoming:
        raise ValueError("the downloaded archive is empty")
    if version and version == record.get("version"):
        return ["Trader is already up to date."]

    messages = []
    changed = 0
    for name, content in incoming.items():
        target = root / name
        if target.exists():
            current = target.read_bytes()
            if current == content:
                continue
            if _edited_by_user(name, current, record):
                backup = target.with_name(target.name + ".backup")
                backup.write_bytes(current)
                messages.append(f"Your previous {name} was saved as {backup.name}, "
                                "in case you had changed it.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        changed += 1

    # Remove files an earlier update installed that are no longer part of Trader.
    removed = 0
    for name in record.get("files", {}):
        target = root / name
        if name not in incoming and not _is_user_file(name) and target.is_file():
            target.unlink()
            removed += 1

    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps({
        "version": version,
        "files": {name: _digest(content) for name, content in incoming.items()},
    }, indent=1))

    summary = f"Updated Trader: {changed} file(s) changed"
    summary += f", {removed} removed." if removed else "."
    return [summary, *messages, "If Trader is open, close it and start it again."]


def _files_in(bundle: zipfile.ZipFile) -> dict[str, bytes]:
    """Program files in the archive, keyed by path inside the project."""
    files = {}
    for info in bundle.infolist():
        if info.is_dir():
            continue
        # GitHub puts everything inside one top-level folder.
        parts = PurePosixPath(info.filename).parts[1:]
        if not parts or any(part in ("..", "") or ":" in part for part in parts):
            continue
        name = "/".join(parts)
        if not _is_user_file(name):
            files[name] = bundle.read(info)
    return files


def _is_user_file(name: str) -> bool:
    return name in USER_FILES or name.split("/")[0] in USER_FOLDERS


def _edited_by_user(name: str, current: bytes, record: dict) -> bool:
    installed = record.get("files", {}).get(name)
    if installed is None:
        # Nothing is known about this file yet (first update). Settings are the
        # only program file people edit by hand, so only those are kept.
        return name == "config.toml"
    return installed != _digest(current)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_record(path: Path) -> dict:
    try:
        return dict(json.loads(path.read_text()))
    except (OSError, ValueError, TypeError):
        return {}


def main() -> int:
    print("Checking GitHub for the newest version of Trader...")
    try:
        archive = download()
        messages = apply_update(archive, ROOT)
    except (urllib.error.URLError, TimeoutError, zipfile.BadZipFile, ValueError) as error:
        print(f"Could not update: {error}")
        print("Nothing was changed. Check your internet connection and try again.")
        return 1
    for line in messages:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
