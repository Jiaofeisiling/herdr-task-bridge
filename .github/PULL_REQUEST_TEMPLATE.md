## What & why

<!-- What does this change do, and what problem does it solve? -->

## Testing

<!-- How did you verify this? Paste relevant `pytest` output. If you
     couldn't test against a live deployed bridge (e.g. sentinel.ps1
     changes), say so explicitly. -->

## Checklist

- [ ] I read [CONTRIBUTING.md](../CONTRIBUTING.md), especially the
      "Sensitive paths" section, and confirmed no real hostnames, project
      codes, usernames, tokens, or task text are in this diff
- [ ] `cd sentinel-bridge && .venv/Scripts/python.exe -m pytest test_bridge.py -v`
      passes (or N/A — explain why below)
- [ ] Docs (`README.md`, `CONTRIBUTING.md`) updated if behavior changed
