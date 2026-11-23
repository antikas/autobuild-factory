"""Emit only the subscription binding fields of the official CLI identity probe."""
import json
import subprocess
import sys


def main():
    result = subprocess.run(sys.argv[1:], capture_output=True, timeout=15)
    if result.returncode:
        return 1
    data = json.loads(result.stdout)
    keys = ("loggedIn", "authMethod", "apiProvider", "subscriptionType")
    print(json.dumps({key: data.get(key) for key in keys}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
