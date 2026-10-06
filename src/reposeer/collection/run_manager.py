"""Manager for collection runs, checkpoints, and execution lifecycle."""

import json
import logging
from pathlib import Path

from reposeer.collection.context import CollectionContext
from reposeer.constants import DEFAULT_DATA_DIR

logger = logging.getLogger("reposeer.collection.run_manager")


class RunManager:
    """Manages run state, persistence of checkpoints, and recovery."""

    def __init__(self, checkpoints_dir: Path | None = None):
        self.checkpoints_dir = checkpoints_dir or (
            DEFAULT_DATA_DIR / "metadata" / "collection_runs"
        )
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)

    def start_run(self, run_id: str | None = None) -> CollectionContext:
        """Start a new collection run context."""
        context = CollectionContext() if run_id is None else CollectionContext(run_id=run_id)
        self.save_checkpoint(context)
        logger.info("Started collection run %s", context.run_id)
        return context

    def save_checkpoint(self, context: CollectionContext) -> Path:
        """Persist current run state to checkpoint file."""
        file_path = self.checkpoints_dir / f"run_{context.run_id}.json"
        data = {
            "run_id": context.run_id,
            "started_at": context.started_at.isoformat(),
            "collected_count": context.collected_count,
            "error_count": context.error_count,
            "checkpoint_state": context.checkpoint_state,
        }
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return file_path

    def resume_run(self, run_id: str) -> CollectionContext | None:
        """Resume an existing run from its saved checkpoint."""
        file_path = self.checkpoints_dir / f"run_{run_id}.json"
        if not file_path.exists():
            logger.warning("Checkpoint for run %s not found", run_id)
            return None
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
        context = CollectionContext(
            run_id=data["run_id"],
            checkpoint_state=data.get("checkpoint_state", {}),
            collected_count=data.get("collected_count", 0),
            error_count=data.get("error_count", 0),
        )
        logger.info("Resumed collection run %s", run_id)
        return context
