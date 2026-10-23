"""Windows OAuth gateway service. Separate from maintenance service."""
import os
from pathlib import Path
import servicemanager
import win32event
import win32service
import win32serviceutil

class AsterWindowsOAuthService(win32serviceutil.ServiceFramework):
    _svc_name_ = "AsterMCPWindowsOAuth"
    _svc_display_name_ = "Aster Universal MCP Avanos OAuth"
    _svc_description_ = "Independent localhost MCP OAuth gateway for Cloudflare Tunnel"

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        import uvicorn
        from oauth_gateway import create_app
        data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "AsterUniversalMCP"
        data.mkdir(parents=True, exist_ok=True)
        if not os.environ.get("ASTER_WINDOWS_PUBLIC_ORIGIN"):
            raise RuntimeError("Set ASTER_WINDOWS_PUBLIC_ORIGIN in the service environment")
        os.environ["ASTER_WINDOWS_OAUTH_DB"] = str(data / "windows-oauth.sqlite")
        os.environ["ASTER_WINDOWS_ADMIN_CLIENTS"] = str(data / "admin-clients.json")
        if not (data / "admin-clients.json").exists():
            (data / "admin-clients.json").write_text("[]", encoding="utf-8")
        config = uvicorn.Config(create_app(), host="127.0.0.1", port=18767,
                                access_log=False, log_level="warning", log_config=None, proxy_headers=False)
        server = uvicorn.Server(config)
        import threading
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        while win32event.WaitForSingleObject(self.stop_event, 1000) != win32event.WAIT_OBJECT_0:
            if not worker.is_alive():
                raise RuntimeError("OAuth gateway worker stopped")
        server.should_exit = True
        worker.join(timeout=15)

if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(AsterWindowsOAuthService)
