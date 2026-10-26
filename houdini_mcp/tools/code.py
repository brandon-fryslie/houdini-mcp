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
* The program runs in a process of its own (:mod:`houdini_mcp.program`), with
  its own link to Houdini and its own stdout and stderr, on Houdini's main
  thread as one undo step labelled as the call. The call kills the process at
  the program's timeout, and kills its process group on every way out, so
  nothing of the program runs once the call returns. A program that raised,
  died or was killed has its step undone from the server's link
  (:func:`~houdini_mcp.tools.transactions.undo_latest`).
"""

import hashlib
import logging
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from types import CodeType
from typing import Any, Literal, TypedDict, assert_never

from ..connection import DEFAULT_SYNC_TIMEOUT, Link, houdini
from ..main_thread import BODY_SILENCE, MainThread
from ..program import DidNotRun, Ended, Request
from ._common import (
    PROGRAM,
    READ,
    Count,
    InvalidInput,
    OperationFailed,
    ProgramTimedOut,
    Seconds,
    ToolError,
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
from .failure import add_note, failed_on, failure_for
from .program_output import Capture
from .program_process import PROGRAM_START_TIMEOUT, Died, Killed, Run, run_program
from .snapshot import CappedDiff, SceneSnapshot, capped, scene_diff, scene_snapshot
from .transactions import Recovered, raised_after_undo, step_label, undo_latest

logger = logging.getLogger("houdini_mcp.tools.code")

_DIFF_ROOT = "/obj"


class LastSceneDiff:
    """What the most recently launched ``capture_diff`` run changed.

    [LAW:no-shared-mutable-globals] The one owner of get_last_scene_diff's
    answer: the diff that run returned, capped as it returned it, or None while
    that run has produced none. Only an execute_code call writes it, while it
    holds the scene, so writes arrive in call order and none outlives its call.
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

    @classmethod
    def scan(cls, code: str) -> "DetectedPatterns":
        # [LAW:one-source-of-truth] The program's identity is computed here once;
        # the audit, the run's record and every refusal name it from this field.
        code_sha256 = hashlib.sha256(code.encode("utf-8", "replace")).hexdigest()
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


@dataclass(frozen=True)
class _Diff:
    """What a program changed under ``_DIFF_ROOT``, capped."""

    changes: CappedDiff


