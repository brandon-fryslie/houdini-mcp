"""Transaction safety, policy coverage, checkpoints, conflicts, and audit tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from houdini_mcp.tools.transactions import (
    MUTATING_TOOL_POLICIES,
    TOOL_POLICIES,
    TransactionConflict,
    TransactionManager,
    TransactionPolicy,
)


def manager(mock_connection, tmp_path: Path, revision):
    return TransactionManager(
        mock_connection,
        revision,
        checkpoint_dir=tmp_path / "checkpoints",
        audit_path=tmp_path / "transactions.jsonl",
        checkpoint_retention=2,
    )


@pytest.mark.parametrize(
    ("policy", "writes_scene", "waits_for_worker", "needs_transaction"),
    [
        (TransactionPolicy.READS_ONLY, False, False, False),
        (TransactionPolicy.READS_IN_PROGRAM, False, True, False),
        (TransactionPolicy.UNDOABLE, True, True, True),
        (TransactionPolicy.CHECKPOINT_REQUIRED, True, True, True),
        (TransactionPolicy.EXTERNAL_SIDE_EFFECT, False, False, True),
    ],
)
def test_policy_says_what_a_tool_does_to_the_world(
    policy: TransactionPolicy, writes_scene: bool, waits_for_worker: bool, needs_transaction: bool
):
    """A file writer (screenshot, save) has an effect to recover but never writes the
    scene, so it may run beside a program still mutating it; a read through a program
    waits for that program without needing a transaction."""
    assert (policy.writes_scene, policy.waits_for_worker, policy.needs_transaction) == (
        writes_scene,
        waits_for_worker,
        needs_transaction,
    )


def test_mutating_tools_are_exactly_the_classified_tools_with_an_effect_to_recover():
    """[LAW:one-source-of-truth] TOOL_POLICIES classifies every served tool (held by
    tests/plugin/test_server_surface.py); the mutating set is derived from it."""
    derived = {tool: policy for tool, policy in TOOL_POLICIES.items() if policy.needs_transaction}
    assert derived == MUTATING_TOOL_POLICIES
    assert derived


def test_begin_refuses_a_reading_policy(mock_connection, tmp_path):
    """A transaction under a reading policy would checkpoint nothing and undo nothing."""
    with pytest.raises(ValueError, match="reading policy"):
        manager(mock_connection, tmp_path, ["before"]).begin(
            ["create_node"], policy=TransactionPolicy.READS_ONLY
        )


def test_failed_multistep_transaction_closes_group_then_undoes(mock_connection, tmp_path):
    revision = ["before"]
    tx_manager = manager(mock_connection, tmp_path, lambda: revision[0])

    with (
        pytest.raises(ValueError, match="step two"),
        tx_manager.begin(["create_node", "set_parameter"], affected_paths=["/obj/geo1"]),
    ):
        revision[0] = "mutated"
        raise ValueError("step two failed")

    assert mock_connection.undos.open_groups == []
    assert mock_connection.undos.performed_undos == 1
    assert tx_manager.history[-1].result == "rolled_back"
    assert tx_manager.active is None


def test_undo_refuses_intervening_human_edit(mock_connection, tmp_path):
    revision = ["before"]
    tx_manager = manager(mock_connection, tmp_path, lambda: revision[0])
    with tx_manager.begin(["set_parameter"], affected_paths=["/obj/geo1"]):
        revision[0] = "agent-after"

    revision[0] = "human-after"
    with pytest.raises(TransactionConflict, match="changed after"):
        tx_manager.undo_last_agent_action()
    assert mock_connection.undos.performed_undos == 0


def test_undo_last_agent_action_when_revision_matches(mock_connection, tmp_path):
    revision = ["before"]
    tx_manager = manager(mock_connection, tmp_path, lambda: revision[0])
    with tx_manager.begin(["set_node_position"], affected_paths=["/obj/geo1"]):
        revision[0] = "agent-after"

    record = tx_manager.undo_last_agent_action()
    assert record.result == "undone"
    assert mock_connection.undos.performed_undos == 1


def test_checkpoint_is_atomic_and_retention_bounded(mock_connection, tmp_path, monkeypatch):
    tx_manager = manager(mock_connection, tmp_path, lambda: "scene")

    def save_backup(path):
        Path(path).write_text("hip", encoding="utf-8")

    mock_connection.hipFile.saveAndBackup.side_effect = save_backup
    for index in range(3):
        with tx_manager.begin(
            ["load_scene"],
            policy=TransactionPolicy.CHECKPOINT_REQUIRED,
            affected_paths=[f"scene-{index}"],
        ):
            pass

    files = list((tmp_path / "checkpoints").glob("mcp-*.hip"))
    assert len(files) == 2
    assert not list((tmp_path / "checkpoints").glob("*.tmp"))


def test_external_side_effect_is_not_claimed_rolled_back(mock_connection, tmp_path):
    tx_manager = manager(mock_connection, tmp_path, lambda: "scene")
    with (
        pytest.raises(RuntimeError),
        tx_manager.begin(["save_scene"], affected_paths=["/tmp/output.hip"]) as tx,
    ):
        tx.add_external_side_effect("saved /tmp/output.hip")
        raise RuntimeError("post-render failure")

    record = tx_manager.history[-1]
    assert record.result == "failed"
    assert record.side_effects == ["saved /tmp/output.hip"]
    assert mock_connection.undos.performed_undos == 0


def test_audit_contains_attribution_paths_duration_and_result(mock_connection, tmp_path):
    tx_manager = manager(mock_connection, tmp_path, lambda: "rev")
    with tx_manager.begin(
        ["connect_nodes"],
        affected_paths=["/obj/a", "/obj/b"],
        caller="mcp-agent",
        session_id="session-42",
    ):
        pass

    row = json.loads((tmp_path / "transactions.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["transaction_id"]
    assert row["caller"] == "mcp-agent"
    assert row["session_id"] == "session-42"
    assert row["tools"] == ["connect_nodes"]
    assert row["affected_paths"] == ["/obj/a", "/obj/b"]
    assert row["duration_ms"] >= 0
    assert row["result"] == "committed"


def test_policy_uses_most_conservative_tool(mock_connection, tmp_path):
    tx_manager = manager(mock_connection, tmp_path, lambda: "rev")
    tx = tx_manager.begin(["create_node", "save_scene"])
    assert tx.record.policy == TransactionPolicy.EXTERNAL_SIDE_EFFECT


def test_rollback_failure_is_audited_as_unknown_consistency(mock_connection, tmp_path):
    tx_manager = manager(mock_connection, tmp_path, lambda: "rev")
    mock_connection.undos.performUndo = lambda: (_ for _ in ()).throw(RuntimeError("undo failed"))
    with pytest.raises(RuntimeError, match="undo failed"), tx_manager.begin(["create_node"]):
        raise ValueError("body failed")
    assert tx_manager.history[-1].result == "unknown_consistency"
    assert tx_manager.history[-1].rollback_error == "undo failed"


def test_undo_refuses_when_agent_label_is_not_stack_top(mock_connection, tmp_path):
    revision = ["before"]
    tx_manager = manager(mock_connection, tmp_path, lambda: revision[0])
    with tx_manager.begin(["set_parameter"]):
        revision[0] = "after"
    mock_connection.undos.closed_groups.append("Human edit")
    with pytest.raises(TransactionConflict, match="not the next undo"):
        tx_manager.undo_last_agent_action()
    assert mock_connection.undos.performed_undos == 0


def test_undo_never_runs_while_group_is_open(mock_connection, tmp_path):
    tx_manager = manager(mock_connection, tmp_path, lambda: "rev")
    with tx_manager.begin(["create_node"]), pytest.raises(TransactionConflict, match="active"):
        tx_manager.undo_last_agent_action()
    assert mock_connection.undos.performed_undos == 0
