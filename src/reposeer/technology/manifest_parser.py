"""Static Python dependency parsing; never reads includes or executes project code."""

import ast
import configparser
import re
import textwrap
import tomllib
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from packaging.requirements import InvalidRequirement, Requirement

from reposeer.contracts.identity import normalize_pypi_name


@dataclass(frozen=True)
class ManifestDependency:
    name: str
    filename: str
    scope: str = "runtime"
    declaration: str = ""


@dataclass
class ManifestParseResult:
    dependencies: list[ManifestDependency] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)


def _clean_requirement_line(line: str) -> str | None:
    # Pip comments require leading whitespace; URL fragments remain intact.
    cleaned = line.strip()
    quote = None
    for index, char in enumerate(cleaned):
        if char in {"'", '"'}:
            quote = None if quote == char else (char if quote is None else quote)
        elif char == "#" and quote is None and index > 0 and cleaned[index - 1].isspace():
            cleaned = cleaned[:index].strip()
            break
    if not cleaned or cleaned.startswith("#"):
        return None
    if cleaned.startswith(("-e ", "--editable ", "--editable=")):
        cleaned = re.sub(r"^(?:-e\s+|--editable[=\s]+)", "", cleaned)
    elif cleaned.startswith("-"):
        return None
    # Hash options apply to the requirement, not to package identity.
    cleaned = re.split(r"\s+--hash(?:=|\s)", cleaned, maxsplit=1)[0]
    if cleaned.startswith(("git+", "hg+", "svn+", "bzr+")):
        names = parse_qs(urlsplit(cleaned).fragment).get("egg", [])
        if len(names) != 1:
            return None
        cleaned = names[0]
    try:
        return normalize_pypi_name(Requirement(cleaned).name)
    except (InvalidRequirement, ValueError):
        return None


def _requirements(content: str, filename: str, scope: str) -> ManifestParseResult:
    result = ManifestParseResult()
    # Join pip line continuations without resolving any file references.
    content = re.sub(r"\\\r?\n", " ", content)
    for line_number, line in enumerate(content.splitlines(), 1):
        name = _clean_requirement_line(line)
        if name:
            result.dependencies.append(ManifestDependency(name, filename, scope, line.strip()))
        elif line.strip() and not line.lstrip().startswith(("#", "-")):
            result.diagnostics.append(f"{filename}:{line_number}: unsupported or invalid requirement")
    return result


def _pyproject(content: str, filename: str) -> ManifestParseResult:
    data = tomllib.loads(content)  # Invalid TOML is observable to direct callers.
    result = ManifestParseResult()

    def add_list(declarations, scope):
        if not isinstance(declarations, list):
            result.diagnostics.append(f"{filename}: invalid dependency list ({scope})")
            return
        for declaration in declarations:
            name = _clean_requirement_line(declaration) if isinstance(declaration, str) else None
            if name:
                result.dependencies.append(ManifestDependency(name, filename, scope, declaration))
            else:
                result.diagnostics.append(f"{filename}: invalid dependency declaration ({scope})")

    project = data.get("project", {})
    if not isinstance(project, dict):
        raise ValueError("project metadata must be a table")
    add_list(project.get("dependencies", []), "runtime")
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise ValueError("optional-dependencies must be a table")
    for group, declarations in sorted(optional.items()):
        add_list(declarations, f"optional:{group}")
    if "dependencies" in project.get("dynamic", []):
        result.diagnostics.append(f"{filename}: dynamic dependencies are unsupported")

    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        raise ValueError("tool metadata must be a table")
    poetry = tool.get("poetry", {})
    if not isinstance(poetry, dict):
        raise ValueError("Poetry metadata must be a table")

    def add_poetry(declarations, scope):
        if not isinstance(declarations, dict):
            raise ValueError("Poetry dependencies must be a table")
        for name, declaration in declarations.items():
            if name.casefold() == "python":
                continue
            try:
                normalized = normalize_pypi_name(name)
            except ValueError:
                result.diagnostics.append(f"{filename}: invalid Poetry package name")
                continue
            dep_scope = "optional:poetry" if isinstance(declaration, dict) and declaration.get("optional") else scope
            result.dependencies.append(ManifestDependency(normalized, filename, dep_scope, name))

    add_poetry(poetry.get("dependencies", {}), "runtime")
    add_poetry(poetry.get("dev-dependencies", {}), "dev:poetry")
    groups = poetry.get("group", {})
    if not isinstance(groups, dict):
        raise ValueError("Poetry groups must be a table")
    for group, metadata in sorted(groups.items()):
        if not isinstance(metadata, dict):
            raise ValueError("Poetry group must be a table")
        add_poetry(metadata.get("dependencies", {}), f"dev:{group}")
    flit = tool.get("flit", {})
    if isinstance(flit, dict) and isinstance(flit.get("metadata", {}), dict):
        add_list(flit.get("metadata", {}).get("requires", []), "runtime")
    return result


