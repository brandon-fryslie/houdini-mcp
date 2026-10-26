"""Code execution tools.

This module provides tools for executing Python code in Houdini
with safety rails, policy profiles, timeout handling, rollback-on-timeout,
and scene diff tracking.

Safety model (bead houdini-mcp-9e2, houdini-laws-etg.pic)
-----------------------------------------------------------
* Every run holds one :class:`ExecutionGrant`: what this run may do, resolved
  once from the policy, the request's bypass flags and the server
  configuration. Every gate reads the grant; nothing re-derives permission.
* Policy profiles:
    - ``read-only``  : inspection only. The program may name only what the
                       inspection allowlist in ``code_policy`` recognizes; a
                       mutator the allowlist has never heard of is refused the
                       same way as one it has, and the bypass flags grant
                       nothing.
    - ``normal``     : default; recognized dangerous operations and
                       heavy-geometry calls are blocked unless the caller opts
                       in *and* the server is configured to permit bypass.
  Both are guard rails against accidents, not a sandbox (``code_policy``).
* Every gate reads the parsed program, the tree the program ``exec`` runs was
  compiled from, so text in comments and strings is not code and a program that
  does not compile is refused before any rail.
* Each bypass flag unlocks exactly its own capability: ``allow_dangerous``
  the dangerous patterns, ``allow_heavy_geometry`` the heavy-geometry
  patterns. Both require server configuration (``HOUDINI_MCP_ALLOW_BYPASS``).
  Missing either fails closed.
* Every call returns a structured ``audit`` block, including the resolved
  grant, and is logged.
* The program runs on Houdini's main thread as one undo step labelled as the
  call (:class:`~houdini_mcp.tools.transactions.UndoStep`), undone if it
  raises. At its timeout it is cut off from Houdini: its link is shut down, so
  Houdini's main thread gives it up once the request it is serving returns,
  and its step is undone on a fresh link. Python cannot kill the worker
  thread, so it stays recorded in :data:`~houdini_mcp.tools.workers.WORKERS`
  until it ends, and every scene-writing tool is refused, naming it, for as
  long as it is.
"""

import builtins
import hashlib
import io
import logging
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from types import CodeType
from typing import Any, Literal, TypedDict

from ..connection import houdini, raised_inside_houdini
from ._common import (
    PROGRAM,
    READ,
    Count,
    InvalidInput,
    OperationFailed,
    ProgramStillRunning,
    Seconds,
    tool,
    using,
)
from .code_policy import (
    _detect_dangerous_code,
    _detect_heavy_geometry_code,
    _detect_import_hou,
    _detect_non_inspection_code,
    compile_program,
    parse_program,
)
from .failure import LinkLost, add_note, failed_on, failure_for, link_lost
from .program_output import Capture, captured
from .reads import reason
from .snapshot import CappedDiff, SceneSnapshot, capped, scene_diff, scene_snapshot
from .transactions import BODY_SILENCE, MainThread
from .workers import HOW_A_WORKER_ENDS, WORKERS

logger = logging.getLogger("houdini_mcp.tools.code")

# Re-exported so tests can monkeypatch a builtin-free reference. ``exec`` is a
# builtin; patching a module-level name lets us prove blocked code never runs.
exec = builtins.exec  # noqa: A001 - intentional shadow for testability

_DIFF_ROOT = "/obj"


