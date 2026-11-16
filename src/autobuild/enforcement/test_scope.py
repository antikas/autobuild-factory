"""The strict-process test rule: a builder seat runs only the covering tests.

The profile's test map pairs source path patterns with the tests that cover
them. An item's covering tests are the entries its declared paths match. In
strict mode a builder test run is admitted only when every test it selects is
one of those covering tests; a run that selects none, the whole suite, is
refused with one sentence naming the covering tests. Flow mode, reviewer and
specialist seats, and commands that are not test runs are never judged here.
The validator the coordinator runs is not a builder seat run and is not judged.

A strict builder seat carries a ``TestGuard``; a harness that can run a check
before each shell command passes the command line to ``shell_command_refusal``.
"""

from __future__ import annotations

import re
from functools import lru_cache

from autobuild.domain import ItemExecutionSpec, Seat
from autobuild.domain.models import ProcessPolicy, TestCoverage, TestGuard
from autobuild.enforcement.triage import declared_path_tokens


@lru_cache(maxsize=256)
def _pattern_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(parts))


def _normal(path: str) -> str:
    """``path`` with forward slashes and its ``.`` and ``..`` segments resolved,
    so ``tests/unit/../..`` is ``.``; a ``..`` above the start is kept."""

    text = path.strip().replace("\\", "/")
    if not text:
        return ""
    absolute = text.startswith("/")
    parts: list[str] = []
    for segment in text.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            elif not absolute:
                parts.append("..")
            continue
        parts.append(segment)
    joined = "/".join(parts)
    return f"/{joined}" if absolute else joined or "."


def path_matches(pattern: str, path: str) -> bool:
    """Whether ``path`` matches the test-map ``pattern``: ``**`` spans
    directories, ``*`` and ``?`` stay inside one path segment."""

    return _pattern_regex(_normal(pattern)).fullmatch(_normal(path)) is not None


def covering_entries(policy: ProcessPolicy, paths: tuple[str, ...]) -> tuple[TestCoverage, ...]:
    """The test-map entries that cover any of ``paths``, in map order."""

    return tuple(
        entry
        for entry in policy.test_map
        if any(path_matches(entry.pattern, path) for path in paths)
    )


def item_covering_entries(policy: ProcessPolicy, brief_text: str) -> tuple[TestCoverage, ...]:
    """The covering tests for the paths an item's brief declares."""

    return covering_entries(policy, declared_path_tokens(brief_text))


def covering_label(entries: tuple[TestCoverage, ...]) -> str:
    return "; ".join(entry.label() for entry in entries)


def _starts_with(argv: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return bool(prefix) and argv[: len(prefix)] == prefix


_PYTHON = re.compile(r"python(?:\d+(?:\.\d+)*)?")
# Interpreter options that read the next word as their value.
_PYTHON_VALUE_FLAGS = frozenset({"-X", "-W", "-Q"})
# ``uv`` and ``uv run`` options that read the next word as their value.
_UV_RUN_VALUE_FLAGS = frozenset(
    {
        "--with", "--with-editable", "--with-requirements", "--python", "-p", "--project",
        "--directory", "--extra", "--group", "--only-group", "--no-group", "--package",
        "--env-file", "--index", "--index-url", "--default-index", "--extra-index-url",
        "--find-links", "-f", "--config-file", "--cache-dir", "--color", "--script",
    }
)
# The runner options that read the next word as their value, by runner name. A
# runner not named here reads a value only in the ``--flag=value`` form.
_RUNNER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "pytest": frozenset(
        {
            "-k", "-m", "-p", "-c", "-o", "-r", "-W", "-n", "--maxfail", "--rootdir",
            "--confcutdir", "--basetemp", "--deselect", "--ignore", "--ignore-glob",
            "--junitxml", "--junit-xml", "--junit-prefix", "--log-level", "--log-cli-level",
            "--log-file", "--log-file-level", "--log-format", "--tb", "--durations",
            "--durations-min", "--override-ini", "--config-file", "--capture", "--import-mode",
            "--show-capture", "--color", "--code-highlight", "--lfnf",
            "--last-failed-no-failures", "--cov", "--cov-report", "--cov-config",
            "--cov-fail-under", "--timeout", "--numprocesses", "--dist", "--maxprocesses",
            "--reruns", "--random-order-seed",
        }
    ),
}


