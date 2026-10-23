"""Windows beta: local-only MCP server, no OAuth listener or public exposure."""
from pathlib import Path
from mcp.server.mcpserver import MCPServer
from executor import run_command as execute

server = MCPServer("Aster Universal MCP — Windows Beta")

@server.tool()
def run_command(command: list[str], cwd: str = ".", timeout: int = 30) -> dict:
    """Execute native Windows argv without a shell. Runs under current process identity."""
    return execute(command, cwd, timeout)

@server.tool()
def list_directory(path: str = ".") -> dict:
    """List directory entries on the Windows host."""
    folder = Path(path).resolve()
    if not folder.is_dir():
        return {"status": "rejected", "error": "not a directory"}
    entries = []
    for item in list(folder.iterdir())[:100]:
        entries.append({"name": item.name, "directory": item.is_dir(), "size": item.stat().st_size})
    return {"status": "completed", "path": str(folder), "entries": entries}

from job_launcher import start_job as launch_job
from jobs import job_status as inspect_job
from job_cancel import cancel_job as stop_job
from transfer import begin_upload as begin_file, upload_chunk as write_chunk
from upload_finish import finish_upload as complete_file
from download import download_info as inspect_file, download_chunk as read_chunk

@server.tool()
def start_job(command: list[str], cwd: str = ".", max_runtime: int = 3600) -> dict:
    """Start a Windows background job."""
    return launch_job(command, cwd, max_runtime)

@server.tool()
def job_status(job_id: str) -> dict:
    """Read durable Windows background job status."""
    return inspect_job(job_id)

@server.tool()
def cancel_job(job_id: str) -> dict:
    """Cancel a Windows background job."""
    return stop_job(job_id)

@server.tool()
def begin_upload(destination: str, size: int, sha256: str) -> dict:
    """Begin a Windows binary upload."""
    return begin_file(destination, size, sha256)

@server.tool()
def upload_chunk(transfer_id: str, offset: int, data_base64: str) -> dict:
    """Append a binary upload chunk."""
    return write_chunk(transfer_id, offset, data_base64)

@server.tool()
def finish_upload(transfer_id: str) -> dict:
    """Verify and complete a binary upload."""
    return complete_file(transfer_id)

@server.tool()
def download_info(path: str) -> dict:
    """Read file metadata and SHA256."""
    return inspect_file(path)

@server.tool()
def download_chunk(path: str, offset: int, limit: int = 16384) -> dict:
    """Read a bounded file chunk."""
    return read_chunk(path, offset, limit)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["stdio"], default="stdio")
    parser.parse_args()
    server.run(transport="stdio")
