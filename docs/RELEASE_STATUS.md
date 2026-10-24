# Release and acceptance status

## Current candidate

This repository is a **development beta candidate** for full remote OAuth MCP operation on Windows and Linux. Implemented capabilities and passing component tests do not establish compatibility with a real Claude account.

The Windows restart work introduces one shared deployment configuration, a frozen source manifest, repository-local Python, runtime identity checks and a documented manual launch mode. These changes require validation on the disposable Windows VM before the candidate can be accepted.

| Acceptance gate | Evidence required |
|---|---|
| Clean Windows setup | Verified source candidate, dependencies, correct clock and secured test identity |
| Running deployment | Matching source/configuration/runtime identity and verified loopback listener |
| Complete HTTP OAuth | Registration, actual browser approval route, cookie/nonce handling, retry behavior, PKCE exchange and authenticated access |
| Public HTTPS | The verified Windows process handles the public flow with correct proxy/origin checks |
| Real Claude account | Successful connection, tool discovery and recorded harmless operations on the intended VM |
| Privilege boundary | Denial before explicit client approval and successful bounded operation afterwards |
| Repeatability | A documented restart followed by another successful authenticated operation |
| Linux | Separate fresh-machine installation and end-to-end account acceptance |

Do not label any pending gate as passed because another gate succeeded. A candidate's acceptance record must identify its source-manifest SHA-256, interpreter and dependency inventory, Windows build, launch mode, UTC timestamps, and the exact operations observed. Keep secrets out of that record.

## Prior engineering evidence

The earlier engineering archive at commit `d5217d7d5a76a51a4c4ed70c9adf0ebd412881aa` passed 118 Windows platform tests and 134 core tests with one skipped on a disposable Windows VM. Physical cancellation checks were also reported; counts can overlap. That is useful component evidence for that archive, not acceptance of a later full remote OAuth distribution.

## Separate release decisions

Source-based manual deployment is intentional for this beta. A one-click installer is not required for the present acceptance test. Windows service lifecycle and autostart, signed release provenance, and additional security cases remain separate decisions and gates.

Repository visibility and publication must be checked at release time. These setup helpers do not publish the repository, change its visibility, install services, create public networking, or approve client permissions. The public README should link to an accepted candidate and its actual evidence before claiming completed account interoperability.
