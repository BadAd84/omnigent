"""Tests for the coalescing session-list row loader.

The loader exists so the sidebar's per-client rescans stop costing one
multi-store read each: concurrent callers must share a single read of the
union of their ids. These tests drive :class:`SessionListRowLoader` with a
recording reader so the *number of reads* — the property the sidebar's
scalability depends on — is asserted directly, alongside the failure and
cancellation paths that a shared read introduces.
"""

from __future__ import annotations

import asyncio

import pytest

from omnigent.server.routes._sessions.list_batch import (
    SessionListRowLoader,
    SessionListSources,
)


def _sources(conversation_ids: list[str], user_ids: list[str]) -> SessionListSources:
    """Build a marker payload recording exactly what the reader was asked for."""
    return SessionListSources(
        grants={cid: [] for cid in conversation_ids},
        admins=frozenset(user_ids),
        conversations={},
        agent_names={},
        child_ids={},
        comments={},
        liveness={},
    )


class _RecordingReader:
    """Reader that records each call's arguments and returns marker sources."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], list[str]]] = []

    def __call__(self, conversation_ids: list[str], user_ids: list[str]) -> SessionListSources:
        """Record the batch's ids and return sources covering them."""
        self.calls.append((conversation_ids, user_ids))
        return _sources(conversation_ids, user_ids)


async def test_concurrent_loads_share_one_read() -> None:
    """Callers that overlap in time are served by a single reader call.

    This is the scalability property: N connected clients rescanning
    together must cost one read of their union, not N reads.
    """
    reader = _RecordingReader()
    loader = SessionListRowLoader(reader, window_s=0.05)

    results = await asyncio.gather(
        loader.load(["conv_a", "conv_b"], ["alice@example.com"]),
        loader.load(["conv_b", "conv_c"], ["bob@example.com"]),
        loader.load(["conv_c"], ["alice@example.com"]),
    )

    assert len(reader.calls) == 1
    conversation_ids, user_ids = reader.calls[0]
    # The batch reads the union once, deduplicated and ordered.
    assert conversation_ids == ["conv_a", "conv_b", "conv_c"]
    assert user_ids == ["alice@example.com", "bob@example.com"]
    # Every waiter gets the shared snapshot, which covers its own ids.
    for result in results:
        assert set(result.grants) == {"conv_a", "conv_b", "conv_c"}
    assert results[0] is results[1] is results[2]


async def test_loads_after_the_window_start_a_new_batch() -> None:
    """A caller arriving after a batch closes gets its own read."""
    reader = _RecordingReader()
    loader = SessionListRowLoader(reader, window_s=0.01)

    await loader.load(["conv_a"], ["alice@example.com"])
    await loader.load(["conv_b"], ["alice@example.com"])

    assert [ids for ids, _ in reader.calls] == [["conv_a"], ["conv_b"]]


async def test_full_batch_flushes_without_waiting_out_the_window() -> None:
    """Reaching the id cap reads immediately instead of holding the batch."""
    reader = _RecordingReader()
    # A window far longer than the test's patience: only the cap can flush it.
    loader = SessionListRowLoader(reader, window_s=30.0, max_ids=3)

    result = await asyncio.wait_for(
        loader.load(["conv_a", "conv_b", "conv_c"], ["alice@example.com"]),
        timeout=5.0,
    )

    assert len(reader.calls) == 1
    assert set(result.grants) == {"conv_a", "conv_b", "conv_c"}


async def test_read_failure_reaches_every_waiter() -> None:
    """A failed batch raises in all its waiters, so each retries on its own.

    The sidebar ticker treats a read failure as "skip this tick"; a waiter
    left parked on an unsettled future would instead stall that client's
    updates for good.
    """
    calls = 0

    def _boom(conversation_ids: list[str], user_ids: list[str]) -> SessionListSources:
        """Fail the batch the way a dropped database connection would."""
        nonlocal calls
        calls += 1
        del conversation_ids, user_ids
        raise RuntimeError("database is gone")

    loader = SessionListRowLoader(_boom, window_s=0.05)

    outcomes = await asyncio.gather(
        loader.load(["conv_a"], ["alice@example.com"]),
        loader.load(["conv_b"], ["bob@example.com"]),
        return_exceptions=True,
    )

    assert calls == 1
    assert [str(outcome) for outcome in outcomes] == ["database is gone"] * 2
    # A later caller is unaffected by the failed batch.
    reader = _RecordingReader()
    healthy = SessionListRowLoader(reader, window_s=0.01)
    assert await healthy.load(["conv_c"], [])


async def test_cancelled_waiter_does_not_disturb_its_batch() -> None:
    """One client disconnecting mid-read still lets the others be served."""
    reader = _RecordingReader()
    loader = SessionListRowLoader(reader, window_s=0.05)

    doomed = asyncio.create_task(loader.load(["conv_a"], ["alice@example.com"]))
    survivor = asyncio.create_task(loader.load(["conv_b"], ["bob@example.com"]))
    # Let both join the same open batch, then drop one before it flushes.
    await asyncio.sleep(0)
    doomed.cancel()

    result = await asyncio.wait_for(survivor, timeout=5.0)

    with pytest.raises(asyncio.CancelledError):
        await doomed
    assert len(reader.calls) == 1
    assert set(result.grants) == {"conv_a", "conv_b"}
