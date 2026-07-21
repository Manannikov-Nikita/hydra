"""Conservative, privacy-safe facts from one literal shell command."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
import shlex


FileFact = tuple[str, str]

_SHELL_META = frozenset({"|", "||", "&", "&&", ";", "<", "<<", "<<<"})
_SHELL_PUNCTUATION = frozenset("|&;<>")
_EXPANSION_MARKERS = ("$", "`", "\n", "\r", "\0")
_PATH_META = ("*", "?", "[", "]", "{", "}")


def _tokens(command: str) -> tuple[str, ...] | None:
    if (
        "#" in command
        or "<" in command
        or "(" in command
        or ")" in command
        or any(operator in command for operator in (">|", ">&", "&>", ">!"))
        or any(marker in command for marker in _EXPANSION_MARKERS)
    ):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        values = tuple(lexer)
    except ValueError:
        return None
    if not values or any(
        value in _SHELL_META
        or (
            value not in {">", ">>"}
            and value
            and set(value) <= _SHELL_PUNCTUATION
        )
        for value in values
    ):
        return None
    return values


def _relative(
    value: str, *, project_root: Path | None, workdir: str | None,
) -> str | None:
    if (
        not value or value == "-" or value.startswith(("-", "~"))
        or any(marker in value for marker in _PATH_META + _EXPANSION_MARKERS)
    ):
        return None
    portable = value.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", portable):
        return None
    candidate = PurePosixPath(portable)
    if any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    root = None if project_root is None else PurePosixPath(
        str(project_root).replace("\\", "/")
    )
    base: PurePosixPath | None = None
    if workdir is not None:
        if (
            not workdir or workdir.startswith("~")
            or any(marker in workdir for marker in _PATH_META + _EXPANSION_MARKERS)
        ):
            return None
        portable_workdir = workdir.replace("\\", "/")
        if re.match(r"^[A-Za-z]:/", portable_workdir):
            return None
        base = PurePosixPath(portable_workdir)
        if any(part in {"", ".", ".."} for part in base.parts):
            return None
        if base.is_absolute():
            if root is None or not root.is_absolute():
                return None
            try:
                base.relative_to(root)
            except ValueError:
                return None
        else:
            if root is None or not root.is_absolute():
                return None
            base = root / base
    if not candidate.is_absolute() and base is not None:
        candidate = base / candidate
    if candidate.is_absolute():
        if root is None or not root.is_absolute():
            return None
        try:
            candidate = candidate.relative_to(root)
        except ValueError:
            return None
    if not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    return candidate.as_posix()


def _cat_operands(arguments: tuple[str, ...]) -> tuple[str, ...] | None:
    paths: list[str] = []
    options = True
    short = frozenset("benstuv")
    long = frozenset({
        "--number-nonblank", "--show-ends", "--number", "--squeeze-blank",
        "--show-tabs", "--show-nonprinting", "--show-all",
    })
    for value in arguments:
        if options and value == "--":
            options = False
        elif options and value in long:
            continue
        elif options and value.startswith("-"):
            if len(value) < 2 or any(char not in short for char in value[1:]):
                return None
        else:
            paths.append(value)
    return tuple(paths)


def _head_tail_operands(
    arguments: tuple[str, ...], *, tail: bool,
) -> tuple[str, ...] | None:
    paths: list[str] = []
    index = 0
    options = True
    booleans = frozenset("qvz" + ("fF" if tail else ""))
    value_flags = {"-n", "-c", "--lines", "--bytes"}
    if tail:
        value_flags.update({"-s", "--sleep-interval", "--pid", "--max-unchanged-stats"})
    while index < len(arguments):
        value = arguments[index]
        if options and value == "--":
            options = False
        elif options and value in value_flags:
            index += 1
            if index >= len(arguments):
                return None
        elif options and any(value.startswith(flag + "=") for flag in value_flags if flag.startswith("--")):
            pass
        elif options and re.fullmatch(r"-\d+", value):
            pass
        elif options and tail and re.fullmatch(r"\+\d+[bcl]?f?", value):
            pass
        elif options and value.startswith("-"):
            if len(value) < 2 or any(char not in booleans for char in value[1:]):
                return None
        else:
            paths.append(value)
        index += 1
    return tuple(paths)


def _sed_operands(arguments: tuple[str, ...]) -> tuple[str, ...] | None:
    paths: list[str] = []
    script_files: list[str] = []
    index = 0
    options = True
    program_supplied = False
    while index < len(arguments):
        value = arguments[index]
        if options and value == "--":
            options = False
        elif options and value in {"-i", "--in-place"} or value.startswith("--in-place="):
            return None
        elif options and value in {"-e", "--expression", "-f", "--file"}:
            is_file = value in {"-f", "--file"}
            index += 1
            if index >= len(arguments):
                return None
            if is_file:
                script_files.append(arguments[index])
            program_supplied = True
        elif options and value.startswith(("--expression=", "--file=")):
            if value.startswith("--file="):
                script_files.append(value.split("=", 1)[1])
            program_supplied = True
        elif options and value.startswith("-"):
            if len(value) < 2 or any(char not in "nErsuz" for char in value[1:]):
                return None
        elif not program_supplied:
            program_supplied = True
        else:
            paths.append(value)
        index += 1
    return tuple(script_files + paths)


def _rg_operands(arguments: tuple[str, ...]) -> tuple[str, ...] | None:
    positions: list[str] = []
    auxiliary_files: list[str] = []
    index = 0
    options = True
    files_mode = False
    pattern_supplied = False
    boolean_long = frozenset({
        "--files", "--hidden", "--no-ignore", "--follow", "--fixed-strings",
        "--ignore-case", "--smart-case", "--case-sensitive", "--line-number",
        "--no-heading", "--with-filename", "--count", "--files-with-matches",
        "--files-without-match", "--multiline", "--pcre2", "--word-regexp",
    })
    value_flags = frozenset({
        "-g", "--glob", "--iglob", "-t", "--type", "-T", "--type-not",
        "-A", "--after-context", "-B", "--before-context", "-C", "--context",
        "--encoding", "--engine", "--max-count", "--max-depth", "--sort",
        "--sortr", "--threads", "-r", "--replace", "--type-add", "--type-clear",
    })
    file_flags = frozenset({"-f", "--file", "--ignore-file"})
    while index < len(arguments):
        value = arguments[index]
        if options and value == "--":
            options = False
        elif options and value in boolean_long:
            files_mode = files_mode or value == "--files"
        elif options and value in {"-e", "--regexp"}:
            index += 1
            if index >= len(arguments):
                return None
            pattern_supplied = True
        elif options and value.startswith("--regexp="):
            pattern_supplied = True
        elif options and value in file_flags:
            index += 1
            if index >= len(arguments):
                return None
            auxiliary_files.append(arguments[index])
            if value in {"-f", "--file"}:
                pattern_supplied = True
        elif options and any(value.startswith(flag + "=") for flag in file_flags if flag.startswith("--")):
            auxiliary_files.append(value.split("=", 1)[1])
            if value.startswith("--file="):
                pattern_supplied = True
        elif options and value in value_flags:
            index += 1
            if index >= len(arguments):
                return None
        elif options and any(value.startswith(flag + "=") for flag in value_flags if flag.startswith("--")):
            pass
        elif options and value.startswith("-"):
            if len(value) < 2 or any(char not in "nHhislLcSuvwFx" for char in value[1:]):
                return None
        else:
            positions.append(value)
        index += 1
    if not files_mode and not pattern_supplied and positions:
        positions.pop(0)
    return tuple(auxiliary_files + positions)


def shell_file_facts(
    command: str, *, project_root: Path | None, workdir: str | None,
) -> tuple[FileFact, ...]:
    """Return only explicit file operands from one unambiguous direct command."""
    tokens = _tokens(command)
    if tokens is None:
        return ()
    redirections = [index for index, value in enumerate(tokens) if value in {">", ">>"}]
    if len(redirections) > 1:
        return ()
    write: str | None = None
    command_tokens = tokens
    if redirections:
        index = redirections[0]
        if index == 0 or index + 2 != len(tokens) or (index > 0 and tokens[index - 1].isdigit()):
            return ()
        write = _relative(tokens[index + 1], project_root=project_root, workdir=workdir)
        if write is None:
            return ()
        command_tokens = tokens[:index]
    if any(value in {">", ">>"} for value in command_tokens) or not command_tokens:
        return ()
    executable = command_tokens[0].rsplit("/", 1)[-1]
    arguments = command_tokens[1:]
    if executable == "cat":
        raw_reads = _cat_operands(arguments)
    elif executable in {"head", "tail"}:
        raw_reads = _head_tail_operands(arguments, tail=executable == "tail")
    elif executable == "sed":
        raw_reads = _sed_operands(arguments)
    elif executable == "rg":
        raw_reads = _rg_operands(arguments)
    elif executable in {"echo", "printf"} and write is not None:
        raw_reads = ()
    else:
        return ()
    if raw_reads is None:
        return ()
    reads: list[str] = []
    for value in raw_reads:
        relative = _relative(value, project_root=project_root, workdir=workdir)
        if relative is None:
            continue
        if relative not in reads:
            reads.append(relative)
    facts = [("read", path) for path in reads]
    if write is not None:
        facts.append(("write", write))
    return tuple(facts)
