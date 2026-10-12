"""Unit tests for dependency manifest parsing."""

import pytest

from reposeer.technology.manifest_parser import (
    parse_manifest_dependencies,
    parse_pyproject_toml,
    parse_requirements_txt,
    parse_setup_cfg_or_py,
)


def test_parse_requirements_txt_basic_and_complex_specifiers():
    content = """
    # This is a comment
    flask==2.3.2
    requests>=2.28.0,<3.0.0
    pydantic~=2.0
    pytest!=7.0.0
    urllib3===1.26.15

    # Extras and markers
    celery[redis,auth]>=5.2.0 ; python_version >= '3.8'
    uvicorn[standard] @ https://github.com/encode/uvicorn/archive/master.zip

    # Options that should be ignored
    -r base.txt
    -i https://pypi.org/simple
    --extra-index-url https://custom.pypi.test/simple
    -e .
    -e git+https://github.com/pallets/werkzeug.git#egg=Werkzeug
    """

    packages = parse_requirements_txt(content)

    assert "flask" in packages
    assert "requests" in packages
    assert "pydantic" in packages
    assert "pytest" in packages
    assert "urllib3" in packages
    assert "celery" in packages
    assert "uvicorn" in packages
    assert "werkzeug" in packages
    assert len(packages) == 8


def test_parse_pyproject_toml_pep621_and_poetry():
    pep621_content = """
    [project]
    name = "demo-app"
    dependencies = [
        "fastapi>=0.100.0",
        "pydantic[email]>=2.0.0",
        "httpx~=0.24.0",
    ]

    [project.optional-dependencies]
    dev = [
        "pytest>=7.4.0",
        "ruff>=0.1.0",
    ]
    test = [
        "coverage>=7.0.0",
    ]
    """

    packages = parse_pyproject_toml(pep621_content)
    assert "fastapi" in packages
    assert "pydantic" in packages
    assert "httpx" in packages
    assert "pytest" in packages
    assert "ruff" in packages
    assert "coverage" in packages

    poetry_content = """
    [tool.poetry]
    name = "poetry-demo"

    [tool.poetry.dependencies]
    python = "^3.11"
    polars = "^0.20.0"
    duckdb = "^0.10.0"

    [tool.poetry.group.dev.dependencies]
    black = "^23.0.0"
    mypy = "^1.5.0"
    """

    poetry_packages = parse_pyproject_toml(poetry_content)
    assert "python" not in poetry_packages
    assert "polars" in poetry_packages
    assert "duckdb" in poetry_packages
    assert "black" in poetry_packages
    assert "mypy" in poetry_packages


def test_parse_setup_cfg_and_setup_py():
    setup_py = """
    from setuptools import setup

    setup(
        name="demo",
        version="0.1.0",
        install_requires=[
            "requests>=2.25.0",
            "click>=8.0",
            'jinja2 >= 3.0.0',
        ],
    )
    """
    packages = parse_setup_cfg_or_py(setup_py)
    assert "requests" in packages
    assert "click" in packages
    assert "jinja2" in packages


def test_parse_manifest_dependencies_multi_manifest():
    manifests = {
        "requirements.txt": "flask>=2.0\nwerkzeug\n",
        "pyproject.toml": """
        [project]
        dependencies = ["pydantic>=2.0", "flask"]
        """,
        "requirements-dev.txt": "pytest>=7.0\n",
    }

    all_deps = parse_manifest_dependencies(manifests)
    assert set(all_deps) == {"flask", "werkzeug", "pydantic", "pytest"}