def _basename(word: str) -> str:
    return _normal(word).rsplit("/", 1)[-1]


def _program(argv: tuple[str, ...], start: int = 0) -> tuple[int, str] | None:
    """Where the program a command line runs sits, past the ``uv run`` and
    ``python -m`` launchers, and its name: ``(index, name)``. ``index`` is the
    last word that names it; the program's own arguments follow. None when the
    line runs inline code or a script through the interpreter."""

    if start >= len(argv):
        return None
    head = _basename(argv[start])
    subcommand = start + 1
    if head == "uv":
        # uv's own options, such as ``-q`` or ``--directory <dir>``, may come
        # before the ``run`` subcommand.
        while subcommand < len(argv) and argv[subcommand].startswith("-") and argv[subcommand] != "--":
            subcommand += 2 if argv[subcommand] in _UV_RUN_VALUE_FLAGS else 1
    if head == "uv" and argv[subcommand : subcommand + 1] == ("run",):
        index = subcommand + 1
        while index < len(argv) and argv[index].startswith("-"):
            option = argv[index]
            if option == "--":
                index += 1
                break
            if option in {"-m", "--module"}:
                return (index + 1, argv[index + 1]) if index + 1 < len(argv) else None
            index += 2 if option in _UV_RUN_VALUE_FLAGS else 1
        return _program(argv, index)
    if _PYTHON.fullmatch(head):
        index = start + 1
        while index < len(argv) and argv[index].startswith("-"):
            option = argv[index]
            if option == "-m":
                return (index + 1, argv[index + 1]) if index + 1 < len(argv) else None
            if option.startswith("-m"):
                return index, option[2:]
            if option.startswith("-c"):
                return None
            index += 2 if option in _PYTHON_VALUE_FLAGS else 1
        return None
    return start, head


