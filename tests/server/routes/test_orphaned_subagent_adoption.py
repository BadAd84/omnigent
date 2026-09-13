"""Tests for proactive adoption of a dead runner's orphaned sub-agent children.

A sub-agent child inherits its parent's runner at create time, and the
reactive stale-binding heal fires only when someone messages the child — with
the parent on the same dead runner, nobody ever does, so a runner death used
to strand every mid-turn child until an explicit message arrived. These tests
cover the proactive path: adoption onto an already-live same-owner runner at
reconciliation time, parking plus adoption when the owner's next runner
connects, the adoptable-population filter, and the tombstone status settle on
archive.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes._sessions import orchestration as orch
from omnigent.stores.conversation_store import ConversationNotFoundError


def _conv(
    session_id: str,
    *,
    kind: str = "sub_agent",
    host_id: str | None = None,
    agent_id: str | None = "ag_worker",
    runner_id: str | None = "runner_dead",
    archived: bool = False,
    labels: dict[str, str] | None = None,
    title: str | None = None,
    live_status: str | None = None,
) -> Any:
    """Build a conversation-shaped row exposing the fields adoption reads."""
    return SimpleNamespace(
        id=session_id,
        kind=kind,
        host_id=host_id,
        agent_id=agent_id,
        runner_id=runner_id,
        archived=archived,
        labels=labels or {},
        title=title,
        live_status=live_status,
    )


class _AdoptionStore:
    """Minimal conversation store recording re-binds for the adoption path."""

    def __init__(self, conv: Any = None, owner: str | None = None) -> None:
        self.conv = conv
        self.owner = owner
        self.rebinds: list[tuple[str, str]] = []

    def replace_runner_id(self, conversation_id: str, runner_id: str) -> Any:
        self.rebinds.append((conversation_id, runner_id))
        if self.conv is None:
            raise ConversationNotFoundError(conversation_id)
        self.conv.runner_id = runner_id
        return self.conv

    def get_session_owner(self, conversation_id: str) -> str | None:
        return self.owner

    def get_conversation(self, conversation_id: str) -> Any:
        return self.conv


class _FakeTunnelRegistry:
    """Registry stub mapping online runner ids to their owners."""

    def __init__(self, online: dict[str, str | None]) -> None:
        self._online = online

    def online_runner_ids(self) -> list[str]:
        return list(self._online)

    def runner_owner(self, runner_id: str) -> str | None:
        return self._online.get(runner_id)

    def get(self, runner_id: str) -> Any:
        return object() if runner_id in self._online else None


class _FakeRunnerRouter:
    """Router stub resolving every session to one recorded client."""

    def __init__(self) -> None:
        self.client = object()

    def client_for_session_resources(self, conversation_id: str) -> Any:
        return SimpleNamespace(runner_id="resolved", client=self.client)


@pytest.fixture(autouse=True)
def _clear_orphan_registry() -> Any:
    """Isolate the module-level parked-orphan registry per test."""
    orch._orphaned_subagent_sessions.clear()
    yield
    orch._orphaned_subagent_sessions.clear()


def _capture_relays(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace the relay starter with a recorder; returns the call log."""
    relays: list[tuple[str, str]] = []

    def _record(session_id: str, runner_id: str, client: Any, store: Any = None) -> None:
        relays.append((session_id, runner_id))

    monkeypatch.setattr(orch, "_ensure_runner_relay", _record)
    return relays


