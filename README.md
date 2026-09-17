# codex-codebuddy-mcp

A minimal Python MCP server that lets Codex control CodeBuddy Code through its
[Agent Client Protocol (ACP)](https://agentclientprotocol.com/) interface.

## Features

- Lazy process start: creating a bridge session does not launch CodeBuddy.
- One CodeBuddy process and ACP session per bridge session.
- Local launch and remote launch over OpenSSH stdio.
- Explicit bridge session IDs with MCP client ownership checks.
- CodeBuddy permission forwarding through MCP elicitation, with a portable two-step fallback.
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

Stores launch configuration and returns a `bridge_session_id`. The CodeBuddy process is not
started until the first `prompt_codebuddy` call.

Local example arguments:

```json
{
  "cwd": "/path/to/project",
  "launch_mode": "local",
  "codebuddy_args": ["--model", "balanced-model", "--permission-mode", "default"]
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
  "codebuddy_args": ["--model", "balanced-model"]
}
```

SSH authentication uses the system `ssh` command, SSH config, and agent. Passwords are not
accepted by this bridge. Values supplied through `env` are forwarded to CodeBuddy but should be
treated as MCP tool input; prefer parent-process or remote host configuration for secrets.

By default the bridge reuses the CodeBuddy login already present on the target machine. If no login
is available it returns an authentication-required error. `auth_method_id` can explicitly request
`external`, `internal`, `iOA`, or `selfhosted` authentication; CodeBuddy may open or wait for its
normal login flow, so use this only when interactive authentication is intended.

### `prompt_codebuddy`

Starts CodeBuddy when needed and sends a text prompt. A completed turn returns the final text,
stop reason, tool summaries, and CodeBuddy session ID.

If the MCP client supports elicitation, permission requests are handled inside the call. Otherwise
the tool returns:

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
CodeBuddy turn and may complete or return another permission request.

### `cancel_codebuddy_turn` and `close_codebuddy_session`

Cancellation keeps the CodeBuddy process available. Closing cancels active work, terminates the
local process or SSH channel, and removes the bridge session.

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
