# Device Port Reporter

> **📍 Repository relocation notice**
>
> This project is in the process of being migrated to Cisco's internal
> GitHub Enterprise. **Cisco employees should use the internal repo going
> forward:**
>
> **➡ https://wwwin-github.cisco.com/jamwelch/DevicePortReporter**
>
> The public mirror at `github.com/jamwelch/DevicePortReporter` is being
> archived and will no longer receive updates. Clone from the internal
> URL above to get the latest code.

---

> **🔒 Restricted-data guarantee (read this first)**
>
> Uploaded configurations, parsed reports, and exported XLSX files are
> treated as restricted and **stay on the local machine**. Specifically:
>
> - The web app **never writes** a user-supplied config, parsed report,
>   or generated XLSX to disk. All processing is in-memory
>   (`BytesIO` → HTTP response).
> - The server **binds to `127.0.0.1`** by default — nothing is exposed
>   on the LAN or the internet.
> - The app has **zero outbound network calls**. No CDN, no telemetry,
>   no error reporting, no update checks.
> - Responses are returned with `Cache-Control: no-store` so neither the
>   browser nor any intermediary may retain a copy.
> - A strict `Content-Security-Policy` blocks every third-party origin,
>   even accidentally-added ones.
> - Run with `--quiet` to also silence the Werkzeug request log (it
>   only ever contained request lines, never bodies, but can be turned
>   off entirely).
> - The repo's `.gitignore` explicitly **blocks** `.xlsx`, `.csv`,
>   `.cfg`, `.conf`, `.nxos`, `.ios`, `.log`, `show*run*.txt`,
>   `running-config*.txt`, `uploads/`, `output/`, and `tmp/`, so real
>   captures can't accidentally get committed. The only tracked data
>   file is the synthetic `example_show_run.txt`.
>
> If you fork this tool, keep the invariant: anything derived from a
> user-supplied config must live only for the lifetime of the HTTP
> request, and nothing must ever be sent off-host.

---

Turn a Cisco **Nexus 7K** (or any NX-OS / IOS / IOS-XE)
`show running-config` capture into a clean, sortable Excel spreadsheet
— via a **local web app** *or* a **command-line script**.

Device Port Reporter identifies every interface and reports:

- Interface name, type, description, admin state (shutdown / no shutdown)
- L2 **mode**: `trunk`, `access`, `fex-fabric`, `dot1q-tunnel`,
  `private-vlan-*`, `routed`, or `n/a` for SVIs / loopbacks
- **Access VLAN**, **Voice VLAN**, **Native VLAN**
- **Allowed VLAN list** on trunks — with the incremental
  `switchport trunk allowed vlan add / remove / except / all / none`
  verbs applied in config order (so the output reflects what the switch
  would actually forward, not just the raw text)
- **Port-channel** membership + LACP/PAgP mode
- **vPC** role (peer-link or vPC ID)
- Speed / duplex / MTU / IPv4 / IPv6 / VRF
- Spanning-tree port type, sub-interface encapsulation, FEX association
- Other notable per-interface flags (`storm-control`, `service-policy`,
  `port-security`, `no cdp enable`, …)

---

## One-time setup

```powershell
cd C:\Users\jamwelch\Projects\device-port-reporter

# Create a dedicated virtualenv for this project
python -m venv .venv

# Install dependencies into it
.\.venv\Scripts\pip install -r requirements.txt
```

Optional (gives you the `device-port-reporter` and
`device-port-reporter-web` console-script shortcuts):

```powershell
.\.venv\Scripts\pip install -e .
```

---

## Quick start (web UI — recommended)

```powershell
# From the project root:
.\.venv\Scripts\python .\web\app.py
```