def _setup_cfg(content: str, filename: str) -> ManifestParseResult:
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(textwrap.dedent(content))
    result = ManifestParseResult()
    if config.has_option("options", "install_requires"):
        result = _requirements(config.get("options", "install_requires"), filename, "runtime")
    if config.has_section("options.extras_require"):
        for group, declarations in config.items("options.extras_require"):
            parsed = _requirements(declarations, filename, f"optional:{group}")
            result.dependencies.extend(parsed.dependencies)
            result.diagnostics.extend(parsed.diagnostics)
    return result


def _setup_py(content: str, filename: str) -> ManifestParseResult:
    tree = ast.parse(textwrap.dedent(content))
    result = ManifestParseResult()
    # Only literal arguments of a top-level setuptools setup() call are accepted.
    setup_names = set()
    module_names = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module in {"setuptools", "distutils.core"}:
            setup_names.update(alias.asname or alias.name for alias in node.names if alias.name == "setup")
        elif isinstance(node, ast.Import):
            module_names.update(alias.asname or alias.name for alias in node.names if alias.name == "setuptools")
    found = False
    for node in tree.body:
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        func = call.func
        is_setup = (isinstance(func, ast.Name) and func.id in setup_names) or (
            isinstance(func, ast.Attribute) and func.attr == "setup"
            and isinstance(func.value, ast.Name) and func.value.id in module_names
        )
        if not is_setup:
            continue
        found = True
        for keyword in call.keywords:
            if keyword.arg not in {"install_requires", "extras_require"}:
                if keyword.arg is None:
                    result.diagnostics.append(f"{filename}: unsupported dynamic setup arguments")
                continue
            try:
                declarations = ast.literal_eval(keyword.value)
            except (ValueError, TypeError):
                result.diagnostics.append(f"{filename}: unsupported dynamic {keyword.arg}")
                continue
            groups = {"runtime": declarations} if keyword.arg == "install_requires" else declarations
            if not isinstance(groups, dict):
                result.diagnostics.append(f"{filename}: unsupported {keyword.arg}")
                continue
            for group, requirements in groups.items():
                if not isinstance(requirements, list | tuple) or not all(isinstance(x, str) for x in requirements):
                    result.diagnostics.append(f"{filename}: unsupported dependency list")
                    continue
                scope = "runtime" if keyword.arg == "install_requires" else f"optional:{group}"
                parsed = _requirements("\n".join(requirements), filename, scope)
                result.dependencies.extend(parsed.dependencies)
                result.diagnostics.extend(parsed.diagnostics)
    if not found:
        result.diagnostics.append(f"{filename}: unsupported setup declaration")
    return result


def parse_manifests(manifests: dict[str, str]) -> ManifestParseResult:
    """Return declarations/scopes and diagnostics, parsing only supplied files."""
    result = ManifestParseResult()
    for filename, content in sorted(manifests.items()):
        base = filename.replace("\\", "/").rsplit("/", 1)[-1].casefold()
        try:
            if base == "pyproject.toml":
                parsed = _pyproject(content, filename)
            elif base == "setup.cfg":
                parsed = _setup_cfg(content, filename)
            elif base == "setup.py":
                parsed = _setup_py(content, filename)
            elif base.endswith((".txt", ".in", ".pip")):
                scope = "dev:requirements" if re.search(r"(?:dev|test)", base) else "runtime"
                parsed = _requirements(content, filename, scope)
            else:
                result.diagnostics.append(f"{filename}: unsupported manifest")
                continue
        except (ValueError, SyntaxError, configparser.Error) as error:
            result.diagnostics.append(f"{filename}: parse error: {error}")
            continue
        result.dependencies.extend(parsed.dependencies)
        result.diagnostics.extend(parsed.diagnostics)
    return result


def _names(result: ManifestParseResult) -> list[str]:
    return list(dict.fromkeys(dep.name for dep in result.dependencies))


def parse_requirements_txt(content: str) -> list[str]:
    return _names(_requirements(content, "requirements.txt", "runtime"))


def parse_pyproject_toml(content: str) -> list[str]:
    """Return normalized names; malformed TOML raises instead of claiming zero deps."""
    return _names(_pyproject(content, "pyproject.toml"))


def parse_setup_cfg_or_py(content: str) -> list[str]:
    parser = _setup_cfg if re.search(r"^\s*\[options", content, re.M) else _setup_py
    result = parser(content, "setup.cfg" if parser is _setup_cfg else "setup.py")
    if result.diagnostics:
        raise ValueError("; ".join(result.diagnostics))
    return _names(result)


def parse_manifest_dependencies(manifests: dict[str, str]) -> list[str]:
    """Compatibility name-list API; richer diagnostics are available via parse_manifests."""
    result = parse_manifests(manifests)
    if result.diagnostics:
        raise ValueError("; ".join(result.diagnostics))
    return _names(result)
