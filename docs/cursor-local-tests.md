# Cursor local gateway tests

These tests run the real `cursor-agent-local` terminal through Omnigent's CLI,
host, runner, and HTTP server. A local mock server supplies model replies, so
they need no Cursor account, API key, or paid inference.

The standard `cursor-agent` CLI and the `cursor` SDK harness use Cursor's backend.
This suite covers the separate local-agent build, pinned to
`2026.07.23-e383d2b`. It supports OpenAI-compatible gateways via `--base-url` or
`CURSOR_LOCAL_AGENT_BASE_URL`.

## Run locally

Install `tmux` (`brew install tmux` on macOS or `apt install tmux` on Linux), then
run these commands from the repository root. The installer supports macOS arm64
and Linux x64 and verifies the download's SHA-256 checksum.

```bash
uv sync --locked --extra all --group dev
bash scripts/install_cursor_local_for_tests.sh /tmp/omnigent-cursor-local

OMNIGENT_TEST_CURSOR_LOCAL_PATH=/tmp/omnigent-cursor-local/dist-package/cursor-agent-local \
OMNIGENT_REQUIRE_CURSOR_LOCAL_TESTS=1 \
uv run --no-sync pytest tests/e2e/test_cursor_local_gateway.py -v --timeout=180
```

Expect two passing tests: one supplies the gateway URL as a flag, the other as an
environment variable. Each sends two messages through the same HTTP endpoint as
the web chat, verifies the replies are saved, closes the Cursor terminal, and
resumes the conversation. The third gateway request must contain both previous
replies. The tests isolate Cursor configuration and use a dummy gateway key.

For the related unit tests:

```bash
uv run --no-sync pytest tests/host/test_connect.py tests/cli/test_host_daemon_env.py \
  tests/inner/test_cursor_native_executor.py tests/test_cursor_native_forwarder.py \
  tests/test_cursor_native_bridge.py \
  -k 'cursor or runner_env or host_daemon_env or dispatch_trace' -q
```

## CI and scope

The normal E2E workflow installs the pinned Linux CLI and requires these tests;
missing prerequisites fail the job. Compatibility jobs using an older server or
runner do not install it. Locally, missing prerequisites skip the tests unless
`OMNIGENT_REQUIRE_CURSOR_LOCAL_TESTS=1` is set.

This covers real processes and terminal injection with a mock model backend.
It does not validate the Cursor SDK, paid Cursor models, a real gateway, browser
rendering, or OS sandboxing. Cursor's model picker is also outside this suite:
the local build's `models` command still tries to contact Cursor's backend.

For local-agent experiments, keep the provider key in
`CURSOR_LOCAL_AGENT_API_KEY`, never in CLI arguments. Explicitly forward the
required settings through a newly started host daemon:

```bash
export OMNIGENT_RUNNER_ENV_PASSTHROUGH=OMNIGENT_CURSOR_PATH,CURSOR_CONFIG_DIR,CURSOR_LOCAL_AGENT_API_KEY,CURSOR_LOCAL_AGENT_BASE_URL,AGENT_CLI_CREDENTIAL_STORE
```

The tests configure this themselves. Existing daemons retain their startup
environment, so changing shell variables requires restarting the relevant host.
