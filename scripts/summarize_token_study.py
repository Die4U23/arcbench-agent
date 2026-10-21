"""Summarize all settled Flash trials, including failures; never call a model."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics


def summarize(study):
    usage = json.loads((study / 'token-usage.json').read_text(encoding='utf-8'))
    if any(r['status'] != 'settled' for r in usage['requests']):
        raise ValueError('Unknown usage: cannot publish a complete token total')
    rows = []
    source_trees = {}
    for trial in usage['trials']:
        if trial['outcome'] == 'running':
            raise ValueError('Study is still running')
        path = study / trial['label'] / 'result.json'
        row = json.loads(path.read_text(encoding='utf-8'))
        records = [r for r in usage['requests'] if r['label'] == trial['label']]
        if len(records) != row['requests'] or row['outcome'] != trial['outcome']:
            raise ValueError('Trial outcome or request count disagrees with usage records')
        for key, value in row['usage'].items():
            measured = [r['usage'].get(key) for r in records]
            expected = sum(measured) if measured and all(type(v) is int for v in measured) else None
            if value != expected:
                raise ValueError('Usage disagrees with original records: ' + trial['label'])
        tools = Counter()
        reuse = 0
        for line in path.with_name('controller.log').read_text(encoding='utf-8').splitlines():
            if 'ARCBENCH_METRIC ' not in line:
                continue
            event = json.loads(line.split('ARCBENCH_METRIC ', 1)[1])
            if event['event'] == 'tool_returned':
                tools[event['tool']] += 1
            reuse += event['event'] == 'source_read_reused'
        fixture, arm, repetition = trial['label'].rsplit('-', 2)
        if arm not in {'baseline', 'candidate'}:
            raise ValueError('Unexpected arm: ' + arm)
        metadata = dict(trial['metadata'])
        tree = metadata.pop('source_sha256')
        tree_digest = hashlib.sha256(json.dumps(tree, sort_keys=True).encode()).hexdigest()
        commit = metadata['source_commit']
        if commit in source_trees and source_trees[commit] != tree:
            raise ValueError('Candidate source changed during the fixed study')
        source_trees[commit] = tree
        row.update(fixture=fixture, arm=arm, repetition=int(repetition), tools=dict(tools),
                   reuse_events=reuse, metadata=dict(metadata, source_tree_sha256=tree_digest),
                   result_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        rows.append(row)
    groups = {}
    for fixture in ['all', *sorted({r['fixture'] for r in rows})]:
        groups[fixture] = {}
        for arm in ('baseline', 'candidate'):
            selected = [r for r in rows if r['arm'] == arm and (fixture == 'all' or r['fixture'] == fixture)]
            if not selected:
                continue
            successes = sum(r['outcome'] == 'passed' for r in selected)
            fields = {}
            for key in selected[0]['usage']:
                values = [r['usage'][key] for r in selected]
                fields[key] = sum(values) if all(type(v) is int for v in values) else None
            total = fields['prompt_tokens'] + fields['completion_tokens']
            tools = Counter()
            for row in selected:
                tools.update(row['tools'])
            groups[fixture][arm] = dict(trials=len(selected), passed=successes,
                requests=sum(r['requests'] for r in selected), usage=fields, total_tokens=total,
                tokens_per_success=total / successes if successes else None,
                input_hit_rate=fields['prompt_cache_hit_tokens'] / fields['prompt_tokens']
                    if fields['prompt_cache_hit_tokens'] is not None and fields['prompt_tokens'] else None,
                median_seconds=statistics.median(r['seconds'] for r in selected), tools=dict(tools))
    return dict(scope='Fixed synthetic repairs; not platform acceptance', policy=usage['policy'],
                stopped_reason=usage['stopped'], planned_trials=usage['policy']['requests'] // usage['policy']['round_requests'],
                execution_order=[t['label'] for t in usage['trials']], groups=groups, rows=rows, source_trees=source_trees,
                total_tokens=sum(r['usage']['prompt_tokens'] + r['usage']['completion_tokens'] for r in rows))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = summarize(args.study)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'groups': report['groups'], 'total_tokens': report['total_tokens']}, indent=2))
