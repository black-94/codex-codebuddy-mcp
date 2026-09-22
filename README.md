# harness-acp-mcp

`harness-acp-mcp` exposes local or SSH Agent Client Protocol harnesses to an MCP client.
The MCP process is a thin stdio client; one per-user daemon owns all live sessions.

Supported harness identifiers are `codebuddy`, `agy`, and `codex`. Every session requires a
working directory and model ID. Harness processes are supervised in dedicated process groups, so
closing a session also closes supported child and background processes.

## Install and run

```bash
uv sync
uv run harness-acp-mcp
```

The thin client starts `harness-acp-mcp-daemon` automatically. Running the daemon command directly
is useful for diagnostics; the per-user lock prevents a second daemon from starting.

Codex MCP configuration:

```toml
[mcp_servers.harness_acp]
command = "uv"
args = ["run", "--project", "<bridge-directory>", "harness-acp-mcp"]
```

Copy `config.example.yaml` to the user configuration directory or set
`HARNESS_ACP_MCP_CONFIG` to a local YAML file. Local configuration, logs, sockets, process files,
and the authentication-rate database must not be committed.

## Tools

- `create_session`: launch `codebuddy`, `agy`, or `codex` locally or through SSH.
- `authenticate`: run one advertised ACP authentication method.
- `get_user_info`: return a reliable login boolean and optional account details.
- `set_model`: switch the required session model between turns.
- `prompt`: run a prompt and prefer MCP elicitation for permission and information requests.
- `respond_interaction`: compatibility response when the MCP client lacks elicitation.
- `cancel_turn`: stop the current turn without closing the session.
- `close_session`: close the supervisor and complete harness process group.

Local session example:

```json
{
  "harness": "codex",
  "cwd": "<project-directory>",
  "model_id": "<model-id>",
  "launch_mode": "local",
  "command": "<harness-command>"
}
```

SSH authentication is delegated to the installed SSH client, its agent, and user-owned SSH
configuration. An SSH session additionally requires `ssh_host`; use a local alias or target supplied
at runtime rather than recording deployment details in this repository.

`create_session` returns process-group launch metadata plus a `session_record_id`. The daemon writes
a private `0600` record outside the repository. A later `create_session` may pass that value as
`resume_record_id`; the bridge resolves the saved harness session ID and requests ACP `session/load`.
The caller still supplies `harness`, `cwd`, and `model_id`, so reconnecting never silently selects a
working directory or model. Environment values and argument values are not persisted; only
environment names and the argument count are recorded.

Model identifiers remain harness-specific. In particular, current `codex-acp` releases may require
the `model[effort]` form; callers must pass the exact identifier accepted by the selected harness.

## Authentication and interactions

An unauthenticated `create_session` returns `authentication_required` while retaining the initialized
ACP process. `authenticate` is serialized per harness and target. Frequency limiting is configurable
and may be disabled, but same-target serialization always remains enabled to prevent concurrent
credential-state writes. Authentication timeout closes the entire session process group.

When the MCP client supports elicitation, permission and structured information requests are shown
through elicitation even if the harness itself uses a two-message request/response protocol. Without
elicitation, `prompt` or `authenticate` returns `interaction_required`; the caller supplies the
answer with `respond_interaction`. This status means an application-level request/response pause,
not two-factor authentication. The bridge never silently approves a permission request.

## Process lifecycle

Local harnesses run in their own process groups. A separate session supervisor watches its daemon
control pipe; daemon loss, harness exit, explicit close, or timeout triggers group-wide TERM followed
by KILL. SSH wrappers enable shell job control, record the actual remote process-group ID, install exit
traps, and perform a second cleanup connection when needed.

Options that intentionally detach a harness, including background and terminal-multiplexer modes,
are rejected. A third-party process that deliberately creates a new session outside the managed
group is outside the supported lifecycle contract.

## Tests

```bash
uv run pytest -m "not real_codebuddy and not real_codex and not model"
uv run ruff check .
```

Real-harness tests are separately marked because interactive authentication, model prompts, and
approved commands can have account or machine side effects. No test performs an account-usage reset.
The real tests take model identifiers from `HARNESS_ACP_REAL_CODEBUDDY_MODEL` and
`HARNESS_ACP_REAL_CODEX_MODEL`; model and permission turns additionally require their explicit
`RUN_HARNESS_ACP_REAL_MODEL_TEST` or `RUN_HARNESS_ACP_REAL_PERMISSION_TEST` opt-in flag.

## Repository privacy

Tracked files must not contain private deployment configuration, real account names, machine names,
network addresses, home-directory paths, or personal project and file names. Documentation uses
placeholders; tests use temporary paths and generated identifiers. Run the tracked-content privacy
scan before publishing changes.