def _runner(test_command: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
    """The runner the configured test command starts: its program name and the
    words that follow it up to the first option, such as ``("pytest", ())`` for
    ``uv run pytest -q`` or ``("npm", ("test",))`` for ``npm test``. Options in
    the configured command are its own settings, not part of the runner."""

    found = _program(test_command)
    if found is None:
        return None
    index, name = found
    rest: list[str] = []
    for word in test_command[index + 1 :]:
        if word.startswith("-"):
            break
        rest.append(word)
    return name, tuple(rest)


def runner_arguments(policy: ProcessPolicy, argv: tuple[str, ...]) -> tuple[str, ...] | None:
    """The arguments ``argv`` passes to the project's test runner, or None when
    it does not run the runner. The configured ``test_command`` and every
    spelling of the same runner count: the bare program, a path ending in it,
    ``python -m`` and ``uv run`` in front of either."""

    return _command_arguments(policy.test_command, argv)


def _command_arguments(test_command: tuple[str, ...], argv: tuple[str, ...]) -> tuple[str, ...] | None:
    """The arguments ``argv`` passes to the runner ``test_command`` starts, or
    None when ``argv`` does not run that runner."""

    if _starts_with(argv, test_command):
        return argv[len(test_command) :]
    runner = _runner(test_command) if test_command else None
    found = _program(argv)
    if runner is None or found is None:
        return None
    name, rest = runner
    index, program = found
    if program != name or argv[index + 1 : index + 1 + len(rest)] != rest:
        return None
    return argv[index + 1 + len(rest) :]


def _takes_value(option: str, value_flags: frozenset[str]) -> bool:
    """Whether ``option`` reads the next word as its value."""

    if "=" in option:
        return False
    if option.startswith("--") or option in value_flags:
        return option in value_flags
    if option[:2] in value_flags:
        return False
    return option[1:].isalpha() and f"-{option[-1]}" in value_flags


def _test_roots(policy: ProcessPolicy) -> frozenset[str]:
    """Every mapped test path and each directory above one, plus the working
    directory and its parent: the bare names that select tests."""

    roots = {".", ".."}
    for entry in policy.test_map:
        for path in entry.tests:
            parts = _normal(path).split("/")
            roots.update("/".join(parts[:end]) for end in range(1, len(parts) + 1))
    return frozenset(roots)


def _selected_tests(
    arguments: tuple[str, ...], roots: frozenset[str], value_flags: frozenset[str]
) -> tuple[str, ...]:
    """The test targets among a runner's arguments.

    A path-like token, a mapped test path, a directory above one (such as a
    bare ``tests``) and ``.`` always select tests. Any other bare token selects
    tests too unless it is the value of an option in ``value_flags``, such as
    the keyword expression after ``-k``. After ``--`` every word is a target."""

    selected: list[str] = []
    expects_value = False
    options_done = False
    for argument in arguments:
        if not options_done and argument == "--":
            options_done = True
            expects_value = False
            continue
        if not options_done and argument.startswith("-") and argument != "-":
            expects_value = _takes_value(argument, value_flags)
            continue
        normal = _normal(argument) or "."
        if "/" in normal or "::" in normal or normal in roots or not expects_value:
            selected.append(normal)
        expects_value = False
    return tuple(selected)


def _within(target: str, test_path: str) -> bool:
    file_part = target.split("::", 1)[0].rstrip("/")
    root = _normal(test_path)
    return file_part == root or file_part.startswith(f"{root}/")


# Shell reserved words that may open a simple command, as in ``{ pytest; }``,
# ``if true; then pytest; fi`` or ``! pytest``.
_LEADING_RESERVED = frozenset({"!", "{", "if", "then", "else", "elif", "do", "while", "until"})
# Leading words that run the command after them: for each, the options that
# read the next word as their value and the operands before the command, such
# as the duration of ``timeout 600 pytest``.
_WRAPPERS: dict[str, tuple[frozenset[str], int]] = {
    "env": (frozenset({"-u", "--unset", "-C", "--chdir"}), 0),
    "time": (frozenset({"-f", "--format", "-o", "--output"}), 0),
    "timeout": (frozenset({"-s", "--signal", "-k", "--kill-after"}), 1),
    "nice": (frozenset({"-n", "--adjustment"}), 0),
    "nohup": (frozenset(), 0),
    "command": (frozenset(), 0),
    "exec": (frozenset({"-a"}), 0),
}
# Shells whose ``-c`` option runs a command string.
_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ksh"})
_SHELL_VALUE_FLAGS = frozenset({"-o", "+o", "-O", "+O"})
_SHELL_DEPTH = 4


def _shell_string(argv: tuple[str, ...]) -> str | None:
    """The command string ``bash -c``, ``sh -c`` or ``bash -lc`` runs, or None
    when ``argv`` is not such a shell call."""

    if _basename(argv[0]) not in _SHELLS:
        return None
    runs_string = False
    index = 1
    while index < len(argv):
        option = argv[index]
        if option == "--":
            index += 1
            break
        if not option or option[0] not in "-+" or option in {"-", "+"}:
            break
        if option in _SHELL_VALUE_FLAGS:
            index += 2
            continue
        if not option.startswith("--") and "c" in option[1:]:
            runs_string = True
        index += 1
    return argv[index] if runs_string and index < len(argv) else None


def _commands_run(argv: tuple[str, ...], depth: int = 0) -> tuple[tuple[str, ...], ...]:
    """The simple commands ``argv`` runs: past leading reserved words and the
    wrappers in ``_WRAPPERS`` with their options, assignments and operands,
    and, for a shell ``-c`` call, the commands in its command string. A
    ``command -v`` lookup runs nothing."""

    index = 0
    while index < len(argv):
        word = argv[index]
        if word in _LEADING_RESERVED:
            index += 1
            continue
        name = _basename(word)
        if name not in _WRAPPERS:
            break
        value_flags, operands = _WRAPPERS[name]
        index += 1
        while index < len(argv):
            option = argv[index]
            if name == "env" and (option == "-" or _ASSIGNMENT.match(option)):
                index += 1
                continue
            if option == "--":
                index += 1
                break
            if not option.startswith("-"):
                break
            if name == "command" and option in {"-v", "-V"}:
                return ()
            index += 2 if option in value_flags else 1
        while name == "env" and index < len(argv) and _ASSIGNMENT.match(argv[index]):
            index += 1
        index += operands
    inner = argv[index:]
    if not inner:
        return ()
    string = _shell_string(inner)
    if string is None:
        return (inner,)
    if depth >= _SHELL_DEPTH:
        return ()
    try:
        lines = shell_commands(string)
    except ValueError:
        return ()
    return tuple(command for line in lines for command in _commands_run(line, depth + 1))


def builder_test_refusal(
    policy: ProcessPolicy,
    seat: Seat,
    argv: tuple[str, ...],
    covering: tuple[TestCoverage, ...],
) -> str | None:
    """The refusal sentence for a builder seat's test run, or None to admit it.

    ``covering`` is the item's covering entries; when the item declares no
    mapped path every map entry counts as covering. ``argv`` is judged by the
    commands it runs: past leading wrapper words such as ``env``, ``timeout``
    and ``time``, and inside a ``bash -c`` or ``sh -c`` command string."""

    if not policy.strict or seat is not Seat.BUILDER or not argv:
        return None
    for command in _commands_run(argv):
        refusal = _command_refusal(policy, command, covering)
        if refusal is not None:
            return refusal
    return None


def _command_refusal(
    policy: ProcessPolicy, argv: tuple[str, ...], covering: tuple[TestCoverage, ...]
) -> str | None:
    """The refusal sentence for one simple command a strict builder runs.

    A test-path entry's runs are judged by the paths they select. A mapped
    command line is admitted only exactly as written: any other run of the
    same runner, such as ``npm test -- api`` beside ``npm test -- web``, or a
    covering line of an entry that does not cover the change, is refused."""

    entries = covering or policy.test_map
    if any(entry.command and argv == entry.command for entry in entries):
        return None
    arguments = runner_arguments(policy, argv)
    runs_mapped_command = any(
        entry.command and _command_arguments(entry.command, argv) is not None
        for entry in policy.test_map
    )
    if arguments is None and not runs_mapped_command:
        return None
    allowed = tuple(path for entry in entries for path in entry.tests)
    runner = _runner(policy.test_command)
    value_flags = _RUNNER_VALUE_FLAGS.get(runner[0], frozenset()) if runner else frozenset()
    targets = (
        _selected_tests(arguments, _test_roots(policy), value_flags)
        if arguments is not None
        else ()
    )
    if targets and all(any(_within(target, path) for path in allowed) for target in targets):
        return None
    return (
        "A builder seat may not run the whole test suite or tests outside its change; "
        f"run only the covering tests: {covering_label(entries)}."
    )


def seat_test_guard(spec: ItemExecutionSpec, seat: Seat) -> TestGuard | None:
    """The pre-command check ``seat`` carries: a guard for a builder seat in
    strict mode, None for flow mode and for reviewer and specialist seats."""

    if not spec.process.strict or seat is not Seat.BUILDER:
        return None
    return TestGuard(spec.process, item_covering_entries(spec.process, spec.brief_text))


_OPERATOR_CHARS = frozenset("();<>|&\n")
# The shell's operators, longest first, so a run of operator characters such
# as ``;>`` reads as the list operator ``;`` and then the redirection ``>``.
_OPERATOR = re.compile(r"&>>|&>|>>|>&|<&|>\||<>|<<<|<<|>|<|&&|\|\||\|&|;;|;|\||&|\(|\)|\n")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# Characters a backslash escapes inside double quotes; before any other
# character the backslash stays.
_DOUBLE_QUOTE_ESCAPES = frozenset('"\\$`\n')


def _shell_tokens(line: str) -> list[tuple[str, bool, bool]]:
    """Split a shell line into ``(text, is_operator, quoted)`` tokens.

    An operator is one shell operator made of unquoted ``();<>|&`` and
    new-line characters, so ``;>`` is two operators; anything quoted or
    escaped belongs to a word. ``quoted`` marks a word that
    starts with a quoted or escaped part, which is never an assignment or a
    file-descriptor number. Raises ValueError for an unclosed quote."""

    tokens: list[tuple[str, bool, bool]] = []
    word: list[str] = []
    in_word = False
    quoted = False

    def end_word() -> None:
        nonlocal in_word, quoted
        if in_word:
            tokens.append(("".join(word), False, quoted))
        word.clear()
        in_word = False
        quoted = False

    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\":
            if index + 1 < len(line) and line[index + 1] == "\n":
                index += 2
                continue
            if index + 1 < len(line):
                word.append(line[index + 1])
            quoted = quoted or not in_word
            in_word = True
            index += 2
        elif char == "'":
            closing = line.find("'", index + 1)
            if closing < 0:
                raise ValueError("No closing quotation")
            word.append(line[index + 1 : closing])
            quoted = quoted or not in_word
            in_word = True
            index = closing + 1
        elif char == '"':
            index += 1
            while True:
                if index >= len(line):
                    raise ValueError("No closing quotation")
                inner = line[index]
                if inner == '"':
                    break
                if inner == "\\" and index + 1 < len(line) and line[index + 1] in _DOUBLE_QUOTE_ESCAPES:
                    if line[index + 1] != "\n":
                        word.append(line[index + 1])
                    index += 2
                    continue
                word.append(inner)
                index += 1
            quoted = quoted or not in_word
            in_word = True
            index += 1
        elif char in " \t\r":
            end_word()
            index += 1
        elif char in _OPERATOR_CHARS:
            end_word()
            operator = _OPERATOR.match(line, index)
            assert operator is not None  # every operator character starts one
            tokens.append((operator.group(), True, False))
            index = operator.end()
        else:
            word.append(char)
            in_word = True
            index += 1
    end_word()
    return tokens


def shell_commands(line: str) -> tuple[tuple[str, ...], ...]:
    """The simple commands in a shell command line, each as its argument list.

    Lists, pipes and subshells split commands. An unquoted ``<`` or ``>``
    operator is a redirection: it, its target and a file-descriptor number
    written straight before it are dropped; a quoted or escaped ``<`` or ``>``
    is part of an argument. Leading variable assignments are dropped. Raises
    ValueError for a line the shell could not parse either, such as an
    unclosed quote."""

    tokens = _shell_tokens(line)
    commands: list[tuple[str, ...]] = []
    current: list[str] = []
    index = 0
    while index < len(tokens):
        text, is_operator, quoted = tokens[index]
        following = tokens[index + 1] if index + 1 < len(tokens) else ("", False, False)
        if is_operator and ("<" in text or ">" in text):
            index += 2 if not following[1] else 1
            continue
        redirects_next = following[1] and following[0][:1] in {"<", ">"}
        if not is_operator and not quoted and text.isdigit() and redirects_next:
            index += 1
            continue
        if is_operator:
            if current:
                commands.append(tuple(current))
            current = []
        elif current or quoted or not _ASSIGNMENT.match(text):
            current.append(text)
        index += 1
    if current:
        commands.append(tuple(current))
    return tuple(commands)


def shell_command_refusal(guard: TestGuard, line: str) -> str | None:
    """The refusal sentence for a strict builder seat's shell command line, or
    None to admit it. A line that does not parse runs no test and is admitted;
    the shell refuses it itself."""

    try:
        commands = shell_commands(line)
    except ValueError:
        return None
    for argv in commands:
        refusal = builder_test_refusal(guard.policy, Seat.BUILDER, argv, guard.covering)
        if refusal is not None:
            return refusal
    return None
