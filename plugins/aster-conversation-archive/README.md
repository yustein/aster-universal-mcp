# Aster Conversation Archive package

This directory contains an Agent Plugins 1.0 package for compatible local or
desktop runtimes. `mcp.json` declares a remote streamable-HTTP MCP connection
using OAuth dynamic client registration (DCR).

## Connect from ChatGPT web

Use **Plugins → + → Add custom MCP server**, enter the deployment's verified
HTTPS endpoint including `/mcp`, and select **OAuth with DCR**. Complete the owner
pairing and exact-client archive grant, review the eight discovered read-only
tools, and install/connect the resulting plugin. The complete procedure is in
the [archive deployment guide](../../deploy/archive/README.md#connect-an-authorized-chatgpt-client).

Uploading this directory as a ZIP through Plugin Creator saves a standalone
package. It does **not** create the ChatGPT web App binding, connect an account,
or replace the custom MCP server flow. A desktop-only listing without an account
connection is not a working ChatGPT web integration.

OpenAI documents the direct server connection separately from package testing:

- [Connect and test your plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt)
- [Connect custom MCP servers in ChatGPT](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)
- [Custom MCP authentication options](https://developers.openai.com/api/docs/guides/custom-mcp-server)

## Install in a compatible local or desktop runtime

Use the runtime's supported package installation procedure and confirm that it
supports the declared remote MCP transport and OAuth DCR. Check that `mcp.json`
points to the intended deployment. The package does not deploy the server or
contain pairing codes, access tokens, or client secrets.

OAuth authenticates the client. The server's protected archive ACL separately
grants that exact client access to an owner and allowed sources/projects. Its
existing `system:admin` scope name does not add host-execution tools to this
read-only archive service.

## Verify the actual client

From the connected client, call `archive_coverage`, then `search` and `fetch` for
a known synthetic passage. Verify its exact text, provenance and authenticated
citation URL. For ChatGPT, make these calls in a fresh conversation with the
archive plugin selected. Installation, discovery and OAuth success alone do not
prove that the client's archive grant is correct.

See the [eight-tool reference](../../docs/CONVERSATION_ARCHIVE.md#mcp-tools-and-permissions)
for branch context, explicit search modes, derived knowledge and decision-candidate
history.
