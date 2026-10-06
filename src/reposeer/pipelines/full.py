"""Full end-to-end integrated pipeline according to T-020."""

import logging

from reposeer.pipelines.github import GitHubPipeline

logger = logging.getLogger("reposeer.pipelines.full")


class FullPipeline:
    """Orchestrates metadata, activity, technology, and external collection."""

    def __init__(self):
        self.github_pipeline = GitHubPipeline()

    def run(self, repo_target: str) -> None:
        logger.info("Executing full pipeline on target: %s", repo_target)
        self.github_pipeline.run(repo_target)
