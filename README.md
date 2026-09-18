# codex-codebuddy-mcp

A minimal Python MCP server that lets Codex control CodeBuddy Code through its
[Agent Client Protocol (ACP)](https://agentclientprotocol.com/) interface.

## Features

- Session creation starts CodeBuddy and establishes the ACP session.
- One CodeBuddy process and ACP session per bridge session.
- Local launch and remote launch over OpenSSH stdio.
- Explicit bridge session IDs with MCP client ownership checks.
- Per-session model switching through ACP.
- At most two active CodeBuddy turns by default across the MCP server.
- Explicit permission approval modes: MCP elicitation or compatible two-step flow.
- Cancellation and deterministic child-process cleanup.

## Requirements

- Python 3.11 or newer
- `uv`
- CodeBuddy Code with ACP support (`codebuddy --acp`)
- OpenSSH when using remote launch

## Install and run

```bash
uv sync
uv run codex-codebuddy-mcp
```

The bridge has built-in defaults. In a source checkout, the repository-level
[`config.yaml`](config.yaml) can override them:

```yaml
max_read: 1048576 # 1 MiB, one ACP stdout/stderr line
max_output: 65536 # 64 KiB, one MCP result
max_concurrency: 2 # simultaneous active prompt turns
approval_mode: elicitation
```

Installed wheels use the built-in defaults unless `CODEX_CODEBUDDY_MCP_CONFIG` names a YAML file.
The `KB`, `MB`, and `GB` suffixes are accepted as binary multiples for backward compatibility;
`max_concurrency` must be a unitless integer. The values can be overridden for an individual
session by passing `max_read` and/or `max_output` to `create_codebuddy_session`.
`max_concurrency` applies globally to all bridge sessions and is loaded when the MCP server starts.

Example Codex MCP configuration:

```toml
[mcp_servers.codebuddy]
command = "uv"
args = [
  "run",
  "--project",
  "/absolute/path/to/codex-codebuddy-mcp",
  "codex-codebuddy-mcp",
]
```

The MCP server writes protocol data to stdout and diagnostics to stderr.

## Tools

### `create_codebuddy_session`

Starts CodeBuddy, establishes the ACP session, and returns both a `bridge_session_id` and the
`codebuddy_session_id`, along with the ACP-reported `model_id` and `model_name`. The model fields
are returned when the session is created, not repeated on every prompt result. The bridge does not
maintain a model allowlist. If CodeBuddy does not report model metadata, those two fields are
`null`, rather than echoing an unverified model argument. If startup or session recovery fails,
this call returns an error and does not leave a usable bridge session behind.

`cwd` is required and sets the working directory for the CodeBuddy session. If it is present but
empty or contains only whitespace, the bridge asks the user to enter a working directory through
MCP elicitation. Clients without elicitation support receive an error that asks the caller to retry
with a non-empty `cwd`.

Local example arguments:

```json
{
  "cwd": "/path/to/project",
  "launch_mode": "local",
  "codebuddy_args": ["--model", "deepseek-v4.1-flash"],
  "permission_mode": "auto",
  "approval_mode": "elicitation",
  "max_read": 4194304,
  "max_output": 65536
}
```

SSH example arguments:

```json
{
  "cwd": "/home/dev/project",
  "launch_mode": "ssh",
  "ssh_host": "dev-box",
  "ssh_args": ["-T"],
  "codebuddy_command": "/usr/local/bin/codebuddy",
  "codebuddy_args": ["--model", "deepseek-v4.1-flash"]
}
```

SSH authentication uses the system `ssh` command, SSH config, and agent. Passwords are not
accepted by this bridge. Values supplied through `env` are forwarded to CodeBuddy but should be
treated as MCP tool input; prefer parent-process or remote host configuration for secrets.

`approval_mode` controls the bridge-to-MCP permission path. `elicitation` is the default and requires
the MCP client to support elicitation; failures are returned as errors. `compatible` explicitly uses
the two-step `permission_required` plus `respond_codebuddy_permission` flow. It does not silently
fall back between modes.

By default the bridge reuses the CodeBuddy login already present on the target machine. If no login
is available it returns an authentication-required error. `auth_method_id` can explicitly request
`external`, `internal`, `iOA`, or `selfhosted` authentication; CodeBuddy may open or wait for its
normal login flow, so use this only when interactive authentication is intended.

`permission_mode` defaults to `auto` and is passed to CodeBuddy as `--permission-mode auto`. The
supported values are `acceptEdits`, `bypassPermissions`, `default`, `plan`, `dontAsk`, and `auto`.
The bridge allows `max_concurrency` active turns at the same time across sessions; turns in one ACP
session remain serialized. `max_read` controls the `asyncio` subprocess stream limit used for
reading each ACP stdout/stderr line. Its YAML default is `1048576` bytes (1 MiB). Increase it when
a single ACP JSON line can be larger; it must be a positive integer. On the first stdout overrun,
the bridge discards that response, cancels the current turn, and keeps the process available so the
caller can request a compressed answer. Changing `max_read` requires creating a new bridge session.
A successful turn clears the overrun count; a second consecutive overrun terminates the process.
Oversized stderr lines are discarded without stopping the session. `max_output` controls the MCP
result threshold and defaults to `65536` bytes (64 KiB).

The concurrency limit applies to active `session/prompt` turns, including turns waiting for a
permission decision. It does not limit the number of started CodeBuddy processes. A third session
may be created and remain ready, but its prompt waits for a turn slot and fails when its timeout
expires. In compatible approval mode, an unanswered permission request is cancelled after the
calling tool's `timeout_seconds`, which releases its global turn slot. Operations within one bridge
session are serialized by that session's lock.

The registry is in memory. Restarting the MCP server loses bridge session IDs, owner bindings,
locks, pending permissions, in-flight buffers, and process handles. Persist the returned
`codebuddy_session_id` if recovery is needed; a new bridge session can pass it as
`resume_session_id`. The bridge cannot discover old CodeBuddy sessions automatically.

### `switch_codebuddy_model`

Switches the model for an existing bridge session between turns:

```json
{
  "bridge_session_id": "...",
  "model_id": "deepseek-v4.1-flash"
}
```

The bridge sends ACP `session/set_model` with the bound CodeBuddy session. CodeBuddy must
acknowledge the change; ACP errors are returned directly and do not change the bridge's recorded
model. A successful call returns `model_id` and `model_name` once, together with both session IDs.
Switching while a prompt or permission request is active is rejected; finish or cancel that turn
first.

### `prompt_codebuddy`

Sends a text prompt to the already-started CodeBuddy session. A completed turn returns the final text,
stop reason, tool summaries, and CodeBuddy session ID.

`max_output` controls the maximum UTF-8 byte size of every prompt result and defaults to `65536`.
When the serialized result is larger, the bridge writes the complete JSON result to a `0600` file in
the local temporary directory and returns `output_path`, `output_bytes`, and
`text_available_in_file=true` instead of embedding the large text in the MCP response. Permission
metadata remains in the compact response so the caller can continue the same turn; the caller can
read the complete result from that path with its local file tools. MCP/JSON-RPC does not define one
universal maximum, but the calling Codex or harness may enforce a per-message limit, so a
conservative `max_output` is useful for long reports. The bridge removes tracked output files when
their bridge session closes or the server shuts down.

For reports that exceed the file threshold, the same bridge session remains available for follow-up
prompts and additional sections.

With `approval_mode="compatible"`, the tool returns:

```json
{
  "status": "permission_required",
  "permission": {
    "request_id": "...",
    "tool_name": "Bash",
    "raw_input": {"command": "..."},
    "options": [
      {"kind": "allow", "name": "Allow", "optionId": "allow"},
      {"kind": "reject", "name": "Deny", "optionId": "deny"}
    ]
  }
}
```

### `respond_codebuddy_permission`

Pass the exact `request_id` and one of the returned `optionId` values. The call continues the same
CodeBuddy turn and may complete or return another permission request. It also accepts `max_output`
with the same file externalization behavior.

### `cancel_codebuddy_turn` and `close_codebuddy_session`

Completing a prompt does not close CodeBuddy. Cancellation keeps the CodeBuddy process available.
Closing cancels active work, terminates the local process or SSH channel, and removes the bridge
session. On SSH, the bridge records a unique remote PID file and performs a second SSH cleanup command
that terminates that process group. The remote shell uses `set -m`, starts CodeBuddy as a monitored
job, and waits for it so the SSH stdio channel stays connected for ACP responses. This handles
CodeBuddy processes that outlive the SSH channel; abrupt network loss or a remote process that
ignores termination cannot be guaranteed by the local client.

## Tests

Run the default suite, including a real installed CodeBuddy ACP handshake:

```bash
uv run pytest
```

The handshake starts the actual `codebuddy-code` binary, validates ACP initialization and login
state handling, and creates an ACP session when the installed CLI is already authenticated. It does
not send a model prompt or consume model quota. It skips only when `codebuddy` is not installed.

An opt-in end-to-end model test is also provided. It sends a real prompt and may consume quota:

```bash
RUN_CODEBUDDY_MODEL_TEST=1 uv run pytest -m model
```

Run static checks:

```bash
uv run ruff check .
```
