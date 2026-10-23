"""SDK v2 OAuth runtime composition, development-only.

Does not expose public HTTP routes until the provider and client-registration
flow have passed full interoperability and negative security tests.
"""
from mcp.server.mcpserver import MCPServer
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from provider_adapter import LocalOAuthProvider

def build_oauth_server(*, db_path: str, origin: str, register_tools=None):
    if not origin.startswith("https://") or origin.endswith("/"):
        raise ValueError("canonical HTTPS origin required")
    resource=origin+"/mcp"
    provider=LocalOAuthProvider(db_path,issuer=origin,resource=resource)
    auth=AuthSettings(
        issuer_url=origin,
        resource_server_url=resource,
        required_scopes=["system:admin"],
        validate_token_resource=True,
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=["system:admin"], default_scopes=["system:admin"]),
    )
    server=MCPServer("Aster Universal MCP",auth_server_provider=provider,auth=auth)
    from approval_http import owner_approval_app
    approval = owner_approval_app(db_path)
    for route in approval.routes:
        server.custom_route(route.path, methods=list(route.methods), include_in_schema=False)(route.endpoint)
    if register_tools is not None:
        register_tools(server)
    return server