@dataclass(frozen=True)
class _DiffFailed:
    """Why a program that ran has no diff: the link's loss, already dropped, when
    that is what failed."""

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
        "%s output stdout=%d stderr=%d characters truncated=%s",
        operation,
        out.length,
        err.length,
        [name for name, written in (("stdout", out), ("stderr", err)) if written.truncated],
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
    - Execution timeout with rollback: the program runs in a process of its
      own, which is killed at its timeout, and its step is undone.
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
        undone, or why they may not have been. A program that outlives its
        timeout is killed with its process: the timeout failure (cut_off
        "program", retry check_scene) carries its output up to the kill.
        A program whose own link to Houdini failed has raised (operation_failed,
        the link's exception in its traceback); the server's link was not
        involved. A program that never began - its link would not open, the
        plugin is missing, Houdini's main thread refused it - is
        operation_failed saying so, with nothing undone.
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

    # --- Run the program in a process of its own -----------------------------
    with using(host, port) as link:
        # [LAW:one-source-of-truth] Each capture holds its own bound, and every way
        # out of the call reports them through _reported_output.
        stdout_capture = Capture(max_stdout_size)
        stderr_capture = Capture(max_stderr_size)
        audit = _build_audit(code=code, grant=grant, detected=detected)
        before: SceneSnapshot | None = None
        if capture_diff:
            # [LAW:one-source-of-truth] From the call's first act until its diff
            # exists, the last capture is this run's, with no diff.
            LAST_SCENE_DIFF.record(None)
            try:
                before = scene_snapshot(link.hou, _DIFF_ROOT)
            except Exception as error:
                add_note(error, "The program never ran: the scene before it could not be read.")
                raise
        label = step_label(operation)
        run = run_program(
            Request(
                reach=houdini().target.reach(link.host, link.port),
                code=code_object,
                label=label,
                # [LAW:no-ambient-temporal-coupling] Houdini's own give-up, for a call
                # whose process is gone, starts before the timeout's clock, so it
                # waits a body's silence longer rather than racing the kill.
                silence=timeout + BODY_SILENCE,
                sync_timeout=DEFAULT_SYNC_TIMEOUT,
                start_timeout=PROGRAM_START_TIMEOUT,
            ),
            timeout,
            stdout_capture,
            stderr_capture,
        )
        output = _reported_output(operation, stdout_capture, stderr_capture)
        # [LAW:single-enforcer] A run that may have left its step - it raised, died
        # or was killed - has it undone by the one undo; one that completed, or
        # never began, has no step to undo. The run's record is written once, as
        # the call ends, with what became of its step.
        recovered: Recovered | None = None
        try:
            match run.ending:
                case Ended(faults=()):
                    result: dict[str, Any] = {
                        "status": "success",
                        **output,
                        "policy": grant.policy,
                        "audit": audit,
                    }
                    if run.ending.reply_lost is not None:
                        result["warning"] = (
                            "Houdini did not confirm the program's undo step closed "
                            f"({run.ending.reply_lost}); its changes stand."
                        )
                    if before is not None:
                        result.update(
                            _diff_after(operation, link, before, max_diff_nodes, run, timeout)
                        )
                    if detected.dangerous:
                        result["dangerous_patterns_executed"] = detected.dangerous
                        result["safety_warning"] = (
                            "Code with dangerous patterns was executed with an approved bypass."
                        )
                    return result
                case DidNotRun(why):
                    # Its process reports this only from before its program began,
                    # so no step was opened the program could have written to.
                    raise ProgramDidNotRun(
                        f"The program never ran: {why}", **output, policy=grant.policy, audit=audit
                    )
                case Ended(faults):
                    first, *later = faults
                    failure: ToolError = OperationFailed(
                        first.told,
                        traceback="".join(fault.traceback for fault in faults),
                        **output,
                        policy=grant.policy,
                        audit=audit,
                    )
                    for fault in later:
                        add_note(failure, fault.told)
                case Died():
                    failure = OperationFailed(
                        "The program's process ended without saying how its program "
                        f"ended (exit status {run.exit_status})",
                        **output,
                        policy=grant.policy,
                        audit=audit,
                    )
                case Killed(phase):
                    failure = ProgramTimedOut(
                        _KILLED[phase].format(timeout=timeout),
                        policy=grant.policy,
                        **output,
                        timeout=timeout,
                        audit=_build_audit(
                            code=code, grant=grant, detected=detected, timed_out=True
                        ),
                    )
                case _:
                    assert_never(run.ending)
            recovered = undo_latest(main_thread, link, operation, label)
            raised_after_undo(operation, failure, recovered)
        finally:
            _log_run(operation, detected.code_sha256, timeout, run, recovered)


class ProgramDidNotRun(OperationFailed):
    """execute_code's program never began: its process could not open its link,
    Houdini lacks the plugin, its main thread refused the call, or the process's
    start deadline passed. Nothing was undone, because nothing ran."""


# How the timeout response reads for each phase whose clock the call killed the process on.
_KILLED: dict[str, str] = {
    "running": "Execution timeout: code did not complete within {timeout} seconds",
    "starting": (
        "Execution timeout: the program's process had not reported by its start "
        f"deadline ({PROGRAM_START_TIMEOUT}s) and a grace, so it was killed"
    ),
    "closing": (
        "Execution timeout: the program ended, but its process had not reported "
        "how its undo step closed within the sync timeout and a grace, so it was killed"
    ),
}


def _diff_after(
    operation: str, link: Link, before: SceneSnapshot, max_diff_nodes: int, run: Run, timeout: int
) -> dict[str, Any]:
    """What a completed program changed under ``_DIFF_ROOT``, capped, or why it has no diff."""
    try:
        diff = scene_diff(before, scene_snapshot(link.hou, _DIFF_ROOT))
    except Exception as error:  # noqa: BLE001
        # [LAW:no-silent-failure] The program ran; only its diff is missing.
        logger.warning("%s ran but its scene diff failed: %r", operation, error)
        return {
            "scene_changes_error": failure_for(
                failed_on(houdini(), link, error), operation
            ).model_dump()
        }
    # [LAW:one-source-of-truth] Which lists were cut travels in the diff, so
    # every result that carries it says so, get_last_scene_diff's too.
    changes = capped(diff, max_diff_nodes)
    logger.info(
        "%s scene diff of %s, program took %ss of timeout %ss: "
        "added=%d removed=%d modified=%d truncated=%s",
        operation,
        _DIFF_ROOT,
        None if run.ran_seconds is None else round(run.ran_seconds, 3),
        timeout,
        *(len(diff[key]) for key in ("added", "removed", "modified")),
        changes["truncated"],
    )
    LAST_SCENE_DIFF.record(changes)
    return {"scene_changes": changes}


def _log_run(
    operation: str, code_sha256: str, timeout: int, run: Run, recovered: Recovered | None
) -> None:
    """[LAW:nothing-unseen] The program's one record, once its process is reaped
    and its step undone: its ending as the wait saw it, the facts the process
    added, and what became of its step."""
    undo: object = "none attempted"
    if recovered is not None:
        undo = {"told": list(recovered.told) or ["no step"], "lost": repr(recovered.lost)}
    logger.info(
        "%s program ended code_sha256=%s pid=%d exit_status=%d began_after_ms=%s "
        "ran_seconds=%s timeout=%ss ending=%s undo=%s",
        operation,
        code_sha256,
        run.pid,
        run.exit_status,
        None if run.began_after_ms is None else round(run.began_after_ms, 1),
        None if run.ran_seconds is None else round(run.ran_seconds, 3),
        timeout,
        _ending_record(run),
        undo,
    )


def _ending_record(run: Run) -> dict[str, Any]:
    """The ending, as the record carries it: the wait's ending type and its datum."""
    match run.ending:
        case Ended(faults, reply_lost):
            return {
                "ended": [fault.step for fault in faults],
                "reply": "lost" if reply_lost is not None else "confirmed",
            }
        case DidNotRun(why):
            return {"did_not_run": why}
        case Killed(phase):
            return {"killed": phase}
        case Died():
            return {"died": run.exit_status}
        case _:
            assert_never(run.ending)


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
