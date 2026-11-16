"""device_port_reporter.py

Device Port Reporter — parse a Cisco NX-OS / IOS / IOS-XE ``show
running-config`` (or ``show running-config interface``) capture and
produce an Excel workbook that lists every interface with its Layer-2
mode, access VLAN, native VLAN, trunk-allowed VLAN list, port-channel
membership and other useful context.

The parser is intentionally forgiving: it accepts partial captures
(e.g. only the interface sections), handles multiple indentation
styles, and copes with the incremental ``switchport trunk allowed vlan
add/remove/except`` verbs the way NX-OS applies them.

CLI usage:

    python device_port_reporter.py -i show_run.txt -o interfaces.xlsx

    # CSV instead of XLSX (no openpyxl needed):
    python device_port_reporter.py -i show_run.txt -o interfaces.csv --format csv

    # Read from stdin, write to default filename (interfaces.xlsx):
    Get-Content show_run.txt | python device_port_reporter.py

Library usage:

    from device_port_reporter import parse_config, write_xlsx
    interfaces = parse_config(open("show_run.txt").read())
    write_xlsx(interfaces, "interfaces.xlsx")   # or pass a BytesIO
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_VLAN = 1
MAX_VLAN = 4094
ALL_VLANS: frozenset[int] = frozenset(range(MIN_VLAN, MAX_VLAN + 1))

INTERFACE_TYPE_MAP: dict[str, str] = {
    "ethernet": "Ethernet",
    "eth": "Ethernet",
    "gigabitethernet": "GigabitEthernet",
    "gi": "GigabitEthernet",
    "tengigabitethernet": "TenGigabitEthernet",
    "te": "TenGigabitEthernet",
    "twentyfivegige": "TwentyFiveGigE",
    "fortygigabitethernet": "FortyGigabitEthernet",
    "fo": "FortyGigabitEthernet",
    "hundredgige": "HundredGigE",
    "hu": "HundredGigE",
    "port-channel": "port-channel",
    "po": "port-channel",
    "vlan": "Vlan",
    "vl": "Vlan",
    "mgmt": "mgmt",
    "management": "mgmt",
    "loopback": "Loopback",
    "lo": "Loopback",
    "tunnel": "Tunnel",
    "tu": "Tunnel",
    "nve": "NVE",
    "fastethernet": "FastEthernet",
    "fa": "FastEthernet",
    "serial": "Serial",
    "se": "Serial",
    "bdi": "BDI",
    "bvi": "BVI",
    "dialer": "Dialer",
}

_INTERFACE_HDR_RE = re.compile(r"^\s*interface\s+(\S.*?)\s*$", re.IGNORECASE)
_IFACE_SPLIT_RE = re.compile(r"^([A-Za-z\-]+)(.*)$")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Interface:
    """Parsed representation of a single interface stanza."""

    name: str
    type: str = ""
    unit: str = ""
    description: str = ""
    switchport: Optional[bool] = None
    mode: str = ""
    access_vlan: Optional[int] = None
    voice_vlan: Optional[int] = None
    native_vlan: Optional[int] = None
    allowed_vlan_ops: list[tuple[str, str]] = field(default_factory=list)
    allowed_vlans_raw: str = ""
    channel_group: Optional[int] = None
    channel_group_mode: str = ""
    vpc: str = ""
    speed: str = ""
    duplex: str = ""
    mtu: str = ""
    ip_address: str = ""
    ipv6_address: str = ""
    vrf: str = ""
    shutdown: bool = False
    fex_associate: str = ""
    stp_port_type: str = ""
    encapsulation: str = ""
    other_flags: list[str] = field(default_factory=list)
    raw_lines: list[str] = field(default_factory=list)

    @property
    def admin_status(self) -> str:
        return "shutdown" if self.shutdown else "no shutdown"

    @property
    def is_switchport(self) -> bool:
        if self.switchport is True:
            return True
        if self.switchport is False:
            return False
        if self.ip_address or self.vrf or self.ipv6_address:
            return False
        if self.type in {
            "Ethernet", "GigabitEthernet", "TenGigabitEthernet",
            "TwentyFiveGigE", "FortyGigabitEthernet", "HundredGigE",
            "FastEthernet", "port-channel",
        }:
            return True
        return False

    @property
    def effective_mode(self) -> str:
        if not self.is_switchport:
            if self.type in {"Vlan", "Loopback", "Tunnel", "mgmt", "BDI", "BVI"}:
                return "n/a"
            return "routed"
        if self.mode:
            return self.mode
        return "access"

    @property
    def allowed_vlans(self) -> list[int]:
        return _reduce_allowed_vlan_ops(self.allowed_vlan_ops)

    @property
    def allowed_vlans_display(self) -> str:
        mode = self.effective_mode
        if mode == "access":
            return str(self.access_vlan) if self.access_vlan else "1"
        if mode == "trunk":
            if not self.allowed_vlan_ops:
                return "all (default 1-4094)"
            return _format_vlan_ranges(self.allowed_vlans)
        if mode == "fex-fabric":
            return "fex-fabric"
        if mode.startswith("private-vlan") or mode == "dot1q-tunnel":
            return self.allowed_vlans_raw or mode
        return ""


# ---------------------------------------------------------------------------
# VLAN helpers
# ---------------------------------------------------------------------------

def _parse_vlan_ranges(spec: str) -> set[int]:
    vlans: set[int] = set()
    if not spec:
        return vlans
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            try:
                start_s, end_s = chunk.split("-", 1)
                start, end = int(start_s.strip()), int(end_s.strip())
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            start = max(start, MIN_VLAN)
            end = min(end, MAX_VLAN)
            vlans.update(range(start, end + 1))
        else:
            try:
                v = int(chunk)
            except ValueError:
                continue
            if MIN_VLAN <= v <= MAX_VLAN:
                vlans.add(v)
    return vlans


def _format_vlan_ranges(vlans: Iterable[int]) -> str:
    v = sorted({int(x) for x in vlans if MIN_VLAN <= int(x) <= MAX_VLAN})
    if not v:
        return "none"
    if len(v) == len(ALL_VLANS) and v[0] == MIN_VLAN and v[-1] == MAX_VLAN:
        return "all (1-4094)"
    ranges: list[str] = []
    start = prev = v[0]
    for cur in v[1:]:
        if cur == prev + 1:
            prev = cur
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = cur
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


def _reduce_allowed_vlan_ops(ops: list[tuple[str, str]]) -> list[int]:
    if not ops:
        return sorted(ALL_VLANS)
    current: set[int] = set(ALL_VLANS)
    first_set_seen = False
    for verb, value in ops:
        verb = verb.lower()
        value = value.strip()
        if verb in {"set", "", "list"}:
            current = _parse_vlan_ranges(value)
            first_set_seen = True
        elif verb == "all":
            current = set(ALL_VLANS)
            first_set_seen = True
        elif verb == "none":
            current = set()
            first_set_seen = True
        elif verb == "except":
            current = set(ALL_VLANS) - _parse_vlan_ranges(value)
            first_set_seen = True
        elif verb == "add":
            if not first_set_seen:
                current.update(_parse_vlan_ranges(value))
            else:
                current.update(_parse_vlan_ranges(value))
        elif verb == "remove":
            current.difference_update(_parse_vlan_ranges(value))
    return sorted(current)


# ---------------------------------------------------------------------------
# Interface name helpers
# ---------------------------------------------------------------------------

def _normalise_interface(name: str) -> tuple[str, str]:
    m = _IFACE_SPLIT_RE.match(name.strip())
    if not m:
        return ("", name.strip())
    prefix, rest = m.group(1), m.group(2).strip()
    key = prefix.lower()
    canonical = INTERFACE_TYPE_MAP.get(key, prefix)
    return (canonical, rest)


def _natural_sort_key(unit: str) -> list:
    parts = re.split(r"([0-9]+)", unit)
    key: list = []
    for p in parts:
        if p.isdigit():
            key.append(int(p))
        elif p:
            key.append(p)
    return key


# ---------------------------------------------------------------------------
# Config parser
# ---------------------------------------------------------------------------

def parse_config(text: str) -> list[Interface]:
    """Parse a full or partial running-config into Interface objects."""
    interfaces: list[Interface] = []
    current: Optional[Interface] = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        header_match = _INTERFACE_HDR_RE.match(line)
        starts_flush = len(line) - len(line.lstrip(" ")) == 0
        if header_match and starts_flush:
            if current is not None:
                interfaces.append(current)
            iface_name = header_match.group(1).strip()
            itype, unit = _normalise_interface(iface_name)
            current = Interface(name=f"{itype}{unit}" if itype else iface_name,
                                type=itype, unit=unit)
            current.raw_lines.append(line)
            continue

        if starts_flush and current is not None and not line.startswith("!"):
            interfaces.append(current)
            current = None
            continue

        if current is None:
            continue

        current.raw_lines.append(line)
        _apply_interface_command(current, line.strip())

    if current is not None:
        interfaces.append(current)

    interfaces.sort(key=lambda i: (i.type or "~", _natural_sort_key(i.unit)))
    return interfaces


# ---------------------------------------------------------------------------
# Per-command handlers
# ---------------------------------------------------------------------------

def _apply_interface_command(iface: Interface, cmd: str) -> None:  # noqa: PLR0912, PLR0915
    if not cmd or cmd.startswith("!"):
        return
    lower = cmd.lower()

    if lower == "shutdown":
        iface.shutdown = True
        return
    if lower in {"no shutdown", "no shut"}:
        iface.shutdown = False
        return

    if lower.startswith("description "):
        iface.description = cmd[len("description "):].strip()
        return

    if lower == "switchport":
        iface.switchport = True
        return
    if lower == "no switchport":
        iface.switchport = False
        return

    if lower.startswith("switchport mode "):
        mode = cmd[len("switchport mode "):].strip().lower()
        iface.mode = mode
        iface.switchport = True
        return

    m = re.match(r"switchport access vlan\s+(\d+)$", lower)
    if m:
        iface.access_vlan = int(m.group(1))
        iface.switchport = True
        return

    m = re.match(r"switchport voice vlan\s+(\d+)$", lower)
    if m:
        iface.voice_vlan = int(m.group(1))
        return

    m = re.match(r"switchport trunk native vlan\s+(\d+)$", lower)
    if m:
        iface.native_vlan = int(m.group(1))
        iface.switchport = True
        return

    m = re.match(r"switchport trunk allowed vlan(?:s)?\s*(.*)$", lower)
    if m:
        payload = m.group(1).strip()
        iface.switchport = True
        iface.allowed_vlans_raw = payload
        if not payload:
            return
        verb_match = re.match(r"(add|remove|except|all|none)\b\s*(.*)$", payload)
        if verb_match:
            verb = verb_match.group(1)
            value = verb_match.group(2).strip()
            iface.allowed_vlan_ops.append((verb, value))
        else:
            iface.allowed_vlan_ops.append(("set", payload))
        return

    m = re.match(r"channel-group\s+(\d+)(?:\s+mode\s+(\S+))?", lower)
    if m:
        iface.channel_group = int(m.group(1))
        iface.channel_group_mode = (m.group(2) or "").strip()
        return

    m = re.match(r"vpc\s+(peer-link|\d+)$", lower)
    if m:
        iface.vpc = m.group(1)
        return

    if lower.startswith("speed "):
        iface.speed = cmd.split(None, 1)[1].strip()
        return
    if lower.startswith("duplex "):
        iface.duplex = cmd.split(None, 1)[1].strip()
        return
    if lower.startswith("mtu "):
        iface.mtu = cmd.split(None, 1)[1].strip()
        return

    m = re.match(r"ip address\s+(.+)$", lower)
    if m:
        iface.ip_address = cmd.split(None, 2)[-1].strip()
        if iface.switchport is None:
            iface.switchport = False
        return

    m = re.match(r"ipv6 address\s+(.+)$", lower)
    if m:
        iface.ipv6_address = cmd.split(None, 2)[-1].strip()
        return

    # VRF names are case-sensitive on the device.
    m = re.match(r"vrf\s+member\s+(\S+)\s*$", cmd, re.IGNORECASE) or re.match(
        r"ip vrf forwarding\s+(\S+)\s*$", cmd, re.IGNORECASE
    )
    if m:
        iface.vrf = m.group(1)
        return

    if lower.startswith("fex associate "):
        iface.fex_associate = cmd.split(None, 2)[-1].strip()
        return

    if lower.startswith("spanning-tree port type "):
        iface.stp_port_type = cmd.split(None, 3)[-1].strip()
        return

    m = re.match(r"encapsulation dot1q\s+(\d+)(?:\s+native)?$", lower)
    if m:
        iface.encapsulation = f"dot1q {m.group(1)}"
        return

    interesting_prefixes = (
        "storm-control",
        "service-policy",
        "no cdp enable",
        "port-security",
        "beacon",
    )
    if any(lower.startswith(p) for p in interesting_prefixes):
        iface.other_flags.append(cmd)


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

_COLUMNS: list[tuple[str, str]] = [
    ("interface", "Interface"),
    ("type", "Type"),
    ("description", "Description"),
    ("admin_status", "Admin"),
    ("mode", "Mode"),
    ("access_vlan", "Access VLAN"),
    ("voice_vlan", "Voice VLAN"),
    ("native_vlan", "Native VLAN"),
    ("allowed_vlans", "Allowed VLANs"),
    ("allowed_vlan_count", "Allowed VLAN Count"),
    ("port_channel", "Port-Channel"),
    ("channel_mode", "Channel Mode"),
    ("vpc", "vPC"),
    ("speed", "Speed"),
    ("duplex", "Duplex"),
    ("mtu", "MTU"),
    ("ip_address", "IPv4 Address"),
    ("ipv6_address", "IPv6 Address"),
    ("vrf", "VRF"),
    ("stp_port_type", "STP Port Type"),
    ("encapsulation", "Encapsulation"),
    ("fex_associate", "FEX Associate"),
    ("other_flags", "Notable Flags"),
]


def _row_from_interface(iface: Interface) -> dict[str, object]:
    allowed = iface.allowed_vlans if iface.effective_mode == "trunk" else []
    return {
        "interface": iface.name,
        "type": iface.type or "",
        "description": iface.description,
        "admin_status": iface.admin_status,
        "mode": iface.effective_mode,
        "access_vlan": iface.access_vlan if iface.access_vlan else "",
        "voice_vlan": iface.voice_vlan if iface.voice_vlan else "",
        "native_vlan": iface.native_vlan if iface.native_vlan else "",
        "allowed_vlans": iface.allowed_vlans_display,
        "allowed_vlan_count": len(allowed) if iface.effective_mode == "trunk" else "",
        "port_channel": iface.channel_group if iface.channel_group else "",
        "channel_mode": iface.channel_group_mode,
        "vpc": iface.vpc,
        "speed": iface.speed,
        "duplex": iface.duplex,
        "mtu": iface.mtu,
        "ip_address": iface.ip_address,
        "ipv6_address": iface.ipv6_address,
        "vrf": iface.vrf,
        "stp_port_type": iface.stp_port_type,
        "encapsulation": iface.encapsulation,
        "fex_associate": iface.fex_associate,
        "other_flags": "; ".join(iface.other_flags),
    }


def write_csv(interfaces: list[Interface], out_path: Path) -> None:
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([label for _, label in _COLUMNS])
        for iface in interfaces:
            row = _row_from_interface(iface)
            writer.writerow([row[key] for key, _ in _COLUMNS])


def write_xlsx(interfaces: list[Interface], out) -> None:  # noqa: ANN001
    """Write a styled multi-sheet XLSX.

    ``out`` may be a filesystem path (``str`` / ``pathlib.Path``) or any
    file-like object with a ``write`` method; openpyxl's
    ``Workbook.save()`` supports both.  The web UI passes an in-memory
    ``BytesIO``; the CLI passes a ``Path``.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "openpyxl is required for XLSX output. Install with:\n"
            "    pip install openpyxl\n"
            "or use --format csv to skip the dependency."
        ) from exc

    wb = Workbook()
    wb.remove(wb.active)

    header_font = Font(bold=True, color="FFFFFFFF", name="Calibri", size=11)
    header_fill = PatternFill("solid", fgColor="FF0B2447")
    alt_fill = PatternFill("solid", fgColor="FFF4F6F7")
    trunk_fill = PatternFill("solid", fgColor="FFE8F4FF")
    access_fill = PatternFill("solid", fgColor="FFEAF7EA")
    routed_fill = PatternFill("solid", fgColor="FFFFF5E6")
    header_align = Alignment(horizontal="left", vertical="center")
    body_align = Alignment(horizontal="left", vertical="top", wrap_text=True)

    def write_sheet(title: str, rows: list[Interface],
                    mode_colour: PatternFill | None = None) -> None:
        ws = wb.create_sheet(title)
        for col_idx, (_, label) in enumerate(_COLUMNS, start=1):
            cell = ws.cell(row=1, column=col_idx, value=label)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
        for row_idx, iface in enumerate(rows, start=2):
            data = _row_from_interface(iface)
            for col_idx, (key, _) in enumerate(_COLUMNS, start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=data[key])
                cell.alignment = body_align
                if mode_colour is not None:
                    cell.fill = mode_colour
                elif row_idx % 2 == 0:
                    cell.fill = alt_fill
        ws.freeze_panes = "A2"
        last_col_letter = get_column_letter(len(_COLUMNS))
        last_row = max(1, len(rows) + 1)
        ws.auto_filter.ref = f"A1:{last_col_letter}{last_row}"
        widths = {
            "Interface": 22, "Type": 16, "Description": 40, "Admin": 12,
            "Mode": 14, "Access VLAN": 12, "Voice VLAN": 11,
            "Native VLAN": 12, "Allowed VLANs": 45, "Allowed VLAN Count": 14,
            "Port-Channel": 12, "Channel Mode": 12, "vPC": 10,
            "Speed": 10, "Duplex": 10, "MTU": 8,
            "IPv4 Address": 22, "IPv6 Address": 30, "VRF": 16,
            "STP Port Type": 14, "Encapsulation": 16,
            "FEX Associate": 22, "Notable Flags": 40,
        }
        for col_idx, (_, label) in enumerate(_COLUMNS, start=1):
            ws.column_dimensions[get_column_letter(col_idx)].width = widths.get(label, 14)

    write_sheet("All Interfaces", interfaces)
    write_sheet("Trunk Ports",
                [i for i in interfaces if i.effective_mode == "trunk"],
                trunk_fill)
    write_sheet("Access Ports",
                [i for i in interfaces if i.effective_mode == "access"],
                access_fill)
    write_sheet("Routed and L3",
                [i for i in interfaces if i.effective_mode in {"routed", "n/a"}],
                routed_fill)
    _write_summary_sheet(wb, interfaces, header_font, header_fill, body_align)

    wb.save(out)


