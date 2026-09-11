"""Project Omnigent sessions into native Herdr Spaces.

The default projection materializes recent, active, confirmed-online top-level
Omnigent sessions as native Herdr workspaces and keeps already-loaded Spaces
when they disconnect. A picker can materialize any stored session explicitly
with ``--open-session``. Each new Space starts ``omnigent open`` so its pane
owns runner and host recovery.

Projection is one-way: it never mutates, archives, or deletes an existing
Omnigent session, and it never closes a Herdr workspace automatically. The
picker also exposes one explicit create action; creation and Space opening stay
separate so retrying a failed open cannot create a duplicate session.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

# Keep a linked source plugin runnable without an editable package install.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


SOURCE = "plugin:omnigent.sessions"
STATE_VERSION = 1
TOKEN_SESSION = "omnigent_session"
TOKEN_ORIGIN = "omnigent_origin"
TOKEN_STATUS = "omnigent_status"
TOKEN_AGENT = "omnigent_agent"
TOKEN_ATTENTION = "omnigent_attention"
TOKEN_LAUNCHER = "omnigent_launcher_version"
LAUNCHER_VERSION = "open-title-sync-v2"
PINNED_LABEL = "omnigent.pinned"
LEGACY_PROJECT_LABEL = "omni_project"
MIN_PYTHON = (3, 12)


def _default_state_file() -> Path:
    plugin_state = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if plugin_state:
        return Path(plugin_state) / "session-spaces.json"
    state_home = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
    return state_home / "omnigent" / "herdr" / "session-spaces.json"


class SyncError(RuntimeError):
    """The snapshots or a synchronization action could not be completed."""


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    title: str | None
    agent_name: str | None
    status: Literal["idle", "running", "waiting", "failed"]
    pending_elicitations: int
    workspace: str | None
    updated_at: int | None
    runner_id: str | None
    host_id: str | None
    runner_online: bool
    host_online: bool | None
    host_resumable: bool | None
    pinned: bool
    project_id: str | None
    project_name: str | None
    search_snippet: str | None = None

    @classmethod
    def from_api(cls, value: dict[str, Any]) -> SessionSummary:
        session_id = value.get("id")
        status = value.get("status")
        if not isinstance(session_id, str) or not session_id:
            raise SyncError("Omnigent returned a session without a valid id")
        if status not in {"idle", "running", "waiting", "failed"}:
            raise SyncError(f"session {session_id!r} has unknown status {status!r}")
        pending = value.get("pending_elicitations_count", 0)
        updated_at = value.get("updated_at")
        host_online = value.get("host_online")
        host_resumable = value.get("host_resumable")
        labels = value.get("labels")
        legacy_project = (
            _optional_string(labels.get(LEGACY_PROJECT_LABEL))
            if isinstance(labels, dict)
            else None
        )
        return cls(
            session_id=session_id,
            title=_optional_string(value.get("title")),
            agent_name=_optional_string(value.get("agent_name")),
            status=status,
            pending_elicitations=(
                pending if isinstance(pending, int) and not isinstance(pending, bool) else 0
            ),
            workspace=_optional_string(value.get("workspace")),
            updated_at=(
                updated_at
                if isinstance(updated_at, int) and not isinstance(updated_at, bool)
                else None
            ),
            runner_id=_optional_string(value.get("runner_id")),
            host_id=_optional_string(value.get("host_id")),
            runner_online=value.get("runner_online") is True,
            host_online=host_online if isinstance(host_online, bool) else None,
            host_resumable=(host_resumable if isinstance(host_resumable, bool) else None),
            pinned=PINNED_LABEL in labels if isinstance(labels, dict) else False,
            project_id=_optional_string(value.get("project_id")),
            project_name=legacy_project,
            search_snippet=_optional_string(value.get("search_snippet")),
        )

    @property
    def label(self) -> str:
        fallback = self.agent_name or self.session_id
        return _clean_text(self.title or fallback, limit=80)

    @property
    def agent_label(self) -> str:
        return _clean_text(self.agent_name or "Omnigent", limit=80)

    @property
    def project_label(self) -> str | None:
        return self.project_name

    @property
    def active(self) -> bool:
        """Whether the session is doing work or needs the user's attention."""
        return self.status in {"running", "waiting"} or self.pending_elicitations > 0

    @property
    def recovery_hint(
        self,
    ) -> Literal["attach", "wake", "resume", "reconnect", "unbound", "unknown"]:
        """Best picker hint from summary data; ``omnigent open`` stays authoritative."""
        if self.runner_online:
            return "attach"
        if self.host_online is True:
            return "wake"
        if self.host_id is None:
            return "unbound"
        if self.host_online is False and self.host_resumable is True:
            return "resume"
        if self.host_online is False and self.host_resumable is False:
            return "reconnect"
        return "unknown"

    def catalog_record(self) -> dict[str, object]:
        """Picker-facing fields for ranking and filtering."""
        return {
            "id": self.session_id,
            "title": self.title,
            "agent": self.agent_name,
            "status": self.status,
            "active": self.active,
            "attention": self.status == "waiting" or self.pending_elicitations > 0,
            "runner_id": self.runner_id,
            "host_id": self.host_id,
            "runner_online": self.runner_online,
            "host_online": self.host_online,
            "host_resumable": self.host_resumable,
            "recovery_hint": self.recovery_hint,
            "pinned": self.pinned,
            "project_id": self.project_id,
            "project": self.project_label,
            "workspace": self.workspace,
            "updated_at": self.updated_at,
            "search_snippet": self.search_snippet,
        }

    @property
    def herdr_state(self) -> Literal["idle", "working", "blocked", "unknown"]:
        if self.pending_elicitations > 0 or self.status == "waiting":
            return "blocked"
        if self.status == "running":
            return "working"
        if self.status == "idle":
            return "idle"
        return "unknown"


@dataclass(frozen=True)
class WorkspaceSummary:
    workspace_id: str
    label: str
    agent_status: str
    tokens: dict[str, str]
    focused: bool = False
    candidate_pane_id: str | None = None

    @classmethod
    def from_api(cls, value: dict[str, Any]) -> WorkspaceSummary:
        workspace_id = value.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise SyncError("Herdr returned a workspace without a valid id")
        tokens = value.get("tokens")
        return cls(
            workspace_id=workspace_id,
            label=_optional_string(value.get("label")) or workspace_id,
            agent_status=_optional_string(value.get("agent_status")) or "unknown",
            tokens={
                str(key): str(item)
                for key, item in tokens.items()
                if isinstance(key, str) and isinstance(item, str)
            }
            if isinstance(tokens, dict)
            else {},
            focused=value.get("focused") is True,
        )


