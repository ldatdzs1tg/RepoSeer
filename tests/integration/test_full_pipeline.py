"""Integration test for full pipeline components."""

from reposeer.schemas.github.repository import RepositoryRecord
from reposeer.storage.processed.parquet import ParquetStorage
from reposeer.storage.raw.jsonl import JsonlStorage
from reposeer.technology.resolver import TechnologyResolver


def test_pipeline_integration_flow(tmp_path):
    # 1. Mock collection
    raw_storage = JsonlStorage()
    parquet_storage = ParquetStorage()

    repo = RepositoryRecord(
        owner="tiangolo",
        name="fastapi",
        full_name="tiangolo/fastapi",
        stargazers_count=75000,
        language="Python",
    )

    # 2. Raw JSONL persistence
    raw_path = tmp_path / "raw.jsonl"
    raw_storage.append_record(raw_path, repo)
    assert raw_path.exists()

    # 3. Technology resolution
    resolver = TechnologyResolver()
    techs = resolver.resolve_from_file(
        "tiangolo/fastapi", "requirements.txt", "pydantic>=2.0.0\nstarlette\n"
    )
    assert any(t.name == "Pydantic" for t in techs)

    # 4. Processed Parquet persistence
    processed_path = tmp_path / "repos.parquet"
    parquet_storage.write(processed_path, [repo.model_dump()])
    assert processed_path.exists()

    loaded_df = parquet_storage.read(processed_path)
    assert len(loaded_df) == 1
    assert loaded_df["stars"][0] == 75000
