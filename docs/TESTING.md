# Testing and troubleshooting

Run commands from the repository root. Tests use Python's standard-library
`unittest`; there is no separate build step or configured lint runner.
Runtime dependencies are listed in `requirements.txt`: pygame-ce and psutil.
Install them into your chosen environment if missing:

```sh
python3 -m pip install -r requirements.txt
```

## Automated checks

Full suite:

```sh
PYGAME_HIDE_SUPPORT_PROMPT=1 python3 -m unittest discover -s tests -v
```

Focused examples:

```sh
python3 -m unittest discover -s tests -p 'test_forms.py' -v
python3 -m unittest discover -s tests -p 'test_loader.py' -v
python3 -m unittest discover -s tests -p 'test_parameters.py' -v
```

Do not rely on running bare `unittest discover` from the root to find the test
directory; specify `-s tests`. For environments without a display/audio device:

```sh
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy PYGAME_HIDE_SUPPORT_PROMPT=1 \
  python3 -m unittest discover -s tests -v
```

Also check shell syntax when modifying Bash scripts and whitespace for changes:

```sh
bash -n custom/net_check.sh
git diff --check
```

Documentation-only edits need no application test run unless they introduce
executable examples whose behavior needs checking.

## Test map

| File | Coverage / useful patterns |
|---|---|
| `tests/test_loader.py` | Temporary entry folders, isolated registry, discovery, subprocess output, environment delivery, cancellation |
| `tests/test_menu.py` | Root order, System partition, nested Custom placement, automatic root entries |
| `tests/test_icons.py` | Icon metadata/overrides, caching, proportional scaling, missing-file warnings, legacy list rendering |
| `tests/test_parameters.py` | Field/JSON validation, defaults, bounds, decimal steps, environment serialization |
| `tests/test_forms.py` | Setup lifecycle, controller/keyboard editing, same-frame input routing, small-screen rendering, vertical alignment |
| `tests/test_parameter_examples.py` | Real demo/ping/shell sample contracts without actual network traffic |
| `tests/test_wifi_scanner.py` | Escaped nmcli output, band filtering, setup/command errors, stale snapshots, scrolling and subprocess cancellation without real wireless scans |
| `tests/test_airodump_clients.py` | File-free PTY capture, fragmented terminal redraws, station filtering/retention, JSON output, display limits, errors and bounded signal cleanup using synthetic displays/executables |
| `tests/test_wifi_deauth.py` | Real scanner-to-app JSON integration with stub radios, same-adapter monitor lifecycle/renaming, Broadcast/client selection, streamed ping output, cancellation/restoration ordering, errors and handheld layouts |
| `tests/test_quickshare.py` | Owner/form selection, NetworkManager commands, interface claims, nftables guard, route conflicts, restoration/failure/cancellation and small-screen dashboard without real network changes |
| `tests/test_quickshare_http.py` | Loopback HTTP streaming, uploads/downloads, filename collisions, origin/framing checks, symlink/path boundaries, disk reserve, interrupted transfers and privilege-drop ordering |
| `tests/test_port_scanner.py` | Local interface choices, ARP/XML parsing, TCP ranges, selectable tables, result/back navigation, errors and stub process cancellation/temporary XML cleanup |
| `tests/test_format_drive.py` | Synthetic USB/SD discovery, safety preflight, conditional setup, formatter commands and cancellation; no real disks are formatted |
| `tests/test_disk_information.py` | Synthetic removable disks, lsblk output, identity changes and table rendering without touching real devices |
| `tests/test_storage_tools.py` | Backup/restore, filesystem check, secure wipe, speed test and fake-capacity tools against synthetic sysfs and a temporary file standing in for the drive; no real disks are touched |
| `tests/test_backup_files.py` | Dependent drive/partition form, read-only mount flags and shutdown cleanup, multi-select browsing, path containment, byte progress, metadata and partial backups using temporary source trees and mocked mount commands |

Port scanner tests do not probe the developer's network. On your authorized
Linux LAN, verify interface selection, arp-scan permissions, device IP/MAC/vendor
rows, A selection, known open TCP services and B returning to devices. Check
cancellation during discovery and Nmap. Conventional service names are not
service-version detection; unreachable/sleeping devices can report filtered
ports. Actual Linux packet behavior cannot be established by macOS stub tests.
| `tests/test_shutdown.py` | Hook registration, success/error/cancel cleanup, restoration commands after cancellation, standalone helpers and GUI close |

`@app` registrations are global. Use `patch.object(context, "_REGISTRY", [])`
for tests that discover or register apps. Preserve source tagging when testing
both `apps/` and `custom/`. Avoid repeated discovery in one process unless the
registry is deliberately isolated. Use temporary files and stub command
executables, not real system-control commands.

Worker tests should join with a bounded timeout, assert termination and close
their navigator for cleanup. Cancellation tests should wait for a readiness
signal rather than assuming a process started after a fixed delay.

## Manual GUI smoke check

```sh
python3 deck.py --windowed
```

Confirm Apps / Custom / System / Exit ordering, nested menus and B back.
Open Custom Python demo, edit each field type, cancel an edit, run it, return
to setup, rerun and reopen to check defaults reset. Test Q/Space inside text
editing versus shortcuts outside it. Check controller-only entry where hardware
is available; simulated controller events do not prove physical mappings.

For UI changes, check both 320x240 and 640x480 through the `size` configuration
or test surfaces. Account for the status bar and footer reducing the body height.
Include long text, scrolled fields, validation errors and keyboard action keys.
Do not change the user's config merely to run a smoke check without preserving
their settings.

