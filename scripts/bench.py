#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run solvers on the paper's benchmark sets, check their proofs, and score them against Z3.

  bench.py run SOLVER [--sets S,..] [--split dev|full] [--timeout 60] [-j N] [--out DIR] [--limit N]
  bench.py score RUN_DIR... [--ref results/z3-full] [--markdown]
  bench.py list [--sets S,..] [--split dev|full]            # print the queries of a split
  bench.py select-canaries CVC5_REGRESS_DIR OUT_DIR         # used by fetch_benchmarks.sh

SOLVER: z3 (reference), sundance, cvc5, alt-ergo, or one of the variants listed in CONFIGS below.
Sets: verus (2,576 = development set without the SplinterDB copies), heldout (4,086), dafny (11,674),
fstar (3,195), canaries (satisfiable soundness checks). Only verus is scored for now, so the default is
verus and the canaries; the other sets run only when you pass them to --sets.
Splits: for verus, `dev` is the 259 queries in splits/verus-dev.txt (10% of each project) and `holdout` is
the other 2,317. For the other sets, `dev` is a 10% sample by hash of the path and `holdout` is the rest.
`full` is everything. The canaries are always run in full.

A query counts as solved if the solver answers `unsat` within the time limit, and, for configs that
produce proofs, the proof checker accepts the proof (edrat-check for Sundance, Logos for cvc5). A `sat`
answer on a benchmark, or an `unsat` answer on a canary, is a WRONG answer. Any wrong answer makes the
run invalid.
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.environ.get('BENCH_ROOT', os.path.join(ROOT, 'benchmarks'))
BIN = os.environ.get('BENCH_BIN', os.path.join(ROOT, 'bin'))
EDRAT_CHECK = os.path.join(ROOT, 'edrat-check', 'edrat_check.py')
PREP = os.path.join(ROOT, 'scripts', 'prep_smtlib.py')
CPC_SIG = os.path.join(ROOT, 'tools', 'cpc-signature', 'proofs', 'eo')
CHECKED = ('VERIFIED', 'VERIFIED-ETHOS')
# cvc5 proof checkers: Logos first, then Ethos for proofs Logos cannot read. CHECKERS=logos: Logos only.
CVC5_CHECKERS = os.environ.get('CHECKERS', 'logos,ethos').split(',')

SETS = {   # name -> (folder, path filter, family)
    'verus': ('verus_benchmarks/UFDTLIA/20241211-verus', lambda r: not r.startswith('anvil/splinterdb-smt-'), 'verus'),
    'heldout': ('sundance_heldout_benchmarks/UFDTLIA', None, 'verus'),
    'dafny': ('dafny_fstar_benchmarks/UFLIA', lambda r: r.startswith('d_'), 'dafny'),
    'fstar': ('dafny_fstar_benchmarks/UFLIA', lambda r: r.startswith('fs_'), 'fstar'),
    'canaries': ('canaries', None, 'canary'),
}
MAIN_SETS = ['verus', 'heldout', 'dafny', 'fstar']

CVC5_STRICT = ['--no-cbqi', '--user-pat=strict']
CVC5_PROOF = ['--produce-proofs', '--dump-proofs', '--proof-format-mode=cpc', '--proof-granularity=dsl-rewrite']


def b(name):
    return os.environ.get(name.upper().replace('-', '_') + '_BIN', os.path.join(BIN, name))


# name -> (binary, kind, proof checker or None, description). The command lines are fixed: improve the
# solver, not its flags. Defaults inside the solver can change.
CONFIGS = {
    'z3': ('z3', 'z3', None, 'Z3, options of the verifier (reference, no proofs)'),
    'z3-proof': ('z3', 'z3', None, 'Z3, options of the verifier, proof=true (not checked)'),
    'sundance': ('sundance-smt', 'sundance', 'edrat', 'Sundance, eDRAT proofs checked by edrat-check'),
    'sundance-noproof': ('sundance-smt', 'sundance', None, 'Sundance without proofs'),
    'cvc5': ('cvc5', 'cvc5', 'logos', 'cvc5 --no-cbqi --user-pat=strict, CPC proofs checked by Logos'),
    'cvc5-noproof': ('cvc5', 'cvc5', None, 'cvc5 --no-cbqi --user-pat=strict without proofs'),
    'alt-ergo': ('alt-ergo', 'alt-ergo', None, 'Alt-Ergo, default options (no proofs)'),
}

