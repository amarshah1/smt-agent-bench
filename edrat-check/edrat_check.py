#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
r"""edrat-check: an independent checker for the eDRAT proofs that Sundance writes.

usage: edrat_check.py INPUT.smt2 PROOF.edrat [--drat-trim PATH] [--oracle z3|cvc5] [--oracle-bin PATH]
                      [--timeout SECONDS] [--lemma-timeout MS] [--keep DIR] [--json FILE] [-v]

Prints `s VERIFIED` and exits 0 if the proof shows that INPUT is unsatisfiable. Otherwise it prints
`s NOT VERIFIED` and exits 1. Exit code 2 means a usage or parse error, and 3 means the time limit ran out.

The proof format is described in README.md. Each step is checked as follows:

  (edrat-literal N T)    variable N stands for the Boolean term T; it can be defined only once
  (declare-skolem L I S Z) the I-th Skolem constant S (of sort Z) for the parent literal L; S must be fresh
  a C 0                  assumption: C must follow from the assertions of INPUT. First this tries Boolean
                         propagation over the term structure (Tseitin clauses, unit assertions). If that
                         fails, it asks an oracle whether assertions /\ ~C is unsat, with every quantified
                         subformula replaced by a fresh Boolean constant
  t TH C 0               theory lemma: C must be valid. It tries Boolean propagation, then asks the oracle
                         whether ~C is unsat (quantifiers abstracted as above)
  q -P I 0               instance: P is (forall xs. phi) and I is phi[xs := ts] (or the dual for exists)
  s L C 0                Skolemization: (exists xs. psi) -> psi[xs := sks], or (not forall) -> (not phi[sks])
  C 0                    derived clause: must be RUP. drat-trim -U checks this
  d C 0                  deletion

Every premise (a/t/q/s) that passes its check goes into a DIMACS CNF. The derived clauses and deletions go
into a DRAT file in proof order, and drat-trim checks that file in RUP-only mode (-U). RAT is not sound
for clauses whose variables have a theory meaning, so RUP-only mode is required. A premise that fails
its own check is not rejected immediately. It is moved into the DRAT file as an ordinary lemma, so it
must then be RUP. This is sound, and it covers the lemmas that Sundance re-adds after it derived them.

Trusted base: this script, drat-trim, and the oracle solver (z3 by default) on quantifier-free queries.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

TOKEN_RE = re.compile(r'\|[^|]*\||"(?:[^"]|"")*"|[()]|;[^\n]*|[^\s()|";]+')
SIMPLE_SYM = re.compile(r'^[A-Za-z~!@$%^&*_+=<>.?/\-][A-Za-z0-9~!@$%^&*_+=<>.?/\-]*$')
RESERVED = {'!', '_', 'as', 'let', 'exists', 'forall', 'match', 'par', 'lambda', 'BINARY', 'DECIMAL',
            'HEXADECIMAL', 'NUMERAL', 'STRING'}
BOOL_CONN = {'not', 'and', 'or', '=>', 'xor', '=', 'distinct', 'ite'}
BOOL_RESULT = {'not', 'and', 'or', '=>', 'xor', '=', 'distinct', '<', '<=', '>', '>=', 'is_int'}
DECL_CMDS = {'declare-sort', 'define-sort', 'declare-fun', 'declare-const', 'define-fun', 'define-fun-rec',
             'define-funs-rec', 'declare-datatypes', 'declare-datatype'}


class CheckError(Exception):
    pass


class Timeout(Exception):
    pass


def unquote(s):
    if s and s[0] == '|' and s[-1] == '|':
        inner = s[1:-1]
        if SIMPLE_SYM.match(inner) and inner not in RESERVED:
            return inner
    return s


def quote(s):
    if s[0] == '|' or (SIMPLE_SYM.match(s) and s not in RESERVED):
        return s
    return '|' + s + '|'


def sexpr_str(e):
    if isinstance(e, str):
        return e
    return '(' + ' '.join(sexpr_str(x) for x in e) + ')'


def sort_str(e):
    if isinstance(e, str):
        return unquote(e)
    return '(' + ' '.join(sort_str(x) for x in e) + ')'


def parse_sexprs(tokens, stack=None):
    """Yields the complete top-level s-expressions in TOKENS. Pass STACK again to continue over more tokens."""
    if stack is None:
        stack = [[]]
    for tok in tokens:
        if tok == '(':
            stack.append([])
        elif tok == ')':
            if len(stack) == 1:
                raise CheckError("unbalanced ')'")
            e = stack.pop()
            stack[-1].append(e)
            if len(stack) == 1:
                yield stack[-1].pop()
        elif tok[0] != ';':
            if len(stack) == 1:
                yield tok
            else:
                stack[-1].append(tok)


# ----------------------------------------------------------------------------------------------- terms
class Terms:
    """Hash-consed terms. Each node is one of ('sym', name), ('lit', text), ('app', head, args),
    or ('q', 'forall'|'exists', ((var, sort), ...), body). Annotations (!) are dropped and lets are expanded."""

    def __init__(self):
        self.table = {}
        self.nodes = []
        self.parents = {}          # child -> Boolean-connective parents (for propagation)
        self.TRUE = self.mk(('sym', 'true'))
        self.FALSE = self.mk(('sym', 'false'))
        self._sort_cache = {}
        self.sigs = {}             # function symbol -> (arg sorts, result sort)
        self.macros = {}           # define-fun name -> (params, body s-expression); expanded on sight
        self.ctors = set()         # datatype constructors; z3's `is-C` tester sugar becomes `(_ is C)`
        self.user_funs = set()     # declared or defined function symbols
        self._expanded = {}

    def mk(self, key):
        i = self.table.get(key)
        if i is None:
            i = len(self.nodes)
            self.nodes.append(key)
            self.table[key] = i
            if key[0] == 'app' and key[1] in BOOL_CONN and (key[1] not in ('=', 'distinct') or len(key[2]) == 2):
                for a in set(key[2]):
                    self.parents.setdefault(a, []).append(i)
        return i

    def conv(self, e, env=None):
        if isinstance(e, str):
            if e[0] == '|':
                e = unquote(e)
            if env:
                v = env.get(e)
                if v is not None:
                    return v
            c = e[0]
            if c.isdigit() or c == '#' or c == '"':
                return self.mk(('lit', e))
            if e in self.macros and not self.macros[e][0]:
                return self.expand(e, ())
            return self.mk(('sym', e))
        if not e:
            raise CheckError("empty term ()")
        h = e[0]
        if h == '!':
            return self.conv(e[1], env)
        if h == 'let':
            new = dict(env) if env else {}
            for b in e[1]:
                new[unquote(b[0])] = self.conv(b[1], env)
            return self.conv(e[2], new)
        if h in ('forall', 'exists'):
            vs = tuple((unquote(v[0]), sort_str(v[1])) for v in e[1])
            new = dict(env) if env else {}
            for n, _ in vs:
                new[n] = self.mk(('sym', n))      # shadows lets and macros
            return self.mk(('q', h, vs, self.conv(e[2], new)))
        if h in ('_', 'as', 'match', 'lambda'):
            return self.mk(('lit', sexpr_str(e)))
        head = unquote(h) if isinstance(h, str) else sort_str(h)
        if head.startswith('is-') and head[3:] in self.ctors and head not in self.user_funs:
            head = '(_ is %s)' % head[3:]
        args = tuple(self.conv(a, env) for a in e[1:])
        m = self.macros.get(head)
        if m is not None and len(m[0]) == len(args) and not (env and head in env):
            return self.expand(head, args)
        return self.mk(('app', head, args))

    def expand(self, name, args):
        key = (name, args)
        r = self._expanded.get(key)
        if r is None:
            params, body = self.macros[name]
            r = self.conv(body, dict(zip(params, args)))
            self._expanded[key] = r
        return r

    # -------------------------------------------------------------------------------------- sorts
    def sort_of(self, t, bound=None):
        """Best-effort sort inference; None when unknown."""
        if not bound and t in self._sort_cache:
            return self._sort_cache[t]
        k = self.nodes[t]
        s = None
        if k[0] == 'sym':
            if bound and k[1] in bound:
                s = bound[k[1]]
            elif k[1] in ('true', 'false'):
                s = 'Bool'
            else:
                sig = self.sigs.get(k[1])
                if sig is not None and not sig[0]:
                    s = sig[1]
        elif k[0] == 'lit':
            x = k[1]
            if x[0].isdigit():
                s = 'Real' if '.' in x else 'Int'
            elif x[0] == '"':
                s = 'String'
        elif k[0] == 'q':
            s = 'Bool'
        else:
            h, args = k[1], k[2]
            if h in BOOL_RESULT or h.startswith('(_ is '):
                s = 'Bool'
            elif h == 'ite' and len(args) == 3:
                s = self.sort_of(args[1], bound) or self.sort_of(args[2], bound)
            elif h in ('+', '-', '*', 'abs'):
                for a in args:
                    s = self.sort_of(a, bound)
                    if s is not None:
                        break
            elif h in ('div', 'mod', 'to_int'):
                s = 'Int'
            elif h in ('/', 'to_real'):
                s = 'Real'
            else:
                sig = self.sigs.get(h)
                if sig is not None:
                    s = sig[1]
        if not bound:
            self._sort_cache[t] = s
        return s

    # ----------------------------------------------------------------------------------- printing
    def emit(self, root, qname):
        """SMT-LIB text for ROOT. Quantified subterms are printed as qname(id), and shared subterms are let-bound."""
        nodes = self.nodes
        refs = {}
        stack = [root]
        while stack:
            t = stack.pop()
            if t in refs:
                refs[t] += 1
                continue
            refs[t] = 1
            k = nodes[t]
            if k[0] == 'app':
                stack.extend(k[2])
        memo = {}
        bindings = []
        stack = [(root, False)]
        while stack:
            t, done = stack.pop()
            if t in memo:
                continue
            k = nodes[t]
            if k[0] == 'app' and not done:
                stack.append((t, True))
                stack.extend((a, False) for a in k[2] if a not in memo)
                continue
            if k[0] == 'sym':
                s = quote(k[1])
            elif k[0] == 'lit':
                s = k[1]
            elif k[0] == 'q':
                s = qname(t)
            else:
                head = k[1] if k[1][0] == '(' else quote(k[1])
                s = '(' + head + ' ' + ' '.join(memo[a] for a in k[2]) + ')' if k[2] else head
                if refs[t] > 1 and len(s) > 32:
                    name = 'edrat!e%d' % t
                    bindings.append((name, s))
                    s = name
            memo[t] = s
        text = memo[root]
        for name, s in reversed(bindings):
            text = '(let ((%s %s)) %s)' % (name, s, text)
        return text


# ------------------------------------------------------------------------------- Boolean propagation
class Prop:
    """Three-valued propagation over the Boolean structure of hash-consed terms. Used to show that a
    clause follows from the term definitions (e.g. Tseitin clauses), optionally given a base assignment
    that is implied by the input assertions."""

    def __init__(self, terms, base=None):
        self.T = terms
        self.base = base if base is not None else {}
        self.budget = 0

    def val(self, ov, t):
        if t == self.T.TRUE:
            return True
        if t == self.T.FALSE:
            return False
        v = ov.get(t)
        if v is None:
            v = self.base.get(t)
        return v

    def assign(self, ov, q, t, v):
        cur = self.val(ov, t)
        if cur is not None:
            return cur == v
        ov[t] = v
        q.append(t)
        return True

    def _unit_or(self, ov, q, signed):
        """signed: list of (term, polarity) whose disjunction holds."""
        unknown = None
        n_unknown = 0
        for a, pol in signed:
            va = self.val(ov, a)
            if va is None:
                n_unknown += 1
                unknown = (a, pol)
            elif va == pol:
                return True
        if n_unknown == 0:
            return False
        if n_unknown == 1:
            return self.assign(ov, q, unknown[0], unknown[1])
        return True

    def down(self, ov, q, t, v):
        """Applies the definition of T, whose value is V. Returns False on conflict."""
        k = self.T.nodes[t]
        if k[0] != 'app':
            return True
        h, args = k[1], k[2]
        if h == 'not' and len(args) == 1:
            return self.assign(ov, q, args[0], not v)
        if h == 'and':
            if v:
                return all(self.assign(ov, q, a, True) for a in args)
            return self._unit_or(ov, q, [(a, False) for a in args])
        if h == 'or':
            if not v:
                return all(self.assign(ov, q, a, False) for a in args)
            return self._unit_or(ov, q, [(a, True) for a in args])
        if h == '=>' and args:
            signed = [(a, False) for a in args[:-1]] + [(args[-1], True)]
            if not v:
                return all(self.assign(ov, q, a, not p) for a, p in signed)
            return self._unit_or(ov, q, signed)
        if h in ('xor', 'distinct', '=') and len(args) == 2:
            same = (h == '=') == v      # do the two sides have the same value?
            va, vb = self.val(ov, args[0]), self.val(ov, args[1])
            if va is not None and vb is not None:
                return (va == vb) == same
            if va is not None:
                return self.assign(ov, q, args[1], va if same else not va)
            if vb is not None:
                return self.assign(ov, q, args[0], vb if same else not vb)
            return True
        if h == 'ite' and len(args) == 3:
            c, a, b = args
            vc = self.val(ov, c)
            if vc is not None:
                return self.assign(ov, q, a if vc else b, v)
            va, vb = self.val(ov, a), self.val(ov, b)
            if va is not None and va != v and not self.assign(ov, q, c, False):
                return False
            if vb is not None and vb != v and not self.assign(ov, q, c, True):
                return False
            return True
        return True

    def up(self, t, ov):
        """Value of T computed from its children, or None."""
        k = self.T.nodes[t]
        h, args = k[1], k[2]
        vals = [self.val(ov, a) for a in args]
        if h == 'not':
            return None if vals[0] is None else not vals[0]
        if h == 'and':
            if False in vals:
                return False
            return True if None not in vals else None
        if h == 'or':
            if True in vals:
                return True
            return False if None not in vals else None
        if h == '=>':
            if False in vals[:-1] or vals[-1] is True:
                return True
            if None not in vals:
                return False
            return None
        if h in ('xor', 'distinct', '=') and len(args) == 2:
            if None in vals:
                return None
            return (vals[0] == vals[1]) == (h == '=')
        if h == 'ite':
            vc = vals[0]
            if vc is not None:
                return vals[1] if vc else vals[2]
            if vals[1] is not None and vals[1] == vals[2]:
                return vals[1]
        return None

    def run(self, ov, q):
        """Propagates to a fixpoint. Returns False on conflict, True if it stalls, None if out of budget."""
        parents = self.T.parents
        while q:
            self.budget -= 1
            if self.budget < 0:
                return None
            t = q.pop()
            v = self.val(ov, t)
            if not self.down(ov, q, t, v):
                return False
            for p in parents.get(t, ()):
                vp = self.val(ov, p)
                if vp is not None:
                    if not self.down(ov, q, p, vp):
                        return False
                else:
                    u = self.up(p, ov)
                    if u is not None and not self.assign(ov, q, p, u):
                        return False
        return True

    def candidates(self, ov, limit):
        out = []
        seen = set()
        for t in list(ov):
            k = self.T.nodes[t]
            if k[0] != 'app' or k[1] not in BOOL_CONN:
                continue
            args = k[2][:1] if k[1] == 'ite' else k[2]
            if k[1] in ('=', 'distinct'):
                continue
            for a in args:
                if a not in seen and self.val(ov, a) is None:
                    seen.add(a)
                    out.append(a)
                    if len(out) >= limit:
                        return out
        return out

    def refutes(self, assignments, budget=200000, probes=8):
        """True if propagation (plus failed-literal probing) shows the assignments are contradictory."""
        self.budget = budget
        ov, q = {}, []
        for t, v in assignments:
            if not self.assign(ov, q, t, v):
                return True
        r = self.run(ov, q)
        if r is not True:
            return r is False
        progress = True
        while progress:
            progress = False
            for x in self.candidates(ov, probes):
                if self.val(ov, x) is not None:
                    continue
                results = []
                for v in (True, False):
                    ov2 = dict(ov)
                    q2 = []
                    self.assign(ov2, q2, x, v)
                    r = self.run(ov2, q2)
                    if r is None:
                        return False
                    results.append(r)
                if results[0] is False and results[1] is False:
                    return True
                for v, r in zip((True, False), results):
                    if r is False:      # x = v is refuted, so x = not v
                        self.assign(ov, q, x, not v)
                        r2 = self.run(ov, q)
                        if r2 is not True:
                            return r2 is False
                        progress = True
        return False


# --------------------------------------------------------------------------------------- checker
class Checker:
    def __init__(self, args):
        self.args = args
        self.T = Terms()
        self.decl_text = []          # input declarations, verbatim
        self.declared = set()        # symbols declared by the input
        self.assertions = []         # term ids
        self.defs = {}               # dimacs var -> term id
        self.skolems = {}            # name -> sort
        self.pending_skolems = {}    # parent literal -> {index: (name, sort)}
        self.steps = []              # (kind, tag, lits, extra)
        self.stats = {}
        self.deadline = time.time() + args.timeout if args.timeout else None
        self._qvar_dep = {}

    def bump(self, key, n=1):
        self.stats[key] = self.stats.get(key, 0) + n

    def remaining(self):
        if self.deadline is None:
            return None
        r = self.deadline - time.time()
        if r <= 0:
            raise Timeout()
        return r

    # ------------------------------------------------------------------------------------- input
    def read_input(self, path):
        with open(path, errors='replace') as f:
            text = f.read()
        scopes = [[]]
        for cmd in parse_sexprs(TOKEN_RE.findall(text)):
            if isinstance(cmd, str) or not cmd:
                continue
            h = cmd[0]
            if h in DECL_CMDS:
                self.decl_text.append(sexpr_str(cmd))
                self.record_decl(cmd)
            elif h == 'assert':
                scopes[-1].append(self.T.conv(cmd[1]))
            elif h == 'push':
                for _ in range(int(cmd[1]) if len(cmd) > 1 else 1):
                    scopes.append([])
            elif h == 'pop':
                for _ in range(int(cmd[1]) if len(cmd) > 1 else 1):
                    if len(scopes) > 1:
                        scopes.pop()
            elif h in ('check-sat', 'check-sat-assuming'):
                break
        self.assertions = [a for s in scopes for a in s]

    def record_decl(self, cmd):
        h = cmd[0]
        sigs = self.T.sigs
        if h in ('declare-fun', 'declare-const', 'define-fun', 'define-fun-rec', 'declare-sort', 'define-sort'):
            name = unquote(cmd[1])
            self.declared.add(name)
            if h not in ('declare-sort', 'define-sort'):
                self.T.user_funs.add(name)
            if h == 'declare-fun':
                sigs[name] = (tuple(sort_str(s) for s in cmd[2]), sort_str(cmd[3]))
            elif h == 'declare-const':
                sigs[name] = ((), sort_str(cmd[2]))
            elif h in ('define-fun', 'define-fun-rec'):
                sigs[name] = (tuple(sort_str(v[1]) for v in cmd[2]), sort_str(cmd[3]))
                if h == 'define-fun':
                    self.T.macros[name] = (tuple(unquote(v[0]) for v in cmd[2]), cmd[4])
        elif h == 'define-funs-rec':
            for d in cmd[1]:
                name = unquote(d[0])
                self.declared.add(name)
                sigs[name] = (tuple(sort_str(v[1]) for v in d[1]), sort_str(d[2]))
        elif h in ('declare-datatypes', 'declare-datatype'):
            if h == 'declare-datatype':
                dts = [(cmd[1], cmd[2])]
            elif cmd[1] == [] or (cmd[1] and isinstance(cmd[1][0], str)):
                dts = [(d[0], d[1:]) for d in cmd[2]]          # old z3 syntax: (declare-datatypes () ((D c..)))
            else:
                for n in cmd[1]:
                    self.declared.add(unquote(n[0]))
                dts = [(n[0], cs) for n, cs in zip(cmd[1], cmd[2]) if n[1] == '0']
            for name, ctors in dts:
                dname = unquote(name)
                self.declared.add(dname)
                if ctors and ctors[0] == 'par':
                    continue
                for c in ctors:
                    if isinstance(c, str):
                        c = [c]
                    cname = unquote(c[0])
                    self.declared.add(cname)
                    self.T.ctors.add(cname)
                    sigs[cname] = (tuple(sort_str(f[1]) for f in c[1:]), dname)
                    sigs['(_ is %s)' % cname] = ((dname,), 'Bool')
                    for f in c[1:]:
                        self.declared.add(unquote(f[0]))
                        sigs[unquote(f[0])] = ((dname,), sort_str(f[1]))

    # ------------------------------------------------------------------------------------- proof
    def read_proof(self, path):
        stack = [[]]
        with open(path, errors='replace') as f:
            for line in f:
                if len(stack) == 1 and not line.lstrip().startswith('('):
                    parts = line.split()
                    if parts:
                        self.read_clause(parts)
                    continue
                for cmd in parse_sexprs(TOKEN_RE.findall(line), stack):
                    if not isinstance(cmd, str):
                        self.read_command(cmd)
        if len(stack) != 1:
            raise CheckError("unterminated s-expression at end of proof")

    def read_clause(self, parts):
        kind = 'r'
        tag = None
        i = 0
        if parts[0] in ('a', 'q', 's', 'd'):
            kind = parts[0]
            i = 1
        elif parts[0] == 't':
            kind = 't'
            tag = parts[1]
            i = 2
        try:
            lits = [int(x) for x in parts[i:]]
        except ValueError:
            raise CheckError("bad clause line: %s" % ' '.join(parts[:8]))
        if not lits or lits[-1] != 0 or 0 in lits[:-1]:
            raise CheckError("clause line must end with a single 0: %s" % ' '.join(parts[:8]))
        lits.pop()
        extra = None
        if kind == 's':
            if not lits:
                raise CheckError("empty Skolemization step")
            for l in lits:      # declare-skolem names the parent's variable; its sign may differ
                if abs(l) in self.pending_skolems:
                    extra = self.pending_skolems.pop(abs(l))
                    break
        self.steps.append((kind, tag, lits, extra))
        self.bump('step_' + kind)

    def read_command(self, cmd):
        h = cmd[0]
        if h == 'edrat-literal':
            n = int(cmd[1])
            if n <= 0:
                raise CheckError("edrat-literal must define a positive variable")
            t = self.T.conv(cmd[2])
            old = self.defs.get(n)
            if old is not None and old != t:
                raise CheckError("variable %d is defined twice with different terms" % n)
            self.defs[n] = t
        elif h == 'declare-skolem':
            parent, idx, name, sort = int(cmd[1]), int(cmd[2]), unquote(cmd[3]), sort_str(cmd[4])
            if name in self.declared or name in self.skolems or ('sym', name) in self.T.table:
                raise CheckError("Skolem constant %s is not fresh" % name)
            self.skolems[name] = sort
            self.T.sigs[name] = ((), sort)
            self.pending_skolems.setdefault(abs(parent), {})[idx] = (name, sort)
        elif h in DECL_CMDS or h in ('set-info', 'set-option', 'set-logic', 'define-theory'):
            pass       # the proof's own declarations are not trusted; INPUT's are used
        else:
            raise CheckError("unknown proof command (%s ...)" % h)

    # -------------------------------------------------------------------------- quantifier steps
    def peel(self, t, pol):
        nodes = self.T.nodes
        while True:
            k = nodes[t]
            if k[0] == 'app' and k[1] == 'not' and len(k[2]) == 1:
                t, pol = k[2][0], not pol
            else:
                return t, pol

    def mentions(self, t, names):
        stack, seen = [t], set()
        nodes = self.T.nodes
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            k = nodes[x]
            if k[0] == 'sym' and k[1] in names:
                return True
            if k[0] == 'app':
                stack.extend(k[2])
            elif k[0] == 'q':
                stack.append(k[3])
        return False

    def var_dependent(self, body, qvars):
        """Set of subterms of BODY that mention one of QVARS (ignoring shadowing, so a superset)."""
        key = (body, qvars)
        if key in self._qvar_dep:
            return self._qvar_dep[key]
        nodes = self.T.nodes
        names = set(n for n, _ in qvars)
        dep = set()
        order, seen, stack = [], set(), [(body, False)]
        while stack:                      # post-order: children before parents
            x, done = stack.pop()
            if done:
                order.append(x)
                continue
            if x in seen:
                continue
            seen.add(x)
            stack.append((x, True))
            k = nodes[x]
            if k[0] == 'app':
                stack.extend((a, False) for a in k[2] if a not in seen)
            elif k[0] == 'q' and k[3] not in seen:
                stack.append((k[3], False))
        for x in order:
            k = nodes[x]
            if (k[0] == 'sym' and k[1] in names) or (k[0] == 'app' and any(a in dep for a in k[2])) or \
                    (k[0] == 'q' and k[3] in dep):
                dep.add(x)
        self._qvar_dep[key] = dep
        return dep

    def match(self, pat, conc, qvars, subst):
        """Does CONC equal PAT with the variables QVARS substituted (binding SUBST)?"""
        nodes = self.T.nodes
        qnames = dict(qvars)
        dep = self.var_dependent(pat, qvars)
        memo = set()
        stack = [(pat, conc, None)]
        while stack:
            p, c, inner = stack.pop()
            if inner is None:
                if p not in dep:
                    if p != c:
                        return False
                    continue
                if (p, c) in memo:
                    continue
                memo.add((p, c))
            kp = nodes[p]
            if kp[0] == 'sym':
                name = kp[1]
                if inner and name in inner:
                    if nodes[c] != ('sym', inner[name]):
                        return False
                elif name in qnames:
                    if name in subst:
                        if subst[name] != c:
                            return False
                    else:
                        if inner and self.mentions(c, set(inner.values())):
                            return False      # would capture a bound variable
                        subst[name] = c
                else:
                    if p != c or (inner and name in inner.values()):
                        return False
                continue
            kc = nodes[c]
            if kp[0] != kc[0]:
                return False
            if kp[0] == 'lit':
                if p != c:
                    return False
            elif kp[0] == 'app':
                if kp[1] != kc[1] or len(kp[2]) != len(kc[2]):
                    return False
                for a, b in zip(kp[2], kc[2]):
                    stack.append((a, b, inner))
            else:
                if kp[1] != kc[1] or len(kp[2]) != len(kc[2]):
                    return False
                new = dict(inner) if inner else {}
                for (pv, ps), (cv, cs) in zip(kp[2], kc[2]):
                    if ps != cs:
                        return False
                    new[pv] = cv
                stack.append((kp[3], kc[3], new))
        return True

    def sorts_ok(self, qvars, subst):
        for name, sort in qvars:
            t = subst.get(name)
            if t is not None:
                s = self.T.sort_of(t)
                if s is not None and s != sort:
                    return False
        return True

    def universal_view(self, lp):
        """If the literal LP being false states a universal (forall xs. phi), returns (xs, phi, phi-polarity)."""
        t = self.defs.get(abs(lp))
        if t is None:
            return None
        t, pol = self.peel(t, lp > 0)     # pol: is the (peeled) parent false when the literal is false?
        k = self.T.nodes[t]
        if k[0] != 'q':
            return None
        # forall must be true (pol False), or exists must be false (pol True)
        if k[1] == 'forall' and not pol:
            return k[2], k[3], True
        if k[1] == 'exists' and pol:
            return k[2], k[3], False
        return None

    def check_instance(self, lits):
        if len(lits) != 2:
            return False
        for lp, lc in ((lits[0], lits[1]), (lits[1], lits[0])):
            view = self.universal_view(lp)
            ct = self.defs.get(abs(lc))
            if view is None or ct is None:
                continue
            qvars, body, bpol = view
            b, bp = self.peel(body, bpol)
            c, cp = self.peel(ct, lc > 0)
            subst = {}
            if bp == cp and self.match(b, c, qvars, subst) and self.sorts_ok(qvars, subst):
                return True
        return False

    def check_skolem(self, lits, sks):
        if len(lits) != 2 or not sks:
            return False
        for lp, lc in ((lits[0], lits[1]), (lits[1], lits[0])):
            t = self.defs.get(abs(lp))
            ct = self.defs.get(abs(lc))
            if t is None or ct is None:
                continue
            t, pol = self.peel(t, lp < 0)       # pol: the parent's truth when the clause needs the child
            k = self.T.nodes[t]
            if k[0] != 'q':
                continue
            # exists true -> psi[sk]; forall false -> not phi[sk]
            if k[1] == 'exists' and pol:
                want = True
            elif k[1] == 'forall' and not pol:
                want = False
            else:
                continue
            qvars = k[2]
            if sorted(sks) != list(range(len(qvars))):
                continue
            if any(sks[i][1] != qvars[i][1] for i in range(len(qvars))):
                continue
            b, bp = self.peel(k[3], want)
            c, cp = self.peel(ct, lc > 0)
            subst = {}
            if bp != cp or not self.match(b, c, qvars, subst):
                continue
            if all(subst.get(qvars[i][0], None) in (None, self.T.mk(('sym', sks[i][0])))
                   for i in range(len(qvars))):
                return True
        return False

    # ----------------------------------------------------------------------------------- oracle
    def oracle(self, theory_qs, assume_qs):
        """Returns the set of query indices proven unsat. Queries are lists of literals; their
        negation is asserted (theory queries alone, assumption queries together with the input)."""
        if not theory_qs and not assume_qs:
            return set(), set()
        lits_used = set()
        for c in theory_qs + assume_qs:
            lits_used.update(abs(l) for l in c)
        qconsts = set()

        def qname(t):
            qconsts.add(t)
            return 'edrat!q%d' % t

        defs_text = []
        for v in sorted(lits_used):
            defs_text.append('(define-fun edrat!l%d () Bool %s)' % (v, self.T.emit(self.defs[v], qname)))
        assert_text = []
        if assume_qs:
            for a in self.assertions:
                assert_text.append('(assert %s)' % self.T.emit(a, qname))

        def clause_text(c):
            if not c:
                return 'false'
            parts = ['edrat!l%d' % l if l > 0 else '(not edrat!l%d)' % -l for l in c]
            return parts[0] if len(parts) == 1 else '(or %s)' % ' '.join(parts)

        out = []
        kind = self.args.oracle
        if kind == 'cvc5':
            out.append('(set-option :incremental true)')
            out.append('(set-logic ALL)')
            out.append('(set-option :tlimit-per %d)' % self.args.lemma_timeout)
        else:
            out.append('(set-option :timeout %d)' % self.args.lemma_timeout)
        out.extend(self.decl_text)
        for name, sort in sorted(self.skolems.items()):
            out.append('(declare-fun %s () %s)' % (quote(name), sort))
        for t in sorted(qconsts):
            out.append('(declare-fun edrat!q%d () Bool)' % t)
        out.extend(defs_text)
        for i, c in enumerate(theory_qs):
            out.append('(push 1)(assert (not %s))(echo "edrat!T%d")(check-sat)(pop 1)' % (clause_text(c), i))
        if assume_qs:
            out.extend(assert_text)
            for i, c in enumerate(assume_qs):
                out.append('(push 1)(assert (not %s))(echo "edrat!A%d")(check-sat)(pop 1)' % (clause_text(c), i))
        fd, path = tempfile.mkstemp(suffix='.smt2', dir=self.workdir)
        with os.fdopen(fd, 'w') as f:
            f.write('\n'.join(out))
            f.write('\n')
        cmd = [self.args.oracle_bin or kind, path]
        if kind == 'z3':
            cmd.insert(1, '-smt2')
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               universal_newlines=True, timeout=self.remaining())
        except subprocess.TimeoutExpired:
            raise Timeout()
        except OSError as e:
            raise CheckError("cannot run oracle %s: %s" % (cmd[0], e))
        proved_t, proved_a = set(), set()
        cur = None
        errors = 0
        for line in r.stdout.splitlines():
            line = line.strip().strip('"')
            if line.startswith('edrat!T') or line.startswith('edrat!A'):
                cur = line
            elif line.startswith('(error') or line.startswith('error'):
                errors += 1
                if self.args.verbose and errors <= 5:
                    print('c oracle: ' + line[:300])
            elif cur is not None and line in ('unsat', 'sat', 'unknown', 'timeout'):
                if line == 'unsat':
                    (proved_t if cur[6] == 'T' else proved_a).add(int(cur[7:]))
                cur = None
        if errors:
            self.bump('oracle_errors', errors)
        return proved_t, proved_a

    # ------------------------------------------------------------------------------------ check
    def check(self):
        T = self.T
        pure = Prop(T)
        base = {}
        based = Prop(T, base)
        based.budget = 10 ** 7
        q = []
        base_conflict = False
        for a in self.assertions:
            if not based.assign(base, q, a, True):
                base_conflict = True
        if not base_conflict and based.run(base, q) is False:
            base_conflict = True
        based.base = base

        def defined(c):
            return all(abs(l) in self.defs for l in c)

        def negation(c):
            return [(self.defs[abs(l)], l < 0) for l in c]

        status = [None] * len(self.steps)      # True: premise accepted; False: fall back to RUP
        theory_qs, theory_idx, assume_qs, assume_idx = [], [], [], []
        for i, (kind, tag, lits, extra) in enumerate(self.steps):
            if kind in ('r', 'd'):
                continue
            self.remaining()
            if not defined(lits):
                status[i] = False
                self.bump('undefined_literal')
                continue
            if kind == 'a':
                if base_conflict or based.refutes(negation(lits)):
                    status[i] = True
                    self.bump('a_by_propagation')
                else:
                    assume_qs.append(lits)
                    assume_idx.append(i)
            elif kind == 't':
                if pure.refutes(negation(lits)):
                    status[i] = True
                    self.bump('t_by_propagation')
                else:
                    theory_qs.append(lits)
                    theory_idx.append(i)
            elif kind == 'q':
                status[i] = self.check_instance(lits)
                self.bump('q_ok' if status[i] else 'q_failed')
            elif kind == 's':
                status[i] = self.check_skolem(lits, extra)
                self.bump('s_ok' if status[i] else 's_failed')
        if theory_qs or assume_qs:
            pt, pa = self.oracle(theory_qs, assume_qs)
            for j, i in enumerate(theory_idx):
                status[i] = j in pt
                self.bump('t_by_oracle' if status[i] else 't_failed')
            for j, i in enumerate(assume_idx):
                status[i] = j in pa
                self.bump('a_by_oracle' if status[i] else 'a_failed')
        return self.run_drat(status)

    def run_drat(self, status):
        maxvar = max([abs(l) for _, _, lits, _ in self.steps for l in lits] + [1])
        cnf_path = os.path.join(self.workdir, 'premises.cnf')
        drat_path = os.path.join(self.workdir, 'derivation.drat')
        n_premises = 0
        empty = False
        with open(drat_path, 'w') as drat:
            for i, (kind, tag, lits, extra) in enumerate(self.steps):
                if kind == 'd':
                    drat.write('d ' + ' '.join(map(str, lits)) + ' 0\n')
                elif kind == 'r' or not status[i]:
                    if kind != 'r':
                        self.bump('fallback_rup')
                    drat.write(' '.join(map(str, lits)) + ' 0\n' if lits else '0\n')
                    if not lits:
                        empty = True
                        break
                else:
                    n_premises += 1
                    if not lits:
                        self.bump('empty_premise')
        if not empty and not self.stats.get('empty_premise'):
            raise CheckError("the proof never derives the empty clause")
        with open(cnf_path, 'w') as cnf:
            cnf.write('p cnf %d %d\n' % (maxvar, n_premises))
            for i, (kind, tag, lits, extra) in enumerate(self.steps):
                if kind not in ('r', 'd') and status[i]:
                    cnf.write(' '.join(map(str, lits)) + ' 0\n' if lits else '0\n')
        if self.stats.get('empty_premise'):
            return True
        drat_trim = self.args.drat_trim or shutil.which('drat-trim')
        if not drat_trim:
            raise CheckError("drat-trim not found (use --drat-trim)")
        rem = self.remaining()
        cmd = [drat_trim, cnf_path, drat_path, '-U', '-w', '-t', str(int(rem) + 1 if rem else 20000)]
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                               timeout=(rem + 5) if rem else None)
        except subprocess.TimeoutExpired:
            raise Timeout()
        if self.args.verbose:
            for line in r.stdout.splitlines()[-12:]:
                print('c drat-trim: ' + line)
        if 's VERIFIED' in r.stdout:
            return True
        if 'TIMEOUT' in r.stdout.upper():
            raise Timeout()
        if 'trivial UNSAT' in r.stdout:
            return True
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('input')
    ap.add_argument('proof')
    ap.add_argument('--drat-trim', default=os.environ.get('DRAT_TRIM'))
    ap.add_argument('--oracle', choices=('z3', 'cvc5'), default='z3')
    ap.add_argument('--oracle-bin', default=os.environ.get('EDRAT_ORACLE'))
    ap.add_argument('--timeout', type=float, default=600, help='overall limit in seconds (0: none)')
    ap.add_argument('--lemma-timeout', type=int, default=10000, help='oracle limit per lemma in ms')
    ap.add_argument('--keep', help='write the generated CNF/DRAT/oracle files to this directory')
    ap.add_argument('--json', help='write statistics as JSON to this file')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()
    if args.timeout == 0:
        args.timeout = None

    result = {}

    def work():
        c = Checker(args)
        t0 = time.time()
        if args.keep:
            os.makedirs(args.keep, exist_ok=True)
            c.workdir = args.keep
            tmp = None
        else:
            tmp = tempfile.mkdtemp(prefix='edrat-check-')
            c.workdir = tmp
        try:
            c.read_input(args.input)
            c.read_proof(args.proof)
            ok = c.check()
            result['status'] = 'VERIFIED' if ok else 'NOT VERIFIED'
        except Timeout:
            result['status'] = 'TIMEOUT'
        except CheckError as e:
            result['status'] = 'NOT VERIFIED'
            result['reason'] = str(e)
        except OSError as e:
            result['status'] = 'ERROR'
            result['reason'] = str(e)
        except (RecursionError, MemoryError) as e:
            result['status'] = 'ERROR'
            result['reason'] = type(e).__name__
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        result['stats'] = c.stats
        result['seconds'] = round(time.time() - t0, 3)

    sys.setrecursionlimit(1000000)
    threading.stack_size(512 * 1024 * 1024)
    th = threading.Thread(target=work)
    th.start()
    th.join()
    st = result.get('stats', {})
    print('c ' + ' '.join('%s=%s' % kv for kv in sorted(st.items())))
    if 'reason' in result:
        print('c reason: ' + result['reason'])
    print('c time: %.2fs' % result.get('seconds', 0))
    print('s ' + result.get('status', 'ERROR'))
    if args.json:
        with open(args.json, 'w') as f:
            json.dump(result, f)
    sys.exit({'VERIFIED': 0, 'NOT VERIFIED': 1, 'TIMEOUT': 3}.get(result.get('status'), 2))


if __name__ == '__main__':
    main()
