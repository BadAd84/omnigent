"""E2E regression: a lease-teardown-cancelled call must not fail requests as unhandled 500s.

Journey (Databricks agentbricks embedding): a client's request is in flight
over a server route backed by a blocking MAS gRPC call on a barnacle-leased
channel. Another client cancels its request, the barnacle lease covering the
channel is released, and the endpoint tears down the connection with GOAWAY
("Cancelling all calls"). The in-flight call raises
``grpc._channel._InactiveRpcError`` with ``StatusCode.UNAVAILABLE`` and
details ``"Cancelling all calls"`` — a *different* status than the
``StatusCode.CANCELLED`` shape already booked as transient upstream — so it
escapes the route into the server's generic catch-all, which answers the
still-connected client with a raw 500 ``internal_error`` and books::

    Unhandled exception: <_InactiveRpcError of RPC that terminated with:
        status = StatusCode.UNAVAILABLE
        details = "Cancelling all calls" ...

at ERROR level (category UNKNOWN, impact BLOCKING).

The reproduction stands in for that deployment: a real in-process gRPC server
holds the call open until the test — acting as the lease manager — revokes the
lease by stopping the backend without grace, which sends the GOAWAY the real
teardown sends. An ``extra_routers`` router (the embedding extension point
agentbricks uses to mount its MAS routers) performs the blocking call inside a
request handler, matching the deployed stack tail
(``with_call`` → ``_end_unary_response_blocking`` → ``_InactiveRpcError``).

The test drives the journey over real HTTP against a real uvicorn server and
asserts the guarded contract: the client still gets an error response, but not
the uncoded 500 ``internal_error``, and the teardown-cancelled call — an
expected lease-lifecycle condition — is not logged through
``_handle_unhandled_exception`` as an ERROR-level ``Unhandled exception``.

Run::

    .venv/bin/python -m pytest tests/e2e/test_lease_teardown_unavailable_not_unhandled.py -v
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Iterator
from concurrent import futures
from pathlib import Path
from typing import NamedTuple

import httpx
import pytest
import uvicorn
from fastapi import APIRouter

# The reported failure is a grpcio client error escaping a route; without
# grpcio there is nothing to reproduce.
grpc = pytest.importorskip("grpc", reason="grpcio is required to raise the teardown-cancelled RPC")

from omnigent.runtime import init as init_runtime  # noqa: E402
from omnigent.runtime.agent_cache import AgentCache  # noqa: E402
from omnigent.server.app import create_app  # noqa: E402
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore  # noqa: E402
from omnigent.stores.artifact_store.local import LocalArtifactStore  # noqa: E402
from omnigent.stores.conversation_store.sqlalchemy_store import (  # noqa: E402
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore  # noqa: E402
from tests.e2e.helpers import HEALTH_TIMEOUT_S, POLL_INTERVAL_S  # noqa: E402

# The gRPC method the deployed stack tail names (MAS tree listing via barnacle).
_GRPC_SERVICE = "mas.TreeService"
_GRPC_METHOD = "ListTreeNodeChildren"


class _LeasedBackend(NamedTuple):
    """A holding gRPC backend plus the controls the lease-manager test needs."""

    target: str
    server: object
    call_in_flight: threading.Event
    release: threading.Event


class _RecordingHandler(logging.Handler):
    """Capture log records emitted by the server's app logger."""

    def __init__(self, records: list[logging.LogRecord]) -> None:
        """
        :param records: Shared list the handler appends every record to.
        """
        super().__init__()
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        """
        Append the record for later assertions.

        :param record: The emitted log record.
        """
        self._records.append(record)


@pytest.fixture()
def leased_backend() -> Iterator[_LeasedBackend]:
    """
    Start a real gRPC backend that holds every call open until released.

    Stands in for the MAS backend reached over a barnacle-leased channel: the
    call stays in flight until the lease teardown (the test stopping the
    backend without grace) cancels it with GOAWAY "Cancelling all calls", the
    exact ``StatusCode.UNAVAILABLE`` shape the deployed teardown injects.

    :returns: The backend's target, server handle, and coordination events.
    """
    call_in_flight = threading.Event()
    release = threading.Event()

    def _hold(request: bytes, context: grpc.ServicerContext) -> bytes:
        """
        Signal the call is in flight, then hold it open until released.

        :param request: Raw request payload (unused).
        :param context: Servicer context (unused; teardown cancels the call).
        :returns: An empty payload if the hold is released before teardown.
        """
        del request, context
        call_in_flight.set()
        release.wait(timeout=30)
        return b""

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                _GRPC_SERVICE,
                {_GRPC_METHOD: grpc.unary_unary_rpc_method_handler(_hold)},
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield _LeasedBackend(
            target=f"127.0.0.1:{port}",
            server=server,
            call_in_flight=call_in_flight,
            release=release,
        )
    finally:
        release.set()
        server.stop(grace=None)


