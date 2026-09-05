"""Print one or more $defs entries from herdr's exported API schema.

Avoids re-typing fragile inline python -c one-liners through several
layers of shell quoting (bash -> PowerShell -> herdr -> remote shell each
mangle $ and nested quotes differently). Just a schema-reading helper,
not part of the feasibility test itself.

Usage:
    herdr api schema --json > /tmp/herdr_schema.json   # once, or when stale
    python3 dump_schema_def.py TabCreateParams [OtherDef ...]
    python3 dump_schema_def.py --method tab.create      # find method + its params ref
"""

import json
import sys

SCHEMA_PATH = "/tmp/herdr_schema.json"


def main():
    with open(SCHEMA_PATH) as f:
        schema = json.load(f)

    args = sys.argv[1:]

    if args and args[0] == "--method":
        raw = json.dumps(schema)
        for method in args[1:]:
            i = raw.find(f'"{method}"')
            print(f"=== method: {method} ===")
            print(raw[max(0, i - 50):i + 400] if i != -1 else "NOT FOUND")
            print()
        return

    defs = schema.get("schemas", {}).get("request", {}).get("$defs", {})
    for name in args:
        print(f"=== {name} ===")
        print(json.dumps(defs.get(name, "NOT FOUND"), indent=1, ensure_ascii=False))
        print()


if __name__ == "__main__":
    main()
