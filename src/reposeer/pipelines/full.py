"""Compatibility entry point: current GitHub metadata only."""

import logging

from reposeer.pipelines.github import GitHubPipeline

logger = logging.getLogger("reposeer.pipelines.full")


class FullPipeline:
    """Collect GitHub metadata; future orchestration is not implemented here."""

    def __init__(self):
        self.github_pipeline = GitHubPipeline()

    def run(self, repo_target: str) -> None:
        logger.info("Collecting GitHub metadata for target: %s", repo_target)
        self.github_pipeline.run(repo_target)
