# Offline extraction fixtures

All content is synthetic. These fixtures model the selected source interfaces;
they are not copies of live posts and make no claims about real projects.

- `article.html` / `article.txt`: main text with four paragraphs, headings, a
  table, Unicode, case-sensitive identifiers, HTML entities and explicit metadata.
  Navigation, scripts, style, comments and footer text must be excluded.
- `hackernews.json`: own story HTML body, encoded title, author and epoch date.
- `devto.json`: article body, timezone-bearing publication and edit metadata.
- `stackexchange.json`: question body, encoded title/owner and epoch edit metadata.

The integration tests compare the complete expected text, assert metadata values
and raw replay, and corrupt each response shape in negative cases. HTML table
spacing is compared as tokens to tolerate serializer padding; paragraph boundaries
and exclusion of boilerplate are checked separately.

## GitHub Markdown regression

`github_changelog.html` and `github_changelog.txt` are synthetic fixtures for
GitHub's rendered Markdown heading/permalink structure. The golden text retains
each version/date heading beside its changes and excludes navigation/footer text.

