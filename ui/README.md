# Aster panel UI

The production artifact is `coordination/ui/aster-panel.html`. It contains the UI,
CSS, and the pinned MCP Apps browser SDK in one document. There are no external
fonts, scripts, styles, assets, direct API calls, or credentials.

## Build

From this directory, run `npm ci --ignore-scripts --no-audit --no-fund`, then
`npm run build`. The lockfile pins the complete dependency graph. The backend
serves the generated document as `text/html;profile=mcp-app` and owns authentication.

The app registers `ontoolresult` before `connect()`. It renders the opening
snapshot without issuing a duplicate read. User refresh calls
`aster_panel_refresh({limit:40})`; task selection calls
`aster_panel_task({task_id})`. Both go through `App.callServerTool` with its object
argument. No task mutations are exposed. Chat handoff uses `App.sendMessage`
only following a button click and a host capability check.

## Local integration checks

Run `npm test` with Playwright available locally or through
`CODEX_PRIMARY_RUNTIME_NODE_MODULES`. The test launches a loopback fixture host
and exercises the actual bundled SDK in an iframe. It checks initialization,
refresh, filtering, task details, explicit handoff, unsupported capabilities,
unavailable data, retained state on errors, untrusted text, and responsive widths.
Screenshots and the results JSON go to `ui/test-artifacts/` and use synthetic data.
Set `ASTER_TEST_BROWSER` to a local Chrome/Chromium executable when the default
Playwright browser is not installed.
These checks do not contact Amsterdam or prove that ChatGPT has registered the
live entrypoints.

## Data contract

Opening/refresh data uses `schema_version: "aster.panel.v1"`, `generated_at`,
`viewer`, `counts`, `tasks`, `agents`, and `sources` with `coordination`, `archive`,
and `costs`. Details return `{task, generated_at}`. Missing values display as
unreported. Source timestamps and numeric values are rendered only when supplied.
Task completion, result verification, and acceptance remain separate.
Visible absolute timestamps use Europe/Istanbul (UTC+3). ISO values remain intact
in the data and time-element metadata; explicitly dated budget periods retain
their reported source timezone.

When changing the deployed bundle, the backend should change its versioned UI
resource URI so the host does not reuse a cached document.

The build preserves bundled third-party licenses in the HTML and writes the
same notices to `THIRD_PARTY_NOTICES.txt`.
