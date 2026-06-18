#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def run(command, check=True):
    result = subprocess.run(command, text=True, capture_output=True)
    if check and result.returncode != 0:
        raise RuntimeError(json.dumps({
            "command": command,
            "rc": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }, ensure_ascii=False))
    return result


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def configs(directory):
    directory = Path(directory)
    if not directory.exists():
        return {}
    return {path.stem: sha256(path) for path in directory.glob("*.conf") if path.is_file()}


def enabled_systemd():
    enabled = set()
    prefix = "wg-quick@"
    suffix = ".service"

    for root in (Path("/etc/systemd/system"), Path("/run/systemd/system")):
        if not root.exists():
            continue
        for path in root.rglob("wg-quick@*.service"):
            unit = path.name
            if unit == "wg-quick@.service":
                continue
            if unit.startswith(prefix) and unit.endswith(suffix):
                enabled.add(unit[len(prefix):-len(suffix)])

    result = run([
        "systemctl",
        "list-unit-files",
        "wg-quick@*.service",
        "--state=enabled",
        "--no-legend",
        "--no-pager",
    ], check=False)
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        unit = line.split()[0]
        if unit.startswith(prefix) and unit.endswith(suffix):
            enabled.add(unit[len(prefix):-len(suffix)])
    return enabled


def enabled_openrc():
    result = run(["rc-update", "show", "default"], check=False)
    enabled = set()
    for line in result.stdout.splitlines():
        for token in line.split():
            if token.startswith("wg-quick."):
                enabled.add(token.removeprefix("wg-quick."))
    return enabled


def service_command(service_manager, action, interface):
    if service_manager == "systemd":
        return ["systemctl", action, f"wg-quick@{interface}.service"]
    if service_manager == "openrc":
        if action == "enable":
            return ["rc-update", "add", f"wg-quick.{interface}", "default"]
        if action == "disable":
            return ["rc-update", "del", f"wg-quick.{interface}", "default"]
        return ["rc-service", f"wg-quick.{interface}", action]
    raise ValueError(f"unsupported service manager: {service_manager}")


def ensure_openrc_link(interface, dry_run):
    path = Path(f"/etc/init.d/wg-quick.{interface}")
    if dry_run:
        return
    if path.exists() or path.is_symlink():
        path.unlink()
    path.symlink_to("/etc/init.d/wg-quick")


def remove_openrc_link(interface, dry_run):
    path = Path(f"/etc/init.d/wg-quick.{interface}")
    if not dry_run and (path.exists() or path.is_symlink()):
        path.unlink()


def service_result_record(interface, action, command, result, ignore_failure=False):
    record = {
        "interface": interface,
        "action": action,
        "command": command,
        "rc": result.returncode,
    }
    if result.stdout:
        record["stdout"] = result.stdout
    if result.stderr:
        record["stderr"] = result.stderr
    if result.returncode != 0 and not ignore_failure:
        record["failed"] = True
    return record


def apply_service_action(service_manager, action, interface, dry_run, ignore_failure=False):
    command = service_command(service_manager, action, interface)
    if dry_run:
        return {
            "interface": interface,
            "action": action,
            "command": command,
            "rc": 0,
            "dry_run": True,
        }

    result = run(command, check=False)
    record = service_result_record(interface, action, command, result, ignore_failure)

    if service_manager == "openrc" and action in {"start", "restart"} and "already exists" in result.stderr:
        cleanup_command = ["ip", "link", "delete", "dev", interface]
        cleanup_result = run(cleanup_command, check=False)
        retry_result = run(command, check=False)
        record = service_result_record(interface, action, command, retry_result, ignore_failure)
        record["recovered_from"] = service_result_record(interface, action, command, result, True)
        record["cleanup"] = service_result_record(interface, "delete-stale-interface", cleanup_command, cleanup_result, True)

    return record


def backup_live(live_dir, backup_dir):
    live = Path(live_dir)
    if not live.exists():
        return False
    Path(backup_dir).mkdir(parents=True, mode=0o700, exist_ok=True)
    run(["rsync", "-a", f"{live}/", f"{backup_dir}/"])
    return True


def promote(staging_dir, live_dir):
    Path(live_dir).mkdir(parents=True, mode=0o700, exist_ok=True)
    run([
        "rsync",
        "-a",
        "--delete",
        "--chmod=D700,F600",
        "--chown=root:root",
        f"{staging_dir}/",
        f"{live_dir}/",
    ])


def restore(backup_dir, live_dir):
    backup = Path(backup_dir)
    if backup.exists():
        run(["rsync", "-a", "--delete", f"{backup}/", f"{live_dir}/"])
        return True
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging-dir", required=True)
    parser.add_argument("--live-dir", required=True)
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--service-manager", choices=["systemd", "openrc"], default="systemd")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    staging_dir = Path(args.staging_dir)
    live_dir = Path(args.live_dir)
    if not staging_dir.is_dir():
        print(json.dumps({"error": f"staging dir does not exist: {staging_dir}"}, ensure_ascii=False), file=sys.stderr)
        return 1

    staging_configs = configs(staging_dir)
    live_configs = configs(live_dir)
    desired = set(staging_configs)
    live = set(live_configs)
    enabled = enabled_systemd() if args.service_manager == "systemd" else enabled_openrc()

    added = sorted(desired - live)
    changed = sorted(name for name in desired & live if staging_configs[name] != live_configs[name])
    removed = sorted((live | enabled) - desired)
    missing_enabled = sorted(desired - enabled)
    start = added
    restart = changed

    service_results = []
    service_failures = []
    backup_available = False
    promoted = False
    restored = False

    summary = {
        "changed": bool(added or changed or removed or missing_enabled),
        "dry_run": args.dry_run,
        "service_manager": args.service_manager,
        "added": added,
        "changed_configs": changed,
        "removed": removed,
        "missing_enabled": missing_enabled,
        "start": start,
        "restart": restart,
        "backup_dir": args.backup_dir,
        "promoted": False,
        "restored": False,
        "service_results": service_results,
        "service_failures": service_failures,
    }

    try:
        if args.dry_run:
            print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
            return 0

        backup_available = backup_live(live_dir, args.backup_dir)

        for interface in removed:
            service_results.append(apply_service_action(args.service_manager, "stop", interface, False, ignore_failure=True))
            service_results.append(apply_service_action(args.service_manager, "disable", interface, False, ignore_failure=True))
            if args.service_manager == "openrc":
                remove_openrc_link(interface, False)

        promote(staging_dir, live_dir)
        promoted = True
        summary["promoted"] = True

        for interface in missing_enabled:
            if args.service_manager == "openrc":
                ensure_openrc_link(interface, False)
            result = apply_service_action(args.service_manager, "enable", interface, False)
            service_results.append(result)
            if result.get("failed"):
                service_failures.append(result)

        for interface in start:
            result = apply_service_action(args.service_manager, "start", interface, False)
            service_results.append(result)
            if result.get("failed"):
                service_failures.append(result)

        for interface in restart:
            result = apply_service_action(args.service_manager, "restart", interface, False)
            service_results.append(result)
            if result.get("failed"):
                service_failures.append(result)

        print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
        return 1 if service_failures else 0

    except Exception as error:
        if promoted and backup_available:
            restored = restore(args.backup_dir, live_dir)
        summary["error"] = str(error)
        summary["promoted"] = promoted
        summary["restored"] = restored
        print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
