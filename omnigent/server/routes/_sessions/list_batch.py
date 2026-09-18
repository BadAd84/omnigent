"""Coalesce concurrent session-list row reads into one batched database pass.

``WS /v1/sessions/updates`` keeps each client's sidebar rows fresh by
re-reading its watch-set on a fixed interval, and each read fans out across
six stores (grants, admin flag, conversations, agent names, child ids,
comment fingerprints, liveness). That is a constant cost *per connected
client*, so the deployment-wide cost grows with the number of open browsers:
thousands of idle tabs re-reading a couple of dozen rows each spend thousands
of round-trips a second discovering that nothing changed.

This module collapses that fan-out. Callers hand their ids to
:class:`SessionListRowLoader`, which holds a batch open for a short window,
unions everything that arrives, and issues ONE read for the whole batch.
Each waiter then builds its own rows from the shared snapshot, so per-client
output is unchanged while the database sees one read per window instead of
one per client.

Sharing a read across users is safe because the batch is a pure by-id
lookup: :class:`SessionListSources` carries raw rows and grants, and every
caller still applies its own access filter to them.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnigent.entities import CommentsFingerprint, Conversation
    from omnigent.entities.permission import SessionPermission
    from omnigent.server.routes._sessions.helpers import SessionLiveness

# How long a batch stays open for more ids to join. Invisible next to the
# sidebar's multi-second rescan interval, yet long enough that the
# independently-timed rescans of many connected clients land in one batch.
_BATCH_WINDOW_S: float = 0.05

# Conversation-id cap for one batch. A batch that reaches it flushes
# immediately instead of waiting out the window, so a burst of large
# watch-sets can't grow a single ``IN`` list without bound.
_BATCH_MAX_IDS: int = 2000


@dataclass(frozen=True)
class SessionListSources:
    """Every by-id input a session-list row is built from.

    One instance is shared by all waiters in a batch, so it may cover more
    ids and users than any single caller asked for. Callers index it by
    their own ids and ignore the rest.

    :param grants: Permission grants per conversation id. Empty when
        permissions are disabled.
    :param admins: The subset of the batch's users that are admins.
    :param conversations: Conversation rows per id; ids with no row are
        absent.
    :param agent_names: Agent display names per agent id.
    :param child_ids: Direct sub-agent child ids per parent conversation id.
    :param comments: Comment change-detection fingerprints per conversation
        id; conversations without comments are absent.
    :param liveness: Runner/host liveness per conversation id. Empty when
        this replica cannot compute liveness.
    """

    grants: dict[str, list[SessionPermission]]
    admins: frozenset[str]
    conversations: dict[str, Conversation]
    agent_names: dict[str, str]
    child_ids: dict[str, list[str]]
    comments: dict[str, CommentsFingerprint]
    liveness: dict[str, SessionLiveness]


SourcesReader = Callable[[list[str], list[str]], SessionListSources]
"""Blocking read of :class:`SessionListSources` for (conversation ids, user ids)."""


@dataclass
class _Batch:
    """One open batch: the ids joined so far and everyone awaiting it."""

    conversation_ids: set[str] = field(default_factory=set)
    user_ids: set[str] = field(default_factory=set)
    waiters: list[asyncio.Future[SessionListSources]] = field(default_factory=list)
    # Set when the batch fills, so its flush stops waiting out the window.
    full: asyncio.Event = field(default_factory=asyncio.Event)
    # True once the batch is committed to reading — late joiners open a new one.
    closed: bool = False


class SessionListRowLoader:
    """Batch concurrent session-list reads into one database pass.

    :param read: Blocking reader invoked once per batch with the union of
        the batch's conversation ids and user ids. Runs in a worker thread;
        it should take a single pooled connection for the whole read (see
        :func:`~omnigent.db.utils.shared_read_scope`) so a batch costs one
        checkout rather than one per store.
    :param window_s: How long a batch stays open for more ids to join.
    :param max_ids: Conversation-id count that flushes a batch early.
    """

    def __init__(
        self,
        read: SourcesReader,
        *,
        window_s: float = _BATCH_WINDOW_S,
        max_ids: int = _BATCH_MAX_IDS,
    ) -> None:
        self._read = read
        self._window_s = window_s
        self._max_ids = max_ids
        self._open: _Batch | None = None
        # Strong refs to in-flight flushes: asyncio only holds weak
        # references to running tasks, so an unreferenced one can vanish.
        self._flushes: set[asyncio.Task[None]] = set()

    async def load(
        self,
        conversation_ids: Iterable[str],
        user_ids: Iterable[str] = (),
    ) -> SessionListSources:
        """Read the sources for *conversation_ids*, batched with concurrent calls.

        :param conversation_ids: Conversation ids this caller needs rows
            for, e.g. ``["conv_abc123", "conv_def456"]``.
        :param user_ids: Users whose admin flag this caller needs, e.g.
            ``["alice@example.com"]``. Empty for a caller with no identity
            to resolve.
        :returns: Sources covering at least the requested ids — possibly
            more, when other callers joined the same batch.
        :raises Exception: Whatever the reader raised, re-raised into every
            waiter of the failed batch.
        """
        future: asyncio.Future[SessionListSources] = asyncio.get_running_loop().create_future()
        # There is no await between the lookup and the mutations below, so
        # get-or-create is race-free on a single event loop.
        batch = self._open
        if batch is None or batch.closed:
            batch = _Batch()
            self._open = batch
            flush = asyncio.create_task(self._flush(batch), name="session-list-batch")
            self._flushes.add(flush)
            flush.add_done_callback(self._flushes.discard)
        batch.conversation_ids.update(conversation_ids)
        batch.user_ids.update(user_ids)
        batch.waiters.append(future)
        if len(batch.conversation_ids) >= self._max_ids:
            batch.full.set()
        return await future

    async def _flush(self, batch: _Batch) -> None:
        """Hold *batch* open for the window, then read it and settle its waiters."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(batch.full.wait(), self._window_s)
        batch.closed = True
        if self._open is batch:
            self._open = None
        try:
            sources = await asyncio.to_thread(
                self._read,
                sorted(batch.conversation_ids),
                sorted(batch.user_ids),
            )
        except asyncio.CancelledError:
            # Shutdown: nobody will read this batch, so release its waiters
            # rather than leaving them parked on a future that never settles.
            for waiter in batch.waiters:
                if not waiter.done():
                    waiter.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 — re-raised into every waiter
            # Every waiter re-raises it and owns its own retry; the failure is
            # reported here rather than left on an unretrieved task.
            for waiter in batch.waiters:
                if not waiter.done():
                    waiter.set_exception(exc)
            return
        for waiter in batch.waiters:
            if not waiter.done():
                waiter.set_result(sources)
