"""Immutable, checksum-checked D09 bundles for an eventually synced team folder."""

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from scripts.run_d09_drive import canonical_json, digest, file_digest
from tools.coordination._common import atomic_bytes

from reposeer.collection.persistence import replace_atomic
from reposeer.contracts.identity import format_utc_timestamp
from reposeer.storage.screening_mapping import checked_mount_path


class SyncPending(ValueError):
    """A publisher has not finished or Drive has not synced every bundle file."""


def filesystem_path(path: Path) -> Path:
    return checked_mount_path(path.absolute()) if os.name == "nt" else path


def safe_name(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or ":" in name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise ValueError(f"unsafe bundle path: {name}")
    return path.as_posix()


def describe(files: dict[str, Path]) -> dict:
    result = {}
    for name, path in sorted(files.items()):
        safe_name(name)
        path = filesystem_path(path)
        if path.is_symlink() or path.is_junction() or not path.is_file():
            raise ValueError(f"bundle input must be a regular file: {path}")
        result[name] = {"sha256": file_digest(path), "size": path.stat().st_size}
    return result


def ready_manifest(directory: Path, *, verify_payload=False) -> dict:
    directory = filesystem_path(directory)
    try:
        ready = json.loads((directory / "READY.json").read_bytes())
        body = (directory / "manifest.json").read_bytes()
        if digest(body) != ready["manifest_sha256"]:
            raise SyncPending(f"manifest is not fully synced: {directory}")
        manifest = json.loads(body)
        identity = {key: manifest[key] for key in ("version", "metadata", "files")}
        if digest(canonical_json(identity)) != manifest["bundle_id"]:
            raise ValueError("team bundle identity mismatch")
        for name, record in manifest["files"].items():
            safe_name(name)
            if type(record["size"]) is not int or record["size"] < 0:
                raise ValueError("invalid team bundle file size")
        payload = directory / "payload.zip"
        if payload.stat().st_size != manifest["payload_size"]:
            raise SyncPending(f"payload is not fully synced: {directory}")
        if verify_payload and file_digest(payload) != manifest["payload_sha256"]:
            raise SyncPending(f"payload checksum has not synced: {directory}")
        return manifest
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as error:
        raise SyncPending(f"team bundle is not ready: {directory}") from error


def publish(parent: Path, files: dict[str, Path], metadata: dict) -> tuple[Path, dict]:
    parent = filesystem_path(parent)
    files = {name: filesystem_path(path) for name, path in files.items()}
    identity = {"version": 1, "metadata": metadata, "files": describe(files)}
    if any(
        identity["files"].get(name, {}).get("sha256") != expected
        for name, expected in metadata.get("required_sha256", {}).items()
    ):
        raise SyncPending("the checkpoint changed before it could be published")
    bundle_id = digest(canonical_json(identity))
    destination = parent / bundle_id
    if (destination / "READY.json").exists():
        existing = ready_manifest(destination, verify_payload=True)
        if existing["bundle_id"] != bundle_id:
            raise ValueError("published team bundle changed")
        return destination, existing
    with tempfile.TemporaryDirectory(prefix="d09-publish-") as temporary:
        archive = Path(temporary) / "payload.zip"
        with zipfile.ZipFile(
            archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3
        ) as output:
            for name, path in sorted(files.items()):
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                hasher, size = hashlib.sha256(), 0
                with path.open("rb") as source, output.open(info, "w", force_zip64=True) as target:
                    while block := source.read(1024 * 1024):
                        target.write(block)
                        hasher.update(block)
                        size += len(block)
                if {"sha256": hasher.hexdigest(), "size": size} != identity["files"][name]:
                    raise SyncPending(f"input changed during checkpoint publication: {path}")
        manifest = {
            **identity,
            "bundle_id": bundle_id,
            "published_at": format_utc_timestamp(datetime.now(UTC)),
            "payload_sha256": file_digest(archive),
            "payload_size": archive.stat().st_size,
        }
        destination.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(dir=destination, suffix=".uploading")
        os.close(handle)
        transfer = Path(temporary_name)
        try:
            shutil.copyfile(archive, transfer)
            if file_digest(transfer) != manifest["payload_sha256"]:
                raise OSError("team bundle transfer checksum mismatch")
            replace_atomic(transfer, destination / "payload.zip")
        finally:
            transfer.unlink(missing_ok=True)
        body = canonical_json(manifest)
        atomic_bytes(destination / "manifest.json", body, immutable=False)
        atomic_bytes(destination / "READY.json", canonical_json({"manifest_sha256": digest(body)}))
    return destination, manifest


def unpack(directory: Path, output: Path) -> dict:
    directory, output = filesystem_path(directory), filesystem_path(output)
    manifest = ready_manifest(directory, verify_payload=True)
    if output.exists():
        raise ValueError("extract a team bundle into a new staging directory")
    output.mkdir(parents=True)
    root = output.resolve()
    with zipfile.ZipFile(directory / "payload.zip") as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(manifest["files"]):
            raise ValueError("team ZIP entries differ from its manifest")
        for entry in archive.infolist():
            name = safe_name(entry.filename)
            expected = manifest["files"][name]
            if entry.file_size != expected["size"]:
                raise ValueError("team ZIP size differs from its manifest")
            target = output / name
            if not target.resolve().is_relative_to(root):
                raise ValueError("team ZIP escapes its staging directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            hasher, size = hashlib.sha256(), 0
            with archive.open(entry) as source, target.open("xb") as destination:
                while block := source.read(1024 * 1024):
                    size += len(block)
                    if size > expected["size"]:
                        raise ValueError("team ZIP exceeded its declared size")
                    destination.write(block)
                    hasher.update(block)
            if size != expected["size"] or hasher.hexdigest() != expected["sha256"]:
                raise ValueError("team checkpoint file checksum mismatch")
    return manifest


def ready_bundles(parent: Path):
    for path in sorted(parent.glob("*/READY.json")):
        try:
            yield path.parent, ready_manifest(path.parent)
        except SyncPending:
            continue
