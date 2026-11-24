#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Authoritative grading of a solver's current state. This is the single source
of truth for "is it better" — it runs OUTSIDE the agent sandbox, rebuilds the
solver from its current source, and re-measures with the frozen referee
(`scripts/bench.py` + the proof checkers), exactly as the final evaluation will.

The agent's own self-checks during a run are advisory; only this post-hoc grade
counts.

Verdict ladder (mapped to this benchmark's rules, see TASK.md):
  L1 build     scripts/fetch_solvers.sh <solver> succeeds
  L2 tests     the solver's own test suite passes (optional; --tests)
  L3 valid     0 wrong answers on dev, holdout and the canaries (soundness gate)
  L4 dev       gap to Z3 on the dev split (259 Verus queries the agent can see)
  L5 holdout   gap to Z3 on the holdout split (the other 2,317 Verus queries, never visible to the
               agent). This is the headline score: score_gap_pp. Lower is better.

A run that fails L3 is INVALID regardless of its gap (an unsound answer scores 0).
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCORED_SETS = ['verus']     # only the Verus development set is scored for now (see splits/)
# Grader-only data (the Z3 holdout reference, holdout runs). The agent sandbox denies this directory.
REFEREE = os.path.join(ROOT, '.referee')


def _run(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, **kw)


def rebuild(solver, log_path=None, jobs=None):
    """L1: rebuild the solver from its current source via the frozen build script
    (keeps the agent's edits; no --update, so no upstream pull)."""
    env = dict(os.environ)
    env.setdefault('JOBS', str(jobs or (os.cpu_count() or 4)))
    bash = env.get('BENCH_BASH', 'bash')
    r = _run([bash, os.path.join('scripts', 'fetch_solvers.sh'), solver], env=env)
    if log_path:
        open(log_path, 'w').write(r.stdout)
    return {'ok': r.returncode == 0, 'returncode': r.returncode, 'tail': r.stdout[-800:]}


TEST_CMDS = {
    'sundance': ['cargo', 'test', '--release', '--no-default-features', '--features', 'minisat'],
    'cvc5': ['ctest', '--test-dir', 'build', '-j4'],
    'alt-ergo': ['dune', 'test'],
}


def run_tests(solver, log_path=None):
    """L2: the solver's own test suite (TASK rule 6). Best-effort; skipped if the
    command is not available."""
    cmd = TEST_CMDS.get(solver)
    if not cmd:
        return {'ok': None, 'note': 'no test command configured'}
    d = os.path.join(ROOT, 'solvers', solver)
    try:
        r = subprocess.run(cmd, cwd=d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True)
    except OSError as e:
        return {'ok': None, 'note': 'cannot run tests: %s' % e}
    if log_path:
        open(log_path, 'w').write(r.stdout)
    return {'ok': r.returncode == 0, 'returncode': r.returncode, 'tail': r.stdout[-800:]}


def _score_json(run_out, ref):
    r = _run([sys.executable, os.path.join('scripts', 'bench.py'), 'score', run_out,
              '--ref', ref, '--json'])
    try:
        return json.loads(r.stdout)[0]
    except (json.JSONDecodeError, IndexError, KeyError):
        raise RuntimeError('bench.py score failed:\n' + r.stdout[-1200:])


def reference(split, timeout, jobs=None):
    """The Z3 reference run for SPLIT, created if missing. Dev lives in results/ (the agent may use it);
    holdout lives in .referee/ (the agent cannot see it)."""
    base = os.path.join(ROOT, 'results') if split == 'dev' else REFEREE
    ref = os.path.join(base, 'z3-%s-t%d' % (split, timeout))
    if not os.path.exists(os.path.join(ref, 'verus.csv')):
        os.makedirs(ref, exist_ok=True)
        cmd = [sys.executable, os.path.join('scripts', 'bench.py'), 'run', 'z3', '--split', split,
               '--timeout', str(timeout), '--out', ref, '--sets', ','.join(SCORED_SETS + (['canaries'] if split == 'dev' else []))]
        if jobs:
            cmd += ['-j', str(jobs)]
        r = _run(cmd)
        if r.returncode != 0:
            raise RuntimeError('Z3 reference run failed:\n' + r.stdout[-1200:])
    return ref


def measure(solver, config, *, split='dev', timeout=60, out_dir, jobs=None, sets=None):
    """Run the solver on SPLIT with the frozen referee and score it against Z3. Returns the
    per-set stats, the gap to Z3, and the soundness verdict. The canaries run with dev only."""
    ref = reference(split, timeout, jobs)
    sets = sets or (SCORED_SETS + (['canaries'] if split == 'dev' else []))
    cmd = [sys.executable, os.path.join('scripts', 'bench.py'), 'run', config,
           '--split', split, '--timeout', str(timeout), '--out', out_dir, '--rerun',
           '--sets', ','.join(sets)]
    if jobs:
        cmd += ['-j', str(jobs)]
    run = _run(cmd)
    row = _score_json(out_dir, ref)
    gaps = []
    per_set = {}
    for s in SCORED_SETS:
        st = row['sets'].get(s)
        if not st or not st.get('n'):
            continue
        solved = 100.0 * st['solved@%d' % timeout] / st['n']
        refp = 100.0 * st['ref_solved'] / st['common'] if st.get('common') else None
        gap = (refp - solved) if refp is not None else None
        if gap is not None:
            gaps.append(gap)
        per_set[s] = {'n': st['n'], 'solved_pct': round(solved, 1),
                      'z3_pct': round(refp, 1) if refp is not None else None,
                      'gap_pp': round(gap, 1) if gap is not None else None,
                      'proof_rejected': st.get('rejected', 0), 'proof_unchecked': st.get('unchecked', 0)}
    canary = row['sets'].get('canaries', {})
    return {'valid': row['valid'], 'wrong': row['wrong'],
            'mean_gap_pp': round(sum(gaps) / len(gaps), 2) if gaps else None,
            'per_set': per_set, 'canaries': canary,
            'solver_run_tail': run.stdout[-600:]}


def grade(solver, config, *, timeout=60, out_dir, jobs=None, do_build=True, do_tests=False,
          holdout=True, baseline=None):
    """Full grade: build (L1), tests (L2), soundness (L3), dev gap (L4), holdout gap (L5).
    Returns a verdict dict suitable for grade.json."""
    v = {'solver': solver, 'config': config, 'timeout': timeout}
    if do_build:
        v['L1_build'] = rebuild(solver, os.path.join(out_dir, 'build.log'), jobs)
        if not v['L1_build']['ok']:
            v['verdict'] = 'BUILD_FAILED'
            return v
    if do_tests:
        v['L2_tests'] = run_tests(solver, os.path.join(out_dir, 'tests.log'))
    runs = {'dev': measure(solver, config, split='dev', timeout=timeout,
                           out_dir=os.path.join(out_dir, 'dev'), jobs=jobs)}
    if holdout:
        runs['holdout'] = measure(solver, config, split='holdout', timeout=timeout,
                                  out_dir=os.path.join(out_dir, 'holdout'), jobs=jobs)
    for split, m in runs.items():
        v[split] = {k: m[k] for k in ('mean_gap_pp', 'per_set', 'wrong', 'valid')}
    v['canaries'] = runs['dev']['canaries']
    v['wrong_answers'] = sum(m['wrong'] for m in runs.values())
    v['L3_valid'] = all(m['valid'] for m in runs.values())
    v['L4_dev_gap_pp'] = runs['dev']['mean_gap_pp']
    v['L5_holdout_gap_pp'] = runs['holdout']['mean_gap_pp'] if holdout else None
    v['score_gap_pp'] = v['L5_holdout_gap_pp'] if holdout else v['L4_dev_gap_pp']   # the headline
    v['score_split'] = 'holdout' if holdout else 'dev'
    base = (baseline or {}).get('%s_mean_gap_pp' % v['score_split'])
    if base is not None and v['score_gap_pp'] is not None:
        v['baseline_gap_pp'] = base
        v['improvement_pp'] = round(base - v['score_gap_pp'], 2)   # positive = better
    v['verdict'] = ('INVALID' if not v['L3_valid'] else
                    'OK' if v['score_gap_pp'] is not None else 'NO_SCORE')
    return v


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='Authoritatively grade a solver (rebuild + referee).')
    ap.add_argument('solver', choices=['sundance', 'cvc5', 'alt-ergo'])
    ap.add_argument('--config', help='bench.py config (default: same as solver)')
    ap.add_argument('--timeout', type=int, default=60)
    ap.add_argument('--out', default=None, help='output dir (default: .referee/grade-<solver>)')
    ap.add_argument('--jobs', type=int, default=None)
    ap.add_argument('--no-build', action='store_true')
    ap.add_argument('--tests', action='store_true')
    ap.add_argument('--no-holdout', action='store_true', help='dev only (quick; not a final grade)')
    a = ap.parse_args()
    out = a.out or os.path.join(REFEREE, 'grade-%s' % a.solver)
    os.makedirs(out, exist_ok=True)
    verdict = grade(a.solver, a.config or a.solver, timeout=a.timeout, out_dir=out, jobs=a.jobs,
                    do_build=not a.no_build, do_tests=a.tests, holdout=not a.no_holdout)
    open(os.path.join(out, 'grade.json'), 'w').write(json.dumps(verdict, indent=2))
    print(json.dumps(verdict, indent=2))
