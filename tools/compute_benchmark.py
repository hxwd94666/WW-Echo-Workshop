# 对同一合成评分矩阵测量 Rust 搜索、通信和解质量，记录完整输入摘要。
import argparse
import hashlib
import json
from pathlib import Path
import random
import statistics
import sys
import time
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]


def fixture(items, roles, varied):
    profile = str(UUID(int=1))
    records = []
    for i in range(items):
        cost = (4, 3, 1)[i % 3]
        main = {
            4: [('暴击', 22), ('攻击', 150)],
            3: [('衍射伤害加成', 30), ('攻击', 100)],
            1: [('攻击%', 18), ('生命', 2280)],
        }[cost]
        sub = [('暴击', 6.3), ('暴击伤害', 12.6), ('攻击%', 6.4), ('防御%', 8.1), ('生命%', 6.4)]
        records.append(
            dict(
                item_id=str(UUID(int=i + 100)),
                profile_id=profile,
                cost=cost,
                name=f'synthetic-{i}',
                sonata='甲',
                review='confirmed',
                level=25,
                main_stats=[dict(stat_name=n, value=v) for n, v in main],
                sub_stats=[dict(stat_name=n, value=v) for n, v in sub],
                captured_at='2026-10-16T00:00:00+00:00',
            )
        )
    specs = [
        dict(
            character_id=f'fixture_{r}',
            modal_id='default',
            target=150,
            rule=dict(element='衍射', sonatas=['甲', '乙']),
            slots=[dict(cost=c, main_names=[], fixed_item_id='') for c in (4, 3, 3, 1, 1)],
            set_requirements=[],
            cost_budget=12,
            distinct_names=True,
            owner_label='',
            set_unique_names=True,
        )
        for r in range(roles)
    ]
    request = dict(
        profile_id=profile,
        roles=specs,
        fully_tuned=True,
        priority_groups=[[[role['character_id'], role['modal_id']] for role in specs]],
    )
    rng = random.Random(20261016)
    points = [rng.randrange(2000, 4001) for _ in records]
    matrix = [
        points[:] if not varied else [rng.randrange(2000, 4001) for _ in records] for _ in specs
    ]
    data = dict(request=request, records=records, matrix_cents=matrix)
    baseline_encoded = json.dumps(
        dict(points=[n / 100 for n in points], request=request), sort_keys=True, ensure_ascii=False
    ).encode()
    return data, hashlib.sha256(baseline_encoded).hexdigest()


def main():
    from echo_workshop.application.compute_protocol import ComputeWorker

    parser = argparse.ArgumentParser()
    parser.add_argument('--items', type=int, default=300)
    parser.add_argument('--roles', type=int, default=5)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--nodes', type=int, default=50000)
    parser.add_argument('--varied', action='store_true')
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not (
        1 <= args.items <= 5000
        and 1 <= args.roles <= 8
        and 1 <= args.repeats <= 20
        and 1 <= args.nodes <= 5000000
    ):
        parser.error('合成基准规模超过允许范围')
    payload, baseline_hash = fixture(args.items, args.roles, args.varied)
    payload['max_nodes'] = args.nodes
    payload['seconds'] = 5
    runs = []
    with ComputeWorker(
        ROOT / 'build/native/release/echo-workshop-compute.exe', fault_tests=True
    ) as worker:
        for _ in range(args.repeats):
            start = time.perf_counter()
            result = worker.call('allocate_matrix', payload, timeout=8)
            elapsed = time.perf_counter() - start
            ids = [item for row in result['roles'] for item in row['item_ids']]
            assert len(ids) == len(set(ids))
            runs.append(
                dict(
                    wall_seconds=elapsed,
                    native_seconds=result['elapsed'],
                    nodes=result['nodes'],
                    objective=result['objective'],
                    totals_cents=[row['total_cents'] for row in result['roles']],
                    exhaustive=result['exhaustive'],
                    stop_reason=result['stop_reason'],
                    statistics=result['statistics'],
                )
            )
    report = dict(
        schema_version=1,
        input=dict(
            items=args.items,
            roles=args.roles,
            repeats=args.repeats,
            nodes=args.nodes,
            varied=args.varied,
            baseline_sha256=baseline_hash,
            full_payload_sha256=hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
        ),
        note='Same matrix search comparison; native timing includes problem preparation/validation, wall timing also includes IPC; not a pure language-only speedup claim.',
        runs=runs,
        median_wall_seconds=statistics.median(r['wall_seconds'] for r in runs),
        median_native_seconds=statistics.median(r['native_seconds'] for r in runs),
    )
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding='utf-8'))
        assert not args.varied and baseline['input_sha256'] == baseline_hash, (
            'baseline input mismatch'
        )
        assert all(
            sum(run['totals_cents']) >= sum(old['totals_cents'])
            for run in runs
            for old in baseline['runs']
        )
        report['baseline_comparison'] = dict(
            source=str(args.baseline),
            same_input_sha256=True,
            previous_median_seconds=baseline['median_seconds'],
            wall_speedup=baseline['median_seconds'] / report['median_wall_seconds'],
            previous_exhaustive=[r['exhaustive'] for r in baseline['runs']],
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != 'runs'}, ensure_ascii=False
        )
    )


if __name__ == '__main__':
    main()