@pytest.mark.asyncio
async def test_reconciliation_adopts_orphan_onto_live_same_owner_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-turn orphan is re-bound, initialized, and relayed at once.

    When the dead runner's reconciliation runs while a live runner owned by
    the same user is already online, the orphan must not wait for anything:
    it is adopted immediately instead of staying pinned to the dead runner.
    """
    relays = _capture_relays(monkeypatch)
    conv = _conv("c_adopt_now", live_status="running")
    store = _AdoptionStore(conv=conv, owner=None)
    inits: list[tuple[str, Any]] = []

    async def _init(adopted: Any, client: Any) -> None:
        inits.append((adopted.id, client))

    router = _FakeRunnerRouter()
    await orch._adopt_or_park_orphaned_subagents(
        [conv],
        "runner_dead",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=router,  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry({"runner_live": None}),  # type: ignore[arg-type]
        initialize_session=_init,
    )

    assert store.rebinds == [("c_adopt_now", "runner_live")]
    assert inits == [("c_adopt_now", router.client)]
    assert relays == [("c_adopt_now", "runner_live")]
    assert "c_adopt_now" not in orch._orphaned_subagent_sessions


@pytest.mark.asyncio
async def test_dead_runners_own_id_is_never_the_adoption_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The departed runner may still look online to a stale registry read.

    Adoption must skip the dead runner's own id even if the registry lists
    it, otherwise the orphan is "re-bound" right back onto its dead runner.
    """
    _capture_relays(monkeypatch)
    conv = _conv("c_skip_dead", live_status="running")
    store = _AdoptionStore(conv=conv, owner=None)

    await orch._adopt_or_park_orphaned_subagents(
        [conv],
        "runner_dead",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=_FakeRunnerRouter(),  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry({"runner_dead": None}),  # type: ignore[arg-type]
        initialize_session=None,
    )

    assert store.rebinds == []
    assert "c_skip_dead" in orch._orphaned_subagent_sessions


