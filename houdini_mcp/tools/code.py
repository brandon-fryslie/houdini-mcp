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
                       in *and* the server is configured to permit bypass. A
                       guard rail against accidents, not a sandbox.
* Every gate reads the parsed program, the same AST ``exec`` compiles, so text
  in comments and strings is not code and a program that does not parse is
  refused before any rail.
* Each bypass flag unlocks exactly its own capability: ``allow_dangerous``
  the dangerous patterns, ``allow_heavy_geometry`` the heavy-geometry
  patterns. Both require server configuration (``HOUDINI_MCP_ALLOW_BYPASS``).
  Missing either fails closed.
* Every call returns a structured ``audit`` block, including the resolved
  grant, and is logged.
* On timeout Python cannot forcibly kill the worker thread. While that thread
  is alive its undo group is still open, so calling ``performUndo()`` could
  undo the previous human action rather than this run. The tool therefore does
  not touch the undo stack on timeout and reports scene consistency as unknown.
  The worker stays recorded in :data:`~houdini_mcp.tools.workers.WORKERS`
  until its thread ends, and every scene-writing tool is refused, naming it,
  for as long as it is.
"""

import builtins
import hashlib
import logging
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from io import StringIO
from typing import Any, Literal

from ..connection import houdini
from ._common import (
    InvalidInput,
    OperationFailed,
    ProgramStillRunning,
    _truncate_output,
    handle_connection_errors,
    using,
)
from .code_policy import (
    _detect_dangerous_code,
    _detect_heavy_geometry_code,
    _detect_import_hou,
    _detect_non_inspection_code,
    parse_code,
)
from .failure import failed_on, failure_for
from .program_output import captured
from .snapshot import SceneSnapshot, scene_diff, scene_snapshot
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
        self._diff: dict[str, Any] | None = None

    def record(self, diff: dict[str, Any] | None) -> None:
        with self._lock:
            self._diff = diff

    def diff(self) -> dict[str, Any] | None:
        with self._lock:
            return self._diff


LAST_SCENE_DIFF = LastSceneDiff()

# Policy profiles, from strictest to most permissive.
Policy = Literal["read-only", "normal"]
DEFAULT_POLICY: Policy = "normal"

# Where a grant came from: the MCP request, or a server tool that wrote the code itself.
GrantSource = Literal["request", "trusted"]

# What a bypass capability can still lack: the "normal" policy, the request flag,
# the server config - or, for a server-authored program, nothing any input can supply.
Requirement = Literal["policy", "request", "config", "source"]

# [LAW:one-source-of-truth] The execution limits, stated once for the public
# tool and for every server-side entry.
DEFAULT_MAX_STDOUT_SIZE = 100000
DEFAULT_MAX_STDERR_SIZE = 100000
DEFAULT_MAX_DIFF_NODES = 1000
DEFAULT_TIMEOUT = 30

# Named undo group used to wrap execution so a timeout can roll back.
_UNDO_GROUP_LABEL = "houdini-mcp execute_code"


@dataclass(frozen=True)
class ExecutionGrant:
    """What one execute_code run is permitted to do.

    [LAW:parse-dont-validate] Resolved once from the policy, the request's
    bypass flags and the server configuration; every gate below reads a
    capability off this value instead of recombining the inputs.
    """

    policy: str
    source: GrantSource
    bypass_config_enabled: bool
    dangerous_requested: bool
    heavy_geometry_requested: bool
    # [LAW:one-source-of-truth] A capability is granted exactly when nothing is
    # unmet; the refusal's reason, hint and payload all read these same tuples.
    dangerous_unmet: tuple[Requirement, ...]
    heavy_geometry_unmet: tuple[Requirement, ...]

    @property
    def mutation(self) -> bool:
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
            source="request",
            bypass_config_enabled=bypass_config_enabled,
            dangerous_requested=allow_dangerous,
            heavy_geometry_requested=allow_heavy_geometry,
            dangerous_unmet=unmet(allow_dangerous),
            heavy_geometry_unmet=unmet(allow_heavy_geometry),
        )

    @classmethod
    def trusted_heavy_geometry(cls, *, bypass_config_enabled: bool) -> "ExecutionGrant":
        """The grant for a server-authored, bounded geometry program.

        Not reachable from the MCP surface: only a tool that generated the code
        itself can hold it, and it unlocks nothing but heavy-geometry access.
        """
        return cls(
            policy=DEFAULT_POLICY,
            source="trusted",
            bypass_config_enabled=bypass_config_enabled,
            dangerous_requested=False,
            heavy_geometry_requested=True,
            dangerous_unmet=("source",),
            heavy_geometry_unmet=(),
        )

    def unmet_audit(self) -> dict[str, list[Requirement]]:
        return {
            "dangerous": list(self.dangerous_unmet),
            "heavy_geometry": list(self.heavy_geometry_unmet),
        }

    def as_audit(self) -> dict[str, bool]:
        return {
            "mutation": self.mutation,
            "dangerous": self.dangerous,
            "heavy_geometry": self.heavy_geometry,
        }


@dataclass(frozen=True)
class DetectedPatterns:
    """Everything the static scan found in the code, whatever the grant says about it.

    The program is parsed once, here; every gate reads the same AST ``exec``
    compiles. A program that does not parse carries its ``syntax_error`` and
    nothing else, because nothing else could be read [LAW:parse-dont-validate].
    """

    code_sha256: str
    mutation: list[str]
    dangerous: list[str]
    heavy_geometry: list[str]
    import_hou: bool
    syntax_error: SyntaxError | None

    @classmethod
    def scan(cls, code: str) -> "DetectedPatterns":
        # [LAW:one-source-of-truth] The program's identity is computed here once;
        # the audit, the worker ledger and every refusal name it from this field.
        code_sha256 = hashlib.sha256(code.encode("utf-8", "replace")).hexdigest()
        try:
            tree = parse_code(code)
        except SyntaxError as exc:
            return cls(
                code_sha256=code_sha256,
                mutation=[],
                dangerous=[],
                heavy_geometry=[],
                import_hou=False,
                syntax_error=exc,
            )
        return cls(
            code_sha256=code_sha256,
            mutation=_detect_non_inspection_code(tree),
            dangerous=_detect_dangerous_code(tree),
            heavy_geometry=_detect_heavy_geometry_code(tree),
            import_hou=_detect_import_hou(tree),
            syntax_error=None,
        )


# [LAW:dataflow-not-control-flow] The approved-bypass trail is always written;
# its level is the grant's source: a request-side bypass is the event the
# WARNING exists for, a server-authored program is routine.
_BYPASS_AUDIT_LEVEL: dict[GrantSource, int] = {
    "request": logging.WARNING,
    "trusted": logging.DEBUG,
}


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
    "source": (
        "A program {operation} wrote is never granted {capability} access; "
        "no request input admits it, so the program is {operation}'s defect."
    ),
}


def _bypass_hint(gate: _BypassGate, unmet: tuple[Requirement, ...], operation: str) -> str:
    steps = (
        _REQUIREMENT_STEP[requirement].format(
            flag=gate.flag, capability=gate.capability, operation=operation
        )
        for requirement in unmet
    )
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
        case "request" | "source":
            return (gate.pattern_reason, gate.found)


def _resolve_undo_primitive(hou: Any) -> Any | None:
    """Return the ``hou.undos`` namespace if a usable undo primitive exists.

    We require both a ``group`` context factory (to wrap execution) and a
    ``performUndo`` callable (to roll back on timeout). If either is missing we
    return None so callers can fail closed instead of silently leaving
    untracked mutations.
    """
    undos = getattr(hou, "undos", None)
    if undos is None:
        return None
    if not callable(getattr(undos, "group", None)):
        return None
    if not callable(getattr(undos, "performUndo", None)):
        return None
    return undos


def _run_code_thread(
    code: str,
    hou: Any,
    stdout_capture: StringIO,
    stderr_capture: StringIO,
    exec_exception: list[BaseException | None],
    exec_traceback: list[str],
) -> None:
    """Execute user code, capturing stdout/stderr and whatever it raised."""
    try:
        exec_globals = {"hou": hou, "__builtins__": builtins.__dict__}
        # [LAW:effects-at-boundaries] Only this thread's writes are captured;
        # other threads keep writing to the streams they always did.
        with captured(stdout_capture, stderr_capture):
            exec(code, exec_globals)
    # Whatever the program raises - SystemExit and KeyboardInterrupt too - is how
    # it ended, not the worker's own failure: the caller reports it.
    except BaseException as e:  # noqa: BLE001 - surfaced to caller as an error result
        exec_exception[0] = e
        exec_traceback[0] = traceback.format_exc()


@dataclass(frozen=True)
class _Diff:
    """What a program changed under ``_DIFF_ROOT``, capped, and which lists the cap cut."""

    changes: dict[str, Any]
    truncated: list[str]


@dataclass(frozen=True)
class _DiffFailed:
    """Why a program that ran has no diff."""

    error: Exception


@dataclass(frozen=True)
class _NeverRan:
    """Why a program never ran: the scene before it could not be read."""

    error: Exception


def _build_audit(
    *,
    code: str,
    grant: ExecutionGrant,
    detected: DetectedPatterns,
    timed_out: bool = False,
    woke_early: int | None = None,
    rollback_attempted: bool = False,
    rollback_succeeded: bool | None = None,
    blocked_reason: str | None = None,
) -> dict[str, Any]:
    """Assemble the structured audit block returned with every response."""
    return {
        "policy": grant.policy,
        "source": grant.source,
        "allow_dangerous_requested": grant.dangerous_requested,
        "allow_heavy_geometry_requested": grant.heavy_geometry_requested,
        "bypass_config_enabled": grant.bypass_config_enabled,
        "granted": grant.as_audit(),
        "unmet": grant.unmet_audit(),
        "dangerous_patterns": detected.dangerous,
        "heavy_geometry_patterns": detected.heavy_geometry,
        "mutation_patterns": detected.mutation,
        "timed_out": timed_out,
        # How often the wait for the program woke before its monotonic deadline;
        # None when the call was refused before anything was waited on.
        "woke_early": woke_early,
        "rollback_attempted": rollback_attempted,
        "rollback_succeeded": rollback_succeeded,
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
        "execute_code blocked: %s policy=%s unmet=%s mutation=%s dangerous=%s heavy=%s",
        reason,
        grant.policy,
        grant.unmet_audit(),
        detected.mutation,
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


@handle_connection_errors("execute_code")
def execute_code(
    code: str,
    capture_diff: bool = False,
    max_stdout_size: int = DEFAULT_MAX_STDOUT_SIZE,
    max_stderr_size: int = DEFAULT_MAX_STDERR_SIZE,
    max_diff_nodes: int = DEFAULT_MAX_DIFF_NODES,
    timeout: int = DEFAULT_TIMEOUT,
    allow_dangerous: bool = False,
    host: str = "localhost",
    port: int = 18811,
    allow_heavy_geometry: bool = False,
    policy: Policy = DEFAULT_POLICY,
    *,
    bypass_config_enabled: bool,
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
      and bypass flags grant nothing) and "normal" (default; a guard rail
      against accidents, not a sandbox).
    - Dangerous operation detection: hou.exit(), hou.hipFile.clear(), file
      state changes (os and tempfile beyond their read side, shutil, pathlib,
      archive and database modules, open() for writing), shell and process
      access, dynamic import/execution, network egress (socket, urllib,
      requests, httpx, http, ftplib, smtplib, ...) and the debugger. Judged on
      the parsed program, so text in comments and strings is not code.
    - Code that does not parse is refused before any rail runs.
    - Heavy geometry guard: Blocks direct node.geometry()/geo.points()/geo.prims() access by default
    - Config+request bypass: allow_dangerous unlocks dangerous patterns and
      allow_heavy_geometry unlocks heavy geometry, each only when the server
      also sets HOUDINI_MCP_ALLOW_BYPASS=true.
    - Output size caps: Prevents massive output from overwhelming the response
    - Execution timeout with rollback: on timeout the run is rolled back via a
      named Houdini undo group when available, else it fails closed.
    - Scene diff limits: Caps the number of nodes returned in scene changes
    - Structured audit block returned on every call.

    Args:
        code: Python code to execute
        capture_diff: If True, captures before/after scene state and returns changes (default: False)
        max_stdout_size: Maximum stdout size in bytes (default: 100000 = 100KB)
        max_stderr_size: Maximum stderr size in bytes (default: 100000 = 100KB)
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
        May include truncation flags (stdout_truncated, stderr_truncated, diff_truncated)
        if output exceeded size limits.
        If dangerous or heavy-geometry patterns are detected and not granted, returns
        error with the detected patterns and unmet_requirements: each of "policy"
        (re-run with policy='normal'), "request" (set the flag), "config" (enable
        HOUDINI_MCP_ALLOW_BYPASS) or "source" (a server-authored program no input
        admits) still short of the grant; the hint asks for exactly these.
        audit.granted reports the one permission decision the run was made under,
        and audit.unmet what each bypass capability still lacked.
        On timeout, a rollback block says whether an undo was attempted and
        whether the scene is known to be consistent.
    """
    grant = ExecutionGrant.resolve(
        policy=policy,
        allow_dangerous=allow_dangerous,
        allow_heavy_geometry=allow_heavy_geometry,
        bypass_config_enabled=bypass_config_enabled,
    )
    return _execute_with_grant(
        code,
        grant,
        operation="execute_code",
        capture_diff=capture_diff,
        max_stdout_size=max_stdout_size,
        max_stderr_size=max_stderr_size,
        max_diff_nodes=max_diff_nodes,
        timeout=timeout,
        host=host,
        port=port,
    )