@dataclass(frozen=True)
class Binding:
    workspace_id: str
    pane_id: str | None
    locally_closed: bool = False
    launcher_started: bool = True
    launcher_version: str | None = None

    @classmethod
    def from_json(cls, value: object) -> Binding:
        if not isinstance(value, dict):
            raise SyncError("binding must be a JSON object")
        workspace_id = value.get("workspace_id")
        pane_id = value.get("pane_id")
        locally_closed = value.get("locally_closed", False)
        # ``attach_started`` is the legacy field. Reading it keeps an
        # existing local state file usable while the launcher token upgrades
        # the pane exactly once.
        launcher_started = value.get("launcher_started", value.get("attach_started", True))
        launcher_version = value.get("launcher_version")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise SyncError("binding has no workspace_id")
        if pane_id is not None and not isinstance(pane_id, str):
            raise SyncError("binding pane_id must be a string or null")
        if not isinstance(locally_closed, bool) or not isinstance(launcher_started, bool):
            raise SyncError("binding lifecycle flags must be booleans")
        if launcher_version is not None and not isinstance(launcher_version, str):
            raise SyncError("binding launcher_version must be a string or null")
        return cls(
            workspace_id,
            pane_id,
            locally_closed,
            launcher_started,
            launcher_version,
        )


ActionKind = Literal[
    "create",
    "adopt",
    "mark_closed",
    "focus",
    "rename",
    "metadata",
    "agent_status",
    "launch_open",
]


@dataclass(frozen=True)
class ReconcileAction:
    kind: ActionKind
    session_id: str
    workspace_id: str | None = None
    pane_id: str | None = None
    launcher_version: str | None = None
    include_launcher_token: bool = True


