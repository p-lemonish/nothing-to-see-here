from __future__ import annotations

import os
import subprocess


def default_iface() -> str | None:
    return os.getenv("NIC") or os.getenv("WFB_IFACE") or os.getenv("IFACE")


def iw_interfaces() -> list[str]:
    try:
        result = subprocess.run(
            ["iw", "dev"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    if result.returncode != 0:
        return []

    interfaces: list[str] = []
    for line in result.stdout.splitlines():
        text = line.strip()
        if text.startswith("Interface "):
            parts = text.split()
            if len(parts) >= 2:
                interfaces.append(parts[1])
    return interfaces


def resolve_iface(value: str | None, *, purpose: str) -> str:
    if value:
        return value

    env_iface = default_iface()
    if env_iface:
        return env_iface

    interfaces = iw_interfaces()
    if len(interfaces) == 1:
        return interfaces[0]

    if not interfaces:
        raise SystemExit(
            f"--iface is required for {purpose}; no interface could be auto-detected. "
            "Run `iw dev` and pass the Interface value, or export NIC=<iface>."
        )

    joined = ", ".join(interfaces)
    raise SystemExit(
        f"--iface is required for {purpose}; multiple interfaces found: {joined}. "
        "Pass --iface explicitly or export NIC=<iface>."
    )
