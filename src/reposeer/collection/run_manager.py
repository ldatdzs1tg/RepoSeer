"""Manager for collection runs, checkpoints, and execution lifecycle."""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from reposeer.collection.context import CollectionContext
from reposeer.collection.persistence import write_json_atomic
from reposeer.config import settings
from reposeer.exceptions import CollectionError

logger = logging.getLogger("reposeer.collection.run_manager")


class RunManager:
    """Manages run state, persistence of checkpoints, and recovery."""

    def __init__(self, checkpoints_dir: Path | None = None):
        self.checkpoints_dir = (
            Path(checkpoints_dir)
            if checkpoints_dir is not None
            else settings.storage.metadata_dir / "collection_runs"
        )
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)

    def _checkpoint_path(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise ValueError("run_id must contain only letters, numbers, underscores, and hyphens")
        return self.checkpoints_dir / f"run_{run_id}.json"

    def start_run(self, run_id: str | None = None) -> CollectionContext:
        """Start a new collection run context."""
        context = CollectionContext() if run_id is None else CollectionContext(run_id=run_id)
        if self._checkpoint_path(context.run_id).exists():
            raise CollectionError(f"Run {context.run_id} already exists; use resume_run")
        self.save_checkpoint(context)
        logger.info("Started collection run %s", context.run_id)
        return context

    def save_checkpoint(self, context: CollectionContext) -> Path:
        """Atomically persist current run state, keeping the previous file on failure."""
        file_path = self._checkpoint_path(context.run_id)
        data = {
            "run_id": context.run_id,
            "started_at": context.started_at.isoformat(),
            "collected_count": context.collected_count,
            "error_count": context.error_count,
            "checkpoint_state": context.checkpoint_state,
        }
        try:
            write_json_atomic(file_path, data)
        except (OSError, ValueError, TypeError) as error:
            logger.error("Could not save checkpoint for run %s", context.run_id)
            raise CollectionError(f"Could not save checkpoint for run {context.run_id}") from error
        return file_path

    def resume_run(self, run_id: str) -> CollectionContext | None:
        """Resume an existing run from its saved checkpoint."""
        file_path = self._checkpoint_path(run_id)
        if not file_path.exists():
            logger.warning("Checkpoint for run %s not found", run_id)
            return None
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            state = data["checkpoint_state"]
            if data["run_id"] != run_id or not isinstance(state, dict):
                raise ValueError("Invalid checkpoint identity or state")
            if any(
                not isinstance(key, str) or type(value) not in (str, int)
                for key, value in state.items()
            ):
                raise ValueError("Invalid checkpoint cursor")
            if any(
                type(data[key]) is not int or data[key] < 0
                for key in ("collected_count", "error_count")
            ):
                raise ValueError("Invalid checkpoint counts")
            context = CollectionContext(
                run_id=run_id,
                started_at=datetime.fromisoformat(data["started_at"]),
                checkpoint_state=state,
                collected_count=data["collected_count"],
                error_count=data["error_count"],
            )
        except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
            logger.error("Could not read checkpoint for run %s", run_id)
            raise CollectionError(f"Invalid or unreadable checkpoint for run {run_id}") from error
        logger.info("Resumed collection run %s", run_id)
        return context