class StateStore:
    """Small atomic JSON binding store for Herdr workspace bindings."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._document = self._load()

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize one read/reconcile/write transaction across processes."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise SyncError(f"could not open state lock {lock_path}: {exc}") from exc
        with os.fdopen(descriptor):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as exc:
                raise SyncError(f"could not lock state file {self.path}: {exc}") from exc
            try:
                self._document = self._load()
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": STATE_VERSION, "origins": {}}
        try:
            value = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise SyncError(f"could not read state file {self.path}: {exc}") from exc
        if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
            raise SyncError(f"unsupported state file format in {self.path}")
        if not isinstance(value.get("origins"), dict):
            raise SyncError(f"state file {self.path} has no origins object")
        return value

    def bindings(self, origin: str) -> dict[str, Binding]:
        origins = self._document["origins"]
        raw_origin = origins.get(origin, {})
        if not isinstance(raw_origin, dict):
            raise SyncError(f"state for origin {origin} is malformed")
        raw_bindings = raw_origin.get("bindings", {})
        if not isinstance(raw_bindings, dict):
            raise SyncError(f"bindings for origin {origin} are malformed")
        return {
            session_id: Binding.from_json(value)
            for session_id, value in raw_bindings.items()
            if isinstance(session_id, str)
        }

    def save(self, origin: str, bindings: dict[str, Binding]) -> None:
        origins = self._document["origins"]
        origins[origin] = {
            "bindings": {
                session_id: asdict(binding) for session_id, binding in sorted(bindings.items())
            }
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(self._document, indent=2, sort_keys=True) + "\n")
            temporary.replace(self.path)
        except OSError as exc:
            raise SyncError(f"could not save state file {self.path}: {exc}") from exc


class OmnigentClient:
    def __init__(self, base_url: str, max_sessions: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_sessions = max_sessions
        self._project_warning_emitted = False

    def list_recent_sessions(
        self,
        *,
        search_query: str | None = None,
        pinned: bool = False,
        resolve_liveness: bool = True,
    ) -> list[SessionSummary]:
        # Reuse the CLI's credential resolution so this works for both a local
        # server and authenticated Databricks Apps without duplicating auth.
        from omnigent.cli import _host_error_text, _host_http_json

        after: str | None = None
        rows: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        while len(rows) < self.max_sessions:
            params: dict[str, str | int] = {
                "limit": min(1000, self.max_sessions - len(rows)),
                "include_archived": "false",
                "kind": "default",
                "order": "desc",
                "sort_by": "updated_at",
            }
            if search_query:
                params["search_query"] = search_query
            if pinned:
                params["pinned"] = "true"
            if after is not None:
                params["after"] = after
            result = _host_http_json(
                base_url=self.base_url,
                method="GET",
                path="/v1/sessions",
                params=params,
            )
            if result.status_code != 200 or not isinstance(result.body, dict):
                raise SyncError(
                    f"session list failed ({result.status_code}): {_host_error_text(result.body)}"
                )
            page = result.body.get("data")
            if not isinstance(page, list):
                raise SyncError("session list returned a malformed data field")
            rows.extend(item for item in page if isinstance(item, dict))
            has_more = result.body.get("has_more") is True
            cursor = result.body.get("last_id")
            if len(rows) >= self.max_sessions or not has_more:
                break
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise SyncError("session pagination returned an invalid/repeated cursor")
            seen_cursors.add(cursor)
            after = cursor

        selected_rows = rows[: self.max_sessions]
        sessions = [
            SessionSummary.from_api(self._resolve_liveness(row) if resolve_liveness else row)
            for row in selected_rows
        ]
        return self._resolve_project_names(sessions)

    def get_session(self, session_id: str) -> SessionSummary:
        from omnigent.cli import _host_error_text, _host_http_json

        result = _host_http_json(
            base_url=self.base_url,
            method="GET",
            path=f"/v1/sessions/{quote(session_id, safe='')}",
        )
        if result.status_code != 200 or not isinstance(result.body, dict):
            raise SyncError(
                f"session lookup failed ({result.status_code}): {_host_error_text(result.body)}"
            )
        session = SessionSummary.from_api(self._resolve_liveness(result.body))
        return self._resolve_project_names([session])[0]

    def list_create_agents(self) -> list[dict[str, str]]:
        """Return the server's built-in native TUI agents for a picker."""
        return [
            self._create_agent_record(row)
            for row in self._list_agent_rows()
            if self._native_agent_for_row(row) is not None
        ]

    def create_session(self, agent_id: str, *, workspace: Path) -> SessionSummary:
        """Create one native-TUI session on this machine's existing host."""
        from omnigent.cli import (
            _host_error_text,
            _host_http_json,
        )

        workspace = validate_create_workspace(workspace)
        agent_row: dict[str, Any] | None = None
        for row in self._list_agent_rows():
            if row.get("id") == agent_id:
                agent_row = row
                break
        if agent_row is None:
            raise SyncError(f"create agent {agent_id!r} was not found")
        agent = self._native_agent_for_row(agent_row)
        if agent is None:
            raise SyncError(f"agent {agent_id!r} is not a native coding agent")
        # Validate the rest of the picker-facing identity too. This keeps a
        # malformed native row from making a session the picker cannot label.
        self._create_agent_record(agent_row)

        host_id = resolve_existing_host_id()
        if host_id is None:
            raise SyncError(
                "this machine has no existing Omnigent host identity; "
                "connect it with `omnigent host` first"
            )
        hosts_result = _host_http_json(
            base_url=self.base_url,
            method="GET",
            path="/v1/hosts",
        )
        if hosts_result.status_code != 200 or not isinstance(hosts_result.body, dict):
            raise SyncError(
                f"host list failed ({hosts_result.status_code}): "
                f"{_host_error_text(hosts_result.body)}"
            )
        hosts = hosts_result.body.get("hosts")
        if not isinstance(hosts, list):
            raise SyncError("host list returned a malformed hosts field")
        local_hosts = [
            row for row in hosts if isinstance(row, dict) and row.get("host_id") == host_id
        ]
        if not local_hosts:
            raise SyncError(f"this machine's host {host_id!r} is not registered on this server")
        if len(local_hosts) > 1:
            raise SyncError(f"host list returned duplicate rows for {host_id!r}")
        local_host = local_hosts[0]
        if local_host.get("status") != "online":
            raise SyncError(
                f"this machine's host {host_id!r} is not online; "
                "reconnect it before creating a session"
            )

        readiness = local_host.get("configured_harnesses")
        if readiness is not None and not isinstance(readiness, dict):
            raise SyncError(f"host {host_id!r} returned malformed configured_harnesses metadata")
        if isinstance(readiness, dict):
            raw_harness = _optional_string(agent_row.get("harness"))
            harnesses = {agent.harness}
            if raw_harness is not None:
                harnesses.add(raw_harness)
            unavailable = [
                (harness, readiness[harness])
                for harness in sorted(harnesses)
                if harness in readiness
                and (readiness[harness] is False or isinstance(readiness[harness], str))
            ]
            if unavailable:
                harness, reason = unavailable[0]
                raise SyncError(
                    f"host {host_id!r} cannot launch {harness!r} "
                    f"(readiness: {reason!r}); configure that harness first"
                )

        result = _host_http_json(
            base_url=self.base_url,
            method="POST",
            path="/v1/sessions",
            json_body={
                "agent_id": agent_id,
                "host_id": host_id,
                "workspace": str(workspace),
                "labels": agent.presentation_labels,
            },
            host_id=host_id,
            timeout_s=120.0,
        )
        if result.status_code == 0:
            raise SyncError(
                "session creation outcome is unknown because the request failed "
                f"without a response: {_host_error_text(result.body)}. "
                "Refresh/search existing sessions before creating again; "
                "do not retry automatically"
            )
        if result.status_code != 201:
            raise SyncError(
                f"session creation failed ({result.status_code}): {_host_error_text(result.body)}"
            )
        if not isinstance(result.body, dict):
            raise SyncError("session creation returned a malformed response")
        session_id = result.body.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise SyncError("session creation returned no valid session id")
        return SessionSummary.from_api(result.body)

    def send_message(self, session_id: str, prompt: str) -> dict[str, Any]:
        """Submit one initial user message to an already-created session.

        The Sessions API deliberately keeps top-level host creation and first
        dispatch separate: ``initial_items`` would be stored as history before
        the new runner is bound, rather than executed. The caller therefore
        creates once, records the returned session id, and invokes this method
        once. A transport failure has an ambiguous outcome and must never be
        retried automatically because the native TUI may already have accepted
        the prompt.
        """
        from omnigent.cli import _host_error_text, _host_http_json

        if not isinstance(session_id, str) or not session_id.strip():
            raise SyncError("initial message requires a valid session id")
        if not isinstance(prompt, str) or not prompt.strip():
            raise SyncError("initial message from stdin must contain non-whitespace text")

        result = _host_http_json(
            base_url=self.base_url,
            method="POST",
            path=f"/v1/sessions/{quote(session_id, safe='')}/events",
            json_body={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            },
            # New-session messages may wait for a cold host runner and native
            # terminal to become ready before the API acknowledges dispatch.
            timeout_s=120.0,
            # This bridge only sends the kickoff for sessions it created on the
            # local host, so route the request to that host's server replica.
            host_id=resolve_existing_host_id(),
        )
        if result.status_code == 0:
            raise SyncError(
                "initial message outcome is unknown because the request failed "
                f"without a response: {_host_error_text(result.body)}. "
                "The session may already have received the prompt; inspect it "
                "before retrying and do not retry automatically"
            )
        if result.status_code != 202:
            raise SyncError(
                f"initial message failed ({result.status_code}): {_host_error_text(result.body)}"
            )
        if not isinstance(result.body, dict):
            raise SyncError("initial message returned a malformed response")
        if result.body.get("queued") is not True:
            raise SyncError(f"initial message was not queued: {_host_error_text(result.body)}")
        return result.body

    def _list_agent_rows(self) -> list[dict[str, Any]]:
        """Read every built-in-agent page with cursor-loop protection."""
        from omnigent.cli import _host_error_text, _host_http_json

        rows: list[dict[str, Any]] = []
        after: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, str | int] = {"limit": 1000, "order": "asc"}
            if after is not None:
                params["after"] = after
            result = _host_http_json(
                base_url=self.base_url,
                method="GET",
                path="/v1/agents",
                params=params,
            )
            if result.status_code != 200 or not isinstance(result.body, dict):
                raise SyncError(
                    f"agent list failed ({result.status_code}): {_host_error_text(result.body)}"
                )
            page = result.body.get("data")
            if not isinstance(page, list):
                raise SyncError("agent list returned a malformed data field")
            rows.extend(item for item in page if isinstance(item, dict))
            if result.body.get("has_more") is not True:
                return rows
            cursor = result.body.get("last_id")
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise SyncError("agent pagination returned an invalid/repeated cursor")
            seen_cursors.add(cursor)
            after = cursor

    @staticmethod
    def _native_agent_for_row(row: dict[str, Any]) -> Any | None:
        from omnigent.native.native_coding_agents import (
            native_coding_agent_for_agent_name,
            native_coding_agent_for_harness,
        )

        # Empty-session auto-TUI creation is a property of the canonical
        # wrapper agents, not of every custom template sharing their harness.
        # Older servers omitted ``builtin``; accept an absent flag, but never
        # an explicitly non-built-in row.
        if "builtin" in row and row.get("builtin") is not True:
            return None
        native_agent = native_coding_agent_for_agent_name(_optional_string(row.get("name")))
        harness_agent = native_coding_agent_for_harness(_optional_string(row.get("harness")))
        if native_agent is None or harness_agent is None or native_agent.key != harness_agent.key:
            return None
        return native_agent

    @classmethod
    def _create_agent_record(
        cls,
        row: dict[str, Any],
    ) -> dict[str, str]:
        native_agent = cls._native_agent_for_row(row)
        if native_agent is None:
            raise SyncError("agent is not a native coding agent")
        agent_id = _optional_string(row.get("id"))
        name = _optional_string(row.get("name"))
        if agent_id is None or name is None:
            raise SyncError("agent list returned a malformed native agent row")
        return {
            "id": agent_id,
            "name": name,
            "display_name": native_agent.display_name,
            "harness": native_agent.harness,
        }

    def _resolve_project_names(self, sessions: Sequence[SessionSummary]) -> list[SessionSummary]:
        project_ids = {session.project_id for session in sessions if session.project_id}
        if not project_ids:
            return list(sessions)
        names = self._list_project_names()
        return [
            replace(
                session,
                project_name=(
                    names.get(session.project_id, session.project_name)
                    if session.project_id is not None
                    else session.project_name
                ),
            )
            for session in sessions
        ]

    def _list_project_names(self) -> dict[str, str]:
        """Best-effort first-class project names for picker display."""
        from omnigent.cli import _host_error_text, _host_http_json

        result = _host_http_json(
            base_url=self.base_url,
            method="GET",
            path="/v1/projects",
        )
        rows = result.body.get("data") if isinstance(result.body, dict) else None
        if result.status_code != 200 or not isinstance(rows, list):
            if not self._project_warning_emitted:
                print(
                    "project catalog warning: could not resolve first-class project "
                    f"names ({result.status_code}): {_host_error_text(result.body)}; "
                    "using legacy labels where available",
                    file=sys.stderr,
                )
                self._project_warning_emitted = True
            return {}

        names: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            project_id = _optional_string(row.get("id"))
            name = _optional_string(row.get("name"))
            if project_id is not None and name is not None:
                names[project_id] = name
        return names

    def _resolve_liveness(self, row: dict[str, Any]) -> dict[str, Any]:
        runner_online = row.get("runner_online")
        if isinstance(runner_online, bool):
            return row
        resolved = dict(row)
        resolved["runner_online"] = False
        runner_id = row.get("runner_id")
        if not isinstance(runner_id, str) or not runner_id:
            return resolved

        from omnigent.cli import _host_http_json

        host_id = row.get("host_id")
        status_result = _host_http_json(
            base_url=self.base_url,
            method="GET",
            path=f"/v1/runners/{quote(runner_id, safe='')}/status",
            host_id=host_id if isinstance(host_id, str) and host_id else None,
        )
        if status_result.status_code == 200 and isinstance(status_result.body, dict):
            resolved["runner_online"] = status_result.body.get("online") is True
        return resolved


