"""Read publisher core metadata from checksum-pinned distributions without executing them."""

import io
import tarfile
import zipfile
from email.parser import BytesParser
from email.policy import default

from scripts.collect_d09_pilot import SOURCE_LINK_LABELS

from reposeer.collectors.package.repository_mapping import _root_url
from reposeer.contracts.identity import package_id

MAX_METADATA = 2 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024


def core_metadata(
    body: bytes, subject: str, version: str, *, require_repository: bool = True
) -> tuple[bytes, dict, bool]:
    candidates = []
    if zipfile.is_zipfile(io.BytesIO(body)):
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            entries = archive.infolist()
            if len(entries) > 5000 or sum(entry.file_size for entry in entries) > MAX_EXPANDED:
                raise ValueError("distribution metadata decoding limit")
            for entry in entries:
                if (
                    entry.filename.endswith(("/METADATA", "/PKG-INFO"))
                    and entry.file_size <= MAX_METADATA
                ):
                    candidates.append(archive.read(entry))
    else:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r|gz") as archive:
            total = 0
            for index, entry in enumerate(archive):
                total += entry.size
                if index > 5000 or total > MAX_EXPANDED:
                    raise ValueError("distribution metadata decoding limit")
                if (
                    entry.isfile()
                    and entry.name.endswith("/PKG-INFO")
                    and entry.size <= MAX_METADATA
                ):
                    candidates.append(archive.extractfile(entry).read(MAX_METADATA + 1))
    matches = []
    for body in candidates:
        metadata = BytesParser(policy=default).parsebytes(body)
        if (
            package_id(metadata.get("Name", "unknown")) == subject
            and metadata.get("Version") == version
        ):
            matches.append((body, metadata))
    if not matches:
        raise ValueError("distribution core metadata identity or version mismatch")
    if not require_repository:
        return matches[0][0], {}, False
    roots, homepages = set(), set()
    for _, metadata in matches:
        for entry in metadata.get_all("Project-URL", []):
            label, _, url = entry.partition(",")
            if label.strip().casefold() in SOURCE_LINK_LABELS:
                roots.add(_root_url(url.strip()))
        homepage = metadata.get("Home-page")
        if homepage:
            try:
                homepages.add(_root_url(homepage))
            except ValueError:
                pass
    use_homepage = not roots
    if use_homepage:
        roots = homepages
    if len(roots) != 1:
        raise ValueError("distribution does not identify one explicit GitHub source")
    return (
        matches[0][0],
        {root: {root.removeprefix("https://github.com/")} for root in roots},
        use_homepage,
    )