def _make_embedding_router(target: str) -> tuple[APIRouter, grpc.Channel]:
    """
    Build a deployment-style router backed by a blocking leased gRPC call.

    Mirrors how the agentbricks embedding mounts MAS routes through
    ``create_app(extra_routers=...)``: the handler performs a synchronous
    ``with_call`` over the leased channel (the exact frame in the deployed
    stack tail) and lets any ``RpcError`` escape into the server's exception
    handling, as the deployed router does.

    :param target: ``host:port`` of the backing gRPC service.
    :returns: The router and the channel (for teardown).
    """
    channel = grpc.insecure_channel(target)
    list_children = channel.unary_unary(
        f"/{_GRPC_SERVICE}/{_GRPC_METHOD}",
        request_serializer=lambda payload: payload,
        response_deserializer=lambda payload: payload,
    )
    router = APIRouter()

    @router.get("/workspace-tree/children")
    def tree_children() -> dict[str, list[str]]:
        """
        List workspace-tree children via the backing leased gRPC service.

        :returns: The (empty) children listing when the backend answers.
        """
        response, _call = list_children.with_call(b"")
        del response
        return {"children": []}

    return router, channel


@pytest.fixture()
def embedded_server(
    db_uri: str,
    tmp_path: Path,
    leased_backend: _LeasedBackend,
) -> Iterator[tuple[str, list[logging.LogRecord]]]:
    """
    Run a real omnigent server with an embedding router over the leased backend.

    Builds the app exactly as a deployment does — real stores, plus an
    ``extra_routers`` entry whose handler calls the holding gRPC service — and
    serves it with uvicorn on a real socket, capturing everything the
    ``omnigent.server.app`` logger emits (the logger that books unhandled
    exceptions).

    :param db_uri: Per-test database URI from the root conftest.
    :param tmp_path: Pytest temp directory for artifacts and cache.
    :param leased_backend: The holding gRPC backend standing in for barnacle.
    :returns: ``(base_url, records)`` — the server URL and captured records.
    """
    agent_store = SqlAlchemyAgentStore(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")
    init_runtime(
        conversation_store=conversation_store,
        agent_store=agent_store,
        agent_cache=agent_cache,
        file_store=file_store,
        artifact_store=artifact_store,
    )
    router, channel = _make_embedding_router(leased_backend.target)
    app = create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        agent_cache=agent_cache,
        extra_routers=[(router, "/v1/mas", ["mas"])],
    )

    records: list[logging.LogRecord] = []
    handler = _RecordingHandler(records)
    app_logger = logging.getLogger("omnigent.server.app")
    app_logger.addHandler(handler)

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    _wait_until_serving(base_url)
    try:
        yield base_url, records
    finally:
        app_logger.removeHandler(handler)
        server.should_exit = True
        thread.join(timeout=15)
        channel.close()


def _wait_until_serving(base_url: str) -> None:
    """
    Block until the server answers HTTP (any status) or the boot budget lapses.

    :param base_url: Server base URL to probe.
    """
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base_url}/health", timeout=2.0)
        except httpx.HTTPError:
            time.sleep(POLL_INTERVAL_S)
            continue
        return
    raise RuntimeError(f"Server did not come up within {HEALTH_TIMEOUT_S}s")


def test_lease_teardown_cancelled_call_is_not_booked_as_unhandled_500(
    embedded_server: tuple[str, list[logging.LogRecord]],
    leased_backend: _LeasedBackend,
) -> None:
    """
    A lease-teardown-cancelled backing call must not surface as an unhandled 500.

    Drives the reconstructed journey: a client's listing request is in flight
    over the leased channel when the lease is revoked (following a client
    cancellation) and the endpoint tears down with GOAWAY "Cancelling all
    calls". The still-connected client must get an error it can act on — not
    the uncoded 500 ``internal_error`` — and the teardown-cancelled call must
    not be booked through ``_handle_unhandled_exception`` as an ERROR-level
    ``Unhandled exception``.

    :param embedded_server: Base URL of the running server plus the records
        captured from the ``omnigent.server.app`` logger.
    :param leased_backend: The holding gRPC backend standing in for barnacle.
    """
    base_url, records = embedded_server
    result: dict[str, object] = {}

    def _request() -> None:
        """Perform the listing request and stash the outcome for assertions."""
        response = httpx.get(f"{base_url}/v1/mas/workspace-tree/children", timeout=30.0)
        result["status"] = response.status_code
        result["body"] = response.text

    client = threading.Thread(target=_request)
    client.start()
    assert leased_backend.call_in_flight.wait(timeout=15), "backing call never went in flight"

    # Lease revoked while the call is in flight: the backend's graceless stop
    # sends the GOAWAY ("Cancelling all calls") the real teardown sends.
    leased_backend.server.stop(grace=None)
    client.join(timeout=30)
    assert not client.is_alive(), "request never completed after the lease teardown"

    # The journey still ends in an observable error the client can act on —
    # not the catch-all's uncoded internal_error 500.
    status = result["status"]
    assert isinstance(status, int) and status >= 400
    try:
        error_code = json.loads(str(result["body"]))["error"]["code"]
    except (json.JSONDecodeError, KeyError, TypeError):
        error_code = None
    assert (status, error_code) != (500, "internal_error"), (
        "lease-teardown-cancelled call failed the client's request as a raw "
        f"internal_error 500: {result['body']!r}"
    )

    unhandled = [
        record
        for record in records
        if record.levelno >= logging.ERROR
        and record.funcName == "_handle_unhandled_exception"
        and record.getMessage().startswith("Unhandled exception:")
        and "Cancelling all calls" in record.getMessage()
    ]
    assert not unhandled, (
        "lease-teardown-cancelled gRPC call was booked as an unhandled session error: "
        + unhandled[0].getMessage().splitlines()[0]
    )
