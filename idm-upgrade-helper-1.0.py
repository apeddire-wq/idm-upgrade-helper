#!/usr/bin/env python3
"""idm-upgrade-helper (1.0) — interactive, read-only RHEL IdM / FreeIPA upgrade advisor.

Reproduces Red Hat's login-gated "IdM Upgrade Helper" web portal as an on-box CLI.
It walks the portal's 5-step wizard (Upgrade Options, Deprecated & Removed
Functionalities, Prerequisites, Environment Checks, Upgrade Steps) plus a value-add
Technology Preview section, driven by the version-keyed catalogs in ./catalogs/.

Release 1.0 adds a Pre-Upgrade Health Check step: on a real IdM server it EXECUTES
`ipa-healthcheck` (read-only inspection), summarizes non-SUCCESS findings, and applies
a SOFT gate before the upgrade steps (recommends clearing findings, then asks whether
to proceed anyway; default No). Availability-gated: shutil.which("ipa-healthcheck") +
root. When unavailable (e.g. a non-IdM box) it falls back to printed guidance and
never executes any system command.

Design decisions this tool encodes (see DESIGN.md / project memory):
  #1 Source RHEL version is AUTO-DETECTED (/etc/os-release, then /etc/redhat-release),
     with confirm/override, and a numbered-menu fallback when detection fails
     (e.g. running the demo on macOS). Everything else is prompted.
  #2 Target is AUTO-POPULATED (deterministic), never a dropdown. Major upgrades are
     stepwise, one major at a time:
       - Not on the latest minor of the major -> tell the user to re-run choosing the
         Minor Update option first, then STOP.
       - On the latest minor -> target = next major's latest minor; full migration advice.
  #3 Upgrade Steps are command-level, portal-style: each step shows the portal's own
     title + explanation prose, followed by the actual command(s). NOTE: the portal's
     translation file contains titles/prose only (no command strings — it builds them in
     app code), so the commands below are documented-standard forms sourced from the RHEL
     IdM docs and should be VERIFIED against the cited guide for your exact versions.
  #4 A Technology Preview section is included (stream-gated to rhel8/rhel9).
  #5 Output goes to screen; --report FILE also writes the full result to a file.
  #6 Catalogs are YAML (loaded via PyYAML).

Read-only: this tool never changes the system. It does not execute the catalog
`detect` hints against the box — it prints them as "how to check" guidance and prompts
for the environment questions. A future --detect mode will turn the hints into probes.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "error: PyYAML is required (pip install pyyaml). "
        "It is present by default on RHEL IdM servers.\n"
    )
    sys.exit(3)

CATALOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalogs")

PROG = "idm-upgrade-helper"
VERSION = "1.0"


# --------------------------------------------------------------------------- #
# Output: colored to the terminal, plain copy captured for --report.
# --------------------------------------------------------------------------- #
class Emitter:
    """Writes to stdout (optionally colored) and accumulates a plain-text report."""

    COLORS = {
        "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
        "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
        "blue": "\033[34m", "cyan": "\033[36m",
    }
    SEVERITY_COLOR = {"error": "red", "warning": "yellow", "info": "cyan"}

    def __init__(self):
        self.buffer = []
        self.use_color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    def _paint(self, text, color):
        if not color or not self.use_color:
            return text
        return "%s%s%s" % (self.COLORS.get(color, ""), text, self.COLORS["reset"])

    def line(self, text="", color=None):
        print(self._paint(text, color))
        self.buffer.append(text)

    def heading(self, text):
        self.line()
        self.line("=" * 72, "dim")
        self.line(text, "bold")
        self.line("=" * 72, "dim")

    def subheading(self, text):
        self.line()
        self.line(text, "bold")
        self.line("-" * len(text), "dim")

    def sev(self, severity, text):
        tag = "[%s]" % severity.upper()
        self.line("%s %s" % (tag, text), self.SEVERITY_COLOR.get(severity))

    def command(self, cmd):
        for ln in cmd.strip("\n").splitlines():
            self.line("    $ " + ln, "green")

    def write_report(self, path):
        with open(path, "w") as fh:
            fh.write("\n".join(self.buffer).rstrip() + "\n")


# --------------------------------------------------------------------------- #
# Version helpers.
# --------------------------------------------------------------------------- #
def vt(s):
    """Parse a version string into a comparable (major, minor) tuple.

    Handles "x.y", a bare major "x" (-> (x, 0)), and a trailing "?" (inferred
    markers in the catalogs). Returns None for anything unparseable.
    """
    if s is None:
        return None
    s = str(s).strip().rstrip("?").strip()
    m = re.match(r"^(\d+)(?:\.(\d+))?$", s)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0))


def in_interval(v, lo, hi):
    """Half-open interval test: lo < v <= hi, comparing (major, minor) tuples."""
    tv = vt(v)
    if tv is None or lo is None or hi is None:
        return False
    return lo < tv <= hi


def fmt(t):
    """Render a (major, minor) tuple back as 'x.y'."""
    return "%d.%d" % t


# --------------------------------------------------------------------------- #
# Catalog loading.
# --------------------------------------------------------------------------- #
def load_catalogs():
    def load(name):
        path = os.path.join(CATALOG_DIR, name)
        if not os.path.exists(path):
            sys.stderr.write("error: missing catalog %s\n" % path)
            sys.exit(3)
        with open(path) as fh:
            return yaml.safe_load(fh)

    return {
        "rules": load("upgrade-rules.yaml"),
        "deprecations": load("deprecations.yaml"),
        "prerequisites": load("prerequisites.yaml"),
        "tech_previews": load("tech-previews.yaml"),
        "known_issues": load("known-issues.yaml"),
        "new_features": load("new-features.yaml"),
    }


# --------------------------------------------------------------------------- #
# Interactive prompt helpers (dropdowns -> numbered menus; yes/no).
# --------------------------------------------------------------------------- #
def choose(prompt, options):
    """Numbered single-choice menu. `options` is a list of (value, label).

    Returns the chosen value. Re-prompts on invalid input. Labels are shown
    verbatim (they come from the portal's translation catalog).
    """
    print("\n%s" % prompt)
    for i, (_, label) in enumerate(options, 1):
        print("  %d) %s" % (i, label))
    while True:
        raw = input("> ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print("Please enter a number between 1 and %d." % len(options))


def yes_no(prompt, default=None):
    suffix = " [y/n]" if default is None else (" [Y/n]" if default else " [y/N]")
    while True:
        raw = input("\n%s%s " % (prompt, suffix)).strip().lower()
        if not raw and default is not None:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("Please answer y or n.")


# --------------------------------------------------------------------------- #
# Source version detection (#1).
# --------------------------------------------------------------------------- #
def detect_version():
    """Return a detected 'x.y' string, or None if it cannot be determined."""
    # Primary: /etc/os-release VERSION_ID (mirrors ipaplatform osinfo, DESIGN.md §7).
    try:
        with open("/etc/os-release") as fh:
            for ln in fh:
                if ln.startswith("VERSION_ID="):
                    val = ln.split("=", 1)[1].strip().strip('"')
                    if vt(val):
                        return val
    except OSError:
        pass
    # Fallback: /etc/redhat-release, e.g. "Red Hat Enterprise Linux release 9.4 (Plow)".
    try:
        with open("/etc/redhat-release") as fh:
            m = re.search(r"(\d+\.\d+)", fh.read())
            if m:
                return m.group(1)
    except OSError:
        pass
    return None


def all_minor_versions(rules):
    versions = []
    for major in sorted(rules["minors"], key=int):
        versions.extend(rules["minors"][major])
    return versions


def resolve_source_version(rules, override):
    """Resolve the source version, returning (version, method).

    method is one of "override", "detected", or "selected" so callers can label
    the source line accurately.
    """
    if override:
        if not vt(override):
            sys.stderr.write("error: --current '%s' is not a valid x.y version\n" % override)
            sys.exit(3)
        return override, "override"

    detected = detect_version()
    if detected and vt(detected):
        print("\nDetected current RHEL version: %s" % detected)
        if yes_no("Use this as the source version?", default=True):
            return detected, "detected"
        print("Okay — pick your source version instead.")
    else:
        print("\nCould not auto-detect the RHEL version on this host "
              "(expected on non-RHEL machines, e.g. a macOS demo box).")

    options = [(v, "RHEL %s" % v) for v in all_minor_versions(rules)]
    return choose("Select source RHEL version:", options), "selected"


# --------------------------------------------------------------------------- #
# Matchers.
# --------------------------------------------------------------------------- #
def match_deprecations(catalog, lo, hi):
    """Group in-scope entries into removed / deprecated / changed buckets.

    An entry is in scope when any of its version fields lands in (lo, hi].
    Status precedence: removed_in > deprecated_in > changed_in (DESIGN.md §5.2).
    """
    buckets = {"removed": [], "deprecated": [], "changed": []}
    for e in catalog.get("deprecations", []):
        if in_interval(e.get("removed_in"), lo, hi):
            buckets["removed"].append((e, e["removed_in"]))
        elif in_interval(e.get("deprecated_in"), lo, hi):
            buckets["deprecated"].append((e, e["deprecated_in"]))
        elif in_interval(e.get("changed_in"), lo, hi):
            buckets["changed"].append((e, e["changed_in"]))
    return buckets


def stream_relevant(entry, target_major):
    """A TP entry with no `stream` applies to all majors; otherwise it must match."""
    stream = entry.get("stream")
    if not stream:
        return True
    return stream == "rhel%d" % target_major


def match_known_issues(catalog, target):
    """Known issues that are OPEN at the target version (cumulative model).

    Mirrors the Red Hat release notes: an issue is re-listed every version until it
    is fixed. Each entry is tagged with first_seen (the version it started in). An
    issue is open at target T when first_seen <= T AND (still_open OR T < resolved_in).
    Returns (entry, first_seen) pairs.
    """
    tv = vt(target)
    tmajor = tv[0] if tv else None
    hits = []
    for k in catalog.get("known_issues", []):
        if not stream_relevant(k, tmajor):
            continue
        fs = vt(k.get("first_seen"))
        if fs is None or tv is None or fs > tv:
            continue  # issue had not appeared yet by the target version
        if k.get("still_open"):
            hits.append((k, k.get("first_seen")))
        else:
            res = vt(k.get("resolved_in"))
            if res is None or tv < res:
                hits.append((k, k.get("first_seen")))
    return hits


def section_doc_link(catalog, ctx):
    """Single release-notes link for a section, from the catalog `doc` map.

    One link per section (not per entry). Prefer an exact target-version key (e.g.
    "9.0", whose release-notes slug/anchor can differ from its major's), then fall
    back to the major pattern (e.g. "9" / "10"). Returns the resolved target-version
    URL, or None when no verified pattern exists.
    """
    docs = catalog.get("doc", {})
    version = ctx["target_version"]
    major = str(ctx["target_major"])
    pattern = docs.get(version) or docs.get(major)
    if not pattern:
        return None
    return pattern.replace("<major>", major).replace("<version>", version)


def match_new_features(catalog, lo, hi, target_major):
    """New features gained along the path: added_in in (lo, hi] (point-in-time).

    Returns (entry, added_in) pairs.
    """
    hits = []
    for f in catalog.get("new_features", []):
        if not stream_relevant(f, target_major):
            continue
        if in_interval(f.get("added_in"), lo, hi):
            hits.append((f, f.get("added_in")))
    return hits


def match_tech_previews(catalog, lo, hi, target):
    """TP features relevant to the resolved path (same interval model, §5.5)."""
    tmajor = vt(target)[0]
    hits = []
    for e in catalog.get("technology_previews", []):
        if not stream_relevant(e, tmajor):
            continue
        intro = vt(e.get("introduced_tp"))
        if intro is None or intro > hi:
            continue  # not yet a TP by the time you reach the target
        # Determine status at/around the target.
        if e.get("still_tp"):
            status = "Technology Preview (not GA as of latest data)"
        elif e.get("ga_in"):
            ga = str(e["ga_in"])
            if ga.endswith("?"):
                status = "graduated to GA in RHEL %s (inferred)" % ga.rstrip("?")
            else:
                status = "graduated to GA in RHEL %s" % ga
        elif e.get("last_tp"):
            status = "last seen as TP in RHEL %s" % e["last_tp"]
        else:
            status = "Technology Preview"
        hits.append((e, status))
    return hits


# --------------------------------------------------------------------------- #
# Upgrade-step runbooks (#3): portal title + portal prose (verbatim) + commands.
# Commands are documented-standard forms; verify against the cited RHEL guide.
# Placeholders mirror the portal: {src_host}, {tgt_host}, {src_major}, {tgt_major},
# {tgt_version}.
# --------------------------------------------------------------------------- #
def render_backup_step(out, ctx):
    """Always-first runbook step: take a full backup before upgrading.

    Applies to both minor updates and major migrations. Verified against the RHEL
    "Backing up and restoring IdM" and "Performing disaster recovery with IdM"
    guides (docs.redhat.com).
    """
    out.subheading("Back up IdM before you upgrade (required)")
    out.sev("warning",
            "Create a full backup of every IdM server BEFORE you start the upgrade. "
            "If the upgrade fails, the backup lets you restore the server to its "
            "previous state. Back up each server in the topology, not just one.")
    out.line("Run a full-server backup on each IdM server (as root). This runs offline: "
             "it stops the IdM services, backs up the configuration and LDAP data, then "
             "restarts the services automatically:")
    out.command("sudo ipa-backup")
    out.line("To encrypt the backup, add --gpg (ipa-restore auto-detects and decrypts "
             "it using the GPG2 root keyring):")
    out.command("sudo ipa-backup --gpg")

    # (#3) Full vs data-only.
    out.line("Use the full-server backup (the default, above) for a pre-upgrade "
             "snapshot. A data-only backup (--data, or --data --online while services "
             "run) captures LDAP data only and is not sufficient on its own here.")

    # (#2/#4) Location, off-box copy, and restore constraints.
    out.line("Backups are stored by default under /var/lib/ipa/backup/ (archives named "
             "ipa-full-<date>). Copy each one to a separate, secure location off the "
             "server. Note the restore constraints: a backup can only be restored onto a "
             "server with the same hostname, IP address, and IdM/RHEL software version "
             "that created it, and ipa-restore cannot read backups kept in /tmp or "
             "/var/tmp. (A pre-upgrade backup therefore restores onto the pre-upgrade "
             "version — restore before upgrading, not after.)")

    # VM snapshot recommendation (all flows). Red Hat recommends snapshots OVER
    # backups for recovery, because a backup restore requires matching RPM versions.
    out.sev("warning",
            "In addition to ipa-backup, it is strongly recommended that you capture a "
            "complete virtual machine snapshot of each IdM server before proceeding. "
            "Whereas ipa-backup preserves only IdM configuration and directory data, a "
            "full VM snapshot preserves the entire system state, enabling recovery in "
            "failure scenarios that an application backup alone cannot address — for "
            "example, a failed or unbootable system, a kernel panic, filesystem "
            "corruption, or any other event that renders the operating system itself "
            "unusable. Red Hat recommends recovering from a VM snapshot in preference to "
            "a backup, because restoring a backup requires the same software versions on "
            "the target host. Where a server is a hidden replica, you can shut it down "
            "periodically, take a full snapshot, and bring it back online without "
            "disrupting IdM clients.")
    out.line("Where virtual machine snapshots are not available (for example, on "
             "bare-metal deployments), take an equivalent full-system or block-level "
             "backup of each server before continuing.")

    # (#5) Replication caveat — only when the environment has replicas.
    if ctx.get("replicas"):
        out.sev("warning",
                "Replicated topology: do NOT restore a single server's snapshot or "
                "backup into a still-running topology — the rolled-back database "
                "diverges from the live replicas and causes generation-ID and "
                "changelog (RUV/CSN) conflicts. If you must recover, first isolate the "
                "affected servers (remove their replication topology segments), restore "
                "one server (ideally a CA replica), verify replication, then reinitialize "
                "or rebuild the remaining replicas from the restored server "
                "(ipa-replica-manage re-initialize).")


def render_minor_steps(out, ctx):
    tv = ctx["target_version"]
    out.subheading("1. Check Current Release Version")
    out.line("Verify the current RHEL release version.")
    out.command("cat /etc/redhat-release")

    out.subheading("2. Set Target Release Version")
    out.line("Set the target RHEL version to %s." % tv)
    out.command("sudo subscription-manager release --set=%s" % tv)

    out.subheading("3. Clean Package Cache")
    out.line("Clean all cached package data to ensure fresh package information.")
    out.command("sudo dnf clean all")

    out.subheading("4. Upgrade IdM Packages")
    out.line("Select one of the following upgrade options:")
    out.line()
    out.line("Option 1: update all IdM packages relevant for your profile that have "
             "updates available:")
    out.command("sudo dnf upgrade")
    out.line("Option 2: install or update packages to match the latest version "
             "available for your profile from any enabled repository:")
    out.command("sudo dnf distro-sync")

    out.subheading("5. Post-Upgrade")
    out.line("After you update the IdM packages on at least one server, all other "
             "servers in the topology receive the updated schema, even if you do not "
             "update their packages. This ensures that any new entries which use the "
             "new schema can be replicated among the other servers.")

    out.sev("warning",
            "When updating multiple IdM servers, wait at least 10 minutes after "
            "updating one server before updating another. Updating servers "
            "simultaneously (or with only short intervals) leaves too little time to "
            "replicate post-upgrade changes and can cause conflicting replication events.")


def _server_config_flags(ctx):
    """Derive (has_dns, has_ca, external_ca) from the chosen server configuration.

    Keys (from prerequisites.yaml / the lab): integratedDnsIntegratedCA,
    integratedDnsExternalCA, integratedDnsWithoutCA, withoutDnsIntegratedCA,
    withoutDnsExternalCA, withoutDNSNoCA. Default to full DNS+CA when unset.
    """
    cfg = ctx.get("server_configuration") or "integratedDnsIntegratedCA"
    has_dns = cfg.startswith("integratedDns")
    has_ca = not (cfg.endswith("WithoutCA") or cfg.endswith("NoCA"))
    external_ca = "External" in cfg
    return has_dns, has_ca, external_ca


# Server configurations whose full runbook has been verified against the portal
# (all six, verified 2026-10-27). no-DNS variants drop --setup-dns/--ip-address/
# --forwarder; no-CA variants drop the CA-renewal/CRL steps and supply certs manually.
CONFIRMED_CONFIGS = {"integratedDnsIntegratedCA", "integratedDnsExternalCA",
                     "integratedDnsWithoutCA", "withoutDnsIntegratedCA",
                     "withoutDnsExternalCA", "withoutDNSNoCA"}

# Earliest IdM version that supports storing keys on an HSM: Technology Preview in
# RHEL 9.5, GA in 9.6/10. Earlier sources (RHEL 8, 9.0-9.4) have no HSM support, so
# the HSM question/guidance is not shown for them.
HSM_MIN_VERSION = (9, 5)


def _replica_install_cmd(has_dns, has_ca, hsm=False):
    """Build the ipa-replica-install command for the resolved server configuration."""
    parts = ["ipa-replica-install"]
    if has_dns:
        parts += ["--setup-dns", "--ip-address 192.0.2.1", "--forwarder 192.0.2.20"]
    if has_ca:
        parts += ["--setup-ca"]
        if hsm:
            # CA keys on an HSM: name the token (token password is prompted).
            parts += ["--token-name=<HSM-TOKEN>"]
    else:
        # No IdM CA: the deployment has no CA to issue the replica's service
        # certificates, so supply the DS and HTTP certs (and their key PINs) manually.
        parts += ["--dirsrv-cert-file /tmp/server.crt", "--dirsrv-cert-file /tmp/server.key",
                  "--dirsrv-pin secret", "--http-cert-file /tmp/server.crt",
                  "--http-cert-file /tmp/server.key", "--http-pin secret",
                  "--pkinit-cert-file /tmp/server.crt", "--pkinit-pin secret"]
    return " ".join(parts)


def render_major_steps(out, ctx):
    # Version-parameterized replica-based migration runbook (Migrating to IdM guide).
    # Hostnames/package manager/commands adapt to the resolved source->target majors;
    # CA- and DNS-specific steps are gated on the selected server configuration.
    src_major, tgt_major = ctx["source_major"], ctx["target_major"]
    src_host = "rhel%d.example.com" % src_major
    tgt_host = "rhel%d.example.com" % tgt_major
    pkg = "yum" if src_major <= 8 else "dnf"
    cfg = ctx.get("server_configuration") or "integratedDnsIntegratedCA"
    has_dns, has_ca, _external_ca = _server_config_flags(ctx)
    hsm = bool(ctx.get("hsm")) and has_ca

    out.sev("warning",
            "Performing an in-place upgrade of RHEL %d IdM servers to RHEL %d is NOT "
            "supported. You add new RHEL %d IdM replicas to the deployment, then retire "
            "the RHEL %d servers." % (src_major, tgt_major, tgt_major, src_major))
    out.sev("info",
            "Migrate all servers in the deployment as quickly as possible. Mixing "
            "different IdM versions for extended periods can lead to incompatibilities "
            "or even unrecoverable data corruption.")

    step = [0]

    def heading(title):
        step[0] += 1
        out.subheading("%d. %s" % (step[0], title))

    if has_ca:
        heading("Identify the CA renewal server")
        out.line("%s is the RHEL %d system that will become the new CA renewal server; "
                 "%s is the original RHEL %d CA renewal server. To identify which RHEL %d "
                 "server is the CA renewal server, run on any IdM server:"
                 % (tgt_host, tgt_major, src_host, src_major, src_major))
        out.command("ipa config-show | grep 'CA renewal'")

    heading("Prepare for migration")
    out.line("On %s:" % src_host)
    out.line("- Upgrade the system to the latest RHEL %d version." % src_major)
    out.line("- Ensure the domain level for your domain is set to 1:")
    out.command("ipa domainlevel-get")
    out.line("- Update the ipa-* packages to their latest version:")
    out.command("%s update ipa-*" % pkg)
    out.line("On %s:" % tgt_host)
    out.line("- Install the latest version of Red Hat Enterprise Linux %d." % tgt_major)
    out.line("- Identify the time server %s is synchronized with:" % src_host)
    out.command("ntpstat")
    out.line("- Enroll the system as an IdM client into the domain %s is authoritative "
             "for (use --ntp-pool instead of --ntp-server if you use an NTP pool):"
             % src_host)
    out.command("ipa-client-install --mkhomedir --ntp-server ntp.example.com")

    heading("Install the RHEL %d replica" % tgt_major)
    out.line("List which server roles are present in your RHEL %d environment:" % src_major)
    out.command("ipa server-role-find --status enabled --server %s" % src_host)
    if hsm:
        out.sev("info",
                "HSM in use: the CA keys and certificates must already be stored on a "
                "supported HSM, with an available slot, token, and token password. All "
                "replicas (and any KRA) must use the SAME HSM as the initial installation. "
                "You are prompted for the token password during the install.")
    desc = ("To set up an IdM server on %s as a replica of the IdM RHEL %d server with "
            "the IP address 192.0.2.1" % (tgt_host, src_major))
    if has_dns:
        desc += " that uses a per-server forwarder with the IP address 192.0.2.20"
    desc += ", run the following command:"
    out.line(desc)
    out.command(_replica_install_cmd(has_dns, has_ca, hsm))
    if not has_ca:
        out.line("Additionally, you must specify either --no-pkinit or both "
                 "--pkinit-cert-file and --pkinit-pin.")
    if not has_dns:
        out.line("Add the newly created IdM DNS service records to your DNS server. "
                 "Export the IdM DNS service records into a file in the nsupdate format:")
        out.command("ipa dns-update-system-records --dry-run --out dns_records_file.nsupdate")
        out.line("Then submit a DNS update request to your DNS server using the nsupdate "
                 "utility and the dns_records_file.nsupdate file.")

    heading("Verify IdM services on RHEL %d" % tgt_major)
    out.line("Verify that the IdM services are running on %s:" % tgt_host)
    out.command("ipactl status")
    out.line("Authenticate, then verify that server roles for %s match %s (except the "
             "NTP server role):" % (tgt_host, src_host))
    out.command("kinit admin\nipa server-role-find --status enabled --server %s" % tgt_host)
    out.line("Verify that %s is synchronized with the NTP server and that replication is "
             "healthy:" % tgt_host)
    out.command("chronyc tracking\nipa-replica-status")

    if has_ca:
        heading("Assign the CA renewal server role to the RHEL %d server" % tgt_major)
        out.line("On %s, configure it as the new CA renewal server to handle CA subsystem "
                 "certificate renewal:" % tgt_host)
        out.command("ipa config-mod --ca-renewal-master-server %s" % tgt_host)
        out.line("On %s, enable the certificate updater task: open "
                 "/etc/pki/pki-tomcat/ca/CS.cfg, remove the ca.certStatusUpdateInterval "
                 "entry (or set it to the desired interval in seconds; default 600), save, "
                 "and restart the IdM service:" % tgt_host)
        out.command("ipactl restart")
        out.line("On %s, disable the certificate updater task: open "
                 "/etc/pki/pki-tomcat/ca/CS.cfg, set ca.certStatusUpdateInterval=0 (add it "
                 "if it does not exist), save, and restart the IdM service:" % src_host)
        out.command("ipactl restart")

        heading("Stop CRL generation on RHEL %d" % src_major)
        out.line("On %s, check whether it is generating the CRL, stop it, and verify:"
                 % src_host)
        out.command("ipa-crlgen-manage status\n"
                    "ipa-crlgen-manage disable\n"
                    "ipa-crlgen-manage status")

        heading("Start CRL generation on RHEL %d" % tgt_major)
        out.line("On %s (you must be logged in as root), start CRL generation and confirm "
                 "it is enabled:" % tgt_host)
        out.command("ipa-crlgen-manage enable\n"
                    "ipa-crlgen-manage status")

    heading("Stop and decommission RHEL %d" % src_major)
    out.line("Ensure all data, including the latest changes, has migrated from %s to %s. "
             "For example, add a user on the source and confirm it replicated to the "
             "target:" % (src_host, tgt_host))
    out.line("- On %s:" % src_host)
    out.command("ipa user-add random_user")
    out.line("- On %s:" % tgt_host)
    out.command("ipa user-find random_user")
    out.line("Ensure a Distributed Numeric Assignment (DNA) ID range is allocated to %s. "
             "Either activate the DNA plug-in directly by creating another user on %s:"
             % (tgt_host, tgt_host))
    out.command("ipa user-add another_random_user")
    out.line("or assign a specific DNA ID range — display the IdM ID range and the "
             "allocated DNA ranges, reduce the range on %s so a section becomes free, and "
             "assign the remainder to %s:" % (src_host, tgt_host))
    out.command("ipa idrange-find\n"
                "ipa-replica-manage dnarange-show\n"
                "ipa-replica-manage dnarange-set %s 196600026-196699999\n"
                "ipa-replica-manage dnarange-set %s 196700000-196799999"
                % (src_host, tgt_host))
    out.line("Finally, stop all IdM services on %s to force domain discovery to %s:"
             % (src_host, tgt_host))
    out.command("ipactl stop")

    if cfg not in CONFIRMED_CONFIGS:
        out.sev("info",
                "This configuration's exact runbook is not yet portal-verified: CA-specific "
                "steps (CA renewal, CRL) appear only when a CA is present, and DNS options "
                "only with integrated DNS. Confirm the replica-install options against the "
                "Migrating to IdM guide.")

    # Cross-major migration warnings (upgrade-rules.yaml global_rules.migration_warnings).
    warnings = ctx["rules"].get("global_rules", {}).get("migration_warnings", [])
    if warnings:
        out.subheading("Migration warnings (mixed-version window)")
        for w in warnings:
            out.sev(w.get("severity", "warning"), w["text"])
            for ref in w.get("refs", []):
                out.line("    ref: %s" % ref, "dim")


# --------------------------------------------------------------------------- #
# Wizard steps.
# --------------------------------------------------------------------------- #
def step_upgrade_options(out, cat, args):
    """Step 1: pick upgrade type, resolve source (auto) and target (auto)."""
    rules = cat["rules"]
    out.heading("Step 1 / 5 — Upgrade Options")

    upgrade_type = getattr(args, "upgrade_type", None)
    if upgrade_type:
        out.line()
        out.line("Upgrade type   : %s (from command line)"
                 % ("Minor Update" if upgrade_type == "minor" else "Major Upgrade"))
    else:
        upgrade_type = choose(
            "Select upgrade type:",
            [("minor", "Minor Update"), ("major", "Major Upgrade")],
        )

    source, method = resolve_source_version(rules, args.current)
    smajor, sminor = vt(source)
    latest_minor = rules["majors"][smajor]["latest_minor"]
    src_label = {"override": "provided via --current",
                 "detected": "auto-detected",
                 "selected": "selected"}[method]

    ctx = {
        "rules": rules,
        "upgrade_type": upgrade_type,
        "source_version": source,
        "source_major": smajor,
    }

    if upgrade_type == "minor":
        minors = rules["minors"].get(smajor) or rules["minors"].get(str(smajor)) or []
        try:
            idx = minors.index(source)
        except ValueError:
            idx = -1
        if idx >= 0 and idx + 1 < len(minors):
            target = minors[idx + 1]
        else:
            out.line()
            out.sev("info",
                    "You are already on %s, the latest minor of RHEL %d. There is no "
                    "further minor update. To move to the next major, re-run this tool "
                    "and choose Major Upgrade." % (source, smajor))
            ctx["stop"] = True
            return ctx
        out.line()
        out.line("Source Version : RHEL %s (%s)" % (source, src_label))
        out.line("Target Version : RHEL %s (auto-populated — next minor)" % target)
        out.sev("info",
                "You can only upgrade to the next minor version; skipping minors can "
                "cause problems. On an EUS subscription, step only to the next EUS "
                "release.")
    else:  # major
        if source != latest_minor:
            out.line()
            out.sev("warning",
                    "You are on RHEL %s, which is not the latest minor of RHEL %d "
                    "(%s). Reach RHEL %s first: re-run this tool and choose the Minor "
                    "Update option, repeating until you are on %s. Then run it again "
                    "and choose Major Upgrade for the migration."
                    % (source, smajor, latest_minor, latest_minor, latest_minor))
            ctx["stop"] = True
            return ctx
        tmajor = smajor + 1
        if str(tmajor) not in {str(k) for k in rules["majors"]}:
            out.line()
            out.sev("info",
                    "RHEL %d is the latest major with no defined successor in the "
                    "catalog. No major migration is available." % smajor)
            ctx["stop"] = True
            return ctx
        target = rules["majors"][tmajor]["latest_minor"]
        out.line()
        out.line("Source Version : RHEL %s (%s, latest minor of RHEL %d)"
                 % (source, src_label, smajor))
        out.line("Target Version : RHEL %s (auto-populated — latest minor of RHEL %d)"
                 % (target, tmajor))
        out.sev("info",
                "Major moves are a replica-based migration, not an in-place upgrade. "
                "This advises one major at a time; re-run afterwards to continue.")

    # Downgrade guard (forward-only).
    if vt(target) <= vt(source):
        out.sev("error", "Target (%s) is not ahead of source (%s). Downgrades are not "
                          "supported." % (target, source))
        ctx["stop"] = True
        return ctx

    ctx["target_version"] = target
    ctx["target_major"] = vt(target)[0]
    ctx["interval"] = (vt(source), vt(target))
    return ctx


def step_deprecations(out, cat, ctx):
    out.heading("Step 2 / 5 — Deprecated and Removed Functionalities")
    lo, hi = ctx["interval"]
    buckets = match_deprecations(cat["deprecations"], lo, hi)

    # Concise default: show only a count + hint; full list requires --all.
    if not ctx.get("full"):
        total = sum(len(v) for v in buckets.values())
        if total:
            out.line("%d deprecated/removed/changed item(s) affect the path (%s, %s]. "
                     "Run with --all to list them." % (total, fmt(lo), fmt(hi)))
        else:
            out.line("No deprecated/removed/changed functionality recorded for this path.")
        return

    out.line("Changes landing in the path (%s, %s]:" % (fmt(lo), fmt(hi)))
    titles = [("removed", "Removed", "error"),
              ("deprecated", "Deprecated", "warning"),
              ("changed", "Changed", "info")]
    any_hit = False
    for key, label, sev in titles:
        entries = buckets[key]
        if not entries:
            continue
        any_hit = True
        out.subheading("%s (%d)" % (label, len(entries)))
        for e, ver in sorted(entries, key=lambda x: vt(x[1])):
            out.sev(e.get("severity", sev),
                    "%s — %s [RHEL %s]" % (e["component"], e["feature"], ver))
            if e.get("remediation"):
                out.line("    remediation: %s" % e["remediation"])
            if e.get("detect"):
                out.line("    check on your box: %s" % e["detect"], "dim")
    if not any_hit:
        out.line()
        out.line("No deprecated/removed/changed functionality recorded for this path.")


def step_prerequisites(out, cat, ctx):
    out.heading("Step 3 / 5 — Prerequisites")
    tmajor = str(ctx["target_major"])
    smajor, sminor = vt(ctx["target_version"])
    for p in cat["prerequisites"].get("prerequisites", []):
        out.subheading(p["title"])
        out.sev(p.get("severity", "info"), p.get("text", ""))
        docs = p.get("docs", {})
        if "pattern" in docs:
            url = docs["pattern"].replace("<major>", tmajor).replace(
                "<minor>", str(sminor))
            out.line("    docs: %s" % url, "dim")
        elif tmajor in docs:
            out.line("    docs: %s" % docs[tmajor], "dim")


def step_environment_checks(out, cat, ctx):
    out.heading("Step 4 / 5 — Environment Checks")
    out.sev("warning",
            "This tool is a convenience aid: it generates IdM upgrade steps tailored to "
            "the inputs you provide. It does NOT auto-detect the Step 4 options (server "
            "configuration, HSM, replicas), so you must verify your actual IdM server "
            "configuration and answer them accurately before confirming — incorrect "
            "answers produce incorrect commands.")
    checks = cat["prerequisites"].get("environment_checks", {})

    # Server configuration and HSM only affect the MAJOR (replica-based) migration
    # runbook; they have no bearing on a minor dnf upgrade, so skip them for minor.
    is_major = ctx.get("upgrade_type") == "major"

    if is_major:
        sc = checks.get("server_configuration", {})
        if sc:
            opts = list(sc["options"].items())  # preserves catalog order
            config = choose(sc["question"], opts)
            ctx["server_configuration"] = config
            out.line()
            out.line("Configuration: %s" % sc["options"][config])

        # HSM stores CA/KRA keys, so it only applies when the chosen configuration has a
        # CA. For no-CA configurations there are no CA keys to place on an HSM.
        # ...and only when the SOURCE version supports HSM (TP from 9.5, GA from 9.6/10).
        hsm = checks.get("hsm", {})
        _, cfg_has_ca, _ = _server_config_flags(ctx)
        src_ver = vt(ctx.get("source_version")) or (0, 0)
        if hsm and cfg_has_ca and src_ver >= HSM_MIN_VERSION:
            ctx["hsm"] = yes_no(hsm["question"])

    rep = checks.get("replicas", {})
    if rep:
        uses_rep = yes_no(rep["question"])
        ctx["replicas"] = uses_rep
        if uses_rep:
            out.sev("info",
                    "Red Hat supports environments with up to %d replicas. Migrate all "
                    "servers quickly and wait >= 10 minutes between each so replication "
                    "can settle." % rep.get("support_limit", 60))


# --------------------------------------------------------------------------- #
# Pre-upgrade health check (1.0). Executes ipa-healthcheck read-only when
# available, otherwise falls back to printed guidance. Soft gate.
# --------------------------------------------------------------------------- #
# Map ipa-healthcheck result levels to our severities.
HC_SEVERITY = {"CRITICAL": "error", "ERROR": "error", "WARNING": "warning",
               "INFO": "info", "SUCCESS": "info"}
HC_TIMEOUT = 300  # seconds


def _load_healthcheck(out, args):
    """Return (results_list, note). results_list is None when nothing was run;
    `note` explains why (shown to the user)."""
    if args.skip_healthcheck:
        return None, "skipped via --skip-healthcheck"

    # Dev/test convenience: read a saved ipa-healthcheck JSON instead of executing.
    if args.healthcheck_json:
        try:
            with open(args.healthcheck_json) as fh:
                return json.load(fh), "loaded from %s" % args.healthcheck_json
        except (OSError, ValueError) as exc:
            return None, "could not read --healthcheck-json (%s)" % exc

    exe = shutil.which("ipa-healthcheck")
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    if not exe:
        return None, ("ipa-healthcheck not found — this step runs on a real IdM "
                      "server")
    if not is_root:
        return None, "ipa-healthcheck requires root; re-run with sudo to execute it"

    # Availability confirmed: run it (prompt unless --run-healthcheck forces it).
    if not args.run_healthcheck:
        if not sys.stdin.isatty():
            return None, "non-interactive; pass --run-healthcheck to execute it"
        if not yes_no("Run ipa-healthcheck now?", default=True):
            return None, "declined by user"

    out.line("Running ipa-healthcheck (read-only)…")
    try:
        proc = subprocess.run([exe, "--output-type", "json"],
                              capture_output=True, text=True, timeout=HC_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, "ipa-healthcheck could not be executed (%s)" % exc
    # ipa-healthcheck exits 1 when it finds issues but still prints JSON on stdout.
    try:
        return json.loads(proc.stdout or "[]"), "executed"
    except ValueError:
        return None, "ipa-healthcheck output was not valid JSON"


def step_healthcheck(out, ctx, args):
    """Pre-upgrade health check with a soft gate. Returns 'proceed' or 'stop'."""
    out.heading("Pre-Upgrade Health Check (ipa-healthcheck)")
    results, note = _load_healthcheck(out, args)

    if results is None:
        out.sev("info", note)
        out.line("Guidance: run `sudo ipa-healthcheck` on each IdM server and resolve "
                 "all findings before upgrading.")
        return "proceed"

    findings = [r for r in results
                if str(r.get("result", "")).upper() != "SUCCESS"]
    out.line("ipa-healthcheck %s: %d checks, %d finding(s)."
             % (note, len(results), len(findings)))
    if not findings:
        out.sev("info", "No issues found — healthcheck is clean.")
        return "proceed"

    grouped = {}
    for r in findings:
        sev = HC_SEVERITY.get(str(r.get("result", "")).upper(), "info")
        grouped.setdefault(sev, []).append(r)
    for sev in ("error", "warning", "info"):
        for r in grouped.get(sev, []):
            src = "%s.%s" % (r.get("source", "?"), r.get("check", "?"))
            kw = r.get("kw") or {}
            msg = ""
            if isinstance(kw, dict):
                msg = kw.get("msg") or kw.get("message") or ""
            out.sev(sev, ("%s [%s] %s" % (src, r.get("result", "?"), msg)).rstrip())

    out.sev("warning",
            "Resolve the ipa-healthcheck findings above before upgrading.")

    # Soft gate: recommend clearing, then ask whether to continue (default No).
    if not sys.stdin.isatty():
        out.sev("info", "Non-interactive: stopping before upgrade steps because "
                        "findings are present (override with --skip-healthcheck).")
        return "stop"
    if yes_no("Proceed to upgrade steps anyway?", default=False):
        return "proceed"
    return "stop"


def step_upgrade_steps(out, cat, ctx):
    out.heading("Step 5 / 5 — Upgrade Steps")
    out.sev("info",
            "Commands below are documented-standard forms; verify them against the "
            "cited RHEL IdM guide for your exact versions before running.")
    refs = ctx["rules"].get("references", {})
    ref = refs.get(str(ctx["target_major"]))
    if ref:
        out.line("Reference: %s — %s" % (ref["title"], ref["url"]))

    for bp in ctx["rules"].get("global_rules", {}).get("best_practices", []):
        out.sev(bp.get("severity", "info"), bp["text"])

    render_backup_step(out, ctx)

    if ctx["upgrade_type"] == "minor":
        render_minor_steps(out, ctx)
    else:
        render_major_steps(out, ctx)


def section_known_issues(out, cat, ctx):
    """Known issues still open at the target version (cumulative, start-tagged)."""
    hits = match_known_issues(cat["known_issues"], ctx["target_version"])
    if not hits:
        return
    out.heading("Known issues open at RHEL %s" % ctx["target_version"])
    out.line("IdM known issues (from the per-minor Release Notes) that are still open "
             "at your target version. Listed cumulatively like the Red Hat docs, each "
             "tagged with the version it started in — these are version-specific, not a "
             "flat cumulative dump.")
    link = section_doc_link(cat["known_issues"], ctx)
    if link:
        out.line("Release notes: %s" % link, "dim")
    for k, since in sorted(hits, key=lambda x: (vt(x[1]) or (0, 0),
                                                x[0].get("component", ""))):
        out.sev(k.get("severity", "warning"),
                "%s — %s [since RHEL %s]" % (k.get("component", "idm"),
                                             k["feature"], since))
        if k.get("remediation"):
            out.line("    remediation: %s" % k["remediation"])
        if k.get("detect"):
            out.line("    check on your box: %s" % k["detect"], "dim")


def section_new_features(out, cat, ctx):
    """Value-add section: IdM new features gained along the resolved path."""
    lo, hi = ctx["interval"]
    hits = match_new_features(cat["new_features"], lo, hi, ctx["target_major"])
    if not hits:
        return
    if not ctx.get("full"):
        out.line("New IdM features in RHEL %s: %d available (run with --all to list)."
                 % (ctx["target_version"], len(hits)))
        return
    out.heading("New IdM features you gain (RHEL %s -> %s)"
                % (ctx["source_version"], ctx["target_version"]))
    out.line("IdM 'New features and enhancements' announced along your path (from the "
             "per-minor Release Notes). Each is tagged with the version that added it — "
             "version-specific, not a flat cumulative dump. RHEL 10.2 has no IdM "
             "new-features section.")
    link = section_doc_link(cat["new_features"], ctx)
    if link:
        out.line("Release notes: %s" % link, "dim")
    for f, added in sorted(hits, key=lambda x: (vt(x[1]) or (0, 0),
                                                x[0].get("component", ""))):
        out.sev("info", "%s — %s [new in RHEL %s]"
                % (f.get("component", "idm"), f["feature"], added))


def section_tech_previews(out, cat, ctx):
    """Value-add section (#4): TP features relevant to the resolved path."""
    lo, hi = ctx["interval"]
    hits = match_tech_previews(cat["tech_previews"], lo, hi, ctx["target_version"])
    if not hits:
        return
    if not ctx.get("full"):
        out.line("Technology Previews relevant to your path: %d (run with --all to list)."
                 % len(hits))
        return
    out.heading("Technology Previews relevant to your path (value-add)")
    out.line("Not part of the web portal — features that are/were Technology Preview "
             "along RHEL %s. Confirm any you rely on before migrating."
             % ctx["target_version"])
    for e, status in sorted(hits, key=lambda x: (x[0].get("component", ""),
                                                 x[0].get("introduced_tp", ""))):
        out.subheading("%s — %s" % (e.get("component", "idm"), e["feature"]))
        out.line("    status: %s (TP since RHEL %s)" % (status, e.get("introduced_tp")))
        if e.get("detect"):
            out.line("    check on your box: %s" % e["detect"], "dim")


# --------------------------------------------------------------------------- #
# Driver.
# --------------------------------------------------------------------------- #
def run_wizard(out, cat, args):
    out.heading("Red Hat IdM Upgrade Helper")
    out.line("Read-only upgrade advisor for RHEL IdM / FreeIPA. This tool never "
             "changes your system.")

    ctx = step_upgrade_options(out, cat, args)
    if ctx.get("stop"):
        return 1
    ctx["full"] = getattr(args, "all", False)
    if not ctx["full"]:
        out.line("Concise view: upgrade steps, warnings, and known issues. "
                 "Run with --all for deprecated/removed functionality, Technology "
                 "Previews, and new features.")
    step_deprecations(out, cat, ctx)
    section_known_issues(out, cat, ctx)
    step_prerequisites(out, cat, ctx)
    step_environment_checks(out, cat, ctx)
    if step_healthcheck(out, ctx, args) == "stop":
        out.heading("Stopped")
        out.line("Resolve the ipa-healthcheck findings above, then re-run this tool.")
        return 1
    step_upgrade_steps(out, cat, ctx)
    section_tech_previews(out, cat, ctx)
    section_new_features(out, cat, ctx)

    out.heading("Done")
    out.line("Summary: %s upgrade from RHEL %s to RHEL %s."
             % (ctx["upgrade_type"], ctx["source_version"], ctx["target_version"]))
    return 0


def _add_common_args(parser, suppress=False):
    """Add the shared options to a parser.

    On subparsers, pass suppress=True so an unspecified option does NOT reset the
    value already parsed before the subcommand (argparse parents gotcha). The main
    parser supplies the real defaults; SUPPRESS on the subparser just means "leave
    whatever is already there" when the flag is absent after the subcommand.
    """
    extra = {"default": argparse.SUPPRESS} if suppress else {}
    parser.add_argument("--current", metavar="X.Y", **extra,
                        help="override the auto-detected source RHEL version")
    parser.add_argument("--all", action="store_true", **extra,
                        help="show everything: deprecated/removed functionality, "
                             "Technology Previews, and new features (default view is "
                             "concise: steps, warnings, and known issues)")
    parser.add_argument("--report", metavar="FILE", **extra,
                        help="also write the full result to FILE (plain text)")
    parser.add_argument("--no-color", action="store_true", **extra,
                        help="disable colored output")
    parser.add_argument("--run-healthcheck", action="store_true", **extra,
                        help="run ipa-healthcheck without prompting (when available)")
    parser.add_argument("--skip-healthcheck", action="store_true", **extra,
                        help="skip the pre-upgrade ipa-healthcheck step")
    parser.add_argument("--healthcheck-json", metavar="FILE", **extra,
                        help="read ipa-healthcheck JSON from FILE instead of running "
                             "it (dev/test convenience)")


EXAMPLES = """\
upgrade types:
  minor   In-place update to the next minor release within the same major
          (e.g. RHEL 9.6 -> 9.7). Walks you through setting the target release,
          cleaning the package cache, and running `dnf upgrade`/`dnf distro-sync`
          on each server in the topology.
  major   Migration to the next major release's latest minor (e.g. RHEL 9 -> 10).
          In-place upgrades are NOT supported for IdM servers: this mode advises
          the replica-based path -- stand up new major-version IdM replicas
          alongside the existing environment, move the CA renewal role, then
          retire the old servers.

examples:
  # Interactive: auto-detect this host's RHEL version and prompt for everything
  idm-upgrade-helper

  # Advise a minor update, overriding the detected source version
  idm-upgrade-helper --current 9.6 minor

  # Show EVERYTHING (deprecated/removed, Technology Previews, new features);
  # the default view is concise: steps, warnings, and known issues
  idm-upgrade-helper --all minor

  # Advise a major migration and save a plain-text copy of the advice
  idm-upgrade-helper major --report ~/idm-major-upgrade.txt

  # Run the pre-upgrade ipa-healthcheck without prompting
  idm-upgrade-helper --run-healthcheck minor

  # Skip the healthcheck entirely (e.g. on a host where it is not installed)
  idm-upgrade-helper --skip-healthcheck minor

Read-only: this tool is advisory-only and never changes the system.
"""


def build_parser():
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Interactive, read-only RHEL IdM / FreeIPA upgrade advisor.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--version", action="version",
        version="%(prog)s " + VERSION)
    _add_common_args(parser)
    sub = parser.add_subparsers(
        dest="upgrade_type", metavar="{minor,major}",
        help="optionally choose the upgrade type directly (otherwise you are prompted)")
    minor = sub.add_parser(
        "minor", help="in-place update to the next minor (x.y -> x.y+1)")
    _add_common_args(minor, suppress=True)
    major = sub.add_parser(
        "major",
        help="replica-based migration to the next major's latest minor")
    _add_common_args(major, suppress=True)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    out = Emitter()
    if args.no_color:
        out.use_color = False

    cat = load_catalogs()
    try:
        rc = run_wizard(out, cat, args)
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        return 130

    if args.report:
        out.write_report(args.report)
        print("\nReport written to %s" % args.report)
    return rc


if __name__ == "__main__":
    sys.exit(main())
