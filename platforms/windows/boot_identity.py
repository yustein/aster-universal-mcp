"""Stable Windows boot identity from OS last-boot timestamp."""
import subprocess
from functools import lru_cache

@lru_cache(maxsize=1)
def boot_id():
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')"],
        capture_output=True, text=True, timeout=15, check=True)
    value = result.stdout.strip()
    if not value or len(value) > 100:
        raise RuntimeError("Windows boot identity unavailable")
    return value
