"""Unit tests for the lightweight heuristic technology extraction agent."""

import pytest

from reposeer.technology.agent import LightweightTechAgent


def test_agent_extracts_headers_and_install_commands():
    agent = LightweightTechAgent()
    text = """
    # Flask Web Framework

    Flask is a micro web framework written in Python.

    ## Quick Start

    ```bash
    pip install Flask
    poetry add pydantic
    ```

    ```python
    import flask
    from pydantic import BaseModel
    ```
    """

    matches = agent.analyze_text(text, repo_name="flask")
    slugs = {m.spec.slug for m in matches}

    assert "flask" in slugs
    assert "pydantic" in slugs
    assert "python" in slugs

    flask_match = next(m for m in matches if m.spec.slug == "flask")
    assert flask_match.confidence >= 0.90
    assert flask_match.signal_kind in {"title", "install_code", "import_code"}


def test_agent_disambiguates_common_words():
    agent = LightweightTechAgent()

    # Ambiguous word 'requests' in casual prose should have lower confidence
    casual_text = "This module handles incoming HTTP requests from users."
    matches = agent.analyze_text(casual_text, repo_name="my-service")
    requests_match = next((m for m in matches if m.spec.slug == "requests"), None)
    # Either not matched or confidence penalized below high-confidence thresholds
    if requests_match:
        assert requests_match.confidence < 0.60

    # Whereas code context or repo name matches with high confidence
    code_text = "```python\nimport requests\nresp = requests.get('https://api')\n```"
    code_matches = agent.analyze_text(code_text, repo_name="psf-requests")
    req_code = next(m for m in code_matches if m.spec.slug == "requests")
    assert req_code.confidence >= 0.85


def test_agent_penalizes_comparative_mentions():
    agent = LightweightTechAgent()
    text = """
    # LiteAPI

    A new fast framework, heavily inspired by FastAPI and unlike Django.
    """
    matches = agent.analyze_text(text, repo_name="liteapi")
    django_match = next((m for m in matches if m.spec.slug == "django"), None)
    if django_match:
        assert django_match.confidence <= 0.50


def test_negative_historical_and_comparison_mentions_do_not_claim_usage():
    agent = LightweightTechAgent()
    for text in ("We do not use Flask.", "This works without Django.",
                 "Migrated from FastAPI to our own implementation.",
                 "Previously used Flask.", "Flask versus Django", "Alternative to FastAPI",
                 "We do not use PyTorch (torch).", "Unlike Flask and Django", "# Feature Requests"):
        assert agent.analyze_text(text) == []
    assert agent.analyze_text("Click here to send Requests to users.") == []
    assert agent.analyze_text("uv radiation is measured here.") == []
    matches = agent.analyze_text("![Flask](https://shields.io/badge/Flask-green)")
    assert any(match.spec.slug == "flask" and match.signal_kind == "badge" for match in matches)
    assert all(0 <= match.confidence <= 1 for match in matches)


@pytest.mark.parametrize(
    "text,expected_slugs",
    [
        ("import requests", {"requests"}),
        ("    import flask, requests  # Django comparison", {"flask", "requests"}),
        ("from pydantic import BaseModel", {"pydantic"}),
        ("from requests.sessions import Session", {"requests"}),
        ("import flask as requests", {"flask"}),
        ("from django import requests", {"django"}),
        ("`import requests`", {"requests"}),
    ],
)
def test_import_signals_match_modules_not_aliases_symbols_or_comments(text, expected_slugs):
    matches = LightweightTechAgent().analyze_text(text)
    assert {match.spec.slug for match in matches} == expected_slugs
    assert all(match.signal_kind == "import_code" and match.confidence >= 0.85 for match in matches)
    assert all(match.context_snippet in text for match in matches)


@pytest.mark.parametrize(
    "text",
    [
        "This module handles incoming HTTP requests from users.",
        "Use requests from our users to prioritize work.",
        "We import requests from users into the work queue.",
    ],
)
def test_natural_language_from_or_import_does_not_create_import_signals(text):
    matches = LightweightTechAgent().analyze_text(text)
    assert all(match.signal_kind != "import_code" for match in matches)
    requests_match = next((match for match in matches if match.spec.slug == "requests"), None)
    assert requests_match is None or requests_match.confidence < 0.60
