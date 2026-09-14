# Codex native lifecycle diagnostics

Runner-owned Codex sessions have two separate lifetimes: the app-server owns
execution, while an auxiliary tmux terminal displays the TUI. Losing the TUI
does not by itself prove the app-server died. These records expose the two
lifetimes through the existing debug-log pipeline; no new sink is required.

## Correlation

Lifecycle metadata is in `attributes` (`MAP<STRING,STRING>` in managed logs).
Null values are omitted; booleans serialize as `"True"` / `"False"`.

| Field | Meaning |
| --- | --- |
| `session_id` (column) | Explicit owning session, including a child sharing its parent's runner |
| `runner_id` | Safe runner identity; joins runner-tunnel events |
| `app_server_instance_id` | Unique per start, retained after close; joins app-server, forwarder and TUI records |
| `app_server_pid` | PID within that process lifetime; not a globally unique key |
| `terminal_instance_id` | SHA-256 of the private tmux socket path; joins launch, loss and orphan cleanup without exporting the path |
| `terminal_lifecycle` | Actual `required` / `auxiliary` relationship, or `unknown` outside the resource registry |
| `codex_thread_id` | Thread adopted by this forwarder; later thread rotation can change the live thread |

Use instance IDs, not just session IDs or PIDs, to separate retries and
replacement processes. Reusing a terminal preserves its original app-server
association. Ownership-transfer events identify the old and new sessions.

Orphan-reaper events have `emitting_context=orphan_reaper`: their runner and
ambient session identify the **reaper**, not the victim. Join the victim's
earlier records by `terminal_instance_id`; `recorded_owner_pid` is the dead
owner from the existing on-disk marker.

## Events and interpretation

`event_name=codex_native_lifecycle`:

- App-server phases: `starting`, `spawned`, `ready`, `startup_failed`,
  `teardown_requested`, `process_exited`, `closed`.
  `ready` means app-server readiness, not TUI authentication or thread
  readiness. `startup_failed` can occur before a process is spawned and does
  not guarantee cleanup; existing startup cleanup boundaries are unchanged.
- Runner phases: `launch_failed`, `terminal_launched`,
  `thread_discovery_started`, `thread_discovered`, `thread_discovery_failed`,
  `forwarder_started`, `forwarder_stopped`, `forwarder_cleanup`.
- `app_server_state`, `app_server_returncode`, `app_server_exit_signal`,
  `app_server_lifetime_ms`, and `codex_cli_version` describe the process.
  Exit observation polls the local process handle, not tmux or another process.
- `app_server_exit_expected=True` means teardown intent was recorded before
  the observed exit. It does **not** mean the session succeeded: a discovery
  timeout can cause a deliberate process termination. A process already dead
  when cleanup begins retains `False`. A signal alone does not prove OOM.
- `teardown_reason` keeps the first recorded teardown intent; `reason`
  describes the local event.
  For example, a stopped forwarder can have `reason=forwarder_cancelled` and
  `teardown_reason=session_deleted`.
- `error_type` records the exception class, without its message.
  Discovery distinguishes `thread_discovery_timeout` from
  `thread_stream_ended`. Login-gated waits have `login_required=True` and
  `discovery_has_deadline=False`.
- `forwarder_stage` distinguishes `thread_discovery`, `bridge_setup`, and
  `forwarding`; `forwarder_mode` is `fresh` or `resume`.
- `cleanup_target_matches_owner=False` identifies cleanup that found a
  different registry entry. `app_server_instance_id` still names the original
  forwarder's process, while `cleanup_app_server_instance_id` / PID name the
  actual cleanup target. Instrumentation does not change this cleanup policy.

Teardown reasons include `session_deleted`, `runner_shutdown`,
`required_terminal_exit`, `idle_pane_reap`, `forwarder_replaced`,
`session_teardown`, `caller_requested`, `startup_failed`, `startup_cancelled`,
the discovery reasons above, and `forwarder_returned` / `forwarder_failed` /
`forwarder_cancelled`. Launch failures distinguish `resume_preload_*`,
`event_client_connect_*`, and `terminal_launch_*` (`failed` or `cancelled`).

