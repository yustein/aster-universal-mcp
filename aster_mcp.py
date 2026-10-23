"""Laboratory MCP server. No remote authentication or deployment yet."""
import os
from mcp.server.mcpserver import MCPServer
from filesystem import list_directory as enumerate_directory

server = MCPServer("Aster Universal MCP - laboratory")

@server.tool()
def document_stats(text: str) -> dict:
    """Count words, characters, and lines in supplied text."""
    if len(text) > 200_000:
        raise ValueError("input exceeds 200000 characters")
    return {"words": len(text.split()), "characters": len(text), "lines": len(text.splitlines())}

@server.tool()
def research_extract(text: str, keyword: str, limit: int = 10) -> dict:
    """Find keyword-containing lines in supplied text."""
    if len(text) > 200_000 or not 1 <= len(keyword) <= 100 or not 1 <= limit <= 50:
        raise ValueError("invalid bounded input")
    matches = [{"line": i, "text": line[:500]} for i, line in enumerate(text.splitlines(), 1) if keyword.casefold() in line.casefold()]
    return {"matches": matches[:limit], "total": len(matches), "truncated": len(matches) > limit}

@server.tool()
def list_directory(path: str = ".", limit: int = 100) -> dict:
    """List actual host directory entries, including hidden files and metadata."""
    return enumerate_directory(path, root=os.environ.get("ASTER_ALLOWED_ROOT"), limit=limit)

def main():
    transport = os.environ.get("ASTER_MCP_TRANSPORT", "stdio")
    if transport not in ("stdio", "streamable-http"):
        raise ValueError("Unsupported transport")
    if transport == "streamable-http":
        if os.environ.get("ASTER_MCP_AUTH_MODE") == "bearer":
            from gated_runtime import run_gated
            run_gated(server)
        else:
            import asyncio
            asyncio.run(server.run_streamable_http_async(host="127.0.0.1", port=int(os.environ.get("ASTER_MCP_PORT", "8765"))))
    else:
        server.run(transport="stdio")

if __name__ == "__main__":
    main()