class LastSceneDiff:
    """What the most recently launched ``capture_diff`` run changed.

    [LAW:no-shared-mutable-globals] The one owner of get_last_scene_diff's
    answer: the diff that run returned, capped as it returned it, or None while
    that run has produced none. Only a worker writes it, from its ledger slot,
    so writes arrive in launch order: no run can launch until the one before it
    has written its last.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._diff: CappedDiff | None = None

    def record(self, diff: CappedDiff | None) -> None:
        with self._lock:
            self._diff = diff

    def diff(self) -> CappedDiff | None:
        with self._lock:
            return self._diff


LAST_SCENE_DIFF = LastSceneDiff()

# Policy profiles, from strictest to most permissive.
Policy = Literal["read-only", "normal"]
DEFAULT_POLICY: Policy = "normal"

# What a bypass capability can still lack: the "normal" policy, the request flag,
# the server config.
Requirement = Literal["policy", "request", "config"]

# [LAW:one-source-of-truth] The execution limits, stated once for the public
# tool and for every server-side entry.
DEFAULT_MAX_STDOUT_SIZE = 100000
DEFAULT_MAX_STDERR_SIZE = 100000
DEFAULT_MAX_DIFF_NODES = 1000
DEFAULT_TIMEOUT = 30


@dataclass(frozen=True)
class ExecutionGrant:
    """What one execute_code run is permitted to do.

    [LAW:parse-dont-validate] Resolved once from the policy, the request's
    bypass flags and the server configuration; every gate below reads a
    capability off this value instead of recombining the inputs.
    """

    policy: str
    bypass_config_enabled: bool
    dangerous_requested: bool
    heavy_geometry_requested: bool
    # [LAW:one-source-of-truth] A capability is granted exactly when nothing is
    # unmet; the refusal's reason, hint and payload all read these same tuples.
    dangerous_unmet: tuple[Requirement, ...]
    heavy_geometry_unmet: tuple[Requirement, ...]

    @property
    def non_inspection(self) -> bool:
        return self.policy == "normal"

    @property
    def dangerous(self) -> bool:
        return not self.dangerous_unmet

    @property
    def heavy_geometry(self) -> bool:
        return not self.heavy_geometry_unmet

    @classmethod
    def resolve(
        cls,
        *,
        policy: str,
        allow_dangerous: bool,
        allow_heavy_geometry: bool,
        bypass_config_enabled: bool,
    ) -> "ExecutionGrant":
        """The public decision: a bypass needs policy "normal", the request flag AND the config.

        Any other policy - read-only, or a value that does not exist - grants nothing.
        """
        bypassable = policy == "normal"

        # In precedence order: a refusal's reason is its first unmet requirement.
        def unmet(requested: bool) -> tuple[Requirement, ...]:
            met: tuple[tuple[Requirement, bool], ...] = (
                ("policy", bypassable),
                ("request", requested),
                ("config", bypass_config_enabled),
            )
            return tuple(requirement for requirement, ok in met if not ok)

        return cls(
            policy=policy,
            bypass_config_enabled=bypass_config_enabled,
            dangerous_requested=allow_dangerous,
            heavy_geometry_requested=allow_heavy_geometry,
            dangerous_unmet=unmet(allow_dangerous),
            heavy_geometry_unmet=unmet(allow_heavy_geometry),
        )

    def unmet_audit(self) -> dict[str, list[Requirement]]:
        return {
            "dangerous": list(self.dangerous_unmet),
            "heavy_geometry": list(self.heavy_geometry_unmet),
        }

    def as_audit(self) -> dict[str, bool]:
        return {
            "non_inspection": self.non_inspection,
            "dangerous": self.dangerous,
            "heavy_geometry": self.heavy_geometry,
        }


@dataclass(frozen=True)
class DetectedPatterns:
    """Everything the static scan found in the code, whatever the grant says about it.

    The program is parsed and compiled once, here; every gate reads the tree its
    code object was compiled from, and that code object is what ``exec`` runs. A
    program that does not compile carries its ``SyntaxError`` as ``program``, and
    one that does not parse carries nothing else, because nothing else could be
    read [LAW:parse-dont-validate].
    """

    code_sha256: str
    non_inspection: list[str]
    dangerous: list[str]
    heavy_geometry: list[str]
    import_hou: bool
    program: CodeType | SyntaxError
    # What the parser and the compiler wrote to stderr reading it: their
    # SyntaxWarnings, which the program's stderr starts with.
    compiler_warnings: str

    @classmethod
    def scan(cls, code: str) -> "DetectedPatterns":
        # [LAW:one-source-of-truth] The program's identity is computed here once;
        # the audit, the worker ledger and every refusal name it from this field.
        code_sha256 = hashlib.sha256(code.encode("utf-8", "replace")).hexdigest()
        warnings = io.StringIO()
        with captured(io.StringIO(), warnings):
            try:
                tree = parse_program(code)
            except SyntaxError as exc:
                return cls(
                    code_sha256=code_sha256,
                    non_inspection=[],
                    dangerous=[],
                    heavy_geometry=[],
                    import_hou=False,
                    program=exc,
                    compiler_warnings=warnings.getvalue(),
                )
            program: CodeType | SyntaxError
            try:
                program = compile_program(tree)
            except SyntaxError as exc:
                program = exc
        return cls(
            code_sha256=code_sha256,
            non_inspection=_detect_non_inspection_code(tree),
            dangerous=_detect_dangerous_code(tree),
            heavy_geometry=_detect_heavy_geometry_code(tree),
            import_hou=_detect_import_hou(tree),
            program=program,
            compiler_warnings=warnings.getvalue(),
        )


@dataclass(frozen=True)
class _BypassGate:
    """One request-flag capability: where it is scanned, what it still lacks, how a refusal reads."""

    capability: str  # the audit key, and the prefix of the refusal's patterns field
    flag: str  # the request flag that asks for it
    pattern_reason: str  # blocked_reason when the code needs it and the grant lacks it
    found: str  # what the scan found
    alternative: str  # a bounded tool to reach for first, or nothing
    patterns: Callable[[DetectedPatterns], list[str]]
    requested: Callable[[ExecutionGrant], bool]
    unmet: Callable[[ExecutionGrant], tuple[Requirement, ...]]


# [LAW:one-type-per-behavior] Dangerous and heavy-geometry access differ only in
# these values; the gate that refuses them is written once below.
_BYPASS_GATES: tuple[_BypassGate, ...] = (
    _BypassGate(
        capability="dangerous",
        flag="allow_dangerous",
        pattern_reason="dangerous_pattern",
        found="Dangerous operations detected in code",
        alternative="",
        patterns=lambda detected: detected.dangerous,
        requested=lambda grant: grant.dangerous_requested,
        unmet=lambda grant: grant.dangerous_unmet,
    ),
    _BypassGate(
        capability="heavy_geometry",
        flag="allow_heavy_geometry",
        pattern_reason="heavy_geometry",
        found="Heavy geometry access detected in code",
        alternative="Use get_geo_summary() for bounded geometry inspection. ",
        patterns=lambda detected: detected.heavy_geometry,
        requested=lambda grant: grant.heavy_geometry_requested,
        unmet=lambda grant: grant.heavy_geometry_unmet,
    ),
)


# What the caller does to meet each requirement; a refusal's hint is these, for the unmet ones.
_REQUIREMENT_STEP: dict[Requirement, str] = {
    "policy": "Re-run with policy='normal'.",
    "request": "Set {flag}=True.",
    "config": "Enable HOUDINI_MCP_ALLOW_BYPASS on the server.",
}


def _bypass_hint(gate: _BypassGate, unmet: tuple[Requirement, ...]) -> str:
    steps = (_REQUIREMENT_STEP[requirement].format(flag=gate.flag) for requirement in unmet)
    return gate.alternative + " ".join(steps)


def _bypass_refusal(
    gate: _BypassGate, unmet: tuple[Requirement, ...], grant: ExecutionGrant
) -> tuple[str, str]:
    """``(blocked_reason, message)`` for code that needs ``gate`` while ``unmet`` is still short of it.

    The first unmet requirement decides: ``unmet`` is in precedence order.
    """
    match unmet[0]:
        case "policy":
            return (
                gate.pattern_reason,
                f"{gate.found} under policy {grant.policy!r}, which permits inspection only.",
            )
        case "config":
            return (
                "bypass_config_disabled",
                f"Bypass requested ({gate.flag}=True) but server config "
                "HOUDINI_MCP_ALLOW_BYPASS is disabled. Failing closed.",
            )
        case "request":
            return (gate.pattern_reason, gate.found)


# What a program's run raised in: the program itself, or the undo step around it.
_Step = Literal["program", "undo step"]

# How each step's failure reads to the caller.
_TOLD: dict[_Step, str] = {
    "program": "{}",
    "undo step": "Running it as one undo step on Houdini's main thread failed: {}",
}


@dataclass(frozen=True)
class _Fault:
    """One exception a program's run raised: the step it raised in, and its traceback
    as Python prints it."""

    step: _Step
    error: BaseException
    traceback: str

    def told(self) -> str:
        """The exception's line, its type leading (some say nothing else: SystemExit(0)
        is "0"). Its notes are in ``traceback``, and a link's loss carries its own.
        One Houdini raised - a ``hou`` error the program let through, or its step's
        own - reads as Houdini gave it, not as rpyc rebuilt it."""
        if raised_inside_houdini(self.error):
            return _TOLD[self.step].format(reason(self.error))
        line = traceback.TracebackException.from_exception(self.error)
        line.__notes__ = None
        return _TOLD[self.step].format("".join(line.format_exception_only()).strip())


@dataclass(frozen=True)
class _Ended:
    """How a program's run ended: every exception it raised, in order (none when it
    completed), and the link's loss when one of them was that."""

    faults: tuple[_Fault, ...]
    lost: LinkLost | None


