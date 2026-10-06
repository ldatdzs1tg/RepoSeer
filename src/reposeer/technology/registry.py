"""Detector registry."""

from reposeer.technology.detectors.base import BaseDetector
from reposeer.technology.detectors.docker import DockerDetector
from reposeer.technology.detectors.java import JavaDetector
from reposeer.technology.detectors.javascript import JavaScriptDetector
from reposeer.technology.detectors.python import PythonDetector
from reposeer.technology.detectors.rust import RustDetector


def get_default_detectors() -> list[BaseDetector]:
    """Return all active detectors in priority order."""
    return [
        PythonDetector(),
        JavaScriptDetector(),
        DockerDetector(),
        JavaDetector(),
        RustDetector(),
    ]