`event_name=terminal_lifecycle`:

- `launch_started`, `launched`, `observed`, `ownership_transferred`,
  `close_requested`, and `closed` describe terminal ownership and cleanup.
- Existing terminal-loss ERRORs get phase `unavailable`; retained-pane exits
  get INFO phase `pane_exited`. Both watcher implementations report the same
  fields, with `watcher=async` or `threaded`.
- `probe_signature` / `probe_errno` describe cached capture-probe failures;
  `session_probe_signature` / `session_probe_errno` describe the confirmatory
  session probe. `probe_start_failed` distinguishes inability to start a
  probe from a missing tmux server. Missing evidence stays unknown.
- `pane_exit_status` is included only when observed. `pane_output_captured`
  and `pane_output_chars` record availability, never the captured text.
- `close_reason` distinguishes replacement, launch failure/race cleanup,
  conversation cleanup, registry shutdown, observed exit and explicit close.
- `orphan_reap_requested` / `orphan_reap_finished` identify each swept socket.
  The latter records `socket_present`, `kill_server_attempted`, optional
  `kill_server_returncode` or `kill_server_error_type` / `kill_server_errno`,
  and `private_dir_removed`. Directory removal alone does not prove a
  successful tmux kill.

New breadcrumbs are INFO; existing warning/error levels and messages remain
unchanged. New structured fields do not include prompts, pane contents,
stderr, command arguments, paths, credentials, or endpoint URLs. Existing
unstructured diagnostics are not expanded or sanitized by this change.

## Inspect one bounded session timeline

Replace `debug_logs` with the configured managed log relation. Bind the
workspace, owning session, and a narrow half-open timestamp window:

```sql
SELECT client_time, log_id, event_name, level,
       attributes['phase'] AS phase,
       attributes['runner_id'] AS runner_id,
       attributes['app_server_instance_id'] AS app_server_instance_id,
       attributes['terminal_instance_id'] AS terminal_instance_id,
       attributes['reason'] AS reason,
       attributes['teardown_reason'] AS teardown_reason,
       attributes['close_reason'] AS close_reason,
       attributes['app_server_state'] AS app_server_state,
       attributes['app_server_returncode'] AS app_server_returncode,
       attributes['app_server_exit_expected'] AS app_server_exit_expected,
       attributes['error_type'] AS error_type,
       attributes['probe_signature'] AS probe_signature
FROM debug_logs
WHERE workspace_id = :workspace_id
  AND session_id = :session_id
  AND client_time >= :window_start AND client_time < :window_end
  AND event_name IN ('codex_native_lifecycle', 'terminal_lifecycle')
ORDER BY client_time, log_id
LIMIT 500;
```

For orphan sweeps, query the same bounded window by `terminal_instance_id`
instead of victim `session_id`, since the reaper may belong to another runner.
The timeline is capped at 500 rows; narrow the window or paginate if it fills.
For aggregate failure analysis, deduplicate `log_id`, group by instance, and
bound each instance's observations to its own launch. Do not count every
breadcrumb as a failure or attach a later error to every earlier session
attempt. Keep an unknown bucket: old versions, abrupt runner loss, and
unflushed logs can still leave incomplete evidence.

## Verify locally

Run the focused offline tests from a development environment (install the
dependencies in `CONTRIBUTING.md` first if the checkout's environment is incomplete):

```bash
uv run --no-sync pytest tests/test_codex_native_lifecycle.py tests/runner/test_codex_lifecycle_logging.py -q
```

For a manual check, launch this checkout with `omnidev`, open its displayed
UI URL, create a disposable Codex session, send a short message, then delete
that session. If testing managed logs in an authorized development deployment,
use the query above: expect one app-server instance across startup/discovery,
`session_deleted` before process exit, and `app_server_exit_expected=True`.
Repeat with a routed Codex child and verify the rows name the child session,
not the runner's parent. Local plain-text logs show the phase messages; the
offline serialization tests verify the managed attribute payloads.
