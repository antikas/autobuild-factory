"""Deterministic reader for brief lines that order the builder to stage through git.

The builder leaves product changes uncommitted and delivery stages them itself,
so a brief that tells the builder to stage, commit or remove through git leaves
an index delivery cannot add. The rules:

- An invocation is the word ``git``, optionally followed by the global options
  ``-C <dir>``, ``-c <name=value>``, ``--git-dir=<dir>`` or ``--work-tree=<dir>``,
  then a subcommand, anywhere on the line, inside or outside a code span.
  Only invocations that give an order are refused, but the allowed ``reset``
  that lets a three-way apply pass may be an order or a report.
- ``add``, ``rm``, ``mv``, ``commit`` and ``stash`` are refused with any
  arguments. ``apply`` is refused with ``--index`` or ``--cached``, and with
  ``-3`` or ``--3way`` unless the same line holds an allowed ``reset``, whether
  that reset is ordered or narrated. ``reset`` is refused unless every argument
  is ``-q``, ``--quiet``, or ``--`` followed by paths, because that form only
  empties the index.
- Arguments run to the end of the invocation's code span, or, outside a code
  span, to the end of its clause. A clause is the text between line ends and
  ``;``, ``:``, ``!``, ``?`` and ``.`` outside code spans. A ``.`` ends a clause
  only when whitespace or the line end follows it and it does not close e.g.,
  i.e., cf., vs. or etc., so ``9.2``, ``../repo`` and ``e.g.`` stay inside one.
- A part is the text of a clause between commas outside code spans. A prose
  invocation is refused only when its part gives an order. After leading list
  markers (``-``, ``*``, ``+``, a number) and the leading connectives and, or,
  then, so, also, first, next, finally, now, please and afterwards, the part
  gives an order when its first word is an imperative verb (add, amend, apply,
  be, call, commit, delete, do, ensure, execute, finish, invoke, make, move,
  remember, remove, rename, reset, revert, run, stage, stash, start, unstage,
  use); when the clause holds, before the invocation, must, should, shall, need,
  needs, "have to" or "has to"; when the clause is a present-tense passive, that
  is it holds is, are or be before the invocation and the word directly before
  the invocation, or before its code span, is with, by, using, via or through;
  when the part holds nothing but the invocation; or when a connective was
  stripped and the invocation is the part's first token. Any other prose
  invocation, including past narration such as "were staged by" or "the patch
  applied with", is a report of what was done and passes.
- An invocation is exempt when its clause holds, before it, one of the words
  never, not, no, nor or without, or a word ending in n't. A line whose first
  non-blank character is ``>`` is a quotation and is exempt. Inside a fenced
  code block every line is one clause, every invocation counts as an order and
  no word in it exempts it.

The reader does not see an order phrased without a listed verb, modal or bare
command, such as "The builder runs `git add -A` at the end." or "When done,
`git add -A` and finish."
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ALWAYS_REFUSED = frozenset({"add", "rm", "mv", "commit", "stash"})
_APPLY_INDEX = frozenset({"--index", "--cached"})
_APPLY_THREE_WAY = frozenset({"-3", "--3way"})
_RESET_QUIET = frozenset({"-q", "--quiet"})
_OPTIONS_WITH_VALUE = frozenset({"-C", "-c"})
_ATTACHED_OPTIONS = ("--git-dir=", "--work-tree=")
_NEGATIONS = frozenset({"never", "not", "no", "nor", "without"})
_CLAUSE_ENDS = re.compile(r"[.;:!?,]")
_ABBREVIATION = re.compile(r"(?:^|[^A-Za-z.])(?:e\.g|i\.e|cf|vs|etc)$", re.IGNORECASE)
_LIST_MARKER = re.compile(r"[-*+]|\(?\d+[.)]?")
_CONNECTIVES = frozenset(
    {
        "and", "or", "then", "so", "also", "first", "next", "finally", "now", "please",
        "afterwards",
    }
)
_IMPERATIVES = frozenset(
    {
        "add", "amend", "apply", "be", "call", "commit", "delete", "do", "ensure",
        "execute", "finish", "invoke", "make", "move", "remember", "remove", "rename",
        "reset", "revert", "run", "stage", "stash", "start", "unstage", "use",
    }
)
_MODALS = frozenset({"must", "should", "shall", "need", "needs"})
_MODAL_PAIRS = frozenset({("have", "to"), ("has", "to")})
_PASSIVE_AUXILIARIES = frozenset({"is", "are", "be"})
_INSTRUMENTS = frozenset({"with", "by", "using", "via", "through"})
_WORD = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
_WORD_EDGES = re.compile(r"^[^A-Za-z]+|[^A-Za-z]+$")
_TRIM = "()[]{}\"',"
_FENCES = ("```", "~~~")


# The longest stretch of a refused brief line a message or a park quotes.
MAX_QUOTED_LINE = 160


@dataclass(frozen=True, slots=True)
class GitStagingLine:
    """One brief line that tells the builder to stage, commit or remove through git."""

    line_number: int
    invocations: tuple[str, ...]
    text: str


@dataclass(frozen=True, slots=True)
class _Token:
    text: str
    # The index of the code span holding the token, or None in prose.
    span: int | None
    # The index of the comma-separated part of the clause holding the token.
    part: int = 0


@dataclass(frozen=True, slots=True)
class _Invocation:
    subcommand: str
    arguments: tuple[str, ...]
    shown: str
    ordered: bool = True


def git_staging_lines(brief_text: str) -> tuple[GitStagingLine, ...]:
    """Every line of ``brief_text`` that instructs the builder to stage, commit
    or remove through git, in brief order."""

    found: list[GitStagingLine] = []
    fence: str | None = None
    for number, line in enumerate(brief_text.splitlines(), start=1):
        stripped = line.strip()
        marker = next((mark for mark in _FENCES if stripped.startswith(mark)), None)
        if marker is not None and (fence is None or marker == fence):
            fence = marker if fence is None else None
            continue
        if stripped.startswith(">"):
            continue
        if fence is not None:
            clauses = [[_Token(text, None) for text in line.split()]]
            in_prose = False
        else:
            clauses = _clauses(line)
            in_prose = True
        invocations = [
            invocation
            for clause in clauses
            for invocation in _invocations(clause, in_prose)
        ]
        refused = _refused(invocations)
        if refused:
            found.append(GitStagingLine(number, refused, line.strip()))
    return tuple(found)


def _clauses(line: str) -> list[list[_Token]]:
    clauses: list[list[_Token]] = [[]]
    part = 0
    span_index = 0
    segments = _segments(line)
    for position, (text, is_code) in enumerate(segments):
        if is_code:
            clauses[-1].extend(_Token(word, span_index, part) for word in text.split())
            span_index += 1
            continue
        at_line_end = position == len(segments) - 1
        start = 0
        for match in _CLAUSE_ENDS.finditer(text):
            mark = match.start()
            if match.group(0) == "." and not _ends_clause(text, mark, at_line_end):
                continue
            clauses[-1].extend(_Token(word, None, part) for word in text[start:mark].split())
            start = mark + 1
            if match.group(0) == ",":
                part += 1
            else:
                clauses.append([])
                part = 0
        clauses[-1].extend(_Token(word, None, part) for word in text[start:].split())
    return clauses


def _ends_clause(text: str, dot: int, at_line_end: bool) -> bool:
    after = dot + 1
    if after < len(text):
        if not text[after].isspace():
            return False
    elif not at_line_end:
        # A code span follows the dot directly.
        return False
    return _ABBREVIATION.search(text[:dot]) is None


def _segments(line: str) -> list[tuple[str, bool]]:
    """Split a line into prose and code-span segments. A backtick run without a
    closing run of the same length is literal text."""

    segments: list[tuple[str, bool]] = []
    prose: list[str] = []
    index = 0
    while index < len(line):
        if line[index] != "`":
            prose.append(line[index])
            index += 1
            continue
        run_end = index
        while run_end < len(line) and line[run_end] == "`":
            run_end += 1
        run = line[index:run_end]
        closing = _closing_run(line, run, run_end)
        if closing < 0:
            prose.append(run)
            index = run_end
            continue
        segments.append(("".join(prose), False))
        prose = []
        segments.append((line[run_end:closing], True))
        index = closing + len(run)
    segments.append(("".join(prose), False))
    return segments


def _closing_run(line: str, run: str, start: int) -> int:
    position = line.find(run, start)
    while position >= 0:
        after = position + len(run)
        before_ok = line[position - 1] != "`"
        after_ok = after >= len(line) or line[after] != "`"
        if before_ok and after_ok:
            return position
        position = line.find(run, after)
    return -1


def _invocations(clause: list[_Token], in_prose: bool) -> list[_Invocation]:
    found: list[_Invocation] = []
    negated = False
    for index, token in enumerate(clause):
        if token.span is None and in_prose and _is_negation(token.text):
            negated = True
            continue
        if _clean(token.text) != "git" or negated:
            continue
        rest = [
            other
            for other in clause[index + 1 :]
            if token.span is None or other.span == token.span
        ]
        invocation = _parse(rest, not in_prose or _gives_order(clause, index))
        if invocation is not None:
            found.append(invocation)
    return found


def _gives_order(clause: list[_Token], index: int) -> bool:
    git = clause[index]
    words = [_word(token) if token.span is None else "" for token in clause[:index]]
    if any(word in _MODALS for word in words) or any(
        pair in _MODAL_PAIRS for pair in zip(words, words[1:])
    ):
        return True
    if _is_present_passive(clause, index, words):
        return True
    part = [position for position, token in enumerate(clause) if token.part == git.part]
    lead = 0
    while (
        lead < len(part)
        and clause[part[lead]].span is None
        and _LIST_MARKER.fullmatch(clause[part[lead]].text)
    ):
        lead += 1
    stripped = False
    while (
        lead < len(part)
        and clause[part[lead]].span is None
        and _word(clause[part[lead]]) in _CONNECTIVES
    ):
        lead += 1
        stripped = True
    if lead >= len(part):
        return False
    first = part[lead]
    if _word(clause[first]) in _IMPERATIVES:
        return True
    if first != index:
        return False
    bare = git.span is None or all(
        clause[position].span == git.span for position in part[lead:]
    )
    return stripped or bare


def _is_present_passive(clause: list[_Token], index: int, words: list[str]) -> bool:
    git = clause[index]
    start = index
    if git.span is not None:
        while start > 0 and clause[start - 1].span == git.span:
            start -= 1
    if start == 0 or clause[start - 1].span is not None:
        return False
    if _word(clause[start - 1]) not in _INSTRUMENTS:
        return False
    return any(word in _PASSIVE_AUXILIARIES for word in words[: start - 1])


def _word(token: _Token) -> str:
    return _WORD_EDGES.sub("", token.text).lower()


def _is_negation(text: str) -> bool:
    match = _WORD.fullmatch(_WORD_EDGES.sub("", text))
    if match is None:
        return False
    lowered = match.group(0).lower().replace("’", "'")
    return lowered in _NEGATIONS or lowered.endswith("n't")


def _clean(text: str) -> str:
    return text.strip(_TRIM)


def _parse(rest: list[_Token], ordered: bool) -> _Invocation | None:
    words = [_clean(token.text) for token in rest]
    shown = ["git"]
    index = 0
    while index < len(words):
        word = words[index]
        if word in _OPTIONS_WITH_VALUE:
            if index + 1 >= len(words):
                return None
            shown.extend(words[index : index + 2])
            index += 2
            continue
        if word.startswith(_ATTACHED_OPTIONS):
            shown.append(word)
            index += 1
            continue
        break
    if index >= len(words) or not words[index]:
        return None
    arguments = tuple(word for word in words[index + 1 :] if word)
    shown.append(words[index])
    shown.extend(arguments)
    return _Invocation(words[index], arguments, " ".join(shown), ordered)


def _refused(invocations: list[_Invocation]) -> tuple[str, ...]:
    unstages = any(
        invocation.subcommand == "reset" and _index_only_reset(invocation.arguments)
        for invocation in invocations
    )
    refused: list[str] = []
    for invocation in invocations:
        if not invocation.ordered:
            continue
        subcommand, arguments = invocation.subcommand, set(invocation.arguments)
        if subcommand in _ALWAYS_REFUSED:
            refused.append(invocation.shown)
        elif subcommand == "apply":
            if arguments & _APPLY_INDEX or (arguments & _APPLY_THREE_WAY and not unstages):
                refused.append(invocation.shown)
        elif subcommand == "reset" and not _index_only_reset(invocation.arguments):
            refused.append(invocation.shown)
    return tuple(refused)


def _index_only_reset(arguments: tuple[str, ...]) -> bool:
    for argument in arguments:
        if argument == "--":
            return True
        if argument not in _RESET_QUIET:
            return False
    return True