HEADER_OPTIONS_RE = re.compile(r'following options:(.*?)\|\)', re.S)
HEADER_OPTION_RE = re.compile(r'\(set-option :(\S+) ([^)]*)\)')
VERDICT_RE = re.compile(r'^[ \t]*(unsat|sat|unknown|timeout)[ \t\r]*$', re.M)


# ------------------------------------------------------------------------------------------ queries
SCORED_SETS = ['verus']
SPLITS = ('dev', 'holdout', 'full')
SPLIT_FILES = {'verus': os.path.join(ROOT, 'splits', 'verus-dev.txt')}
_dev_lists = {}


def in_split(name, rel, split):
    if split == 'full' or name == 'canaries':
        return True
    if name in SPLIT_FILES:
        if name not in _dev_lists:
            with open(SPLIT_FILES[name]) as f:
                _dev_lists[name] = set(l.strip() for l in f if l.strip())
        dev = rel in _dev_lists[name]
    else:
        dev = int(hashlib.sha1(rel.encode()).hexdigest()[:8], 16) % 10 == 0
    return dev if split == 'dev' else not dev


def list_set(name, split):
    folder, keep, _ = SETS[name]
    base = os.path.join(BENCH, folder)
    out = []
    for d, _, files in os.walk(base):
        for f in files:
            if f.endswith('.smt2'):
                rel = os.path.relpath(os.path.join(d, f), base)
                if (keep is None or keep(rel)) and in_split(name, rel, split):
                    out.append(rel)
    return base, sorted(out)


def header_z3_options(path):
    with open(path, errors='replace') as f:
        head = f.read(8192)
    m = HEADER_OPTIONS_RE.search(head)
    return ['%s=%s' % (k, v.strip()) for k, v in HEADER_OPTION_RE.findall(m.group(1))] if m else []


def has_push(path):
    with open(path, errors='replace') as f:
        head = f.read(1 << 20)
    return '(push' in head


# ------------------------------------------------------------------------------------------ running
def no_core_dumps():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run_limited(cmd, timeout, stdout_path=None, cwd=None):
    """Runs CMD with a wall-clock limit, killing its process group. Returns (stdout, stderr, secs, rc|None)."""
    t0 = time.time()
    out_f = open(stdout_path, 'w') if stdout_path else subprocess.PIPE
    try:
        p = subprocess.Popen(cmd, stdout=out_f, stderr=subprocess.PIPE, universal_newlines=True,
                             start_new_session=True, cwd=cwd or tempfile.gettempdir(), preexec_fn=no_core_dumps)
    except OSError as e:
        return '', 'cannot run %s: %s' % (cmd[0], e), 0.0, -1
    try:
        out, err = p.communicate(timeout=timeout)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = p.communicate()
        rc = None
    finally:
        if stdout_path:
            out_f.close()
    secs = time.time() - t0
    if stdout_path:
        with open(stdout_path, errors='replace') as f:
            out = f.read(1 << 16)
    return out or '', err or '', secs, rc


