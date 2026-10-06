"""GitHub collection pipeline."""

import logging

from reposeer.services.repository_service import RepositoryService

logger = logging.getLogger("reposeer.pipelines.github")


class GitHubPipeline:
    def __init__(self):
        self.service = RepositoryService()

    def run(self, repo_url_or_slug: str) -> None:
        parts = repo_url_or_slug.rstrip("/").split("/")
        owner, repo = parts[-2], parts[-1]
        logger.info("Running GitHub pipeline for %s/%s", owner, repo)
        self.service.collect_and_store(owner, repo)