def _execute_with_grant(
    code: str,
    grant: ExecutionGrant,
    *,
    operation: str,
    capture_diff: bool = False,
    max_stdout_size: int = DEFAULT_MAX_STDOUT_SIZE,
    max_stderr_size: int = DEFAULT_MAX_STDERR_SIZE,
    max_diff_nodes: int = DEFAULT_MAX_DIFF_NODES,
    timeout: int = DEFAULT_TIMEOUT,
    host: str,
    port: int,
) -> dict[str, Any]:
    """Run ``code`` under an already-resolved grant.

    ``execute_code`` is the public door; a server-side tool that generated the
    program itself may enter here with :meth:`ExecutionGrant.trusted_heavy_geometry`.
    ``operation`` is the tool the caller invoked, so a refusal names it.
    """
    logger.debug(
        "execute_code policy=%s source=%s granted=%s config=%s requested dangerous=%s heavy=%s",
        grant.policy,
        grant.source,
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
    if detected.syntax_error is not None:
        # [LAW:no-silent-failure] the same grammar exec would reject; say so here, before any rail.
        exc = detected.syntax_error
        location = f" (line {exc.lineno})" if exc.lineno is not None else ""
        raise _blocked(
            code=code,
            grant=grant,
            detected=detected,
            reason="syntax_error",
            message=f"Code does not parse: {exc.msg}{location}",
            hint="Fix the syntax error; the gates judge the parsed program and refuse one they cannot read.",
            traceback="".join(traceback.format_exception_only(exc)),
        )

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

    if detected.mutation and not grant.mutation:
        raise _blocked(
            code=code,
            grant=grant,
            detected=detected,
            reason="read_only_mutation",
            message=(
                "Blocked under read-only policy: the code names something outside "
                "the inspection allowlist. Use policy='normal' (default) for changes."
            ),
            hint=(
                "read-only admits only recognized inspection (navigation, parameter "
                "reads, flags, wiring, geometry reads, pure stdlib); keep your own "
                "records in dicts, not attributes. Re-run with policy='normal' to "
                "allow anything else."
            ),
            mutation_patterns=detected.mutation,
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
                hint=_bypass_hint(gate, unmet, operation),
                unmet_requirements=list(unmet),
                bypass_requested=gate.requested(grant),
                bypass_config_enabled=grant.bypass_config_enabled,
                **{f"{gate.capability}_patterns": patterns},
            )

    # Emit the durable bypass audit BEFORE execution so a later exception
    # cannot skip the server-side trail. Source code and secrets are excluded.
    if detected.dangerous or detected.heavy_geometry:
        logger.log(
            _BYPASS_AUDIT_LEVEL[grant.source],
            "execute_code approved bypass policy=%s source=%s dangerous=%s heavy=%s code_sha256=%s",
            grant.policy,
            grant.source,
            detected.dangerous,
            detected.heavy_geometry,
            detected.code_sha256,
        )

    # --- Connect and prepare execution --------------------------------------
    with using(host, port) as link:
        hou = link.hou
        undos = _resolve_undo_primitive(hou)

        stdout_capture = StringIO()
        stderr_capture = StringIO()
        exec_exception: list[BaseException | None] = [None]
        exec_traceback: list[str] = [""]
        # With capture_diff, the worker writes this on every way out: the program's
        # diff, why it has none, or why the program never ran. Its first value
        # stands only if the worker died without writing it. Without capture_diff,
        # or once the program raised: None.
        scene: _Diff | _DiffFailed | _NeverRan | Exception | None = (
            RuntimeError(f"{operation}'s worker ended before it wrote the scene diff")
            if capture_diff
            else None
        )
        # The program's clock: timeout is the program's, not the scene reads around it.
        program_starts, program_ends = threading.Event(), threading.Event()

        def diff_since(before: SceneSnapshot, program_took: float) -> _Diff | _DiffFailed:
            # [LAW:one-source-of-truth] The worker is the one writer of its run's
            # last diff, so a program that outlived its caller's timeout still
            # leaves its diff once it ends.
            try:
                changes = scene_diff(before, scene_snapshot(hou, _DIFF_ROOT))
            except Exception as error:  # noqa: BLE001
                # [LAW:no-silent-failure] The program ran; only its diff is missing.
                # [LAW:effects-at-boundaries] Classifying the error is the caller's,
                # which drops this call's link when the link is what failed. A worker
                # that outlived its call has no caller left (houdini-laws-etg.il8).
                logger.warning("%s ran but its scene diff failed: %r", operation, error)
                return _DiffFailed(error)
            lists = ("added", "removed", "modified", "added_nodes")
            over = [key for key in lists if len(changes[key]) > max_diff_nodes]
            logger.info(
                "%s scene diff of %s, program took %.3fs of timeout %ss: "
                "added=%d removed=%d modified=%d truncated=%s",
                operation,
                _DIFF_ROOT,
                program_took,
                timeout,
                *(len(changes[key]) for key in ("added", "removed", "modified")),
                over,
            )
            changes.update({key: changes[key][:max_diff_nodes] for key in lists})
            LAST_SCENE_DIFF.record(changes)
            return _Diff(changes, over)

        def run_code() -> None:
            # [LAW:no-ambient-temporal-coupling] Both reads of the scene happen in the
            # worker, under its ledger slot, so no other program runs between them
            # and the diff is this program's alone. A scene that could not be read
            # first is not written. Every way out, SystemExit included, leaves
            # through the finally, so the caller's clock never waits on a program
            # that is not running.
            nonlocal scene
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
                        scene = _NeverRan(error)
                        return
                started = time.monotonic()
                program_starts.set()
                run_program()
            finally:
                program_starts.set()
                program_ends.set()
                if before is not None:
                    # A program that raised has no diff; its scene is not read again.
                    scene = (
                        None
                        if exec_exception[0] is not None
                        else diff_since(before, time.monotonic() - started)
                    )

        def run_program() -> None:
            # Never retry user code if the undo context fails: __exit__ may raise
            # after the body already mutated the scene, so fallback would execute it
            # twice. Fail closed and surface the context error instead.
            if undos is not None:
                try:
                    with undos.group(_UNDO_GROUP_LABEL):
                        _run_code_thread(
                            code,
                            hou,
                            stdout_capture,
                            stderr_capture,
                            exec_exception,
                            exec_traceback,
                        )
                except Exception as error:  # noqa: BLE001
                    if exec_exception[0] is None:
                        exec_exception[0] = error
                        exec_traceback[0] = traceback.format_exc()
                return
            _run_code_thread(
                code,
                hou,
                stdout_capture,
                stderr_capture,
                exec_exception,
                exec_traceback,
            )

        # [LAW:no-ambient-temporal-coupling] The ledger owns the worker's lifetime: it
        # starts the thread inside this call's hold on the scene, the thread clears
        # the slot once the program, its undo group, and the scene reads around it
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

        # [LAW:one-source-of-truth] The program's own end, set once its undo group
        # has closed, says whether it is still running - not the ledger slot, which
        # the worker holds on through the scene read after it - so a program that
        # ended as the wait expired is reported as complete.
        if not program_ends.is_set():
            # The worker is still inside the undo-group context. Calling performUndo
            # now can undo the PREVIOUS human action because this run's group has not
            # closed yet, while the worker can continue mutating afterward. Never
            # touch the undo stack from this state.
            logger.warning(
                "%s exceeded timeout of %ss; worker code_sha256=%s still running, undo not attempted",
                operation,
                timeout,
                detected.code_sha256,
            )
            raise ProgramStillRunning(
                f"Execution timeout: code did not complete within {timeout} seconds",
                policy=grant.policy,
                stdout=stdout_capture.getvalue()[:max_stdout_size],
                stderr=stderr_capture.getvalue()[:max_stderr_size],
                timeout=timeout,
                warning=(
                    "Execution timed out while its background thread and undo group are still active. "
                    "No undo was attempted because doing so could undo a previous human action. "
                    "Every tool that writes the scene is refused until this worker finishes. "
                    f"{HOW_A_WORKER_ENDS} Scene consistency is unknown: verify state before "
                    "further edits."
                ),
                worker=worker.describe(),
                rollback={
                    "attempted": False,
                    "succeeded": False,
                    "error": None,
                    # True whenever we hit this branch: the thread was still running
                    # at the moment we decided not to roll back, and we have no way to
                    # confirm it has stopped since.
                    "thread_still_running": True,
                    "scene_consistency": "unknown",
                },
                audit=_build_audit(
                    code=code,
                    grant=grant,
                    detected=detected,
                    timed_out=True,
                    woke_early=woke_early,
                    rollback_attempted=False,
                    rollback_succeeded=False,
                ),
            )

        # The program ended; what remains of the worker is the scene read after it.
        worker.thread.join()
        audit = _build_audit(code=code, grant=grant, detected=detected, woke_early=woke_early)

        # --- Exception during execution -----------------------------------------
        if exec_exception[0] is not None:
            stdout_val, stdout_truncated = _truncate_output(
                stdout_capture.getvalue(), max_stdout_size
            )
            stderr_val, stderr_truncated = _truncate_output(
                stderr_capture.getvalue(), max_stderr_size
            )
            error_fields: dict[str, Any] = {
                "traceback": exec_traceback[0],
                "stdout": stdout_val,
                "stderr": stderr_val,
                "policy": grant.policy,
                "audit": audit,
            }
            if stdout_truncated:
                error_fields["stdout_truncated"] = True
            if stderr_truncated:
                error_fields["stderr_truncated"] = True
            # The exception's type leads, for some say nothing else: SystemExit(0) is "0".
            told = traceback.format_exception_only(exec_exception[0])
            raise OperationFailed("".join(told).strip(), **error_fields)

        # [LAW:no-silent-failure] A scene that could not be read before the program
        # is raised: the program never ran on it.
        if isinstance(scene, _NeverRan):
            raise scene.error
        # The worker died without writing what became of the scene.
        if isinstance(scene, Exception):
            raise scene

        # --- Success -------------------------------------------------------------
        stdout_val, stdout_truncated = _truncate_output(stdout_capture.getvalue(), max_stdout_size)
        stderr_val, stderr_truncated = _truncate_output(stderr_capture.getvalue(), max_stderr_size)

        result = {
            "status": "success",
            "stdout": stdout_val,
            "stderr": stderr_val,
            "policy": grant.policy,
            "audit": audit,
        }

        if stdout_truncated:
            result["stdout_truncated"] = True
            result["stdout_warning"] = f"stdout truncated to {max_stdout_size} bytes"
        if stderr_truncated:
            result["stderr_truncated"] = True
            result["stderr_warning"] = f"stderr truncated to {max_stderr_size} bytes"

        if isinstance(scene, _DiffFailed):
            # A diff that failed on the link drops this call's own link, as a
            # failure leaving the scope would.
            lost = failed_on(houdini(), link, scene.error)
            result["scene_changes_error"] = failure_for(lost, operation).model_dump()
        if isinstance(scene, _Diff):
            if scene.truncated:
                result["diff_truncated"] = True
                result["diff_warning"] = (
                    f"{', '.join(scene.truncated)} truncated to {max_diff_nodes} nodes"
                )
            result["scene_changes"] = scene.changes

        if detected.dangerous:
            result["dangerous_patterns_executed"] = detected.dangerous
            result["safety_warning"] = (
                "Code with dangerous patterns was executed with an approved bypass."
            )

        return result


def get_last_scene_diff() -> dict[str, Any]:
    """
    Get the scene diff of the execute_code run with capture_diff=True launched last.

    Shows the nodes under /obj that run added, removed, or replaced by a node of
    another type ("modified"), at any depth. While that run has no diff - still
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
