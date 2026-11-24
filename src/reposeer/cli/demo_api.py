"""Run the standalone demo backend; frontend is a separate process."""

from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import typer

from reposeer.agents import MultiAgentCoordinator
from reposeer.agents.provider import ReplayBundle, ReplaySpecialistLLM, create_specialist_llm
from reposeer.analysis.application import ApplicationOrchestrator
from reposeer.analysis.application_inputs import StoredAnalysisBackend
from reposeer.analysis.collection_backend import CollectionAnalysisBackend
from reposeer.demo.api import AnalysisJobs, make_server


def demo_api(
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
    origin: Annotated[str, typer.Option(help='Exact allowed frontend origin.')] = 'http://localhost:5173',
    input_bundle: Annotated[list[Path] | None, typer.Option(exists=True, dir_okay=False)] = None,
    model: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    mock_dir: Annotated[Path | None, typer.Option(exists=True, file_okay=False)] = None,
    data_dir: Annotated[Path, typer.Option()] = Path('data'),
    live: Annotated[bool, typer.Option(help='Explicit opt-in to current network collection.')] = False,
    agent_replay: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    agent_live: Annotated[bool, typer.Option(help='Explicit opt-in to paid live specialist LLM calls.')] = False,
) -> None:
    """Serve /api/analyses on loopback. No frontend framework or web dependency required."""
    if live and input_bundle:
        raise typer.BadParameter('--live cannot be combined with --input-bundle')
    if agent_replay and agent_live:
        raise typer.BadParameter('--agent-replay and --agent-live are mutually exclusive')
    parsed = urlsplit(origin)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise typer.BadParameter('--origin must be an exact HTTP(S) origin without a path')
    jobs = None
    server = None
    providers = []

    def agent_factory():
        if agent_replay:
            if agent_replay.stat().st_size > 4 * 1024 * 1024:
                raise ValueError('agent_replay_exceeds_4MiB')
            provider = ReplaySpecialistLLM(ReplayBundle.model_validate_json(agent_replay.read_bytes()))
        else:
            if not providers:
                providers.append(create_specialist_llm())
            provider = providers[0]
        return MultiAgentCoordinator(provider, execution_mode='live' if agent_live else 'replay')
    try:
        backend = CollectionAnalysisBackend(data_dir) if live else StoredAnalysisBackend.from_files(
            input_bundle if input_bundle is not None else sorted((data_dir / 'analysis_inputs').glob('*.json')))
        jobs = AnalysisJobs(ApplicationOrchestrator(backend, model_path=model,
            agent_factory=agent_factory if agent_replay or agent_live else None,
            reports_dir=data_dir / 'reports/analysis'), mock_dir)
        server = make_server(jobs, port, origin)
        typer.echo(f'RepoSeer API ready: http://127.0.0.1:{port}/api/health; frontend origin: {origin}')
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError):
        typer.echo('API stopped: invalid_configuration_or_port_unavailable', err=True)
        raise typer.Exit(2) from None
    finally:
        if server:
            server.server_close()
        if jobs:
            jobs.close()
        for provider in providers:
            try:
                provider.close()
            except Exception:
                pass