class HerdrClient:
    def __init__(self, executable: str, session: str | None = None) -> None:
        self.executable = str(Path(executable).expanduser())
        self.prefix = [self.executable]
        if session:
            self.prefix.extend(["--session", session])

    def _command(self, *args: str) -> list[str]:
        return [*self.prefix, *args]

    def _run(self, *args: str, expect_json: bool) -> dict[str, Any] | None:
        command = self._command(*args)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise SyncError(f"could not run {command[0]!r}: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise SyncError(
                f"Herdr command failed ({completed.returncode}): {shlex.join(command)}: {detail}"
            )
        if not expect_json:
            return None
        stdout = completed.stdout.strip()
        try:
            value = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise SyncError(f"Herdr command returned invalid JSON: {shlex.join(command)}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("result"), dict):
            raise SyncError(f"Herdr command returned an unexpected response: {value!r}")
        return value

    def _result(self, *args: str, result_type: str) -> dict[str, Any]:
        response = self._run(*args, expect_json=True)
        assert response is not None
        result = response["result"]
        if result.get("type") != result_type:
            raise SyncError(f"Herdr returned {result.get('type')!r}; expected {result_type!r}")
        return result

    def list_workspaces(self) -> list[WorkspaceSummary]:
        result = self._result("workspace", "list", result_type="workspace_list")
        values = result.get("workspaces")
        if not isinstance(values, list):
            raise SyncError("Herdr workspace list has no workspaces array")
        return [WorkspaceSummary.from_api(item) for item in values if isinstance(item, dict)]

    def candidate_pane(self, workspace_id: str, session_id: str) -> str | None:
        result = self._result("pane", "list", "--workspace", workspace_id, result_type="pane_list")
        values = result.get("panes")
        if not isinstance(values, list):
            raise SyncError("Herdr pane list has no panes array")
        candidates = [item for item in values if isinstance(item, dict)]
        for item in candidates:
            agent_session = item.get("agent_session")
            if isinstance(agent_session, dict) and agent_session.get("value") == session_id:
                return _optional_string(item.get("pane_id"))
        pane_ids = sorted(
            pane_id
            for item in candidates
            if (pane_id := _optional_string(item.get("pane_id"))) is not None
        )
        return pane_ids[0] if pane_ids else None

    def create_workspace(
        self,
        session: SessionSummary,
        *,
        base_url: str,
        fallback_cwd: Path,
        focus: bool = False,
    ) -> Binding:
        cwd = fallback_cwd
        if session.workspace:
            candidate = Path(session.workspace).expanduser()
            if candidate.is_dir():
                cwd = candidate
        result = self._result(
            "workspace",
            "create",
            "--cwd",
            str(cwd),
            "--label",
            session.label,
            "--env",
            f"OMNIGENT_SESSION_ID={session.session_id}",
            "--env",
            f"OMNIGENT_SERVER_URL={base_url}",
            "--focus" if focus else "--no-focus",
            result_type="workspace_created",
        )
        workspace = result.get("workspace")
        root_pane = result.get("root_pane")
        if not isinstance(workspace, dict) or not isinstance(root_pane, dict):
            raise SyncError("Herdr workspace creation returned no root pane")
        workspace_id = _optional_string(workspace.get("workspace_id"))
        pane_id = _optional_string(root_pane.get("pane_id"))
        if workspace_id is None or pane_id is None:
            raise SyncError("Herdr workspace creation returned invalid identifiers")
        return Binding(workspace_id, pane_id, launcher_started=False)

    def rename_workspace(self, workspace_id: str, label: str) -> None:
        self._run("workspace", "rename", workspace_id, label, expect_json=True)

    def focus_workspace(self, workspace_id: str) -> None:
        self._run("workspace", "focus", workspace_id, expect_json=True)

    def report_metadata(self, workspace_id: str, tokens: dict[str, str]) -> None:
        args = ["workspace", "report-metadata", workspace_id, "--source", SOURCE]
        for key, value in sorted(tokens.items()):
            args.extend(["--token", f"{key}={value}"])
        self._run(*args, expect_json=False)

    def report_agent(self, pane_id: str, session: SessionSummary) -> None:
        self._run(
            "pane",
            "report-agent",
            pane_id,
            "--source",
            SOURCE,
            "--agent",
            session.agent_label,
            "--state",
            session.herdr_state,
            "--message",
            session.status,
            "--agent-session-id",
            session.session_id,
            expect_json=False,
        )

    def launch_open(
        self,
        pane_id: str,
        workspace_id: str,
        session_id: str,
        *,
        base_url: str,
        omnigent_executable: str,
        initial_label: str,
    ) -> None:
        supervisor = Path(__file__).with_name("session_open_supervisor.py").resolve()
        command = shlex.join(
            [
                sys.executable,
                str(supervisor),
                "--session",
                session_id,
                "--workspace",
                workspace_id,
                "--server",
                base_url,
                "--herdr",
                self.executable,
                "--omnigent",
                omnigent_executable,
                "--initial-label",
                initial_label,
            ]
        )
        self._run("pane", "run", pane_id, command, expect_json=False)


def origin_fingerprint(base_url: str) -> str:
    normalized = base_url.rstrip("/").casefold().encode()
    return hashlib.sha256(normalized).hexdigest()[:16]


def expected_tokens(session: SessionSummary, origin: str) -> dict[str, str]:
    return {
        TOKEN_SESSION: session.session_id[:80],
        TOKEN_ORIGIN: origin,
        TOKEN_STATUS: session.status,
        TOKEN_AGENT: session.agent_label,
        TOKEN_ATTENTION: str(max(0, session.pending_elicitations)),
        TOKEN_LAUNCHER: LAUNCHER_VERSION,
    }


def plan_reconciliation(
    sessions: Sequence[SessionSummary],
    workspaces: Sequence[WorkspaceSummary],
    bindings: dict[str, Binding],
    *,
    origin: str,
    activate: set[str] | frozenset[str] = frozenset(),
) -> list[ReconcileAction]:
    """Produce a non-destructive, deterministic reconciliation plan."""
    workspaces_by_id = {workspace.workspace_id: workspace for workspace in workspaces}
    sessions_by_id = {session.session_id: session for session in sessions}
    if len(sessions_by_id) != len(sessions):
        raise SyncError("Omnigent snapshot contains duplicate session ids")

    token_targets: dict[str, list[WorkspaceSummary]] = {}
    for workspace in workspaces:
        if workspace.tokens.get(TOKEN_ORIGIN) != origin:
            continue
        session_id = workspace.tokens.get(TOKEN_SESSION)
        if session_id and session_id in sessions_by_id:
            token_targets.setdefault(session_id, []).append(workspace)
    duplicates = {
        session_id: values for session_id, values in token_targets.items() if len(values) > 1
    }
    if duplicates:
        details = ", ".join(
            f"{session_id}: {[item.workspace_id for item in values]}"
            for session_id, values in sorted(duplicates.items())
        )
        raise SyncError(f"duplicate managed Herdr workspaces detected ({details})")

    actions: list[ReconcileAction] = []
    for session in sessions:
        binding = bindings.get(session.session_id)
        if binding is not None and binding.locally_closed:
            continue

        workspace: WorkspaceSummary | None = None
        if binding is not None:
            workspace = workspaces_by_id.get(binding.workspace_id)
            if workspace is None:
                actions.append(
                    ReconcileAction(
                        "mark_closed",
                        session.session_id,
                        binding.workspace_id,
                        binding.pane_id,
                    )
                )
                continue
        else:
            matches = token_targets.get(session.session_id, [])
            if matches:
                workspace = matches[0]
                launcher_version = workspace.tokens.get(TOKEN_LAUNCHER)
                actions.append(
                    ReconcileAction(
                        "adopt",
                        session.session_id,
                        workspace.workspace_id,
                        workspace.candidate_pane_id,
                        launcher_version,
                    )
                )
                binding = Binding(
                    workspace.workspace_id,
                    workspace.candidate_pane_id,
                    launcher_started=workspace.candidate_pane_id is not None,
                    launcher_version=launcher_version,
                )
            else:
                actions.append(ReconcileAction("create", session.session_id))
                continue

        assert binding is not None and workspace is not None
        if workspace.label != session.label:
            actions.append(
                ReconcileAction(
                    "rename", session.session_id, workspace.workspace_id, binding.pane_id
                )
            )
        if session.session_id in activate and not workspace.focused:
            actions.append(
                ReconcileAction(
                    "focus",
                    session.session_id,
                    workspace.workspace_id,
                    binding.pane_id,
                )
            )
        wanted_tokens = expected_tokens(session, origin)
        current_launcher = LAUNCHER_VERSION in {
            binding.launcher_version,
            workspace.tokens.get(TOKEN_LAUNCHER),
        }
        legacy_launcher = (
            binding.launcher_started and not current_launcher and TOKEN_SESSION in workspace.tokens
        )
        if legacy_launcher:
            # A running older TUI cannot safely accept an upgrade command.
            # Keep it usable until the Space is explicitly reopened.
            wanted_tokens.pop(TOKEN_LAUNCHER)
        if any(workspace.tokens.get(key) != value for key, value in wanted_tokens.items()):
            actions.append(
                ReconcileAction(
                    "metadata",
                    session.session_id,
                    workspace.workspace_id,
                    binding.pane_id,
                    include_launcher_token=not legacy_launcher,
                )
            )
        # Herdr persists workspace/pane ids across a supervised server restart,
        # but restores fresh shells and intentionally drops metadata tokens.
        restored_shell = (
            binding.launcher_started
            and TOKEN_SESSION not in workspace.tokens
            and TOKEN_ORIGIN not in workspace.tokens
        )
        needs_launch = not binding.launcher_started or restored_shell
        if needs_launch and binding.pane_id:
            actions.append(
                ReconcileAction(
                    "launch_open", session.session_id, workspace.workspace_id, binding.pane_id
                )
            )
        elif binding.pane_id and not _agent_status_matches(
            workspace.agent_status, session.herdr_state
        ):
            actions.append(
                ReconcileAction(
                    "agent_status", session.session_id, workspace.workspace_id, binding.pane_id
                )
            )
    return actions


def hydrate_adoption_panes(
    herdr: HerdrClient,
    workspaces: Sequence[WorkspaceSummary],
    *,
    origin: str,
    session_ids: set[str] | frozenset[str] | None = None,
) -> list[WorkspaceSummary]:
    hydrated: list[WorkspaceSummary] = []
    for workspace in workspaces:
        session_id = workspace.tokens.get(TOKEN_SESSION)
        if (
            workspace.tokens.get(TOKEN_ORIGIN) == origin
            and session_id
            and (session_ids is None or session_id in session_ids)
        ):
            pane_id = herdr.candidate_pane(workspace.workspace_id, session_id)
            workspace = replace(workspace, candidate_pane_id=pane_id)
        hydrated.append(workspace)
    return hydrated


def reopen_bindings(
    requested: set[str],
    bindings: dict[str, Binding],
    workspaces: Sequence[WorkspaceSummary],
) -> bool:
    if not requested:
        return False
    existing = {workspace.workspace_id for workspace in workspaces}
    selected = set(bindings) if "all" in requested else requested
    changed = False
    for session_id in selected:
        binding = bindings.get(session_id)
        if binding is None:
            continue
        if binding.workspace_id not in existing:
            del bindings[session_id]
        elif binding.locally_closed:
            bindings[session_id] = replace(binding, locally_closed=False)
        else:
            continue
        changed = True
    return changed


def select_projection_sessions(
    catalog: Sequence[SessionSummary],
    bindings: dict[str, Binding],
    workspaces: Sequence[WorkspaceSummary],
    *,
    origin: str,
    open_session: str | None,
) -> list[SessionSummary]:
    """Select the bounded loaded/active working set from the stored catalog."""
    managed_workspace_sessions = {
        session_id
        for workspace in workspaces
        if workspace.tokens.get(TOKEN_ORIGIN) == origin
        if (session_id := workspace.tokens.get(TOKEN_SESSION))
    }
    desired_ids = {
        session.session_id for session in catalog if session.runner_online and session.active
    }
    desired_ids.update(bindings)
    desired_ids.update(managed_workspace_sessions)
    if open_session is not None:
        desired_ids.add(open_session)
    return [session for session in catalog if session.session_id in desired_ids]


def merge_session_catalogs(
    *catalogs: Sequence[SessionSummary],
) -> list[SessionSummary]:
    """Merge ordered catalogs while keeping the first copy of each session."""
    merged: list[SessionSummary] = []
    seen: set[str] = set()
    for catalog in catalogs:
        for session in catalog:
            if session.session_id in seen:
                continue
            seen.add(session.session_id)
            merged.append(session)
    return merged


def loaded_session_ids(
    workspaces: Sequence[WorkspaceSummary],
    bindings: dict[str, Binding],
    *,
    origin: str,
) -> set[str]:
    """Return sessions with a real workspace in this Herdr server."""
    workspace_ids = {workspace.workspace_id for workspace in workspaces}
    loaded = {
        session_id
        for workspace in workspaces
        if workspace.tokens.get(TOKEN_ORIGIN) == origin
        if (session_id := workspace.tokens.get(TOKEN_SESSION))
    }
    loaded.update(
        session_id
        for session_id, binding in bindings.items()
        if binding.workspace_id in workspace_ids
    )
    return loaded


def sync_loaded_workspace_titles(
    sessions: Sequence[SessionSummary],
    workspaces: Sequence[WorkspaceSummary],
    bindings: dict[str, Binding],
    *,
    origin: str,
    herdr: HerdrClient,
) -> int:
    """Best-effort title repair for managed Spaces encountered by discovery."""
    workspaces_by_id = {workspace.workspace_id: workspace for workspace in workspaces}
    token_targets: dict[str, set[str]] = {}
    for workspace in workspaces:
        if workspace.tokens.get(TOKEN_ORIGIN) != origin:
            continue
        session_id = workspace.tokens.get(TOKEN_SESSION)
        if session_id:
            token_targets.setdefault(session_id, set()).add(workspace.workspace_id)

    renamed = 0
    for session in sessions:
        if session.title is None:
            continue
        targets = set(token_targets.get(session.session_id, set()))
        binding = bindings.get(session.session_id)
        if (
            targets
            and binding is not None
            and binding.workspace_id in workspaces_by_id
            and binding.workspace_id not in targets
        ):
            continue
        if len(targets) != 1:
            continue
        workspace = workspaces_by_id[targets.pop()]
        if workspace.label == session.label:
            continue
        try:
            herdr.rename_workspace(workspace.workspace_id, session.label)
        except SyncError as exc:
            print(
                f"title sync warning for {session.session_id}: {exc}",
                file=sys.stderr,
            )
            continue
        renamed += 1
    return renamed


def catalog_records(
    sessions: Sequence[SessionSummary],
    *,
    loaded: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    """Build the process boundary consumed by the popup picker."""
    return [
        {**session.catalog_record(), "loaded": session.session_id in loaded}
        for session in sessions
    ]


def session_from_picker_record(value: object, *, expected_id: str) -> SessionSummary:
    """Validate the fresh picker snapshot used by the low-latency open path."""
    if not isinstance(value, dict):
        raise SyncError("picker open snapshot must be a JSON object")
    session_id = _optional_string(value.get("id"))
    if session_id != expected_id:
        raise SyncError("picker open snapshot id does not match --open-session")
    assert session_id is not None
    status = value.get("status")
    if status not in {"idle", "running", "waiting", "failed"}:
        raise SyncError(f"picker open snapshot has unknown status {status!r}")
    updated_at = value.get("updated_at")
    return SessionSummary(
        session_id=session_id,
        title=_optional_string(value.get("title")),
        agent_name=_optional_string(value.get("agent")),
        status=status,
        pending_elicitations=1 if value.get("attention") is True else 0,
        workspace=_optional_string(value.get("workspace")),
        updated_at=(
            updated_at
            if isinstance(updated_at, int) and not isinstance(updated_at, bool)
            else None
        ),
        runner_id=None,
        host_id=None,
        runner_online=value.get("runner_online") is True,
        host_online=None,
        host_resumable=None,
        pinned=value.get("pinned") is True,
        project_id=None,
        project_name=_optional_string(value.get("project")),
    )


def apply_plan(
    actions: Sequence[ReconcileAction],
    *,
    sessions: Sequence[SessionSummary],
    bindings: dict[str, Binding],
    origin: str,
    state_store: StateStore,
    herdr: HerdrClient,
    base_url: str,
    fallback_cwd: Path,
    omnigent_executable: str,
    activate: set[str] | frozenset[str],
    dry_run: bool,
) -> None:
    sessions_by_id = {session.session_id: session for session in sessions}
    for action in actions:
        session = sessions_by_id[action.session_id]
        print(_describe_action(action, session))
        if dry_run:
            continue

        if action.kind == "create":
            binding = herdr.create_workspace(
                session,
                base_url=base_url,
                fallback_cwd=fallback_cwd,
                focus=session.session_id in activate,
            )
            # Persist the identity before the non-idempotent pane launch.  A
            # crash cannot make the next run create a duplicate workspace.
            bindings[session.session_id] = binding
            state_store.save(origin, bindings)
            assert binding.pane_id is not None
            pane_id = binding.pane_id
            herdr.launch_open(
                pane_id,
                binding.workspace_id,
                session.session_id,
                base_url=base_url,
                omnigent_executable=omnigent_executable,
                initial_label=session.label,
            )
            binding = replace(
                binding,
                launcher_started=True,
                launcher_version=LAUNCHER_VERSION,
            )
            bindings[session.session_id] = binding
            state_store.save(origin, bindings)
            herdr.report_metadata(binding.workspace_id, expected_tokens(session, origin))
            herdr.report_agent(pane_id, session)
            continue

        if action.kind == "adopt":
            assert action.workspace_id is not None
            bindings[session.session_id] = Binding(
                action.workspace_id,
                action.pane_id,
                launcher_started=action.pane_id is not None,
                launcher_version=action.launcher_version,
            )
            state_store.save(origin, bindings)
            continue

        binding = bindings.get(session.session_id)
        if binding is None:
            raise SyncError(f"action {action.kind} has no binding for {session.session_id}")
        if action.kind == "mark_closed":
            bindings[session.session_id] = replace(
                binding,
                locally_closed=True,
                launcher_started=False,
                launcher_version=None,
            )
            state_store.save(origin, bindings)
        elif action.kind == "focus":
            herdr.focus_workspace(binding.workspace_id)
        elif action.kind == "rename":
            herdr.rename_workspace(binding.workspace_id, session.label)
        elif action.kind == "metadata":
            tokens = expected_tokens(session, origin)
            if not action.include_launcher_token:
                tokens.pop(TOKEN_LAUNCHER)
            herdr.report_metadata(binding.workspace_id, tokens)
        elif action.kind == "agent_status":
            if binding.pane_id:
                assert binding.pane_id is not None
                herdr.report_agent(binding.pane_id, session)
        elif action.kind == "launch_open":
            if binding.pane_id:
                bindings[session.session_id] = replace(
                    binding,
                    launcher_started=False,
                    launcher_version=None,
                )
                state_store.save(origin, bindings)
                herdr.launch_open(
                    binding.pane_id,
                    binding.workspace_id,
                    session.session_id,
                    base_url=base_url,
                    omnigent_executable=omnigent_executable,
                    initial_label=session.label,
                )
                bindings[session.session_id] = replace(
                    binding,
                    launcher_started=True,
                    launcher_version=LAUNCHER_VERSION,
                )
                state_store.save(origin, bindings)
                herdr.report_metadata(
                    binding.workspace_id,
                    expected_tokens(session, origin),
                )
                herdr.report_agent(binding.pane_id, session)


def synchronize_once(
    *,
    omnigent: OmnigentClient,
    herdr: HerdrClient,
    state_store: StateStore,
    origin: str,
    reopen: set[str],
    open_session: str | None,
    open_session_record: SessionSummary | None = None,
    fallback_cwd: Path,
    omnigent_executable: str,
    dry_run: bool,
) -> int:
    if open_session_record is not None and (
        open_session is None or open_session_record.session_id != open_session
    ):
        raise SyncError("picker open snapshot does not match the requested session")
    catalog = (
        omnigent.list_recent_sessions()
        if open_session is None
        else [open_session_record or omnigent.get_session(open_session)]
    )
    workspaces = hydrate_adoption_panes(
        herdr,
        herdr.list_workspaces(),
        origin=origin,
        session_ids={open_session} if open_session is not None else None,
    )
    bindings = state_store.bindings(origin)
    catalog_by_id = {session.session_id: session for session in catalog}

    if open_session is None:
        sessions = select_projection_sessions(
            catalog,
            bindings,
            workspaces,
            origin=origin,
            open_session=None,
        )
    else:
        # A picker selection is intentionally selection-only. Background/watch
        # passes remain responsible for the active working-set projection.
        sessions = [catalog_by_id[open_session]]

    requested_reopens = set(reopen)
    if open_session is not None:
        requested_reopens.add(open_session)
    if reopen_bindings(requested_reopens, bindings, workspaces) and not dry_run:
        state_store.save(origin, bindings)
    activate = {open_session} if open_session is not None else set()
    actions = plan_reconciliation(
        sessions,
        workspaces,
        bindings,
        origin=origin,
        activate=activate,
    )
    if not actions:
        print(
            f"in sync: {len(sessions)} loaded/active of {len(catalog)} recent Omnigent session(s)"
        )
        return 0
    apply_plan(
        actions,
        sessions=sessions,
        bindings=bindings,
        origin=origin,
        state_store=state_store,
        herdr=herdr,
        base_url=omnigent.base_url,
        fallback_cwd=fallback_cwd,
        omnigent_executable=omnigent_executable,
        activate=activate,
        dry_run=dry_run,
    )
    return len(actions)


def resolve_server_url(explicit: str | None) -> str:
    from omnigent.cli import _load_effective_config, _resolve_attach_server

    config = _load_effective_config()
    configured = config.get("server")
    base_url = _resolve_attach_server(
        explicit, configured if isinstance(configured, str) else None
    )
    if base_url is None:
        raise SyncError("no Omnigent server found; pass --server or start a local server first")
    return base_url.rstrip("/")


def resolve_existing_host_id() -> str | None:
    """Read this machine's validated host identity without creating one."""
    from omnigent.cli import _effective_global_config_path
    from omnigent.host.identity import load_host_identity_if_present

    identity = load_host_identity_if_present(_effective_global_config_path())
    return identity.host_id if identity is not None else None


def validate_create_workspace(path: Path) -> Path:
    """Validate and normalize the local workspace sent to session creation."""
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise SyncError("--cwd must be an absolute path when creating a session")
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise SyncError(f"--cwd does not exist or cannot be resolved: {expanded}: {exc}") from exc
    if not resolved.is_dir():
        raise SyncError(f"--cwd must be an existing directory: {expanded}")
    return resolved


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", help="Omnigent server URL")
    parser.add_argument("--herdr", default="herdr", help="path to the Herdr executable")
    parser.add_argument("--herdr-session", help="optional named Herdr server/session")
    parser.add_argument(
        "--omnigent",
        default=(
            str(REPO_ROOT / ".venv" / "bin" / "omnigent")
            if (REPO_ROOT / ".venv" / "bin" / "omnigent").is_file()
            else "omnigent"
        ),
        help="Omnigent executable placed in each root pane",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=_default_state_file(),
    )
    parser.add_argument(
        "--max-sessions",
        type=int,
        default=20,
        help="maximum number of recent stored sessions to scan (default: 20)",
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        default=Path.cwd(),
        help="fallback pane cwd when the session workspace is not local",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--open-session",
        metavar="SESSION_ID",
        help="materialize/focus one stored session selected by a picker",
    )
    action.add_argument(
        "--list-sessions",
        action="store_true",
        help="print picker-facing session catalog JSON without contacting Herdr",
    )
    action.add_argument(
        "--list-create-agents",
        action="store_true",
        help="print native TUI agents available for new-session creation",
    )
    action.add_argument(
        "--create-session",
        metavar="AGENT_ID",
        help="create one session on this machine; opening is a separate action",
    )
    action.add_argument(
        "--send-message",
        metavar="SESSION_ID",
        help="read one initial prompt from stdin and submit it to an existing session",
    )
    parser.add_argument(
        "--open-session-record-stdin",
        action="store_true",
        help="read the picker's fresh session record from stdin to avoid another server lookup",
    )
    parser.add_argument(
        "--search-query",
        help="server-side title/content search for --list-sessions",
    )
    parser.add_argument(
        "--include-pinned",
        action="store_true",
        help="merge older pinned sessions into an unfiltered picker catalog",
    )
    parser.add_argument(
        "--include-loaded",
        action="store_true",
        help="annotate picker records with current Herdr workspace state",
    )
    parser.add_argument(
        "--skip-liveness",
        action="store_true",
        help="avoid per-session status probes in a discovery catalog",
    )
    parser.add_argument("--watch", action="store_true", help="keep reconciling")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--reopen",
        action="append",
        default=[],
        metavar="SESSION_ID",
        help="recreate a locally closed binding; use 'all' for every binding",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if sys.version_info < MIN_PYTHON:
        required = ".".join(str(part) for part in MIN_PYTHON)
        actual = ".".join(str(part) for part in sys.version_info[:3])
        print(
            f"error: Python {required}+ is required (found {actual}); "
            "run the plugin from an Omnigent environment",
            file=sys.stderr,
        )
        return 2
    if args.max_sessions < 1:
        parser.error("--max-sessions must be at least 1")
    if args.interval <= 0:
        parser.error("--interval must be positive")
    if args.list_sessions and (args.watch or args.open_session or args.reopen):
        parser.error("--list-sessions cannot be combined with projection actions")
    if args.list_create_agents and (args.watch or args.reopen or args.dry_run):
        parser.error("--list-create-agents cannot be combined with projection actions")
    if args.create_session is not None and (args.watch or args.reopen or args.dry_run):
        parser.error("--create-session cannot be combined with projection actions")
    if args.send_message is not None and (args.watch or args.reopen or args.dry_run):
        parser.error("--send-message cannot be combined with projection actions")
    if args.open_session_record_stdin and args.open_session is None:
        parser.error("--open-session-record-stdin requires --open-session")
    if args.open_session_record_stdin and args.watch:
        parser.error("--open-session-record-stdin cannot be combined with --watch")
    if not args.list_sessions and (
        args.search_query or args.include_pinned or args.include_loaded or args.skip_liveness
    ):
        parser.error("picker catalog options require --list-sessions")
    try:
        create_workspace = (
            validate_create_workspace(args.cwd) if args.create_session is not None else None
        )
        message: str | None = None
        if args.send_message is not None:
            try:
                message = sys.stdin.read()
            except OSError as exc:
                raise SyncError(f"could not read initial message from stdin: {exc}") from exc
            if not message.strip():
                raise SyncError("initial message from stdin must contain non-whitespace text")
        open_session_record: SessionSummary | None = None
        if args.open_session_record_stdin:
            try:
                raw_record = sys.stdin.read()
            except OSError as exc:
                raise SyncError(f"could not read picker open snapshot from stdin: {exc}") from exc
            try:
                record_value = json.loads(raw_record)
            except json.JSONDecodeError as exc:
                raise SyncError("picker open snapshot is not valid JSON") from exc
            assert args.open_session is not None
            open_session_record = session_from_picker_record(
                record_value,
                expected_id=args.open_session,
            )
        base_url = resolve_server_url(args.server)
        origin = origin_fingerprint(base_url)
        omnigent = OmnigentClient(base_url, args.max_sessions)
        if args.list_create_agents:
            print(json.dumps(omnigent.list_create_agents(), indent=2, sort_keys=True))
            return 0
        if args.create_session is not None:
            assert create_workspace is not None
            created = omnigent.create_session(
                args.create_session,
                workspace=create_workspace,
            )
            print(
                json.dumps(
                    {**created.catalog_record(), "loaded": False},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.send_message is not None:
            assert message is not None
            response = omnigent.send_message(args.send_message, message)
            print(json.dumps(response, indent=2, sort_keys=True))
            return 0
        if args.list_sessions:
            catalog = omnigent.list_recent_sessions(
                search_query=args.search_query,
                resolve_liveness=not args.skip_liveness,
            )
            if args.include_pinned and not args.search_query:
                catalog = merge_session_catalogs(
                    catalog,
                    omnigent.list_recent_sessions(
                        pinned=True,
                        resolve_liveness=not args.skip_liveness,
                    ),
                )
            loaded: set[str] = set()
            if args.include_loaded:
                herdr = HerdrClient(args.herdr, args.herdr_session)
                workspaces = herdr.list_workspaces()
                state_store = StateStore(args.state_file.expanduser())
                with state_store.locked():
                    bindings = state_store.bindings(origin)
                    sync_loaded_workspace_titles(
                        catalog,
                        workspaces,
                        bindings,
                        origin=origin,
                        herdr=herdr,
                    )
                    loaded = loaded_session_ids(workspaces, bindings, origin=origin)
            print(
                json.dumps(
                    catalog_records(catalog, loaded=loaded),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        state_store = StateStore(args.state_file.expanduser())
        herdr = HerdrClient(args.herdr, args.herdr_session)
        reopen = set(args.reopen)
        open_session = args.open_session
        while True:
            try:
                with state_store.locked():
                    synchronize_once(
                        omnigent=omnigent,
                        herdr=herdr,
                        state_store=state_store,
                        origin=origin,
                        reopen=reopen,
                        open_session=open_session,
                        open_session_record=open_session_record,
                        fallback_cwd=args.cwd.expanduser().resolve(),
                        omnigent_executable=args.omnigent,
                        dry_run=args.dry_run,
                    )
            except SyncError as exc:
                if not args.watch:
                    raise
                print(f"sync warning: {exc}", file=sys.stderr)
            reopen.clear()
            open_session = None
            open_session_record = None
            if not args.watch:
                return 0
            time.sleep(args.interval)
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _clean_text(value: str, *, limit: int) -> str:
    cleaned = " ".join("".join(ch for ch in value if ch.isprintable()).split())
    return (cleaned or "Omnigent")[:limit]


def _agent_status_matches(actual: str, desired: str) -> bool:
    if desired == "idle" and actual in {"idle", "done"}:
        return True
    return actual == desired


def _describe_action(action: ReconcileAction, session: SessionSummary) -> str:
    target = f" -> {action.workspace_id}" if action.workspace_id else ""
    return f"{action.kind}: {session.session_id} ({session.label}){target}"


if __name__ == "__main__":
    raise SystemExit(main())