def _run_code_thread(
    program: CodeType,
    compiler_warnings: str,
    hou: Any,
    stdout_capture: Capture,
    stderr_capture: Capture,
) -> None:
    """Execute the gate's compiled program, capturing stdout/stderr."""
    exec_globals = {"hou": hou, "__builtins__": builtins.__dict__}
    # The compiler's warnings lead the program's stderr, where exec compiling the
    # source wrote them.
    stderr_capture.write(compiler_warnings)
    # [LAW:effects-at-boundaries] Only this thread's writes are captured;
    # other threads keep writing to the streams they always did.
    with captured(stdout_capture, stderr_capture):
        exec(program, exec_globals)


@dataclass(frozen=True)
class _Diff:
    """What a program changed under ``_DIFF_ROOT``, capped."""

    changes: CappedDiff


@dataclass(frozen=True)
class _DiffFailed:
    """Why a program that ran has no diff: the link's loss, already dropped, when
    that is what failed."""

    error: Exception


@dataclass(frozen=True)
class _NeverRan:
    """Why a program never ran: the scene before it could not be read (the link's
    loss, already dropped, when that is what failed)."""

    error: Exception


class _ReportedOutput(TypedDict):
    """A program's output as every way out of execute_code reports it."""

    stdout: str
    stdout_truncated: bool
    stderr: str
    stderr_truncated: bool