def _write_summary_sheet(wb, interfaces: list[Interface], header_font,
                         header_fill, body_align) -> None:
    from openpyxl.styles import Font  # local-only need

    ws = wb.create_sheet("Summary", 0)

    total = len(interfaces)
    trunks = [i for i in interfaces if i.effective_mode == "trunk"]
    access = [i for i in interfaces if i.effective_mode == "access"]
    routed = [i for i in interfaces if i.effective_mode == "routed"]
    other = [i for i in interfaces if i.effective_mode not in
             {"trunk", "access", "routed"}]
    shutdown_count = sum(1 for i in interfaces if i.shutdown)
    po_count = sum(1 for i in interfaces if i.type == "port-channel")
    fex_count = sum(1 for i in interfaces if i.mode == "fex-fabric")

    vlan_usage: dict[int, int] = {}
    for i in trunks:
        for v in i.allowed_vlans:
            vlan_usage[v] = vlan_usage.get(v, 0) + 1
    for i in access:
        if i.access_vlan:
            vlan_usage[i.access_vlan] = vlan_usage.get(i.access_vlan, 0) + 1

    kpis = [
        ("Total interfaces", total),
        ("Trunk ports", len(trunks)),
        ("Access ports", len(access)),
        ("Routed / L3 interfaces", len(routed)),
        ("Other (Vlan/Loopback/mgmt/etc.)", len(other)),
        ("Administratively shutdown", shutdown_count),
        ("Port-channels", po_count),
        ("FEX-fabric interfaces", fex_count),
        ("Distinct VLANs referenced", len(vlan_usage)),
    ]

    ws.cell(row=1, column=1, value="Nexus / IOS Interface Inventory").font = \
        Font(bold=True, size=14, color="FF0B2447")

    for idx, (label, value) in enumerate(kpis, start=3):
        ws.cell(row=idx, column=1, value=label).font = Font(bold=True)
        ws.cell(row=idx, column=2, value=value)

    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 20

    if vlan_usage:
        start_row = 4 + len(kpis)
        hdr_row = start_row
        ws.cell(row=hdr_row, column=1, value="VLAN").font = header_font
        ws.cell(row=hdr_row, column=1).fill = header_fill
        ws.cell(row=hdr_row, column=2, value="Ports referencing it").font = header_font
        ws.cell(row=hdr_row, column=2).fill = header_fill
        for offset, (vlan, count) in enumerate(sorted(vlan_usage.items()), start=1):
            ws.cell(row=hdr_row + offset, column=1, value=vlan)
            ws.cell(row=hdr_row + offset, column=2, value=count)