def solve(cfg, family, query, timeout, work):
    """Returns dict(status, seconds, proof, note)."""
    binary, kind, checker, _ = CONFIGS[cfg]
    exe = b(binary)
    proof = None
    stdout_path = None
    q = query
    cmd = []
    if kind == 'z3':
        cmd = [exe] + (header_z3_options(query) if family == 'verus' else []) + \
              (['proof=true'] if cfg == 'z3-proof' else []) + [query, '-t:%d' % (timeout * 1000)]
    elif kind == 'sundance':
        cmd = [exe, '--arithmetic', 'internalincremental', '--timeout=%d' % timeout]
        if family != 'verus':
            cmd.append('--infer-triggers')
        if checker:
            proof = os.path.join(work, 'proof.edrat')
            cmd.append('--proof=' + proof)
        cmd.append(query)
    elif kind == 'cvc5':
        cmd = [exe] + (['--incremental'] if family != 'verus' and has_push(query) else []) + CVC5_STRICT
        if checker:
            cmd += CVC5_PROOF
            stdout_path = os.path.join(work, 'stdout.txt')
        cmd += ['--tlimit-per=%d' % (timeout * 1000), query]
    elif kind == 'alt-ergo':
        q = os.path.join(work, 'query.smt2')
        r = subprocess.run([sys.executable, PREP, query, q], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
        if r.returncode != 0:
            return dict(status='error', seconds=0.0, note='prep: ' + r.stderr.strip()[-200:])
        cmd = [exe, '--timelimit=%d' % timeout, q]
    out, err, secs, rc = run_limited(cmd, timeout + 5, stdout_path)
    m = VERDICT_RE.search(out)
    if rc is None:
        status = 'timeout'
    elif m:
        status = m.group(1)
        if status == 'unknown' and secs >= timeout * 0.95:
            status = 'timeout'
    else:
        status = 'error'
    note = '' if status != 'error' else (err.strip() or out.strip())[-300:].replace('\n', ' ')
    res = dict(status=status, seconds=round(secs, 3), note=note)
    if status == 'unsat' and checker == 'logos':
        # the CPC proof follows the `unsat` line, wrapped in one pair of parentheses (the get-proof response)
        with open(stdout_path, errors='replace') as f:
            text = f.read()
        i = text.find('unsat\n')
        text = text[i + 6:].strip() if i >= 0 else ''
        if text.startswith('(') and text.endswith(')'):
            text = text[1:-1]
        proof = os.path.join(work, 'proof.cpc')
        with open(proof, 'w') as g:
            g.write(text)
    res['proof'] = proof if status == 'unsat' and proof and os.path.exists(proof) else None
    return res


def check_proof(cfg, query, proof, timeout, work):
    """Returns (verdict, seconds, note). verdict: VERIFIED, NOT VERIFIED, INCOMPLETE, TIMEOUT, ERROR."""
    checker = CONFIGS[cfg][2]
    if checker == 'edrat':
        cmd = [sys.executable, EDRAT_CHECK, query, proof, '--drat-trim', b('drat-trim'),
               '--oracle-bin', b('z3'), '--timeout', str(timeout), '--json', os.path.join(work, 'check.json')]
        out, err, secs, rc = run_limited(cmd, timeout + 30)
        last = (out.strip().splitlines() or [''])[-1]
        verdict = {'s VERIFIED': 'VERIFIED', 's NOT VERIFIED': 'NOT VERIFIED', 's TIMEOUT': 'TIMEOUT'}.get(last)
        if rc is None:
            verdict = 'TIMEOUT'
        note = ''
        m = re.search(r'^c reason: (.*)$', out, re.M)
        if m:
            note = m.group(1)
        return verdict or 'ERROR', round(secs, 3), note or (err.strip()[-200:] if not verdict else '')
    if checker == 'logos':
        # Logos is slow on large proofs: give it 2/3 of the budget and Ethos the rest
        out, err, secs, rc = run_limited([b('logos'), proof], timeout * 2 / 3)
        word = out.strip().split('\n')[-1].strip() if out.strip() else ''
        verdict = {'correct': 'VERIFIED', 'incorrect': 'NOT VERIFIED', 'incomplete': 'INCOMPLETE'}.get(word, 'ERROR')
        if rc is None:
            verdict = 'TIMEOUT'
        note = err.strip()[-200:].replace('\n', ' ') if verdict != 'VERIFIED' else ''
        if verdict in ('ERROR', 'INCOMPLETE', 'TIMEOUT') and 'ethos' in CVC5_CHECKERS and os.path.exists(b('ethos')) and os.path.isdir(CPC_SIG):
            # Logos cannot parse this proof (or it is outside Logos's SMT-LIB model): fall back to Ethos
            ev, es, enote = check_ethos(proof, max(1, timeout - secs), work)
            return ev, round(secs + es, 3), 'logos: %s %s; ethos: %s' % (verdict, note, enote)
        return verdict, round(secs, 3), note
    return '', 0.0, ''


def check_ethos(proof, timeout, work):
    # not *.eo: Ethos reads .eo files as signatures, where `let` and `Type` are not available
    f = os.path.join(work, 'proof-with-signature.cpc')
    with open(f, 'w') as g, open(proof) as p:
        g.write('(include "%s/cpc/Cpc.eo")\n(include "%s/cpc/expert/CpcExpert.eo")\n' % (CPC_SIG, CPC_SIG))
        shutil.copyfileobj(p, g)
    out, err, secs, rc = run_limited([b('ethos'), '--require-proof-of-false', f], timeout)
    if rc is None:
        return 'TIMEOUT', secs, ''
    text = (out + err).strip()
    if rc == 0 and 'correct' in out and 'incorrect' not in out:
        return 'VERIFIED-ETHOS', secs, ''
    if 'incorrect' in text or 'does not have conclusion false' in text:
        return 'NOT VERIFIED', secs, text[-200:].replace('\n', ' ')
    return 'ERROR', secs, text[-200:].replace('\n', ' ')       # e.g. a parse error: the proof is not checkable


def solver_version(cfg):
    exe = b(CONFIGS[cfg][0])
    real = os.path.realpath(exe)
    info = {'binary': real}
    d = os.path.dirname(real)
    for _ in range(6):
        if os.path.isdir(os.path.join(d, '.git')):
            r = subprocess.run(['git', '-C', d, 'describe', '--always', '--dirty', '--tags'],
                               stdout=subprocess.PIPE, universal_newlines=True)
            info['git'] = r.stdout.strip()
            break
        d = os.path.dirname(d)
    try:
        with open(real, 'rb') as f:
            info['sha256'] = hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        info['sha256'] = None
    return info


FIELDS = ['query', 'status', 'seconds', 'check', 'check_seconds', 'note']


def cmd_run(a):
    cfg = a.solver
    if cfg not in CONFIGS:
        sys.exit('unknown solver config %s; one of: %s' % (cfg, ', '.join(CONFIGS)))
    out = a.out or os.path.join(ROOT, 'results', '%s-%s-t%d' % (cfg, a.split, a.timeout))
    os.makedirs(out, exist_ok=True)
    meta = dict(solver=cfg, description=CONFIGS[cfg][3], split=a.split, timeout=a.timeout,
                version=solver_version(cfg), host=os.uname()[1], started=datetime.datetime.now().isoformat())
    with open(os.path.join(out, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=1)
    if a.keep_proofs:
        os.makedirs(os.path.join(out, 'proofs'), exist_ok=True)
    lock = threading.Lock()
    for s in a.sets.split(','):
        base, rels = list_set(s, a.split)
        if a.limit:
            rels = rels[:a.limit]
        if not rels:
            print('%s: no queries (run scripts/fetch_benchmarks.sh)' % s)
            continue
        family = SETS[s][2]
        path = os.path.join(out, s + '.csv')
        done = {}
        if os.path.exists(path) and not a.rerun:
            with open(path) as f:
                done = {r['query']: r for r in csv.DictReader(f)}
        todo = [r for r in rels if r not in done]
        print('%s: %d queries, %d to run (%s, %ds, -j %d)' % (s, len(rels), len(todo), cfg, a.timeout, a.jobs),
              flush=True)
        new = not os.path.exists(path) or a.rerun
        fh = open(path, 'w' if new else 'a', newline='')
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        counts = {}

        def one(rel):
            work = tempfile.mkdtemp(prefix='bench-', dir=a.tmp)
            try:
                q = os.path.join(base, rel)
                r = solve(cfg, family, q, a.timeout, work)
                r['query'] = rel
                if r.get('proof') and not a.no_check:
                    r['check'], r['check_seconds'], cnote = check_proof(cfg, q, r['proof'], a.check_timeout, work)
                    if cnote:
                        r['note'] = (r.get('note', '') + ' check: ' + cnote).strip()
                    if a.keep_proofs:
                        dst = os.path.join(out, 'proofs', s, rel + os.path.splitext(r['proof'])[1])
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.move(r['proof'], dst)
                return r
            finally:
                shutil.rmtree(work, ignore_errors=True)

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            futs = [ex.submit(one, rel) for rel in todo]
            for i, fut in enumerate(as_completed(futs), 1):
                r = fut.result()
                key = r['status'] + ('/' + r['check'] if r.get('check') else '')
                with lock:
                    counts[key] = counts.get(key, 0) + 1
                    w.writerow({k: r.get(k, '') for k in FIELDS})
                    fh.flush()
                if a.verbose or i % 100 == 0 or i == len(todo):
                    print('  [%d/%d %.0fs] %s' % (i, len(todo), time.time() - t0,
                                                 ' '.join('%s=%d' % kv for kv in sorted(counts.items()))), flush=True)
        fh.close()
    print('results in %s' % out)


# ------------------------------------------------------------------------------------------ scoring
def load_run(d):
    with open(os.path.join(d, 'meta.json')) as f:
        meta = json.load(f)
    sets = {}
    for s in SETS:
        p = os.path.join(d, s + '.csv')
        if os.path.exists(p):
            with open(p) as f:
                sets[s] = {r['query']: r for r in csv.DictReader(f)}
    return meta, sets


def solved(r, meta, budget):
    if r['status'] != 'unsat' or float(r['seconds'] or 0) > budget:
        return False
    if CONFIGS.get(meta['solver'], (0, 0, None))[2]:
        return r.get('check') in CHECKED
    return True


def cmd_score(a):
    runs = [(d,) + load_run(d) for d in a.runs]
    ref = load_run(a.ref) if a.ref else None
    rows = []
    for d, meta, sets in runs:
        T = meta['timeout']
        row = {'run': os.path.basename(d.rstrip('/')), 'solver': meta['solver'], 'valid': True, 'sets': {}}
        wrong = 0
        for s in MAIN_SETS + ['canaries']:
            if s not in sets:
                continue
            res = sets[s]
            n = len(res)
            if s == 'canaries':
                # a canary is only trusted if the reference does not disagree with its `sat` label
                refc = ref[1].get(s, {}) if ref else {}
                bad = [q for q, r in res.items() if r['status'] == 'unsat' and refc.get(q, {}).get('status') != 'unsat']
                wrong += len(bad)
                row['sets'][s] = {'n': n, 'wrong': len(bad)}
                continue
            refres = ref[1].get(s, {}) if ref else {}
            w = [q for q, r in res.items() if r['status'] == 'sat' and refres.get(q, {}).get('status') != 'sat']
            wrong += len(w)
            st = {'n': n, 'wrong': len(w)}
            for budget in (1, 10, T):
                st['solved@%d' % budget] = sum(solved(r, meta, budget) for r in res.values())
            st['unsat'] = sum(r['status'] == 'unsat' for r in res.values())
            st['ethos_only'] = sum(r['status'] == 'unsat' and r.get('check') == 'VERIFIED-ETHOS' for r in res.values())
            st['rejected'] = sum(r['status'] == 'unsat' and r.get('check') == 'NOT VERIFIED' for r in res.values())
            st['unchecked'] = sum(r['status'] == 'unsat' and r.get('check') in ('ERROR', 'TIMEOUT', 'INCOMPLETE')
                                  for r in res.values())
            if ref:
                rs = ref[1].get(s, {})
                common = [q for q in res if q in rs]
                st['ref_solved'] = sum(solved(rs[q], ref[0], T) for q in common)
                st['common'] = len(common)
            row['sets'][s] = st
        row['valid'] = wrong == 0
        row['wrong'] = wrong
        rows.append(row)
    if a.json:
        print(json.dumps(rows, indent=1))
        return
    for row in rows:
        T = [m for d, m, _ in runs if os.path.basename(d.rstrip('/')) == row['run']][0]['timeout']
        print('\n## %s (%s)%s' % (row['run'], row['solver'], '' if row['valid'] else
                                   '   *** INVALID: %d wrong answers ***' % row['wrong']))
        print('| set | n | solved@1s | solved@10s | solved@%ds | Z3@%ds | gap (pp) | proof rejected | proof unchecked |'
              % (T, T))
        print('|---|---:|---:|---:|---:|---:|---:|---:|---:|')
        gaps = []
        for s in MAIN_SETS:
            st = row['sets'].get(s)
            if not st or not st['n']:
                continue
            n = st['n']
            pct = lambda k: 100.0 * st[k] / n
            refp = 100.0 * st['ref_solved'] / st['common'] if st.get('common') else None
            gap = refp - pct('solved@%d' % T) if refp is not None else None
            if gap is not None:
                gaps.append(gap)
            print('| %s | %d | %.1f%% | %.1f%% | %.1f%% | %s | %s | %d | %d |' % (
                s, n, pct('solved@1'), pct('solved@10'), pct('solved@%d' % T),
                '%.1f%%' % refp if refp is not None else '-', '%.1f' % gap if gap is not None else '-',
                st['rejected'], st['unchecked']))
        c = row['sets'].get('canaries')
        if c:
            print('\ncanaries: %d satisfiable queries, %d answered unsat (must be 0)' % (c['n'], c['wrong']))
        rej = sum(row['sets'].get(s, {}).get('rejected', 0) for s in MAIN_SETS)
        if rej:
            print('\nWARNING: %d unsat answers whose proof the checker REJECTED (possible soundness bugs)' % rej)
        if gaps:
            print('\nmean gap to Z3: %.2f pp over %d sets (lower is better; 0 = as good as Z3)' %
                  (sum(gaps) / len(gaps), len(gaps)))


# ------------------------------------------------------------------------------------------ canaries
BAD_TOKENS = re.compile(r'\b(bv\w*|BitVec|str\.\w+|seq\.\w+|set\.\w+|Array|select|store|fp\.\w+|FloatingPoint|'
                        r'RoundingMode|bag\.\w+|Seq|Set|String|RegLan|re\.\w+|ff\.\w+|declare-codatatypes?|'
                        r'lambda|match|define-fun-rec|define-funs-rec|declare-heap|sep|wand|pto|nil)\b')
GOOD_LOGIC = re.compile(r'\(set-logic\s+(QF_)?(UF|DT|UFDT|LIA|LRA|LIRA|UFLIA|UFLRA|UFLIRA|UFDTLIA|UFDTLRA|'
                        r'UFDTLIRA|ALL)\)')


def cmd_select_canaries(a):
    os.makedirs(a.out, exist_ok=True)
    n = 0
    for d, _, files in os.walk(a.regress):
        for f in sorted(files):
            if not f.endswith('.smt2') or '/parser' in d:      # parser tests use cvc5-only semantics
                continue
            p = os.path.join(d, f)
            if os.path.getsize(p) > 200000:
                continue
            with open(p, errors='replace') as fh:
                text = fh.read()
            expects = re.findall(r'^; EXPECT: (\S+)', text, re.M)
            if expects != ['sat'] or text.count('(check-sat') != 1:
                continue
            if 'EXPECT-ERROR' in text or 'DISABLE-TESTER' in text or 'REQUIRES' in text:
                continue
            if not GOOD_LOGIC.search(text) or BAD_TOKENS.search(re.sub(r';[^\n]*', '', text)):
                continue
            if re.search(r'\((get-|push|pop|check-sat-assuming|declare-pool|assert-rewrite)', text):
                continue
            rel = os.path.relpath(p, a.regress).replace('/', '__')
            shutil.copy(p, os.path.join(a.out, rel))
            n += 1
    print('canaries: %d satisfiable queries in %s' % (n, a.out))


def cmd_list(a):
    for s in a.sets.split(','):
        base, rels = list_set(s, a.split)
        for r in rels:
            print(os.path.join(base, r))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd')
    r = sub.add_parser('run')
    r.add_argument('solver')
    r.add_argument('--sets', default=','.join(SCORED_SETS + ['canaries']))
    r.add_argument('--split', choices=SPLITS, default='dev')
    r.add_argument('--timeout', type=int, default=60)
    r.add_argument('--check-timeout', type=int, default=600)
    r.add_argument('-j', '--jobs', type=int, default=max(1, (os.cpu_count() or 2) * 3 // 4))
    r.add_argument('--out')
    r.add_argument('--limit', type=int)
    r.add_argument('--tmp', default=None, help='scratch directory for proofs (default: system temp)')
    r.add_argument('--keep-proofs', action='store_true')
    r.add_argument('--no-check', action='store_true', help='do not check proofs (they then do not count)')
    r.add_argument('--rerun', action='store_true', help='discard earlier results in --out')
    r.add_argument('-v', '--verbose', action='store_true')
    s = sub.add_parser('score')
    s.add_argument('runs', nargs='+')
    s.add_argument('--ref')
    s.add_argument('--json', action='store_true')
    c = sub.add_parser('select-canaries')
    c.add_argument('regress')
    c.add_argument('out')
    l = sub.add_parser('list')
    l.add_argument('--sets', default=','.join(SCORED_SETS))
    l.add_argument('--split', choices=SPLITS, default='dev')
    a = ap.parse_args()
    if a.cmd == 'run':
        cmd_run(a)
    elif a.cmd == 'score':
        cmd_score(a)
    elif a.cmd == 'select-canaries':
        cmd_select_canaries(a)
    elif a.cmd == 'list':
        cmd_list(a)
    else:
        ap.print_help()


if __name__ == '__main__':
    main()
