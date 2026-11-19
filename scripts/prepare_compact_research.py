"""Prepare an explicit, reproducible research-only experiment; never select by heuristics."""

import argparse
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path

from reposeer.agents.intake import SPECIALISTS
from reposeer.agents.orchestrator import MultiAgentRunRequest


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_selection_key')
        value[key] = item
    return value


def prepare_compact(input_path: Path, selection_path: Path, output_path: Path,
                    max_excerpt_chars: int = 2000) -> tuple[Path, Path]:
    if type(max_excerpt_chars) is not int or not 1 <= max_excerpt_chars <= 10000:
        raise ValueError('invalid_max_excerpt_chars')
    input_path, selection_path, output_path = map(Path, (input_path, selection_path, output_path))
    audit_path = output_path.with_name(output_path.stem + '.selection.json')
    if output_path.exists() or audit_path.exists():
        raise ValueError('compact_output_already_exists')
    if any(p.resolve() == source.resolve() for p in (output_path, audit_path)
           for source in (input_path, selection_path)):
        raise ValueError('compact_output_conflicts_with_input')
    original_bytes = input_path.read_bytes()
    # Validate the same bytes used for the identity hash.
    if len(original_bytes) > 4 * 1024 * 1024:
        raise ValueError('explicit_json_input_exceeds_4MiB_budget')
    request = MultiAgentRunRequest.model_validate_json(original_bytes)
    if request.strict_requests:
        raise ValueError('compact_research_requires_research_only_input')
    if selection_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('selection_input_exceeds_4MiB_budget')
    plan = json.loads(selection_path.read_bytes(), object_pairs_hook=_unique_object)
    if not isinstance(plan, dict) or set(plan) != set(SPECIALISTS):
        raise ValueError('selection_requires_exactly_three_specialists')
    if {r.agent_kind for r in request.research_requests} != set(SPECIALISTS):
        raise ValueError('compact_input_requires_three_specialists')
    children, per_specialist = [], {}
    for child in request.research_requests:
        refs = plan[child.agent_kind]
        if (not isinstance(refs, list) or not 1 <= len(refs) <= 3
                or any(not isinstance(ref, str) or not ref for ref in refs)
                or len(set(refs)) != len(refs)):
            raise ValueError('selection_requires_one_to_three_unique_source_refs')
        by_ref = {}
        for candidate in child.candidates:
            by_ref.setdefault(candidate.source_ref, []).append(candidate)
        if any(ref not in by_ref for ref in refs):
            raise ValueError('selection_source_ref_not_found')
        if any(len(by_ref[ref]) != 1 for ref in refs):
            raise ValueError('selection_source_ref_is_ambiguous')
        selected = [by_ref[ref][0] for ref in refs]
        candidates = [c.model_copy(update={
            'text_excerpt': c.text_excerpt[:max_excerpt_chars] if c.text_excerpt is not None else None
        }) for c in selected]
        children.append(child.model_copy(update={'candidates': candidates}))
        omitted = [c.source_ref for c in child.candidates if c.source_ref not in refs]
        per_specialist[child.agent_kind] = dict(
            original_candidate_count=len(child.candidates), selected_candidate_count=len(selected),
            omitted_candidate_count=len(omitted), selected_source_refs=refs, omitted_source_refs=omitted,
            truncated_candidate_count=sum(c.text_excerpt is not None and
                                          len(c.text_excerpt) > max_excerpt_chars for c in selected),
        )
    value = request.model_dump()
    value.update(research_requests=[c.model_dump() for c in children], intake_summary=None)
    compact = MultiAgentRunRequest.model_validate(value)
    compact_bytes = (compact.model_dump_json(indent=2) + '\n').encode('utf-8')
    audit = dict(
        source_input=str(input_path.resolve()),
        source_input_sha256=hashlib.sha256(original_bytes).hexdigest(),
        compact_input_sha256=hashlib.sha256(compact_bytes).hexdigest(),
        package_id=request.package_id, cutoff_at=request.cutoff_at, analysis_run_id=request.analysis_run_id,
        experiment_profile='compact_explicit_selection', max_excerpt_chars=max_excerpt_chars,
        original_source_count=len({c.source_ref for r in request.research_requests for c in r.candidates}),
        per_specialist=per_specialist,
        original_intake_summary=(request.intake_summary.model_dump(mode='json')
                                 if request.intake_summary is not None else None),
    )
    audit_bytes = (json.dumps(audit, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Reserve both files before writing; clean up only this invocation's newly created files on failure.
    created = []
    try:
        with ExitStack() as stack:
            streams = []
            for path in (output_path, audit_path):
                streams.append(stack.enter_context(path.open('xb')))
                created.append(path)
            for stream, content in zip(streams, (compact_bytes, audit_bytes), strict=True):
                stream.write(content)
    except OSError:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return output_path, audit_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--selection', type=Path, required=True)
    parser.add_argument('--max-excerpt-chars', type=int, default=2000)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        prepare_compact(args.input, args.selection, args.output, args.max_excerpt_chars)
    except (OSError, ValueError, TypeError) as error:
        allowed = {
            'invalid_max_excerpt_chars', 'compact_output_already_exists', 'compact_output_conflicts_with_input',
            'compact_research_requires_research_only_input', 'duplicate_selection_key',
            'selection_requires_exactly_three_specialists', 'compact_input_requires_three_specialists',
            'selection_requires_one_to_three_unique_source_refs', 'selection_source_ref_not_found',
            'selection_source_ref_is_ambiguous', 'selection_input_exceeds_4MiB_budget',
            'explicit_json_input_exceeds_4MiB_budget',
        }
        code = str(error) if str(error) in allowed else 'invalid_compact_input_or_output'
        print(f'Compact preparation error: {code}')
        return 2
    print('Compact research request and selection audit created')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
