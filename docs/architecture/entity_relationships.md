# Entity Relationships

- **Repository** (`full_name` as primary key):
  - 1-to-N **CommitRecord**
  - 1-to-N **IssueRecord**
  - 1-to-N **PullRequestRecord**
  - 1-to-N **ReleaseRecord**
  - 1-to-N **ContributorRecord**
  - 1-to-N **RepoTechnologyAssociation**
  - 1-to-N **MaintenanceEvent**
- **TechnologyEntity** (`name` canonical key):
  - 1-to-N **TechnologyAlias**
- **ExternalCandidate** / **ArticleDocument**:
  - Linked to technologies or repositories by mention / keyword.