# ---------------------------------------------------------------------------
# Web-UI helper: build the full JSON payload the React/vanilla UI renders.
# Kept here so the web layer stays thin and the CLI stays dependency-free.
# ---------------------------------------------------------------------------

def build_report_payload(interfaces: list[Interface]) -> dict:
    """Shape parsed interfaces into a JSON-serialisable dict for the UI."""
    trunks = [i for i in interfaces if i.effective_mode == "trunk"]
    access = [i for i in interfaces if i.effective_mode == "access"]
    routed = [i for i in interfaces if i.effective_mode in {"routed", "n/a"}]

    vlan_usage: dict[int, int] = {}
    for i in trunks:
        for v in i.allowed_vlans:
            vlan_usage[v] = vlan_usage.get(v, 0) + 1
    for i in access:
        if i.access_vlan:
            vlan_usage[i.access_vlan] = vlan_usage.get(i.access_vlan, 0) + 1

    # Top VLANs (chart would be unreadable at >25).
    top_vlans = sorted(vlan_usage.items(), key=lambda kv: (-kv[1], kv[0]))[:25]

    return {
        "columns": [{"key": key, "label": label} for key, label in _COLUMNS],
        "rows": [_row_from_interface(i) for i in interfaces],
        "kpis": {
            "total": len(interfaces),
            "trunks": len(trunks),
            "access": len(access),
            "routed": len(routed),
            "other": sum(1 for i in interfaces
                         if i.effective_mode not in {"trunk", "access",
                                                     "routed", "n/a"}),
            "shutdown": sum(1 for i in interfaces if i.shutdown),
            "port_channels": sum(1 for i in interfaces if i.type == "port-channel"),
            "fex_fabric": sum(1 for i in interfaces if i.mode == "fex-fabric"),
            "distinct_vlans": len(vlan_usage),
        },
        "vlan_usage_top": [{"vlan": v, "count": c} for v, c in top_vlans],
        "vlan_usage_total": len(vlan_usage),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_input(path: Optional[str]) -> str:
    if path is None or path == "-":
        return sys.stdin.read()
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Input file not found: {resolved}")
    max_bytes = 20 * 1024 * 1024
    if resolved.stat().st_size > max_bytes:
        raise ValueError(
            f"Input file too large ({resolved.stat().st_size} bytes; "
            f"max {max_bytes}). Split the config or raise the limit."
        )
    return resolved.read_text(encoding="utf-8", errors="replace")


def _resolve_output(path: str, fmt: str) -> Path:
    out = Path(path).expanduser().resolve()
    if not out.parent.exists():
        raise FileNotFoundError(f"Output directory does not exist: {out.parent}")
    if out.suffix.lower() not in {".xlsx", ".csv"}:
        out = out.with_suffix(f".{fmt}")
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="device-port-reporter",
        description=(
            "Device Port Reporter — parse a Cisco NX-OS/IOS "
            "'show running-config' capture and produce a spreadsheet "
            "listing every interface, its mode (trunk/access/routed), "
            "and the VLANs it carries."
        ),
    )
    parser.add_argument("-i", "--input",
                        help="Path to the show-run capture. Use '-' or omit to read stdin.")
    parser.add_argument("-o", "--output", default="interfaces.xlsx",
                        help="Output file path (default: interfaces.xlsx).")
    parser.add_argument("--format", choices=("xlsx", "csv"), default=None,
                        help="Output format. Inferred from -o extension if omitted.")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Suppress the summary printed to stdout.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    fmt = args.format
    if fmt is None:
        ext = Path(args.output).suffix.lower().lstrip(".")
        fmt = ext if ext in {"xlsx", "csv"} else "xlsx"

    try:
        text = _read_input(args.input)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    interfaces = parse_config(text)
    if not interfaces:
        print("[warn] No interface stanzas were found in the input.",
              file=sys.stderr)

    out_path = _resolve_output(args.output, fmt)

    try:
        if fmt == "csv":
            write_csv(interfaces, out_path)
        else:
            write_xlsx(interfaces, out_path)
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 3

    if not args.quiet:
        trunks = sum(1 for i in interfaces if i.effective_mode == "trunk")
        access = sum(1 for i in interfaces if i.effective_mode == "access")
        routed = sum(1 for i in interfaces if i.effective_mode in {"routed", "n/a"})
        print(
            f"Parsed {len(interfaces)} interface(s): "
            f"{trunks} trunk / {access} access / {routed} routed-or-other"
        )
        print(f"Wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
