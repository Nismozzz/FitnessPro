"""Deterministic trace scoring; text patterns are regression signals, not a medical/semantic judge."""
import re
from collections import Counter

from eval.common import mean, subset_matches


def score_agent(case, trace, final_answer, done, state):
    counts = Counter(t['name'] for t in trace)
    missing = [e.name for e in case.expected_tools if counts[e.name] < e.min_calls]
    forbidden = sorted(set(case.forbidden_tools) & set(counts))
    ordered = iter(t['name'] for t in trace)
    order_ok = all(any(name == expected for name in ordered) for expected in case.ordered_tools)
    arg_checks = []
    for expected in case.expected_tools:
        if expected.arguments:
            matches = sum(t['name'] == expected.name and t['success'] and subset_matches(expected.arguments, t['arguments']) for t in trace)
            arg_checks.append({'name': expected.name, 'expected': expected.arguments, 'passed': matches >= expected.min_calls})
    successful_required = all(sum(t['name'] == e.name and t['success'] for t in trace) >= e.min_calls for e in case.expected_tools)
    patterns_found = [p for p in case.forbidden_patterns if re.search(p, final_answer, re.I)]
    patterns_missing = [p for p in case.required_patterns if not re.search(p, final_answer, re.I)]
    validations = [t for t in trace if t['name'] == 'draft_weekly_plan']
    first_valid = (validations[0]['success'] and bool((validations[0].get('data') or {}).get('valid'))) if validations else None
    selection = not missing and not forbidden and order_ok
    args_correct = all(c['passed'] for c in arg_checks)
    # For plan tasks a missing validation counts as first-pass failure, avoiding survivor bias.
    if case.expect_plan_saved and first_valid is None:
        first_valid = False
    grounding = state.get('plan_grounded') if case.check_plan_grounding else None
    persistence = (not case.expect_plan_saved or state['new_plans'] > 0) and (not case.expect_log_saved or state['new_logs'] > 0)
    safe_plans = not state.get('injury_risk_plan_ids', [])
    completed = done.get('status') == 'completed'
    retrieval_healthy = not state.get('retrieval_degraded', False)
    return {'passed': bool(completed and bool(final_answer.strip()) and selection and args_correct and successful_required and not patterns_found and not patterns_missing and persistence and safe_plans and retrieval_healthy and grounding is not False),
            'completed': completed, 'required_tools_succeeded': successful_required, 'tool_selection_correct': bool(selection), 'missing_tools': missing,
            'forbidden_tools_called': forbidden, 'tool_order_correct': order_ok,
            'argument_correct': args_correct if arg_checks else None, 'argument_checks': arg_checks,
            'forbidden_patterns_found': patterns_found, 'required_patterns_missing': patterns_missing,
            'plan_first_pass_valid': first_valid, 'plan_grounded': grounding, 'persistence_correct': persistence,
            'no_injury_risk_plans': safe_plans, 'retrieval_healthy': retrieval_healthy, 'tool_success_rate': mean(t['success'] for t in trace)}


def aggregate_agent(rows):
    # Include infrastructure failures in case-level denominator, but not pretend they had trace metrics.
    scores = [r['score'] for r in rows if r.get('score')]
    dones = [r['done'] for r in rows if r.get('done')]
    return {'cases': len(rows), 'evaluated_cases': len(scores), 'error_cases': sum(r.get('error') is not None for r in rows),
            'pass_rate': sum(bool(r.get('score', {}).get('passed')) for r in rows) / len(rows) if rows else None,
            'tool_selection_accuracy': mean(s['tool_selection_correct'] for s in scores),
            'argument_accuracy': mean(s['argument_correct'] for s in scores),
            'argument_cases': sum(s['argument_correct'] is not None for s in scores),
            'plan_first_pass_rate': mean(s['plan_first_pass_valid'] for s in scores),
            'plan_validation_cases': sum(s['plan_first_pass_valid'] is not None for s in scores),
            'plan_grounding_rate': mean(s['plan_grounded'] for s in scores),
            'average_iterations': mean(d.get('iterations') for d in dones),
            'average_prompt_tokens': mean(d.get('usage', {}).get('prompt_tokens') for d in dones),
            'average_completion_tokens': mean(d.get('usage', {}).get('completion_tokens') for d in dones),
            'average_total_tokens': mean(d['usage']['prompt_tokens'] + d['usage']['completion_tokens'] for d in dones if d.get('usage')),
            'average_latency_ms': mean(r.get('latency_ms') for r in rows)}
