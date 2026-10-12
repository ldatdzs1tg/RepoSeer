"""Dependency manifest parser extracting declared package names."""

import re
import tomllib
from typing import Any

from reposeer.contracts.identity import normalize_pypi_name

PACKAGE_NAME_REGEX = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")


def _clean_requirement_line(line: str) -> str | None:
    """Extract clean distribution package name from a single requirements line."""
    # Strip comments and outer whitespace
    cleaned = line.split("#")[0].strip()
    if not cleaned:
        return None

    # Ignore pip options and references
    if cleaned.startswith(("-r", "-i", "-f", "-e", "--extra-index-url", "--index-url", "--trusted-host", "-c")):
        # If editable install like -e . or -e git+... skip or extract name if egg=
        if "#egg=" in line:
            egg_part = line.split("#egg=")[-1].strip()
            return _normalize_safe(egg_part)
        return None

    # Ignore direct local paths or direct URLs without package name
    if cleaned.startswith(("./", "../", "/", "http://", "https://", "git+")):
        if "#egg=" in line:
            egg_part = line.split("#egg=")[-1].strip()
            return _normalize_safe(egg_part)
        return None

    # Strip environment markers (e.g. '; python_version >= "3.8"')
    cleaned = cleaned.split(";")[0].strip()

    # Split on version comparison operators or direct URL separator '@'
    parts = re.split(r"(?:===|==|~=|!=|>=|<=|>|<|@)", cleaned, maxsplit=1)
    base_name = parts[0].strip()

    # Strip extras e.g. 'requests[security]' -> 'requests'
    if "[" in base_name:
        base_name = base_name.split("[")[0].strip()

    return _normalize_safe(base_name)


def _normalize_safe(name: str) -> str | None:
    """Normalize package name to canonical PyPI format if valid."""
    name = name.strip()
    if not name:
        return None
    match = PACKAGE_NAME_REGEX.match(name)
    if not match:
        return None
    valid_prefix = match.group(0)
    try:
        return normalize_pypi_name(valid_prefix)
    except ValueError:
        return None


def parse_requirements_txt(content: str) -> list[str]:
    """Parse dependencies from a requirements.txt file."""
    packages: list[str] = []
    seen = set()

    for raw_line in content.splitlines():
        pkg = _clean_requirement_line(raw_line)
        if pkg and pkg not in seen:
            seen.add(pkg)
            packages.append(pkg)

    return packages


def parse_pyproject_toml(content: str) -> list[str]:
    """Parse dependencies from a PEP 621 or Poetry pyproject.toml file."""
    try:
        data = tomllib.loads(content)
    except Exception:
        return []

    packages: list[str] = []
    seen = set()

    def add_pkg(name_or_spec: str):
        pkg = _clean_requirement_line(name_or_spec)
        if pkg and pkg not in seen:
            seen.add(pkg)
            packages.append(pkg)

    # 1. PEP 621 [project.dependencies]
    project = data.get("project", {})
    if isinstance(project, dict):
        for dep in project.get("dependencies", []):
            if isinstance(dep, str):
                add_pkg(dep)

        # [project.optional-dependencies]
        optional_deps = project.get("optional-dependencies", {})
        if isinstance(optional_deps, dict):
            for group_deps in optional_deps.values():
                if isinstance(group_deps, list):
                    for dep in group_deps:
                        if isinstance(dep, str):
                            add_pkg(dep)

    # 2. Poetry [tool.poetry.dependencies]
    tool = data.get("tool", {})
    if isinstance(tool, dict):
        poetry = tool.get("poetry", {})
        if isinstance(poetry, dict):
            poetry_deps = poetry.get("dependencies", {})
            if isinstance(poetry_deps, dict):
                for dep_name in poetry_deps.keys():
                    if dep_name.lower() != "python":
                        add_pkg(dep_name)

            # Poetry dependency groups [tool.poetry.group.*.dependencies]
            group_dict = poetry.get("group", {})
            if isinstance(group_dict, dict):
                for group_data in group_dict.values():
                    if isinstance(group_data, dict):
                        g_deps = group_data.get("dependencies", {})
                        if isinstance(g_deps, dict):
                            for dep_name in g_deps.keys():
                                if dep_name.lower() != "python":
                                    add_pkg(dep_name)

        # Flit [tool.flit.metadata.requires]
        flit_metadata = tool.get("flit", {}).get("metadata", {})
        if isinstance(flit_metadata, dict):
            for dep in flit_metadata.get("requires", []):
                if isinstance(dep, str):
                    add_pkg(dep)

    return packages


def parse_setup_cfg_or_py(content: str) -> list[str]:
    """Basic extraction of dependencies from setup.cfg or setup.py."""
    packages: list[str] = []
    seen = set()

    # Look for install_requires block
    pattern = re.compile(r"install_requires\s*=\s*\[(.*?)\]", re.DOTALL)
    match = pattern.search(content)
    if match:
        block = match.group(1)
        # Find all quoted strings inside the list
        for item in re.findall(r"['\"]([^'\"]+)['\"]", block):
            pkg = _clean_requirement_line(item)
            if pkg and pkg not in seen:
                seen.add(pkg)
                packages.append(pkg)

    # In setup.cfg: install_requires = ... newline-separated
    cfg_pattern = re.compile(r"install_requires\s*=\s*\n((?:\s+[^\n]+\n)+)")
    cfg_match = cfg_pattern.search(content)
    if cfg_match:
        for line in cfg_match.group(1).splitlines():
            pkg = _clean_requirement_line(line)
            if pkg and pkg not in seen:
                seen.add(pkg)
                packages.append(pkg)

    return packages


def parse_manifest_dependencies(manifests: dict[str, str]) -> list[str]:
    """Parse dependencies from a dictionary of filename -> content mappings."""
    collected: list[str] = []
    seen = set()

    for filename, content in manifests.items():
        base_name = filename.lower().split("/")[-1].split("\\")[-1]

        if "requirements" in base_name and (base_name.endswith(".txt") or base_name.endswith(".in")):
            pkgs = parse_requirements_txt(content)
        elif base_name == "pyproject.toml":
            pkgs = parse_pyproject_toml(content)
        elif base_name in {"setup.cfg", "setup.py"}:
            pkgs = parse_setup_cfg_or_py(content)
        elif base_name.endswith((".txt", ".pip")):
            pkgs = parse_requirements_txt(content)
        else:
            continue

        for p in pkgs:
            if p not in seen:
                seen.add(p)
                collected.append(p)

    return collected