def _reported_output(operation: str, stdout: Capture, stderr: Capture) -> _ReportedOutput:
    """Each capture's text beside whether its bound cut it."""
    out, err = stdout.written(), stderr.written()
    logger.info(
        "%s output stdout=%d stderr=%d characters truncated=%s closed=%s",
        operation,
        out.length,
        err.length,
        [name for name, written in (("stdout", out), ("stderr", err)) if written.truncated],
        [name for name, capture in (("stdout", stdout), ("stderr", stderr)) if capture.closed],
    )
    return {
        "stdout": out.text,
        "stdout_truncated": out.truncated,
        "stderr": err.text,
        "stderr_truncated": err.truncated,
    }


def _build_audit(
    *,
    code: str,
    grant: ExecutionGrant,
    detected: DetectedPatterns,
    timed_out: bool = False,
    woke_early: int | None = None,
    blocked_reason: str | None = None,
) -> dict[str, Any]:
    """Assemble the structured audit block returned with every response."""
    return {
        "policy": grant.policy,
        "allow_dangerous_requested": grant.dangerous_requested,
        "allow_heavy_geometry_requested": grant.heavy_geometry_requested,
        "bypass_config_enabled": grant.bypass_config_enabled,
        "granted": grant.as_audit(),
        "unmet": grant.unmet_audit(),
        "dangerous_patterns": detected.dangerous,
        "heavy_geometry_patterns": detected.heavy_geometry,
        "non_inspection_patterns": detected.non_inspection,
        "timed_out": timed_out,
        # How often the wait for the program woke before its monotonic deadline;
        # None when the call was refused before anything was waited on.
        "woke_early": woke_early,
        "blocked_reason": blocked_reason,
        "code_length": len(code),
        "code_sha256": detected.code_sha256,
    }


def _blocked(
    *,
    code: str,
    grant: ExecutionGrant,
    detected: DetectedPatterns,
    reason: str,
    message: str,
    hint: str,
    **fields: Any,
) -> InvalidInput:
    """The one shape of a pre-execution refusal: the program is the caller's to fix."""
    # Patterns only, never the source: this line is the durable server-side trail.
    logger.warning(
        "execute_code blocked: %s policy=%s unmet=%s non_inspection=%s dangerous=%s heavy=%s",
        reason,
        grant.policy,
        grant.unmet_audit(),
        detected.non_inspection,
        detected.dangerous,
        detected.heavy_geometry,
    )
    return InvalidInput(
        message,
        policy=grant.policy,
        hint=hint,
        **fields,
        audit=_build_audit(code=code, grant=grant, detected=detected, blocked_reason=reason),
    )