@pytest.mark.asyncio
async def test_parked_orphan_adopted_when_owners_runner_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No live runner at reconciliation parks the orphan for a later connect.

    The reported incident shape: the orchestrator's runner dies, and the
    user's next runner comes online minutes later under a fresh id. Another
    user's runner must not pick the orphan up; the owner's runner must.
    """
    relays = _capture_relays(monkeypatch)
    conv = _conv("c_parked", runner_id="runner_dead", live_status="running")
    store = _AdoptionStore(conv=conv, owner="alice@example.com")

    await orch._adopt_or_park_orphaned_subagents(
        [conv],
        "runner_dead",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=_FakeRunnerRouter(),  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry({}),  # type: ignore[arg-type]
        initialize_session=None,
    )
    assert store.rebinds == []
    parked = orch._orphaned_subagent_sessions["c_parked"]
    assert parked.dead_runner_id == "runner_dead"
    assert parked.owner == "alice@example.com"

    # Another user's runner connecting leaves the orphan parked.
    await orch._adopt_parked_orphans_onto_connected_runner(
        "runner_bob",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=_FakeRunnerRouter(),  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry(  # type: ignore[arg-type]
            {"runner_bob": "bob@example.com"}
        ),
        initialize_session=None,
    )
    assert store.rebinds == []
    assert "c_parked" in orch._orphaned_subagent_sessions

    # The owner's fresh runner adopts it and the entry is evicted.
    await orch._adopt_parked_orphans_onto_connected_runner(
        "runner_alice_2",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=_FakeRunnerRouter(),  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry(  # type: ignore[arg-type]
            {"runner_alice_2": "alice@example.com"}
        ),
        initialize_session=None,
    )
    assert store.rebinds == [("c_parked", "runner_alice_2")]
    assert relays == [("c_parked", "runner_alice_2")]
    assert "c_parked" not in orch._orphaned_subagent_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda c: setattr(c, "archived", True), "archived"),
        (lambda c: c.labels.update({"omnigent.closed": "true"}), "closed label"),
        (lambda c: setattr(c, "runner_id", "runner_other"), "healed elsewhere"),
    ],
)
async def test_parked_orphan_dropped_when_no_longer_adoptable(
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
    reason: str,
) -> None:
    """A parked entry that went stale is dropped instead of adopted.

    Between parking and the next runner connect the child can be archived,
    tombstoned by ``sys_session_close``, or healed onto another runner; the
    drain must re-validate against the store and never revive those.
    """
    _capture_relays(monkeypatch)
    conv = _conv("c_stale", runner_id="runner_dead")
    store = _AdoptionStore(conv=conv, owner=None)
    orch._orphaned_subagent_sessions["c_stale"] = orch._OrphanedSubagent(
        dead_runner_id="runner_dead", owner=None
    )
    mutate(conv)

    await orch._adopt_parked_orphans_onto_connected_runner(
        "runner_new",
        conversation_store=store,  # type: ignore[arg-type]
        runner_router=_FakeRunnerRouter(),  # type: ignore[arg-type]
        tunnel_registry=_FakeTunnelRegistry({"runner_new": None}),  # type: ignore[arg-type]
        initialize_session=None,
    )

    assert store.rebinds == [], f"stale ({reason}) orphan was adopted"
    assert "c_stale" not in orch._orphaned_subagent_sessions


@pytest.mark.parametrize(
    ("conv", "adoptable"),
    [
        (_conv("a"), True),
        (_conv("b", kind="default"), False),
        (_conv("c", host_id="host_1"), False),
        (_conv("d", agent_id=None), False),
        (_conv("e", archived=True), False),
        (_conv("f", labels={"omnigent.closed": "true"}), False),
        (_conv("g", title="worker:task:closed:conv_g"), False),
    ],
)
def test_adoptable_population_is_plain_open_subagents(conv: Any, adoptable: bool) -> None:
    """Only plain, still-open sub-agent children may be adopted.

    Host-bound sessions have a dedicated respawn path, a top-level session's
    runner is the user-facing process itself, and a closed or archived child
    was ended on purpose — none may be silently repointed at another runner.
    """
    assert orch._subagent_is_adoptable(conv) is adoptable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live_status", "error_code", "orphaned"),
    [
        # Still mid-turn: the reconciliation has not settled it yet.
        ("running", None, True),
        ("waiting", None, True),
        # Already settled failed by whichever watcher won the race (the
        # per-session relay or the disconnect grace) — the persisted cause
        # is what identifies the runner's death.
        ("failed", "runner_disconnected", True),
        ("failed", "runner_failed_to_start", True),
        # A genuine task error is not an orphan and is left alone.
        ("failed", "llm_error", False),
        ("failed", None, False),
        # Idle: the child finished its work before the runner died.
        ("idle", None, False),
        (None, None, False),
    ],
)
async def test_orphan_evidence_is_mid_turn_or_runner_death_failure(
    live_status: str | None,
    error_code: str | None,
    orphaned: bool,
) -> None:
    """Orphan detection must not depend on which watcher settled the status.

    A dead runner's interrupted sessions are flipped to ``failed`` by either
    the per-session relay's give-up branch or the disconnect-grace
    reconciliation, in racy order — so both the still-mid-turn and the
    already-failed-with-runner-death-cause shapes count, while genuine task
    failures and finished work never do.
    """
    labels: dict[str, str] = {}
    if error_code is not None:
        labels = {
            sessions_module._LAST_TASK_ERROR_CODE_LABEL_KEY: error_code,
            sessions_module._LAST_TASK_ERROR_MESSAGE_LABEL_KEY: "the runner went away",
        }
    conv = _conv("c_evidence", live_status=live_status, labels=labels)
    store = _AdoptionStore(conv=conv)

    fresh = await orch._dead_runner_orphaned_subagent(conv, store)  # type: ignore[arg-type]

    assert (fresh is not None) is orphaned


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cached", "live_status", "expected"),
    [
        # A held turn on a dead runner: archive must settle it at once
        # instead of letting the disconnect grace answer "running".
        ("running", None, "idle"),
        ("waiting", None, "idle"),
        # Cache miss falls back to the persisted row value.
        (None, "running", "idle"),
        # An already-settled failure is preserved, not clobbered to idle.
        ("failed", None, "failed"),
    ],
)
async def test_archive_stop_settles_leftover_mid_turn_status(
    monkeypatch: pytest.MonkeyPatch,
    cached: str | None,
    live_status: str | None,
    expected: str,
) -> None:
    """Archiving (the ``sys_session_close`` tombstone) never leaves "running".

    A dead or wedged runner cannot deliver the best-effort stop, and nothing
    else settles the cached mid-turn status until the disconnect grace
    expires — so a tombstoned child kept reporting ``running`` for the full
    grace window. The archive teardown itself must settle it.
    """
    from omnigent.runtime import session_stream

    session_id = "c9e3a7b5f1d84e2b8c6a5d4f3e2b1a09"

    async def _noop_stop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sessions_module, "_best_effort_stop", _noop_stop)
    if cached is not None:
        sessions_module._session_status_cache[session_id] = cached
    store = _AdoptionStore(
        conv=_conv(session_id, runner_id="runner_dead", live_status=live_status)
    )

    try:
        await orch._archive_stop(session_id, store, None, None)  # type: ignore[arg-type]
        assert sessions_module._session_status_cache.get(session_id) == expected
    finally:
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)
        # The settle publish is synchronous, but give any stray task a tick.
        await asyncio.sleep(0)
