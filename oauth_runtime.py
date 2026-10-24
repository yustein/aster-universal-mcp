"""Shared OAuth MCP composition with owner approval and token-resource binding."""
from mcp.server.mcpserver import MCPServer
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from provider_adapter import LocalOAuthProvider
from oauth_token_resource import TokenResourceBinding


class ResourceBoundMCPServer(MCPServer):
    """Apply one token-request boundary to both platform HTTP applications."""
    def __init__(self, *args, token_resource, **kwargs):
        super().__init__(*args, **kwargs)
        self._aster_token_resource = token_resource

    def streamable_http_app(self, **kwargs):
        app = super().streamable_http_app(**kwargs)
        app.add_middleware(TokenResourceBinding, resource=self._aster_token_resource)
        return app

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
    server=ResourceBoundMCPServer("Aster Universal MCP", token_resource=resource,
                                 auth_server_provider=provider, auth=auth)
    from approval_http import owner_approval_app
    approval = owner_approval_app(db_path)
    for route in approval.routes:
        server.custom_route(route.path, methods=list(route.methods), include_in_schema=False)(route.endpoint)
    if register_tools is not None:
        register_tools(server)
    return server