@tool(PROGRAM)
def execute_code(
    code: str,
    capture_diff: bool = False,
    max_stdout_size: Count = DEFAULT_MAX_STDOUT_SIZE,
    max_stderr_size: Count = DEFAULT_MAX_STDERR_SIZE,
    max_diff_nodes: Count = DEFAULT_MAX_DIFF_NODES,
    timeout: Seconds = DEFAULT_TIMEOUT,
    allow_dangerous: bool = False,
    host: str = "localhost",
    port: int = 18811,
    allow_heavy_geometry: bool = False,
    policy: Policy = DEFAULT_POLICY,
    *,
    bypass_config_enabled: bool,
    main_thread: MainThread,
) -> dict[str, Any]:
    """
    Execute Python code in Houdini with scene change tracking and safety rails.

    IMPORTANT: The 'hou' module is pre-injected as a global variable. Do NOT use
    'import hou' or 'from hou import ...' - just use 'hou' directly in your code.

    Use this for complex operations that aren't covered by other tools.

    SAFETY FEATURES:
    - Policy profiles: "read-only" (inspection only: the code may name only
      recognized inspection APIs, pure stdlib and dicts for its own records;
      anything else, including hou APIs added after this release, is refused,
      and bypass flags grant nothing) and "normal" (default). Both are guard
      rails against accidents, not a sandbox.
    - Dangerous operation detection: hou.exit(), discarding the open scene
      (hou.hipFile.clear(), hou.hipFile.load(), hou.hipFile.importFBX() with
      merge_into_scene=False, hou.hipFile.merge() with
      overwrite_on_conflict=True), file
      state changes (os and tempfile beyond their read side, shutil, pathlib,
      archive and database modules, open() for writing), shell and process
      access, dynamic import/execution, HScript (hou.hscript(), its expression
      evaluators, and string expansion holding a backtick), network egress (socket, urllib,
      requests, httpx, http, ftplib, smtplib, ...) and the debugger. Judged on
      the parsed program, so text in comments and strings is not code.
    - Code that does not compile is refused before any rail runs.
    - Heavy geometry guard: Blocks direct node.geometry()/geo.points()/geo.prims() access by default
    - Config+request bypass: allow_dangerous unlocks dangerous patterns and
      allow_heavy_geometry unlocks heavy geometry, each only when the server
      also sets HOUDINI_MCP_ALLOW_BYPASS=true.
    - Output size caps: Prevents massive output from overwhelming the response
    - One undo step: the program runs on Houdini's main thread as one step on
      its undo stack, labelled as the call, and a program that raises is
      undone. While it runs, Houdini's interface does not redraw and other
      calls wait for it.
    - Execution timeout with rollback: at its timeout the program is cut off
      from Houdini, which frees Houdini's main thread once the request it is
      serving returns, and its step is undone.
    - Scene diff limits: Caps the number of nodes returned in scene changes
    - Structured audit block returned on every call.

    Args:
        code: Python code to execute
        capture_diff: If True, captures before/after scene state and returns changes (default: False)
        max_stdout_size: Maximum stdout length in characters (default: 100000)
        max_stderr_size: Maximum stderr length in characters (default: 100000)
        max_diff_nodes: Maximum nodes in each scene diff list (default: 1000)
        timeout: Execution timeout in seconds (default: 30)
        allow_dangerous: Request-side opt-in to run dangerous patterns.
            Only honored under policy "normal" when HOUDINI_MCP_ALLOW_BYPASS is enabled server-side.
        allow_heavy_geometry: Request-side opt-in to direct SOP geometry access.
            Only honored under policy "normal" when HOUDINI_MCP_ALLOW_BYPASS is enabled.
            Prefer get_geo_summary().
        policy: Safety profile - "read-only" or "normal" (default).

    Example:
        execute_code('''
            obj = hou.node("/obj")
            geo = obj.createNode("geo", "my_geometry")
            sphere = geo.createNode("sphere")
            sphere.parm("radx").set(2.0)
            sphere.setDisplayFlag(True)
            sphere.setRenderFlag(True)
        ''')

    Returns:
        Dict with status, stdout, stderr, and scene_changes (if capture_diff=True):
        the nodes under /obj added, removed, or replaced by another type, at any
        depth. When the scene could not be read after the code ran, the code's
        result stands and scene_changes_error says why in place of scene_changes.
        stdout_truncated and stderr_truncated, on every result that carries the
        output, say whether each was cut to its max size; scene_changes.truncated
        names the diff lists cut to max_diff_nodes, empty when none was.
        If dangerous or heavy-geometry patterns are detected and not granted, returns
        error with the detected patterns and unmet_requirements: each of "policy"
        (re-run with policy='normal'), "request" (set the flag), "config" (enable
        HOUDINI_MCP_ALLOW_BYPASS) still short of the grant; the hint asks for
        exactly these.
        audit.granted reports the one permission decision the run was made under,
        and audit.unmet what each bypass capability still lacked.
        A failure's message ends by saying what became of the program's changes:
        undone, or why they may not have been. The program's thread runs on
        after a timeout until it ends, cut off from Houdini, and every
        scene-writing tool is refused, naming it, until then.
        A program that failed because the link to Houdini did reads as that
        failure (timeout with connection_reset, or connection_lost) with the
        same stdout, stderr, traceback and audit; the next call reconnects.
    """
    grant = ExecutionGrant.resolve(
        policy=policy,
        allow_dangerous=allow_dangerous,
        allow_heavy_geometry=allow_heavy_geometry,
        bypass_config_enabled=bypass_config_enabled,
    )
    # the tool a refusal, a log line and a failure name
    operation = execute_code.__name__
    logger.debug(
        "execute_code policy=%s granted=%s config=%s requested dangerous=%s heavy=%s",
        grant.policy,
        grant.as_audit(),
        grant.bypass_config_enabled,
        grant.dangerous_requested,
        grant.heavy_geometry_requested,
    )

    # [LAW:dataflow-not-control-flow] The scan always runs in full, before any
    # refusal: the grant decides what each finding means, and every audit and
    # log line reports all of it, whatever stopped the code.
    detected = DetectedPatterns.scan(code)

    # --- Syntax gate: a program the gates cannot read is a program that does not run
    if isinstance(detected.program, SyntaxError):
        # [LAW:no-silent-failure] the compile exec would have done; say so here, before any rail.
        exc = detected.program
        location = f" (line {exc.lineno})" if exc.lineno is not None else ""
        raise _blocked(
            code=code,
            grant=grant,
            detected=detected,
            reason="syntax_error",
            message=f"Code does not compile: {exc.msg}{location}",
            hint="Fix the error the compiler reports; a program that does not compile cannot run.",
            traceback="".join(traceback.format_exception_only(exc)),
        )
    # [LAW:one-source-of-truth] What runs is the code object the gate compiled;
    # the worker never compiles the source again.
    code_object = detected.program

    # --- Empty code ----------------------------------------------------------
    if not code or not code.strip():
        return {
            "status": "success",
            "stdout": "",
            "stderr": "",
            "message": "Empty code - nothing to execute",
            "policy": grant.policy,
            "audit": _build_audit(code=code or "", grant=grant, detected=detected),
        }

    # Detect the common mistake of importing hou when it is pre-injected via RPyC.
    # (Preserved from bead houdini-mcp-vzp.1 / PR #9.)
    if detected.import_hou:
        raise _blocked(
            code=code,
            grant=grant,
            detected=detected,
            reason="import_hou",
            message="Code attempts to import hou module, but hou is already available",
            hint=(
                "The 'hou' module is pre-injected as a global variable by execute_code via RPyC. "
                "You can use 'hou' directly without importing it. Remove 'import hou' or "
                "'from hou import ...' from your code and access hou directly (e.g., hou.node('/obj'))."
            ),
            suggested_fix=code.replace(
                "import hou", "# import hou  # Not needed - hou is pre-injected"
            ).replace("from hou import ", "# from hou import "),
        )

    if detected.non_inspection and not grant.non_inspection:
        raise _blocked(
            code=code,
            grant=grant,
            detected=detected,
            reason="read_only_non_inspection",
            message=(
                "Blocked under read-only policy: the code names something outside "
                "the inspection allowlist."
            ),
            hint=(
                "read-only admits only recognized inspection (navigation, parameter "
                "reads, flags, wiring, geometry reads, pure stdlib); keep your own "
                "records in dicts, not attributes. Re-run with policy='normal' to "
                "allow anything else."
            ),
            non_inspection_patterns=detected.non_inspection,
        )

    for gate in _BYPASS_GATES:
        patterns = gate.patterns(detected)
        unmet = gate.unmet(grant)
        if patterns and unmet:
            reason, message = _bypass_refusal(gate, unmet, grant)
            raise _blocked(
                code=code,
                grant=grant,
                detected=detected,
                reason=reason,
                message=message,
                hint=_bypass_hint(gate, unmet),
                unmet_requirements=list(unmet),
                bypass_requested=gate.requested(grant),
                bypass_config_enabled=grant.bypass_config_enabled,
                **{f"{gate.capability}_patterns": patterns},
            )

    # Emit the durable bypass audit BEFORE execution so a later exception
    # cannot skip the server-side trail. Source code and secrets are excluded.
    if detected.dangerous or detected.heavy_geometry:
        logger.warning(
            "execute_code approved bypass policy=%s dangerous=%s heavy=%s code_sha256=%s",
            grant.policy,
            detected.dangerous,
            detected.heavy_geometry,
            detected.code_sha256,
        )

    # --- Connect and prepare execution --------------------------------------
    with using(host, port) as link:
        hou = link.hou
        # [LAW:one-source-of-truth] The program's timeout is the one bound on how
        # long it holds Houdini's main thread: the call cuts it off then.
        # [LAW:no-ambient-temporal-coupling] Houdini's own give-up, for a call whose
        # client is gone, starts before that timeout's clock, so it waits a body's
        # silence longer rather than racing the cut.
        step = main_thread.step(operation, timeout + BODY_SILENCE)

        # [LAW:one-source-of-truth] Each capture holds its own bound, and every way
        # out of the call reports them through _reported_output.
        stdout_capture = Capture(max_stdout_size)
        stderr_capture = Capture(max_stderr_size)
        # The worker writes this once the program and its undo step have ended.
        ended = _Ended((), None)
        # With capture_diff, the worker writes this on every way out: the program's
        # diff, why it has none, or why the program never ran. Its first value
        # stands only if the worker died without writing it. Without capture_diff,
        # or once the program raised: None.
        scene: _Diff | _DiffFailed | _NeverRan | Exception | None = (
            RuntimeError(f"{operation}'s worker ended before it wrote the scene diff")
            if capture_diff
            else None
        )
        # The program's clock: timeout is the program's, not the scene reads around it,
        # nor the wait for Houdini's main thread to take it. A program that never
        # began reads its time from here.
        program_starts, program_ends = threading.Event(), threading.Event()
        started = time.monotonic()

        def diff_since(before: SceneSnapshot, program_took: float) -> _Diff | _DiffFailed:
            # [LAW:one-source-of-truth] The worker is the one writer of its run's
            # last diff, so a program that outlived its caller's timeout still
            # leaves its diff once it ends.
            try:
                diff = scene_diff(before, scene_snapshot(hou, _DIFF_ROOT))
            except Exception as error:  # noqa: BLE001
                # [LAW:no-silent-failure] The program ran; only its diff is missing.
                # [LAW:single-enforcer] Judged here, where the link was used: a worker
                # that outlived its call has no caller left to drop a link that failed.
                logger.warning("%s ran but its scene diff failed: %r", operation, error)
                return _DiffFailed(failed_on(houdini(), link, error))
            # [LAW:one-source-of-truth] Which lists were cut travels in the diff, so
            # every result that carries it says so, get_last_scene_diff's too.
            changes = capped(diff, max_diff_nodes)
            logger.info(
                "%s scene diff of %s, program took %.3fs of timeout %ss: "
                "added=%d removed=%d modified=%d truncated=%s",
                operation,
                _DIFF_ROOT,
                program_took,
                timeout,
                *(len(diff[key]) for key in ("added", "removed", "modified")),
                changes["truncated"],
            )
            LAST_SCENE_DIFF.record(changes)
            return _Diff(changes)

        def run_code() -> None:
            # [LAW:no-ambient-temporal-coupling] Both reads of the scene happen in the
            # worker, under its ledger slot, so no other program runs between them
            # and the diff is this program's alone. A scene that could not be read
            # first is not written. Every way out, SystemExit included, leaves
            # through the finally, so the caller's clock never waits on a program
            # that is not running.
            nonlocal scene, ended
            before: SceneSnapshot | None = None
            try:
                if capture_diff:
                    # [LAW:one-source-of-truth] From its first act until its diff
                    # exists, the last capture is this run's, with no diff.
                    LAST_SCENE_DIFF.record(None)
                    try:
                        before = scene_snapshot(hou, _DIFF_ROOT)
                    except Exception as error:  # noqa: BLE001
                        logger.warning(
                            "%s did not run: the scene before it could not be read: %r",
                            operation,
                            error,
                        )
                        scene = _NeverRan(failed_on(houdini(), link, error))
                        return
                faults = run_program()
                # Written before the link is judged, so a judgment that raises cannot
                # leave the program's failure reading as a success.
                ended = _Ended(faults, None)
                # [LAW:single-enforcer] Judged once the undo step has ended, so its
                # recovery still reached Houdini, and here, where the link was used, so a
                # program that outlived its call still drops the link it failed on. The
                # first fault that is the link's drops it; none after is judged.
                for fault in faults:
                    if (lost := link_lost(houdini(), link, fault.error)) is not None:
                        ended = _Ended(faults, lost)
                        break
                # The worker's own record of its run: it alone outlives a call that
                # timed out, and a link it dropped is traced to the program here.
                logger.info(
                    "%s program ended code_sha256=%s after %.3fs of timeout %ss: "
                    "faults=%s link_lost=%s",
                    operation,
                    detected.code_sha256,
                    time.monotonic() - started,
                    timeout,
                    [fault.step for fault in faults],
                    ended.lost,
                )
            finally:
                program_starts.set()
                program_ends.set()
                if before is not None:
                    # A program that raised has no diff; its scene is not read again.
                    scene = None if ended.faults else diff_since(before, time.monotonic() - started)

        def run_program() -> tuple[_Fault, ...]:
            # Never retry user code if its step fails: the step may fail after the
            # program already changed the scene, so a retry would run it twice. The
            # step's failure is told after whatever the program raised.
            raised: list[_Fault] = []

            def program() -> None:
                nonlocal started
                # The program's clock starts as it begins on Houdini's main thread,
                # not while the call waits for the main thread to take it.
                started = time.monotonic()
                program_starts.set()
                try:
                    _run_code_thread(
                        code_object, detected.compiler_warnings, hou, stdout_capture, stderr_capture
                    )
                # Whatever the program raises - SystemExit and KeyboardInterrupt too -
                # is how it ended, not the worker's own failure: the caller reports it.
                except BaseException as error:
                    raised.append(_Fault("program", error, traceback.format_exc()))
                    raise
                # The caller's clock stops as the program ends, not once its step has
                # closed and been recovered, which the caller joins on: a program that
                # ended in time is never cut off from that step's undo.
                finally:
                    program_ends.set()

            try:
                step.run(link, program)
            # Whatever else the step raises is how the run ended, as for the program.
            except BaseException as error:  # noqa: BLE001
                if not raised or error is not raised[0].error:
                    return (*raised, _Fault("undo step", error, traceback.format_exc()))
            return tuple(raised)

        # [LAW:no-ambient-temporal-coupling] The ledger owns the worker's lifetime: it
        # starts the thread inside this call's hold on the scene, the thread clears
        # the slot once the program, its undo step, and the scene reads around it
        # have ended, and a timeout leaves the slot occupied after this call returns.
        worker = WORKERS.launch(
            run_code, operation=operation, code_sha256=detected.code_sha256, timeout=timeout
        )
        program_starts.wait()
        # [LAW:no-ambient-temporal-coupling] The timeout runs on the monotonic clock
        # the program's duration is read on, from after that clock started, so a
        # program called timed out has run its whole timeout. Event.wait alone can
        # keep the wall clock (CPython on macOS) and return before it has.
        deadline = time.monotonic() + timeout
        woke_early = 0
        while (left := deadline - time.monotonic()) > 0 and not program_ends.wait(left):
            woke_early += time.monotonic() < deadline

        # [LAW:one-source-of-truth] The program's own end says whether it is still
        # running - not the ledger slot, which the worker holds on through its step's
        # recovery and the scene read after it - and the step's verdict settles a
        # program that ends as the wait expires: one that ended is reported as
        # complete, one cut off as timed out.
        if not program_ends.is_set() and step.cut_off():
            # Once its link has ended, Houdini's main thread gives the program's call
            # up after the request it is serving returns, closing the step's group,
            # and the program's request fails. Its step is then recovered on a fresh
            # link, behind that call on the main thread - by this call or the worker,
            # whichever reaches it first.
            told = step.recover().told
            logger.warning(
                "%s exceeded timeout of %ss; cut off worker code_sha256=%s: %s",
                operation,
                timeout,
                detected.code_sha256,
                told or "no step",
            )
            timed_out = ProgramStillRunning(
                f"Execution timeout: code did not complete within {timeout} seconds",
                policy=grant.policy,
                **_reported_output(operation, stdout_capture, stderr_capture),
                timeout=timeout,
                warning=(
                    "Execution timed out, so the program was cut off from Houdini. Its "
                    "thread runs on until it ends, and every tool that writes the scene is "
                    f"refused until then. {HOW_A_WORKER_ENDS}"
                ),
                worker=worker.describe(),
                audit=_build_audit(
                    code=code, grant=grant, detected=detected, timed_out=True, woke_early=woke_early
                ),
            )
            for note in told:
                add_note(timed_out, note)
            raise timed_out

        # The program ended; what remains of the worker is the scene read after it.
        worker.thread.join()
        audit = _build_audit(code=code, grant=grant, detected=detected, woke_early=woke_early)

        # --- Exception during execution -----------------------------------------
        if ended.faults:
            first, *later = ended.faults
            failure = OperationFailed(
                first.told(),
                traceback="".join(fault.traceback for fault in ended.faults),
                **_reported_output(operation, stdout_capture, stderr_capture),
                policy=grant.policy,
                audit=audit,
            )
            for fault in later:
                add_note(failure, fault.told())
            for note in step.told():
                add_note(failure, note)
            # A program that failed because its link did reads as the link's failure,
            # carrying all of the above; its own failure has no cause to be judged by.
            raise failure from ended.lost

        # [LAW:no-silent-failure] A scene that could not be read before the program
        # is raised: the program never ran on it.
        if isinstance(scene, _NeverRan):
            raise scene.error
        # The worker died without writing what became of the scene.
        if isinstance(scene, Exception):
            raise scene

        # --- Success -------------------------------------------------------------
        result: dict[str, Any] = {
            "status": "success",
            **_reported_output(operation, stdout_capture, stderr_capture),
            "policy": grant.policy,
            "audit": audit,
        }

        if isinstance(scene, _DiffFailed):
            result["scene_changes_error"] = failure_for(scene.error, operation).model_dump()
        if isinstance(scene, _Diff):
            result["scene_changes"] = scene.changes

        if detected.dangerous:
            result["dangerous_patterns_executed"] = detected.dangerous
            result["safety_warning"] = (
                "Code with dangerous patterns was executed with an approved bypass."
            )

        return result


@tool(READ)
def get_last_scene_diff() -> dict[str, Any]:
    """
    Get the scene diff of the execute_code run with capture_diff=True launched last.

    Shows the nodes under /obj that run added, removed, or replaced by a node of
    another type ("modified"), at any depth, capped as that run returned it:
    "truncated" names the lists its max_diff_nodes cut. While that run has no diff - still
    running, timed out, raised, or its scene could not be read - there is none.
    """
    diff = LAST_SCENE_DIFF.diff()
    if diff is None:
        return {
            "status": "warning",
            "message": (
                "No scene diff available: the last execute_code run with capture_diff=True"
                " produced none, or none has run."
            ),
        }

    return {"status": "success", "diff": diff}
