# idm-upgrade-helper

An interactive, **read-only** command-line advisor for upgrading Red Hat Identity
Management (IdM) / FreeIPA servers across RHEL minor and major versions. It reproduces
the step-by-step guidance of Red Hat's "IdM Upgrade Helper" as an on-box CLI, driven by
version-keyed catalogs of deprecated/removed functionality, Technology Previews, new
features, and known issues (RHEL 8.0 → 10.2).

> ⚠️ **Advisory tool — verify before you run anything.** This tool is a convenience aid.
> It does **not** inspect or modify your server to confirm your environment; it generates
> steps strictly from the inputs you provide and from bundled catalogs. **Verify your
> actual IdM configuration, and verify every command against the official Red Hat IdM
> documentation, before running it on a production server.** Not affiliated with or
> endorsed by Red Hat.

## Editions

Two editions ship side by side — pick the one that matches how much you want the tool to do:

| File | Edition | What it does |
|------|---------|--------------|
| `idm-upgrade-helper-1.0.py` | **1.0 — Advisory** | Read-only. Walks the 5-step wizard and prints the upgrade runbook. Runs `ipa-healthcheck` read-only when available (soft gate). **Never changes the system.** |
| `idm-upgrade-helper-2.0.py` | **2.0 — Advisory + opt-in apply** | Everything in 1.0, **plus** an opt-in, heavily-guarded automated **minor** upgrade via `minor --apply` (root + real RHEL host + confirmed backup + healthcheck + typed confirmation). Major migrations remain advisory only. |

## Requirements

- Python 3.6+
- PyYAML (`pip install pyyaml`; present by default on RHEL IdM servers)
- The `catalogs/` directory must sit next to the script (it is loaded at startup).

## Usage

```bash
# Interactive: auto-detect this host's RHEL version and prompt for the rest
python3 idm-upgrade-helper-2.0.py

# Choose the upgrade type directly
python3 idm-upgrade-helper-2.0.py minor
python3 idm-upgrade-helper-2.0.py major

# Override the source version (e.g. off-box) and show everything
python3 idm-upgrade-helper-2.0.py --current 9.6 --all minor

# Save a plain-text copy of the advice
python3 idm-upgrade-helper-2.0.py major --report ~/idm-upgrade.txt

# (2.0 only) EXECUTE a guarded minor upgrade on this server
sudo python3 idm-upgrade-helper-2.0.py minor --apply
```

By default the output is **concise** (upgrade steps, warnings, and known issues). Add
`--all` to also show deprecated/removed functionality, Technology Previews, and new
features. See `--help` for the full option list.

## Feedback

Testing the tool? Your feedback is very welcome:

- 🐞 **Bugs / feature requests:** open a [GitHub Issue](../../issues/new/choose).
- 💬 **Questions / ideas / general feedback:** start a [GitHub Discussion](../../discussions).
- 📝 **Quick survey (no GitHub account needed):** [Tester feedback form](https://docs.google.com/forms/d/e/1FAIpQLSfe9pyfE9_sFpBlu8xN2JEMSVZ4KTnEdt14rX8Lxt_1aJJTRA/viewform)

When reporting an issue, please include: the edition (1.0 / 2.0), the source → target
RHEL versions, minor vs major, the server-configuration option you chose, and the exact
command/output.

## License

Licensed under the **GNU General Public License v3.0** (GPLv3). See [LICENSE](LICENSE).
