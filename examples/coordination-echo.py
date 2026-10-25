"""No-model fixture: confirm JSON task receipt without executing task instructions."""
import json
import sys


def main():
    raw = sys.stdin.buffer.read(131073)
    if len(raw) > 131072:
        raise ValueError("input too large")
    request = json.loads(raw)
    task = request["task"]
    print(json.dumps({
        "status": "completed", "exit_code": 0, "task_id": task["task_id"],
        "result": {"kind": "echo_fixture", "received_instruction": task["instruction"],
                   "received_inputs": task.get("inputs"), "executed_instruction": False},
    }))


if __name__ == "__main__":
    main()
