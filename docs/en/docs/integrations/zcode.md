---
status: community
title: ZCode
description: Install the PowerContext ZCode plugin and verify recall, Source capture, and MCP Memory operations.
---

# ZCode

`community` · `experimental`

This integration supports the [open-source ZCode CLI](https://github.com/zai-org/ZCode). The official Windows desktop
app version 3.14.3 has also been exercised with a live PowerContext Server and GLM-5.3-Flash:
its Hook and MCP Memory read/write paths worked. Other official releases and desktop Handoff workflows remain unverified.

## Install matching Server and plugin versions

Install the open-source ZCode CLI or the official Windows desktop app. Put Node.js 24 or newer on `PATH` for the Hook.
Use one PowerContext checkout for both Server and plugin so their contracts match:

```bash
powercontext setup zcode --source /path/to/powercontext
```

The `powercontext` command should come from that checkout too; install `powercontext[cli,server]` from it for development.
For a GitHub source, pass `--source owner/repository --ref <git-ref>` using the same commit or release tag as the Server.
Omitting `--source` selects the default PowerContext repository. A moving branch can change between installs; its name
alone does not establish that the two components match.

Setup copies the plugin to `~/.zcode/cli/plugins/powercontext` and adds that path to `plugins.dirs` in the shared
`~/.zcode/cli/config.json`. It preserves unrelated model, provider, and plugin settings. Repeating setup refreshes the
PowerContext-managed copy. On Windows, setup detects the official desktop app at
`%LOCALAPPDATA%\Programs\ZCode\ZCode.exe`. For a source CLI build, point to its built entry first:

```powershell
$env:ZCODE_CLI_BIN = 'C:\path\to\ZCode\apps\zcode-cli\packages\cli\dist\zcode.cjs'
powercontext setup zcode --source 'C:\path\to\powercontext'
```

Fully quit ZCode, including its Windows tray process, and reopen it after setup. Closing the window alone may leave
the old process running with its previous plugin configuration.

## Start the Server and the host

To extract Memory automatically from Sources, configure the Server's Generation model and background processing:

```bash
powercontext config init --output powercontext.env
powercontext config validate --env-file powercontext.env
powercontext server run --env-file powercontext.env
```

Keep the separate Server process running. Explicit `remember_memory` writes and full-text search need neither
Generation nor Embedding. A Server without them can be healthy, but it cannot automatically turn ordinary Sources into
Memory or provide semantic search. See [Configure models and complete Memory](../get-started/configure-models.md).

The Hook and MCP use the same Server URL saved by setup, defaulting to `http://127.0.0.1:8000`. For another endpoint,
rerun `powercontext setup zcode --server-url https://host --source /path/to/powercontext`. Non-loopback plaintext HTTP also requires
`--allow-insecure-http` at setup. Changing `POWERCONTEXT_ZCODE_SERVER_URL` in a launch terminal does not replace the
URL saved by a normally installed plugin.

Prepare an existing Scope before launching ZCode: use the Server default, a persistent ZCode workspace/session binding,
or an explicit `POWERCONTEXT_ZCODE_SCOPE_ID`. The Hook does not create a Scope. See
[Scopes and access](../workflows/scopes-and-access.md). The official desktop app manages its own model credentials;
its GLM Coding Plan API key does not automatically configure PowerContext Server Generation.

`powercontext setup --env-file .env zcode` can read setup parameters from `.env`; it does not cause the ZCode process
to load that file. Model keys and PowerContext authorization variables must be available when the host starts. Keep
secrets out of the plugin directory.

## Diagnose installation and running configuration

```bash
powercontext doctor zcode --json
```

This checks CLI or Windows desktop discovery, plugin registration, Hook files and Node syntax, MCP declaration, and
Server readiness independently. `ok: true` proves those static and read-only checks, not that an already running
ZCode process loaded the latest plugin or performed Scope resolution, injection, or an MCP call. Fully restart ZCode
and check for PowerContext tools in a new session.

| Failed check | What to inspect |
| --- | --- |
| `zcode` | CLI on `PATH`, `ZCODE_CLI_BIN` pointing to a build, or the desktop app at a detectable location. |
| `plugin` | The PowerContext entry in `plugins.dirs` and the plugin's `.zcode-plugin/plugin.json`. |
| `hooks` | `node --version`, `hooks/hooks.json`, and `hooks/user_prompt_submit.mjs`. |
| `mcp` | The installed `.mcp.json` and `powercontext.json` selecting one Server, with matching authorization settings. |
| `server` | The Server listener and `/health/ready`; this check performs no Memory operation. |

## Inspect evidence from an automatic turn

This integration has no in-session `/pc` or `/pc doctor` command like DeepSeek Harness. For an ordinary prompt, the
Hook attempts to resolve a Scope, calls `POST /v1/context/prepare`, and captures the prompt with
`POST /v1/sources/content`. Inspect the Server requests, persisted Source, and the context actually sent to the model;
`doctor zcode` cannot substitute for those observations.

| Stage | Evidence of success | Limit |
| --- | --- | --- |
| Scope | Resolver returns an existing `scope_id` | Without one, injection and capture are skipped for that turn. |
| Prepare | `context/prepare` returns a non-empty `ready` result | `empty` is normal; a correct model answer alone does not prove recall. |
| Capture | `sources/content` returns `202 accepted` and the Source exists on the Server | A Source is evidence, not a Memory entry or proof of extraction. |
| Injection | Model input contains historical context beginning `PowerContext context for this request` | The model may reject it; current instructions and repository state take precedence. |

The Hook derives a stable Source ID from Scope, session, turn, and prompt. If ZCode omits `turnId`, repeated identical
text in one session conservatively reuses a Source ID and cannot distinguish a new submission from a retry. Prompt
capture is enabled by default, while text resembling a secret is not captured automatically. Preparation and capture
are independent: one can succeed when the other fails.

## Verify writes and fresh-session recall

This acceptance procedure writes test evidence. After preparing the Server, plugin, and Scope, use a unique synthetic fact:

1. In a new ZCode session, explicitly ask it to call PowerContext `remember_memory` with an existing `scope_id` and a
   fact such as “The aurora project's validation color is violet-cedar-1457.” Inspect the successful tool result rather
   than a model statement that it remembered something.
2. Search for the code with `POST /v1/memory/search` or ZCode's `search_memory` MCP tool and confirm the Server returns a
   Memory entry. Capturing the prompt as Source does not prove that the explicit write succeeded.
3. Open a fresh ZCode session in the same Scope and ask for the validation color. Also inspect that turn's
   `context/prepare` and injected content. A correct final answer alone could come from the prompt or old chat text.

Official Windows desktop version 3.14.3 has passed live GLM-5.3-Flash checks for Hook injection, persisted Source,
`search_memory` returning an existing entry, and `remember_memory` writing an entry searchable through the Server.
For the open-source CLI, run the plugin and host tests:

```bash
node --test integrations/zcode/plugins/powercontext/tests/plugin.test.mjs
node --test integrations/zcode/plugins/powercontext/tests/host.test.mjs
```

The second command needs `ZCODE_CLI_BIN` pointing to a built CLI. It uses a fake model and Server and does not replace
a live-host acceptance run.

## Understand what the plugin does

The plugin reaches the same PowerContext Server through two paths:

- The `UserPromptSubmit` Hook requests up to 8000 bytes of PreparedContext before the model analyzes a prompt and
  independently captures that prompt as a Source.
- ZCode's native MCP client loads the plugin's `.mcp.json` and exposes explicit Memory and Handoff tools, including
  `search_memory` and `remember_memory`. Desktop Handoff has not yet had a live-host acceptance check.

The Hook selects a Scope through `POWERCONTEXT_ZCODE_SCOPE_ID`, then current session binding, workspace binding, and
finally the Server default. A canonical Git-root or workspace path is hashed into an external binding key; the path
itself is not a Scope ID. For a remote workspace, set `POWERCONTEXT_ZCODE_REMOTE_WORKSPACE=true` and an existing
`POWERCONTEXT_ZCODE_SCOPE_ID` before launching ZCode, so the Hook skips local path inference. MCP tools still need
the correct Scope when called.

## Diagnose MCP tools and the automatic Hook

If PowerContext tools are absent, fully quit and reopen ZCode, then inspect `plugins.dirs`, the installed plugin, and
`.mcp.json`. If a tool is present but fails, inspect its result for Scope, authorization, Server URL, and HTTP errors;
the model's prose is not a tool result. `doctor zcode` checks declarations and does not call `search_memory` or
`remember_memory`.

Automatic Hook failures leave the ZCode conversation running. The Hook writes a redacted
`component=powercontext.zcode`, stage, and code to stderr; whether ZCode displays them depends on its logging setup.

| Code | Meaning and recovery |
| --- | --- |
| `scope_unresolved` | Check explicit Scope, session/workspace binding, or the Server default. |
| `server_unavailable` / `timeout` | Check the Server, saved URL, network, and request latency. |
| `unauthorized` / `forbidden` | Check the complete Authorization header in the running ZCode process and principal permissions. |
| `not_found` / `conflict` | Inspect Scope, route, and business state; a 404 alone does not prove a version mismatch. |
| `invalid_response` / `invalid_server_url` | Check the Server response contract or rerun setup with a valid endpoint. |

An empty recall is normal and injects no error notice. The Hook validates response shape and the 8000-byte bound,
then fails open on read failure. Explicit MCP writes still require checking the tool result.

## Control prompt capture

Capture is on by default. Disable it before starting ZCode, or pass `--no-capture-prompts` at setup:

```powershell
$env:POWERCONTEXT_ZCODE_CAPTURE_PROMPTS = 'false'
```

The environment variable overrides the value saved by setup. Changing an environment variable outside a running
process does not change the current session; restart ZCode. Disabling capture does not disable PreparedContext recall
or explicit MCP tools.

## Connect to an authenticated local Server

When the Server enforces access control, provide the full Authorization header during plugin setup so MCP references
the runtime variable:

```powershell
$env:POWERCONTEXT_ZCODE_AUTHORIZATION = "Bearer $env:POWERCONTEXT_LOCAL_TOKEN"
powercontext setup zcode --source 'C:\path\to\powercontext'
```

The process that starts ZCode must receive the same `POWERCONTEXT_ZCODE_AUTHORIZATION`. The Hook reads the variable,
and MCP expands `${POWERCONTEXT_ZCODE_AUTHORIZATION}`. Do not put the token in `.mcp.json`, `powercontext.json`, or
the Server URL. Rerun setup after enabling authentication to keep the Hook and MCP declarations aligned. See
[Deployment authentication](../operate/deploy-server.md) for Server configuration.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `ZCODE_CLI_BIN` | unset | Select a source CLI build; the official Windows desktop app is auto-detected. |
| `POWERCONTEXT_ZCODE_SCOPE_ID` | unset | Select an existing Scope before session/workspace binding and Server default. |
| `POWERCONTEXT_ZCODE_REMOTE_WORKSPACE` | `false` | Disable local path binding for remote workspaces; requires a Scope ID. |
| `POWERCONTEXT_ZCODE_CAPTURE_PROMPTS` | setup value, `true` by default | Override prompt capture at runtime. |
| `POWERCONTEXT_ZCODE_AUTHORIZATION` | unset | Complete `Bearer <token>` header for Hook and MCP. |

Setup saves the Server URL, non-loopback plaintext HTTP consent, and default capture setting in `powercontext.json`.
Rerun setup and restart ZCode after changing them. `POWERCONTEXT_ZCODE_SERVER_URL` is only a Hook fallback when no
Server URL was saved; it cannot change the MCP endpoint in a normal installation.

## Uninstall

Remove only the PowerContext path from `plugins.dirs` in `~/.zcode/cli/config.json`. Delete
`~/.zcode/cli/plugins/powercontext` only after confirming it contains `.powercontext-owned`. Fully quit and reopen
ZCode. Uninstalling the plugin does not remove Server data or ZCode model configuration.
