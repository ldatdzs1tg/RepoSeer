"""Unit tests for the lightweight heuristic technology extraction agent."""

import pytest

from reposeer.technology.agent import LightweightTechAgent
from reposeer.technology.registry import TechnologyRegistry


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
