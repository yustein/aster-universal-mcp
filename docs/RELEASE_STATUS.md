# Release and acceptance status

## Windows technical beta scope

Version `0.1.0b1` is prepared as a source-based Windows technical beta with manual elevated operation. The accepted artifact is the exact `aster-universal-mcp-0.1.0b1-windows.zip` identified by its external `SHA256SUMS.txt` and `windows-beta-0.1.0b1-acceptance.json` release assets.

The acceptance JSON is written after testing the frozen archive. It must identify the source-manifest SHA-256, archive SHA-256, source commit, Python and dependency inventory, Windows build, launch mode, UTC timestamps and observed operations. This keeps the source archive unchanged while its results are recorded separately. A repository version string, branch name or earlier successful run is not a substitute for that record.

## Required release evidence

| Gate | Required observation |
|---|---|
| Install the distributed package | Extract the exact release archive, install the specified dependencies using the public instructions, and verify source/configuration identity |
| Running deployment | Verify process image and creation time, candidate identity, elevation and ownership of the loopback listener |
| Normal workspace use | Create an ordinary relative output file without changing the frozen source inventory; status and client management remain usable |
| Operator recovery | Revoke or disable administrative permission despite deliberate source drift; stop only the independently verified gateway process |
| HTTP OAuth | Actual discovery, registration, browser cookie/nonce handling, owner approval, PKCE exchange, denial and authenticated access |
| Token renewal | Exercise the refresh grant, rotation/replay and relevant binding/expiry negatives, then authenticated use of the renewed token |
| Public HTTPS | The public origin reaches the same verified Windows deployment; preserve origin checks and prove privileged-operation denial before approval and after revoke |
| Functional tools | Harmless native commands, small file transfer with matching size/hash, bounded job cancellation with actual process termination |
| Actual Claude account | Connection and harmless command use; separately record observed session renewal and use after a documented restart |
| Repeatability and cleanup | Restart through the documented manual mode, verify authenticated use, remove temporary acceptance data and restore temporary test grants |

Controlled HTTP tests and actual account observations must be reported separately. HTTP 200 alone does not prove a successful MCP tool result: check the structured result and its effect. If a screenshot provides the account evidence, state what is visible and whether complete request identifiers or raw tool messages were available. Never include bearer tokens, owner codes, cookies, client secrets or account passwords in a public acceptance asset.

## Previous accepted candidate: RC2, 24 October 2026

The prior candidate had source-manifest SHA-256 `dccc4690651a93a077ac7316fd9907f98c18036ee66dc7d755cce93d9d019ba0` and source commit `3481f6623488c413d0caed05f787f52153691005`.

| Evidence | Result for RC2 |
|---|---|
| Native Windows suite | 54 passed |
| Same-source Linux runner | 51 passed; 3 Windows-only tests skipped |
| Controlled public HTTPS acceptance | 19 acceptance and cleanup stages passed |
| Actual Claude account | Owner connected and supplied a screenshot of successful hostname and account-identity commands |
| Post-test source check | One extra test-output file was preserved outside source; all 78 expected source files were unchanged; source, process and listener verification then passed |

The test-output incident exposed a release defect: commands defaulted to the source directory and source drift could prevent the documented revoke helper from running. The follow-up candidate moves default work into a protected workspace and supplies a protected operator recovery path. It requires its own release record after those changes. RC2 counts must not be presented as the new candidate's test totals.

The reference Windows environment was Windows 11 Enterprise Evaluation 26H2 build 26300.9457, Python 3.11.9 x64, manual elevated gateway operation. The RC2 account screenshot showed Claude-rendered results; complete command request IDs were cropped. Its server logs corroborated the account authorization and contemporaneous HTTP activity without recording per-command UUIDs.

An earlier foundation result was written before the VM clock correction. Its `completed_at` field is unsuitable for event ordering; the later verified-environment and acceptance timestamps were recorded after correction.

## Boundaries of this release

- Manual source deployment, explicit client approval and an owner-managed HTTPS route are intentional requirements.
- Windows service installation, unattended startup and reboot survival are not qualified by the manual-mode tests.
- Longer sessions, sustained load, large multi-chunk transfers and filesystem race/adversarial cases require their own evidence; only operations recorded in the matching acceptance asset are claimed.
- Linux needs separate fresh-machine installation and end-to-end account acceptance before it is labeled accepted. Native macOS has not been qualified here.
- The beta has no independent third-party security audit or signed binary installer.

The prior engineering archive at commit `d5217d7d5a76a51a4c4ed70c9adf0ebd412881aa` had separate component-test evidence. Its test totals are not added to this release's totals.

## Publication

Before publication, the release maintainer checks that the reviewed source commit, packaged bytes, manifest, dependency lists, checksums and acceptance asset all refer to the same candidate. Publication and repository visibility are explicit release actions. The setup helpers do not publish source, create tunnels, change firewall rules or approve clients.
