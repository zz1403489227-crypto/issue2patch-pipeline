"""Node 2 (deterministic): find candidate files from names and symbols in the issue."""
from __future__ import annotations

import re
import shlex
from collections import defaultdict
from pathlib import PurePosixPath

from ..contracts import Candidate, PrefetchOut
from ..testing import is_test_path
from .context import TaskContext

PATH_RE = re.compile(r"(?<![\w.])((?:[\w-]+/)*[\w-]+\.py)\b")
DOTTED_RE = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\b")
BACKTICK_RE = re.compile(r"`([^`\n]{2,80})`")
CAMEL_RE = re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+)\b")
CALL_RE = re.compile(r"\b([a-z_][a-z0-9_]{2,})\(")
IDENT_RE = re.compile(r"[A-Za-z_]\w{2,}")
STOP = set("""self cls None True False print list dict str int float bool set tuple len range type object
return import from class def lambda yield assert raise except finally while with else elif pass break continue
the and for not this that error Error Exception TypeError ValueError KeyError AttributeError IndexError
python Python django pytest numpy test tests example foo bar baz args kwargs value values data result
""".split())


UPPER_RE = re.compile(r"\b([A-Z][A-Z0-9]*_[A-Z0-9_]+)\b")        # constants and settings: SECURE_REFERRER_POLICY
SNAKE_RE = re.compile(r"\b([a-z][a-z0-9]*_[a-z0-9_]+)\b")         # snake_case identifiers: values_list
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")
WORD_STOP = STOP | set("""when with that this have from into does doesn should would could there their they then than
also only some like just used using uses because about which while where what after before other these those
issue description problem example following expected actual behavior behaviour error errors work works working
case cases instead currently version file files line lines code returns return value values default""".split())


def extract(issue: str) -> tuple[list[str], list[str], list[str]]:
    paths = sorted(set(PATH_RE.findall(issue)))
    dotted = sorted({d for d in DOTTED_RE.findall(issue) if not d.endswith(".py") and d.count(".") <= 6})
    score: dict[str, float] = defaultdict(float)
    for chunk in BACKTICK_RE.findall(issue):
        for ident in IDENT_RE.findall(chunk):
            score[ident] += 2
    for ident in CAMEL_RE.findall(issue):
        score[ident] += 1.5
    for ident in UPPER_RE.findall(issue):
        score[ident] += 1.5
    for ident in CALL_RE.findall(issue):
        score[ident] += 1
    for ident in SNAKE_RE.findall(issue):
        score[ident] += 0.5
    for d in dotted:
        score[d.split(".")[-1]] += 1
    # dunder names (__init__, __str__) are defined everywhere and say nothing about where the bug is
    symbols = [s for s, _ in sorted(score.items(), key=lambda kv: -kv[1])
               if s not in STOP and len(s) > 2 and not (s.startswith("__") and s.endswith("__"))]
    return paths, dotted, symbols[:15]


def _path_tokens(f: str) -> set[str]:
    parts = PurePosixPath(f).with_suffix("").parts
    toks = set()
    for p in parts:
        toks.add(p.lower())
        toks.update(t.lower() for t in p.split("_") if len(t) > 2)
    return toks


def rank(sb, issue: str, files: list[str]):
    """Full ranking of candidate files for an issue. Returns (source, tests, symbols, paths).

    v2 (closed-loop case 2): every signal is weighted by how specific it is, the way term weighting
    works in retrieval: a symbol defined in one file counts fully, one defined in thirty files hardly at
    all, and each (file, symbol) pair counts once; a mentioned path that matches dozens of files
    (`__init__.py`) is ignored; paths mentioned with a prefix (`site-packages/pkg/mod.py`) match by
    suffix; words of the issue that name directories or modules (`postgresql`, `client`, `runserver`)
    add a little, more when several meet in one path.
    """
    import math

    file_set = set(files)
    n_files = max(len(files), 1)
    paths, dotted, symbols = extract(issue)
    scores: dict[str, float] = defaultdict(float)
    reasons: dict[str, list[str]] = defaultdict(list)

    def add(f: str, s: float, why: str) -> None:
        scores[f] += s
        if why not in reasons[f]:
            reasons[f].append(why)

    for p in paths:
        hits = [f for f in files if f == p or f.endswith("/" + p) or p.endswith("/" + f)]
        if 0 < len(hits) <= 5:
            for f in hits:
                add(f, 5, f"path {p} named in issue")
    for d in dotted:
        rel = d.replace(".", "/")
        for cand in (rel + ".py", rel + "/__init__.py"):
            if cand in file_set:
                add(cand, 4, f"module {d} named in issue")
        parent = "/".join(rel.split("/")[:-1])
        if parent and parent + ".py" in file_set:
            add(parent + ".py", 2, f"parent module of {d}")
    if symbols:
        alt = "|".join(re.escape(s) for s in symbols)
        pattern = r"^\s*((async\s+)?(def|class)\s+(" + alt + r")\b|(" + alt + r")\s*=)"
        defs: dict[str, set[str]] = defaultdict(set)
        for line in sb.git(f"grep -n -E {shlex.quote(pattern)} -- '*.py'").output.splitlines():
            f, _, rest = line.partition(":")
            m = re.search(r"(?:def|class)\s+(\w+)", rest) or re.match(r"\d+:\s*(\w+)\s*=", rest)
            if f in file_set and m and m.group(1) in symbols:
                defs[m.group(1)].add(f)
        for sym, fs in defs.items():
            w = 3.0 * min(1.0, 3.0 / len(fs))
            for f in fs:
                add(f, w, f"defines {sym}")
        for sym in symbols[:8]:
            hits = sb.git(f"grep -l -w -F {shlex.quote(sym)} -- '*.py'").output.splitlines()
            if 0 < len(hits) <= 40:
                for f in hits:
                    add(f, 0.5, f"mentions {sym}")
    words = {w.lower() for w in WORD_RE.findall(issue) if len(w) > 3} - {w.lower() for w in WORD_STOP}
    if words:
        tok = {f: _path_tokens(f) for f in files}
        df: dict[str, int] = defaultdict(int)
        for ts in tok.values():
            for t in ts & words:
                df[t] += 1
        for f, ts in tok.items():
            matched = [t for t in ts & words if df[t] <= 50]
            if matched:
                idf = sum(math.log(n_files / df[t]) for t in matched) / math.log(n_files)
                add(f, round(1.5 * idf * (1.5 if len(matched) > 1 else 1.0), 2),
                    "path words " + ",".join(sorted(matched)))
    ranked = sorted(scores, key=lambda f: (-scores[f], len(PurePosixPath(f).parts), f))
    src = [Candidate(path=f, score=round(scores[f], 2), reasons=reasons[f]) for f in ranked if not is_test_path(f)]
    tst = [Candidate(path=f, score=round(scores[f], 2), reasons=reasons[f]) for f in ranked if is_test_path(f)]
    return src, tst, symbols, paths


def run(ctx: TaskContext):
    sb = ctx.sb
    files = [f for f in sb.git("ls-files").output.splitlines() if f.endswith(".py")]
    ctx.cache["py_files"] = files
    src, tst, symbols, paths = rank(sb, ctx.task.issue, files)
    out = PrefetchOut(source_candidates=src[:10], test_candidates=tst[:5], symbols=symbols, mentioned_paths=paths)
    return out, {"passed": bool(out.source_candidates), "n_source": len(src), "n_test": len(tst)}
