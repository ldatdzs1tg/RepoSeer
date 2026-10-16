"""CLI handoff from canonical discovery JSONL or one URL into T-017."""

from pathlib import Path
from typing import Annotated

import typer

from reposeer.pipelines.external_crawl import ExternalCrawlPipeline
from reposeer.storage.paths import StoragePaths, paths
from reposeer.storage.raw.jsonl import JsonlStorage

crawl_external_app = typer.Typer(help="Fetch external candidate content (T-017).")


@crawl_external_app.callback(invoke_without_command=True)
def main(
    url: Annotated[str | None, typer.Argument(help="One public article URL.")] = None,
    candidates_file: Annotated[
        Path | None, typer.Option("--candidates-file", help="Canonical T-016 discovery JSONL.")
    ] = None,
    from_stored: Annotated[
        bool, typer.Option("--from-stored", help="Read data/raw/external/discovery.jsonl.")
    ] = False,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
    max_candidates: Annotated[int, typer.Option("--max-candidates", min=1, max=1000)] = 100,
    cursor: Annotated[
        str | None, typer.Option("--cursor", help="Offset in the same input batch.")
    ] = None,
    max_redirects: Annotated[int, typer.Option("--max-redirects", min=0, max=20)] = 5,
    max_response_bytes: Annotated[
        int, typer.Option("--max-response-bytes", min=1, help="Decoded byte limit per response.")
    ] = 5_000_000,
) -> None:
    if sum((url is not None, candidates_file is not None, from_stored)) != 1:
        raise typer.BadParameter("Choose one URL, --candidates-file or --from-stored.")
    storage_paths = StoragePaths(data_dir) if data_dir else paths
    try:
        if url is not None:
            candidates = [url]
        else:
            path = candidates_file or storage_paths.raw_dir / "external" / "discovery.jsonl"
            if not path.is_file():
                raise ValueError("candidate_file_missing")
            candidates = JsonlStorage().read_contract_records(path)
        with ExternalCrawlPipeline(
            storage_paths=storage_paths,
            max_redirects=max_redirects,
            max_response_bytes=max_response_bytes,
        ) as pipeline:
            result = pipeline.crawl(candidates, max_candidates=max_candidates, cursor=cursor)
    except (ValueError, TypeError):
        # Do not print validation errors containing untrusted URL/payload input.
        typer.echo("Invalid candidate input, file or cursor.", err=True)
        raise typer.Exit(2) from None
    except OSError:
        typer.echo("Cannot read candidates or persist crawl output.", err=True)
        raise typer.Exit(1) from None
    collection = result.collection_result
    extracted = sum(r.payload["content_status"] == "full" for r in collection.records)
    typer.echo(f"Run: {result.run_id}\nStatus: {collection.status}")
    typer.echo(
        f"Raw records: {len(collection.records)}; extracted: {extracted}; errors: {len(collection.errors)}"
    )
    typer.echo(f"Raw dataset: {result.output_path}\nManifest: {result.manifest_path}")
    if collection.metadata.next_cursor:
        typer.echo(f"Next cursor: {collection.metadata.next_cursor}")
    if collection.status != "complete":
        raise typer.Exit(1)
