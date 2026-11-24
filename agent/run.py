#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run one benchmark task: launch Claude Code at solvers/<solver>, confined so it
can only write the solver, with network off, then grade the result authoritatively.

  python3 -m agent.run --solver sundance [--model claude-opus-4-8] [--auth auto]
          [--network off] [--timeout 3600] [--split dev] [--tests] [--no-sandbox] [--tag t1]

What it does, in order:
  1. resolve auth (api-key / subscription / bedrock / auto)
  2. make a run dir runs/<ts>-<model>-<solver>-<tag>/ and capture provenance
     (solver base commit, task-file hashes, platform, CLI version)
  3. build the FS sandbox (write only to solvers/<solver> + results + build caches;
     everything else read-only) and the network policy, then launch the agent with
     the task instruction as its prompt
  4. save transcript.jsonl, summary.json, COST.md, result.md, patch.diff
  5. rebuild + re-measure with the frozen referee (agent/grade.py) -> grade.json

The agent's self-reported success is logged but never trusted; grade.json decides.
The stop criterion is the wall-clock --timeout (the task instruction states it so the
agent can pace itself); the agent also stops on its own when it judges the task done.
"""
import argparse
import datetime
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

from agent import backend, metrics, sandbox, grade as grading

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / 'tasks'
RUNS = ROOT / 'runs'


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _git(args, cwd):
    try:
        r = subprocess.run(['git', '-C', str(cwd)] + args, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, universal_newlines=True)
        return r.stdout.strip() if r.returncode == 0 else None
    except OSError:
        return None


def _sha256(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _provenance(solver_dir, task_files, model):
    import shutil
    cv = subprocess.run(['claude', '--version'], stdout=subprocess.PIPE,
                        universal_newlines=True).stdout.strip() if shutil.which('claude') else None
    return {
        'model': model,
        'solver_base_commit': _git(['rev-parse', 'HEAD'], solver_dir),
        'solver_describe': _git(['describe', '--always', '--dirty', '--tags'], solver_dir),
        'solver_dirty': bool(_git(['status', '--porcelain'], solver_dir)),
        'repo_commit': _git(['rev-parse', 'HEAD'], ROOT),
        'repo_dirty': bool(_git(['status', '--porcelain'], ROOT)),
        'task_file_sha256': {Path(p).name: _sha256(p) for p in task_files},
        'platform': platform.platform(), 'machine': platform.machine(),
        'python': platform.python_version(), 'claude_version': cv,
    }


def _capture_patch(solver_dir, base_commit, run_dir):
    """Diff the solver's current state (including new files) against its base
    commit, without disturbing the working tree."""
    if not base_commit:
        return {'note': 'no base commit; solver is not a git repo'}
    subprocess.run(['git', '-C', str(solver_dir), 'add', '-A'], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        patch = _git(['diff', '--cached', base_commit], solver_dir) or ''
        stat = _git(['diff', '--cached', '--stat', base_commit], solver_dir) or ''
        short = _git(['diff', '--cached', '--shortstat', base_commit], solver_dir) or ''
    finally:
        subprocess.run(['git', '-C', str(solver_dir), 'reset', '-q'], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (run_dir / 'patch.diff').write_text(patch + '\n')
    (run_dir / 'changed_files.txt').write_text(stat + '\n')
    import re
    d = {'shortstat': short.strip(), 'lines_added': 0, 'lines_removed': 0, 'files_changed': 0}
    for n, key in ((r'(\d+) files? changed', 'files_changed'), (r'(\d+) insertions?', 'lines_added'),
                   (r'(\d+) deletions?', 'lines_removed')):
        m = re.search(n, short)
        if m:
            d[key] = int(m.group(1))
    return d


def build_policy(solver):
    """FS allowlist: write only solvers/<solver> + results + build caches; the rest
    of the repo (scripts, edrat-check, benchmarks, bin/z3, tools, config.env) is
    read-only; home is denied. This is how the referee-integrity rule is enforced."""
    st = backend.claude_state_paths()
    home = Path.home()
    allow_rw = [ROOT / 'solvers' / solver, ROOT / 'results', home / '.cargo'] + st['allow_rw']
    allow_ro = [ROOT] + st['allow_ro'] + backend.toolchain_ro_paths()
    return sandbox.FsPolicy(allow_rw=tuple(str(p) for p in allow_rw),
                            allow_ro=tuple(str(p) for p in allow_ro),
                            deny=tuple(str(p) for p in st['deny']))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--solver', required=True, choices=['sundance', 'cvc5', 'alt-ergo'])
    ap.add_argument('--model', default=None, help='claude model id/alias (default: CLI default)')
    ap.add_argument('--auth', default='auto', choices=['auto', 'api-key', 'subscription', 'bedrock'])
    ap.add_argument('--network', default='off', help='off | loopback | on')
    ap.add_argument('--timeout', type=int, default=3600, help='wall-clock budget in seconds (the stop criterion)')
    ap.add_argument('--split', choices=('dev', 'holdout', 'full'), default='dev', help='grading split')
    ap.add_argument('--grade-timeout', type=int, default=60, help='per-query timeout when grading')
    ap.add_argument('--tests', action='store_true', help='also run the solver test suite (L2)')
    ap.add_argument('--no-sandbox', action='store_true', help='DANGER: run the agent unconfined')
    ap.add_argument('--no-grade', action='store_true', help='skip the post-run authoritative grade')
    ap.add_argument('--tag', default='t1')
    ap.add_argument('--jobs', type=int, default=None)
    a = ap.parse_args()

    spec_path = TASKS / ('%s.json' % a.solver)
    instr_path = TASKS / ('%s.md' % a.solver)
    if not spec_path.exists() or not instr_path.exists():
        sys.exit('missing task files %s / %s' % (spec_path, instr_path))
    spec = json.loads(spec_path.read_text())
    instruction = render_instruction(spec, instr_path.read_text(), a.timeout)
    solver_dir = ROOT / 'solvers' / a.solver
    if not solver_dir.exists():
        sys.exit('%s not built; run: scripts/fetch_solvers.sh %s' % (solver_dir, a.solver))

    auth_mode, env_override, warnings = backend.resolve_auth(a.auth)
    for w in warnings:
        print('  [auth] %s' % w, file=sys.stderr)
    net = sandbox.resolve_network(a.network)

    ts = _utcnow().strftime('%Y%m%dT%H%M%SZ')
    model_tag = (a.model or 'default').replace('/', '-')
    run_dir = RUNS / ('%s-%s-%s-%s' % (ts, model_tag, a.solver, a.tag))
    run_dir.mkdir(parents=True, exist_ok=True)

    argv = backend.build_argv(model=a.model, extra=sandbox.network_cli_args(net))
    base_commit = _git(['rev-parse', 'HEAD'], solver_dir)
    meta = {
        'run_id': run_dir.name, 'started_at': _utcnow().isoformat(),
        'solver': a.solver, 'config': spec['config'], 'auth_mode': auth_mode,
        'backend': backend.backend_provenance(auth_mode), 'network': net,
        'sandbox': (not a.no_sandbox), 'timeout_seconds': a.timeout, 'grading_split': a.split,
        'instruction_files': ['tasks/_common.md', str(instr_path.relative_to(ROOT))],
        'task_spec': spec, 'argv_without_prompt': argv,
        'provenance': _provenance(solver_dir, [TASKS / '_common.md', spec_path, instr_path], a.model),
    }
    (run_dir / 'prompt.md').write_text(instruction)   # the exact text the agent saw
    if not a.no_sandbox:
        policy = build_policy(a.solver)
        meta['sandbox_policy'] = policy.provenance()
        try:
            argv = sandbox.fs_confine_wrap(argv, policy, protect=ROOT)
        except sandbox.SandboxUnavailable as e:
            sys.exit('sandbox unavailable (fail closed): %s\n  (use --no-sandbox to override, UNSAFE)' % e)
    (run_dir / 'meta.json').write_text(json.dumps(meta, indent=2))
    print('run %s\n  solver=%s config=%s model=%s auth=%s network=%s sandbox=%s timeout=%ds'
          % (run_dir.name, a.solver, spec['config'], a.model or 'default', auth_mode, net,
             not a.no_sandbox, a.timeout))

    run = backend.run_agent(argv, instruction, cwd=ROOT, env_override=env_override,
                            timeout=a.timeout, transcript_path=str(run_dir / 'transcript.jsonl'))
    m = metrics.collect(run.events)
    diffstat = _capture_patch(solver_dir, base_commit, run_dir)
    metrics.write_summary(run_dir, m)
    metrics.write_cost_md(run_dir, run_dir.name, m, auth_mode, diffstat)
    (run_dir / 'result.md').write_text(run.final_text or '(no final text)')

    meta.update({'finished_at': _utcnow().isoformat(), 'elapsed_seconds': run.seconds,
                 'returncode': run.returncode, 'timed_out': run.timed_out,
                 'diffstat': diffstat, 'num_events': len(run.events),
                 'cost_usd': (m.get('result') or {}).get('total_cost_usd'),
                 'tool_uses': m['tool_names']})
    (run_dir / 'meta.json').write_text(json.dumps(meta, indent=2))
    print('  agent done in %.0fs (timed_out=%s), +%d/-%d lines, $%.4f'
          % (run.seconds, run.timed_out, diffstat.get('lines_added', 0), diffstat.get('lines_removed', 0),
             (m.get('result') or {}).get('total_cost_usd') or 0))

    if a.no_grade:
        print('  grading skipped (--no-grade). artifacts in %s' % run_dir)
        return
    print('  grading (rebuild + referee, this runs unsandboxed) ...')
    verdict = grading.grade(a.solver, spec['config'], split=a.split, timeout=a.grade_timeout,
                            out_dir=str(run_dir / 'grade'), jobs=a.jobs,
                            do_build=True, do_tests=a.tests,
                            baseline_gap=spec.get('baseline', {}).get('%s_mean_gap_pp' % a.split))
    (run_dir / 'grade.json').write_text(json.dumps(verdict, indent=2))
    meta['grade'] = verdict
    (run_dir / 'meta.json').write_text(json.dumps(meta, indent=2))
    _report(verdict)
    if verdict.get('verdict') == 'INVALID':
        (run_dir / 'INVALID.md').write_text(
            '# INVALID RUN\n\n%d wrong answer(s): an unsound result scores 0.\n\nSee grade.json.\n'
            % verdict.get('wrong_answers', 0))
    print('  artifacts in %s' % run_dir)


def _report(v):
    print('\n  === GRADE: %s ===' % v.get('verdict'))
    if 'L1_build' in v and not v['L1_build']['ok']:
        print('  L1 build FAILED'); return
    if v.get('L4_mean_gap_pp') is not None:
        line = '  mean gap to Z3: %.2f pp' % v['L4_mean_gap_pp']
        if 'improvement_pp' in v:
            line += '  (baseline %.2f -> %+.2f improvement)' % (v['baseline_gap_pp'], v['improvement_pp'])
        print(line)
    for s, st in (v.get('per_set') or {}).items():
        print('    %-8s solved %5.1f%%  z3 %5.1f%%  gap %5.1f pp' %
              (s, st['solved_pct'], st['z3_pct'] or 0, st['gap_pp'] or 0))
    print('  soundness: %s (%d wrong answers)' % ('VALID' if v.get('L3_valid') else 'INVALID',
                                                  v.get('wrong_answers', 0)))


def _fmt_budget(secs):
    if secs >= 3600:
        return '%.1f hours' % (secs / 3600.0)
    return '%d minutes' % (secs // 60)


def render_instruction(spec, specifics_md, timeout):
    """Compose the agent-visible prompt from the shared template (identical rules
    for every solver) + this solver's spec fields + its where-to-start block."""
    import re
    common = (TASKS / '_common.md').read_text()
    specifics = re.sub(r'^<!--.*?-->\s*', '', specifics_md, count=1, flags=re.S).strip()
    subs = {'SOLVER': spec['solver'], 'LANGUAGE': spec.get('language', ''),
            'CONFIG': spec['config'], 'BIN': spec.get('bin', spec['solver']),
            'BUILD_CMD': spec.get('build_cmd', ''), 'TEST_CMD': spec.get('test_cmd', ''),
            'SOLVED_CONDITION': spec.get('solved_condition', ''),
            'START_HINTS': specifics, 'TIME_BUDGET': _fmt_budget(timeout)}
    out = common
    for k, v in subs.items():
        out = out.replace('{{%s}}' % k, v)
    return out


if __name__ == '__main__':
    main()
