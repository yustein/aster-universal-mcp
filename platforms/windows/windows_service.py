"""Windows service for local maintenance and restart recovery.

No network listener. The stdio MCP server remains a separate entrypoint.
"""
import json
import os
import threading
import time
from pathlib import Path

import servicemanager
import win32event
import win32service
import win32serviceutil
from jobs import ROOT
from recovery import reconcile
from boot_identity import boot_id

DATA = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "AsterUniversalMCP"

def maintenance():
    DATA.mkdir(parents=True, exist_ok=True)
    status = {"state": "running", "timestamp": time.time(), "pid": os.getpid()}
    temp = DATA / "service-status.tmp"
    temp.write_text(json.dumps(status), encoding="utf-8")
    os.replace(temp, DATA / "service-status.json")

class AsterMCPWindowsService(win32serviceutil.ServiceFramework):
    _svc_name_ = "AsterUniversalMCP"
    _svc_display_name_ = "Aster Universal MCP Windows Beta"
    _svc_description_ = "Local Aster MCP maintenance and recovery; no public listener."

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        servicemanager.LogInfoMsg("Aster Universal MCP Windows beta service started")
        reconcile(ROOT, boot_id())
        while True:
            maintenance()
            if win32event.WaitForSingleObject(self.stop_event, 15000) == win32event.WAIT_OBJECT_0:
                break

if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(AsterMCPWindowsService)
