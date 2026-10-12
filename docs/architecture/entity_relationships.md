# Entity Relationships

T-008 v0.3 / canonical contract 0.1.0:

- **PyPI package** (`pypi:<PEP-503-normalized-name>`) is the prediction entity. Verified package↔repository mappings may include multiple packages in one repository; unresolved mapping stays pending.
- **Repository** (`github:repository:<numeric-ID>`) supplies activity and `repo_group_id`. Names/URLs are observation attributes and may change.
- **PackageRelease** belongs to a PyPI package and optionally a verified repository; **RepositoryActivity** belongs to its stable repository ID.
- **TechnologyEntity** (`tech:<slug>`) is taxonomy; **Article** (`article:sha256:<canonical-URL-hash>`) links package/repository IDs and technology mentions with provenance.
- **PackageSnapshot** has grain `(package_id, cutoff_at)`, `repo_group_id=repo_id` and a 12-calendar-month horizon.
- Dependency impact, vulnerabilities and confirmed maintenance/abandonment evidence remain logical entities; dedicated canonical models and business pipelines are not implemented by T-011.

Legacy DTO associations are internal compatibility models. See [canonical contracts](../contracts/data_contract.md) for public interfaces and cutoff safety.
