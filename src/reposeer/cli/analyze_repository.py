"""Application Analyze command; the prepared-feature `analyze` command stays compatible."""

from pathlib import Path
from typing import Annotated

import typer

from reposeer.agents import MultiAgentCoordinator
from reposeer.agents.provider import ReplayBundle, ReplaySpecialistLLM, create_specialist_llm
from reposeer.analysis.application import ApplicationOrchestrator
from reposeer.analysis.application_inputs import AnalysisInputError, StoredAnalysisBackend
from reposeer.analysis.collection_backend import CollectionAnalysisBackend


def analyze_repository(
    identifier: Annotated[str, typer.Argument(help='PyPI name/ID or a verified GitHub URL.')],
    model: Annotated[Path | None, typer.Option(dir_okay=False, help='Frozen Model A artifact.')] = None,
    input_bundle: Annotated[list[Path] | None, typer.Option(exists=True, dir_okay=False)] = None,
    data_dir: Annotated[Path, typer.Option()] = Path('data'),
    live: Annotated[bool, typer.Option(help='Explicitly acquire current data through T-020.')] = False,
    package: Annotated[str | None, typer.Option(help='Select the PyPI package for a GitHub URL.')] = None,
    cutoff_at: Annotated[str | None, typer.Option()] = None,
    mode: Annotated[str, typer.Option(help='CORE or ENHANCED.')] = 'CORE',
    purpose: Annotated[str, typer.Option(help='current_inference or retrospective_analysis.')] = 'retrospective_analysis',
    insights: Annotated[bool, typer.Option()] = False,
    agents: Annotated[bool, typer.Option()] = False,
    agent_replay: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    agent_live: Annotated[bool, typer.Option(help='Opt in to live specialist LLM calls.')] = False,
    impact: Annotated[bool, typer.Option()] = False,
    output: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
) -> None:
    """Resolve a package, run eligible stages and return one validated FinalReport JSON."""
    if live and input_bundle:
        raise typer.BadParameter('--live cannot be combined with --input-bundle')
    if agent_replay and agent_live:
        raise typer.BadParameter('--agent-replay and --agent-live are mutually exclusive')
    if (agent_replay or agent_live) and not agents:
        raise typer.BadParameter('agent provider options require --agents')
    if output is not None and output.exists():
        raise typer.BadParameter('output_already_exists')
    providers = []

    def agent_factory():
        if agent_replay:
            if agent_replay.stat().st_size > 4 * 1024 * 1024:
                raise ValueError('agent_replay_exceeds_4MiB')
            provider = ReplaySpecialistLLM(ReplayBundle.model_validate_json(agent_replay.read_bytes()))
        elif agent_live:
            provider = create_specialist_llm()
            providers.append(provider)
        else:
            raise ValueError('agent_provider_not_selected')
        return MultiAgentCoordinator(provider, execution_mode='live' if agent_live else 'replay')

    try:
        backend = (CollectionAnalysisBackend(data_dir) if live else StoredAnalysisBackend.from_files(
            input_bundle if input_bundle is not None else sorted((data_dir / 'analysis_inputs').glob('*.json'))))
        report = ApplicationOrchestrator(
            backend, model_path=model, agent_factory=agent_factory if agents else None,
            reports_dir=data_dir / 'reports/analysis' if output is None else None,
        ).analyze_repository(
            identifier, package_name=package, cutoff_at=cutoff_at, mode_requested=mode, purpose=purpose,
            include_external_insights=insights, include_agents=agents, include_dependency_impact=impact,
        )
        payload = report.model_dump_json(indent=2) + '\n'
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open('x', encoding='utf-8') as stream:
                stream.write(payload)
        typer.echo(payload, nl=False)
        if report.result_status == 'failed':
            raise typer.Exit(1)
    except AnalysisInputError as error:
        typer.echo(f'Analyze input stopped: {error}', err=True)
        raise typer.Exit(2) from None
    except (OSError, ValueError, TypeError):
        typer.echo('Analyze stopped: invalid_input_or_output_configuration', err=True)
        raise typer.Exit(2) from None
    finally:
        for provider in providers:
            try:
                provider.close()
            except Exception:
                pass
