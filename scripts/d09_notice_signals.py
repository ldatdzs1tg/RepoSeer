"""Search hints for D09 first-party sources. Reviewers assign outcomes separately."""

from pathlib import PurePosixPath

SIGNALS = {
    "cessation": (
        "no longer maintained",
        "no longer actively maintained",
        "not maintained",
        "not actively maintained",
        "no longer being maintained",
        "unmaintained",
        "maintenance has ended",
        "maintenance has stopped",
        "stopped maintaining",
        "will not maintain",
        "will no longer maintain",
        "no longer developed",
        "development has stopped",
        "development has ceased",
        "development has ended",
        "has been abandoned",
        "project is abandoned",
        "project is dead",
        "has been discontinued",
        "is discontinued",
        "decommissioned",
        "decommissioning",
        "will not be updated",
        "no further releases",
        "no more releases",
        "no future releases",
        "no further updates",
        "no more updates",
        "will not receive updates",
        "sunsetting",
        "sunset",
        "retiring this",
        "retirement notice",
        "end of life",
        "end-of-life",
        "ngừng bảo trì",
        "không còn được bảo trì",
        "no se mantiene",
        "no se mantendrá",
        "n'est plus maintenu",
        "nicht mehr gewartet",
        "不再维护",
        "メンテナンス終了",
    ),
    "scope_or_transition": (
        "deprecat",
        "no longer supported",
        "not supported",
        "superseded",
        "obsolet",
        "migrate to",
        "moved to",
        "development has moved",
        "development will take place",
        "new maintainer",
        "handover",
        "hand over",
        "looking for a maintainer",
        "maintenance mode",
        "repository is archived",
        "repository has been archived",
        "project is archived",
        "project has been archived",
        "archiving this",
        "archive this repo",
        "archived by the owner",
        "unarchived",
        "maintenance resumed",
        "development resumed",
        "reactivated",
    ),
    "maintenance": (
        "actively maintained",
        "maintenance release",
        "security release",
        "still maintained",
        "continue to maintain",
        "will continue to be maintained",
        "security fixes",
    ),
}

PUBLISHER_NOTICE_PHRASES = (*SIGNALS["cessation"], *SIGNALS["scope_or_transition"])

DOCUMENT_STEMS = {
    "readme",
    "changelog",
    "change-log",
    "changes",
    "news",
    "history",
    "release-notes",
    "releasenotes",
    "maintenance",
    "status",
    "support",
    "deprecation",
    "deprecations",
    "eol",
    "sunset",
    "retirement",
    "migration",
    "announcement",
    "announcements",
    "index",
    "upgrading",
    "upgrade",
    "deprecated",
    "security",
}


RELEASE_NOTE_DIRS = {
    "changes",
    "changelog.d",
    "news",
    "release-notes",
    "releases",
    "releasenotes",
    "whatsnew",
}


def notice_document(path: str) -> bool:
    doc = PurePosixPath(path.lower())
    if doc.is_absolute() or ".." in doc.parts:
        return False
    if doc.suffix not in {"", ".md", ".markdown", ".rst", ".txt", ".adoc", ".mdx"}:
        return False
    if len(doc.parts) > 1 and doc.parts[0] not in {
        "doc",
        "docs",
        "documentation",
        *RELEASE_NOTE_DIRS,
    }:
        return False
    stem = doc.stem.replace("_", "-")
    return (
        bool(RELEASE_NOTE_DIRS.intersection(doc.parts[:-1]))
        or stem in DOCUMENT_STEMS
        or any(
            stem.startswith(prefix + "-")
            for prefix in ("readme", "changelog", "release-notes", "deprecation", "migration")
        )
    )


def notice_hits(text: str, *, limit: int = 12) -> list[dict]:
    """Return verbatim line context and search categories, with no inferred labels."""
    hits = {kind: [] for kind in SIGNALS}
    for number, line in enumerate(text.splitlines(), 1):
        lowered = line.casefold()
        ends = []
        for category, phrases in SIGNALS.items():
            # Prefer the longest phrase so overlapping phrases yield one hit per category.
            matching = [phrase for phrase in phrases if phrase in lowered]
            if matching:
                phrase = max(matching, key=len)
                pos = lowered.index(phrase)
                if any(start <= pos < end for start, end in ends):
                    continue
                ends.append((pos, pos + len(phrase)))
                hits[category].append(
                    {
                        "kind": category,
                        "phrase": phrase,
                        "line": number,
                        "excerpt": line[max(0, pos - 100) : pos + len(phrase) + 180],
                    }
                )
    return [hit for group in hits.values() for hit in group][:limit]