A browser tab opens at **http://127.0.0.1:5173**. Drag a `show
running-config` capture (or an interface-only excerpt) onto the drop
zone and you'll immediately see:

- **KPI cards** — totals for trunks, access, routed, shutdown,
  port-channels, FEX links, distinct VLANs.
- **Top VLANs chart** — bars showing which VLANs are referenced by the
  most ports.
- **Filterable interface table** — tabs for All / Trunks / Access /
  Routed-L3 / Shutdown, plus a free-text search box.
- **Download XLSX** button — produces a 5-sheet workbook
  (Summary + All + Trunks + Access + Routed-and-L3), with frozen
  headers and Excel auto-filter on every sheet.

### Options

```powershell
.\.venv\Scripts\python .\web\app.py --help
# --host         (default 127.0.0.1 = loopback only; override at your own risk)
# --port         (default 5173)
# --no-browser   (don't auto-open a browser tab)
# --quiet | -q   (silence the per-request access log entirely)
```

### Security / privacy details

- **Restricted-data safe** — see the banner at the top of this README.
  No uploads, no reports, no XLSX files touch the disk.
- Binds to **`127.0.0.1`** by default; a warning is printed if the user
  overrides `--host`.
- Flask debug & the Werkzeug debugger are **disabled**, so there is no
  RCE / pin attack surface.
- Max upload size **20 MB** (hard-enforced by Flask's
  `MAX_CONTENT_LENGTH` plus a per-request check).
- API responses carry **`Cache-Control: no-store, no-cache,
  must-revalidate, private`** plus `Pragma: no-cache` / `Expires: 0`.
- Every response carries **`X-Content-Type-Options: nosniff`**,
  **`X-Frame-Options: DENY`**, **`Referrer-Policy: no-referrer`**, and
  a **strict CSP** (`default-src 'self'`) that blocks every third-party
  origin — including any future accidental addition.
- The frontend builds all DOM via `textContent` / element nodes, so
  interface descriptions can't inject script even if they contain
  HTML-looking characters.
- No outbound HTTP client in the Python code, no CDN link in the HTML,
  no analytics or error-reporting hook. You can confirm this with:
  ```powershell
  rg -n "https?://|cdn\.|googleapis|sentry|analytics" .
  ```
  The only hits are the local `http://127.0.0.1:…` URL printed on
  startup.

---

## Quick start (CLI)

Still useful for scripting, cron jobs, or batching many files.

```powershell
# XLSX output
.\.venv\Scripts\python .\device_port_reporter.py -i example_show_run.txt -o interfaces.xlsx

# CSV (no openpyxl needed for CSV mode)
.\.venv\Scripts\python .\device_port_reporter.py -i show_run.txt -o interfaces.csv --format csv

# Pipe directly from a captured show-run
ssh admin@n7k-core "show running-config" | .\.venv\Scripts\python .\device_port_reporter.py -o n7k.xlsx
```

Partial captures (only `show running-config interface`) work too — the
parser doesn't need the whole config.

---

## XLSX tab structure

Both the web app's download button and the CLI produce the same
workbook:

1. **Summary** — KPIs + per-VLAN port-count table.
2. **All Interfaces** — every stanza in one flat table.
3. **Trunk Ports** — filtered to trunks only (blue tint).
4. **Access Ports** — filtered to access only (green tint).
5. **Routed and L3** — routed ports, SVIs, loopbacks, mgmt (amber tint).

Every sheet has a frozen header row and Excel auto-filter enabled.

---

## Notes on VLAN math

- A trunk with **no** `switchport trunk allowed vlan` line shows as
  `all (default 1-4094)` to match NX-OS default behaviour.
- `add / remove / except / all / none` verbs are applied in the order
  they appear, so e.g. `set 10,20,30` → `add 40,50` → `remove 20`
  gives `10,30,40,50` (not just the last raw line).
- Extended / reserved VLAN IDs (1002-1005, 3968-4047, 4094) are
  accepted as valid — the switch enforces reserved semantics at
  runtime.

---

## Project layout

```
device-port-reporter/
├── device_port_reporter.py # parser + XLSX/CSV writer + CLI (library + script)
├── example_show_run.txt    # synthetic NX-OS 8.4 sample for smoke tests
├── requirements.txt        # openpyxl + flask
├── pyproject.toml          # enables `pip install -e .` + console scripts
├── .gitignore              # privacy safeguards (blocks real captures from commit)
├── README.md               # this file
└── web/
    ├── __init__.py
    ├── app.py              # Flask server (localhost-only launcher)
    ├── templates/
    │   └── index.html      # single-page UI
    └── static/
        ├── styles.css      # self-contained styling
        └── app.js          # vanilla-JS controller
```

## Extending

The `Interface` dataclass in `device_port_reporter.py` is the single
source of truth. To add a field:

1. Add it to the dataclass.
2. Extend `_apply_interface_command()` with a parser for the relevant
   NX-OS/IOS line.
3. Add it to `_row_from_interface()` and the `_COLUMNS` list — the web
   UI and XLSX writer both pick it up automatically.