Wi-Fi scanner unit/integration tests use synthetic AP records and a temporary
stub `nmcli`; they do not prove radio behavior. On the intended Linux handheld,
with NetworkManager running and an enabled dual-band adapter, open Apps >
Network > Wi-Fi scanner and confirm actual encryption labels (`--` for open
networks), BSSID/channel readings, both band filters, interface selection,
refresh and B cancellation. Permission, rfkill and driver restrictions should
be checked there. Never run real discovery merely to validate parsing on a
developer machine. Official command behavior is
documented in the [nmcli manual](https://networkmanager.dev/docs/api/latest/nmcli.html).

### Wifi Deauth and file-free client scanner checks

```sh
PYTHONDONTWRITEBYTECODE=1 SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy \
  PYGAME_HIDE_SUPPORT_PROMPT=1 \
  python3 -m unittest discover -s tests -p 'test_wifi*.py' -v
PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest discover -s tests -p 'test_airodump_clients.py' -v
```

Automated tests mock AP discovery and use only temporary `airmon-ng`, `iw`,
`ip`, `airodump-ng` and `ping` executables. The ping stub supplies streamed
output without contacting Google; the app does not invoke a deauthentication
tool. Scanner tests use a pseudo-terminal and JSON stream without scanning the
host network, changing real interfaces or sending disconnect packets. They do
not prove actual Linux driver/radio behavior.

On an explicitly authorized Linux test network, with the README prerequisites,
confirm AP selection stops discovery and enables monitor mode on that same
adapter. Check renamed and unchanged monitor interface names where hardware
allows. Confirm known clients appear as observed MAC/dBm/packet rows with no
capture files, not invented names or claims of current connectivity.
Broadcast must be selectable with zero clients. Selecting either target stops
capture and displays streamed `ping google.com` output; the ping process is
reaped on B and capture resumes while retaining observations. B from clients restores
managed mode and original link up/down
state before AP scans resume. Check app/launcher exit, activation
failure/cancellation, capture error, adapter removal and visible
cleanup failures, plus 320x240/640x480 layouts. Never use an actual
deauthentication command to smoke-test this flow. The standalone scanner still
requires an existing monitor interface and must not change its mode. Forced
termination cannot guarantee automatic recovery.

### QuickShare deployment smoke check

Run the focused automated checks without privileged networking:

```sh
PYTHONDONTWRITEBYTECODE=1 SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy \
  PYGAME_HIDE_SUPPORT_PROMPT=1 \
  python3 -m unittest discover -s tests -p 'test_quickshare*.py' -v
```

The HTTP tests use only loopback sockets and temporary shares. Network tests
mock commands and interface locks; they never create a hotspot, change a real
firewall or drop the test runner's privileges. They do not prove Linux packet
isolation, DHCP delivery, AP client isolation or WPA3 support.

On the intended Linux handheld and your authorized test devices, check:

- The selected non-root owner receives `~/QuickShare/` and uploaded files,
  including with a sudo launcher and a plain-root launcher with no SUDO_UID.
  The actual HTTP child must have that user's UID/GID/groups, not root.
- AP activation, a new password on each enable, explicit unsupported-WPA3
  failure, Open-mode consent, radio/rfkill/permission errors, and DHCP/browser
  access in each of the four subnets. Check existing-route conflicts too.
- With a live Ethernet/USB upstream, clients cannot reach the internet, DNS
  forwarding, IPv6 routes or unrelated host services. The sharing port must
  not be reachable through another interface. Check nftables/firewalld/UFW
  compatibility without flushing or loosening existing policy.
- Two stations appear even when idle; unknown IPs are labeled honestly.
  Verify client isolation, real-phone "stay connected" behavior, large uploads
  and downloads, repeated names and low-space/quota/filesystem-limit failures.
- At 320x240 and 640x480, all Details (including password/URL), Clients and
  Activity content is reachable by scrolling and tab changes.
- B during setup/activation/transfer, failed server startup, adapter removal,
  connection changes elsewhere, normal reconnection and cleanup failure.
  After a controlled crash, use only the recorded profile UUID/table for
  targeted recovery, preserving the guard until the AP is down.

## Troubleshooting

| Symptom | Inspect |
|---|---|
| Custom script missing | Direct child of configured custom folder; filename not hidden/underscore; supported extension or executable bit |
| Entry shown as `! ...` | Open it to see traceback; inspect syntax, imports and sidecar JSON |
| Setup fails before running | Function must return `Form`; required defaults valid; keys unique ignoring case |
| Standalone parameters absent | Full filename sidecar suffix; read `DECK_PARAM_*`; launch through the form, not directly from a shell |
| Custom group does not create submenu | Expected: edit explicit tree in `menu.py` |
| Relative asset file missing | Standalone cwd is its directory; icon paths use deck root; framework apps should resolve their own data paths |
| Bash syntax fails | Check shebang; explicit `#!/bin/sh` overrides `.sh`'s Bash fallback |
| App ignores B | Use cancellation checkpoints and `ctx.run`; Python threads cannot be forcibly terminated by closing a screen |
| Script waits for user input | stdin is disabled; move interaction into a form |
| Missing icon | Warning identifies path; check root-relative location and restart after asset changes |
| Text sits at top of form row | Form labels opt into `valign="center"`; ordinary `Label` defaults to top |
| Controls unavailable on desktop | Linux tool dependency/permissions; not necessarily a GUI bug |

The current subprocess runner can emit `ResourceWarning` about unclosed pipe
handles during tests. Do not hide warnings globally or treat that as a new form
failure; investigate runner cleanup separately if that is the task.
