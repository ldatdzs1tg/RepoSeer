"""Run explicit D07 research/strict inputs or retained canonical documents, without crawling."""

import argparse
from pathlib import Path
from uuid import uuid4

from reposeer.agents.intake import IntakeSummary, build_research_requests_from_raw
from reposeer.agents.orchestrator import MultiAgentCoordinator, MultiAgentRunRequest
from reposeer.agents.provider import OpenAISpecialistLLM, ReplayBundle, ReplaySpecialistLLM


def _read(path, model):
    path = Path(path)
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('explicit_json_input_exceeds_4MiB_budget')
    return model.model_validate_json(path.read_bytes())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('live', 'replay'), required=True)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--input', type=Path)
    inputs.add_argument('--raw-documents', type=Path)
    parser.add_argument('--responses', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model')
    parser.add_argument('--analysis-run-id')
    parser.add_argument('--package-id')
    parser.add_argument('--repo-id')
    parser.add_argument('--cutoff-at')
    parser.add_argument('--package-description')
    parser.add_argument('--description-source-ref', action='append', default=[])
    parser.add_argument('--max-candidates', type=int, default=12)
    parser.add_argument('--max-excerpt-chars', type=int, default=5000)
    parser.add_argument('--max-llm-calls', type=int, default=7)
    parser.add_argument('--max-review-calls', type=int, default=1)
    args = parser.parse_args(argv)
    if args.mode == 'replay' and args.responses is None:
        parser.error('replay requires --responses')
    if args.mode == 'live' and args.responses is not None:
        parser.error('--responses applies only to replay')
    if args.raw_documents and (not args.package_id or not args.cutoff_at):
        parser.error('--raw-documents requires --package-id and --cutoff-at')
    provider = None
    try:
        if args.output.exists():
            print('Agent run error: output_already_exists')
            return 2
        if args.input:
            request = _read(args.input, MultiAgentRunRequest)
        else:
            report = IntakeSummary()
            run_id = args.analysis_run_id or str(uuid4())
            requests = build_research_requests_from_raw(
                args.raw_documents, analysis_run_id=run_id, package_id=args.package_id,
                cutoff_at=args.cutoff_at, repo_id=args.repo_id,
                package_description=args.package_description,
                description_source_refs=args.description_source_ref,
                max_candidates=args.max_candidates, max_excerpt_chars=args.max_excerpt_chars,
                summary=report,
            )
            request = MultiAgentRunRequest(
                schema_version='0.1.0', record_type='multi_agent_run_request',
                analysis_run_id=run_id, package_id=args.package_id, cutoff_at=args.cutoff_at,
                research_requests=requests, strict_requests=[], intake_summary=report,
            )
        provider = (ReplaySpecialistLLM(_read(args.responses, ReplayBundle)) if args.mode == 'replay'
                    else OpenAISpecialistLLM(model=args.model))
        result = MultiAgentCoordinator(
            provider, execution_mode=args.mode, max_llm_calls=args.max_llm_calls,
            max_review_calls=args.max_review_calls, max_candidates_per_agent=args.max_candidates,
            max_excerpt_chars=args.max_excerpt_chars,
        ).analyze(request)
        payload = result.model_dump_json(indent=2) + '\n'
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents overwriting even if another writer wins a race.
        with args.output.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(payload)
        print(f'{result.execution_mode}: {result.overall_status}; '
              f'{len(result.research_responses)} research responses; '
              f'{len(result.research_insights)} insights, {len(result.information_gaps)} gaps, '
              f'{len(result.enrichment_suggestions)} suggestions; '
              f'{len(result.excluded_or_rejected_items)} rejected/audited items')
        for status in result.agent_execution_statuses:
            if status.lane == 'research' or status.status != 'not_requested':
                print(f'{status.agent_kind}/{status.lane}: {status.status} ({status.llm_calls} provider calls)')
        return 1 if result.overall_status == 'failed' else 0
    except (OSError, ValueError, TypeError) as error:
        # Validation errors can contain entire input values; do not print them or provider details.
        allowed = {
            'missing_agent_model_configuration', 'missing_openai_api_key',
            'optional_functional_sdk_unavailable', 'llm_client_initialization_failed',
        }
        code = str(error) if str(error) in allowed else 'invalid_input_output_or_configuration'
        print(f'Agent run error: {code}; check explicit paths, schemas and live configuration')
        return 2
    finally:
        if isinstance(provider, OpenAISpecialistLLM):
            try:
                provider.close()
            except Exception:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
