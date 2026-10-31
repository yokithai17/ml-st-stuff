"""Download exactly one pinned official archive; verify before extraction."""

import hashlib
import json
import shutil
import tempfile
import urllib.request
from pathlib import Path
from zipfile import ZipFile

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = PROJECT_ROOT / "data" / "manifest.json"


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def sample_paths(root: Path = PROJECT_ROOT) -> tuple[Path, Path]:
    metadata = json.loads(MANIFEST.read_text(encoding="utf-8"))
    folder = root / "data" / "extracted" / metadata["archive_name"].removesuffix(".zip")
    return folder / metadata["message_name"], folder / metadata["orderbook_name"]


def fetch_sample(root: Path = PROJECT_ROOT) -> tuple[Path, Path]:
    metadata = json.loads(MANIFEST.read_text(encoding="utf-8"))
    archive = root / "data" / "raw" / metadata["archive_name"]
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        # Never replace an existing file or publish an unverified download.
        with tempfile.TemporaryDirectory(dir=archive.parent) as temporary:
            download = Path(temporary) / "download.zip"
            with urllib.request.urlopen(metadata["download_url"], timeout=60) as response:
                with download.open("wb") as handle:
                    shutil.copyfileobj(response, handle)
            if sha256(download) != metadata["sha256"]:
                raise ValueError("archive SHA-256 mismatch; download was not retained")
            download.replace(archive)
    if sha256(archive) != metadata["sha256"]:
        raise ValueError(f"archive SHA-256 mismatch: {archive}; existing file retained")
    message, orderbook = sample_paths(root)
    destination = message.parent
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(archive) as zipped:
        if sorted(zipped.namelist()) != sorted(metadata["members"]):
            raise ValueError("unexpected archive members")
        if destination.exists():
            for name, member in metadata["members"].items():
                target = destination / name
                if not target.is_file() or sha256(target) != member["sha256"]:
                    raise ValueError(f"extracted file missing or changed: {target}; retained for inspection")
            return message, orderbook
        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            staging = Path(temporary) / "verified"
            staging.mkdir()
            for name, member in metadata["members"].items():
                # Manifest contains flat filenames only; never use extractall.
                if Path(name).name != name:
                    raise ValueError("unsafe archive member name")
                target = staging / name
                with zipped.open(name) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                if sha256(target) != member["sha256"]:
                    raise ValueError(f"member SHA-256 mismatch: {name}")
            staging.replace(destination)
    return message, orderbook

