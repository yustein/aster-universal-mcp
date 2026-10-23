"""Windows Job Object child execution: assign before ResumeThread."""
import ctypes
from ctypes import wintypes
import subprocess
import _winapi
import threading

# Serializes temporary standard-handle inheritance changes and CreateProcess.
# This only coordinates callers within this Python process.
_INHERITANCE_LOCK = threading.Lock()

CREATE_SUSPENDED = 0x4
KILL_ON_CLOSE = 0x2000
EXTENDED_INFO = 9

class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

class BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]

class EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BASIC_LIMIT),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]

def api():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.ResumeThread.argtypes = [wintypes.HANDLE]
    k.ResumeThread.restype = wintypes.DWORD
    k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.TerminateJobObject.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    return k

def create_job():
    k = api()
    h = k.CreateJobObjectW(None, None)
    if not h:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
    info = EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = KILL_ON_CLOSE
    if not k.SetInformationJobObject(h, EXTENDED_INFO, ctypes.byref(info), ctypes.sizeof(info)):
        e = ctypes.get_last_error()
        k.CloseHandle(h)
        raise OSError(e, "SetInformationJobObject")
    return h

def run_contained(command, cwd, stdout, stderr, timeout):
    """Run a child with Job Object assignment before its first instruction.

    Uses native CreateProcess with inherited standard handles supplied by the
    caller; never launches user code if assignment fails.
    """
    import os
    import msvcrt
    import _winapi
    import subprocess
    if not command or not isinstance(command, list):
        raise ValueError("invalid command")
    k = api()
    job = create_job()
    proc = thread = None
    handles = []
    previous = []
    try:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESTDHANDLES
        startup.hStdInput = _winapi.GetStdHandle(_winapi.STD_INPUT_HANDLE)
        startup.hStdOutput = msvcrt.get_osfhandle(stdout.fileno())
        startup.hStdError = msvcrt.get_osfhandle(stderr.fileno())
        handles = [startup.hStdInput, startup.hStdOutput, startup.hStdError]
        # Hold the lock only while toggling and creating the child, never
        # while waiting for it. Restore flags even if CreateProcess fails.
        with _INHERITANCE_LOCK:
            previous = [os.get_handle_inheritable(h) for h in handles]
            try:
                for h in handles:
                    os.set_handle_inheritable(h, True)
                startup.lpAttributeList = {"handle_list": handles}
                cmdline = subprocess.list2cmdline(command)
                proc, thread, pid, tid = _winapi.CreateProcess(
                    command[0], cmdline, None, None, True,
                    CREATE_SUSPENDED | _winapi.CREATE_NO_WINDOW,
                    None, str(cwd), startup)
            finally:
                for h, was_inheritable in zip(handles, previous):
                    try:
                        os.set_handle_inheritable(h, was_inheritable)
                    except OSError:
                        pass
        if not k.AssignProcessToJobObject(job, proc):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
        if k.ResumeThread(thread) == 0xFFFFFFFF:
            raise OSError(ctypes.get_last_error(), "ResumeThread")
        waited = _winapi.WaitForSingleObject(proc, int(timeout * 1000))
        if waited == _winapi.WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(command, timeout)
        if waited != 0:
            raise OSError("WaitForSingleObject failed")
        return subprocess.CompletedProcess(command, _winapi.GetExitCodeProcess(proc))
    finally:
        # The inheritance flags were restored while holding the lock.
        # Always close the Job Object, including on CreateProcess failure.
        try:
            k.CloseHandle(job)
        finally:
            if thread is not None:
                _winapi.CloseHandle(thread)
            if proc is not None:
                _winapi.CloseHandle(proc)
