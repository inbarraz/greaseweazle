# Greaseweazle Host Tools: Diagnostic Edition

*Tools for accessing a floppy drive at the raw flux level.*

![CI Badge][ci-badge]
![Downloads Badge][downloads-badge]
![Version Badge][version-badge]

<img src="https://raw.githubusercontent.com/wiki/keirf/greaseweazle/assets/banner2.jpg">

---

## About this fork: the `gw diag` command

This is a fork of [keirf/greaseweazle][upstream] that adds **one new command,
`gw diag`**: an interactive, live disk/drive diagnostic for bench-testing
floppy drives and disks in real time. Everything else in this repository is
unchanged from upstream, and the change is self-contained (a new
`tools/diag/` package plus a single dispatch line in `cli.py`).

### What it does

Where `gw read` takes a single pass and exits, `gw diag` keeps the spindle
spinning and lets you drive the head interactively while it continuously
decodes the track currently under the head. It is built for diagnosing
drives, checking head alignment, and identifying unknown disks at the bench.
For the current track it reports:

* **On-track vs off-track sector counts**, decoded live from the flux with
  an IBM MFM/FM decoder, so you can see at a glance whether the head is
  reading clean data. When sectors are off-track, it also tells you *which*
  track those stray sectors actually belong to.
* **Drive status pins**: Write-Protect, Disk-Change, Track-0 and Density,
  each labelled with its 34-pin connector pin number.
* **Live RPM**, self-correcting the read window as the measured speed drifts.

Interactive keys: number keys jump to a track, `+` / `-` / arrow keys step a
single track, `r` recalibrates, `h` toggles head, `m` toggles the motor,
`d` toggles density-select, and `q` / `Esc` quits.

### Usage

```
gw diag --rate 500 [options]
```

`--rate` (data rate in kbps) is required; the rest have sensible defaults.
Common options:

| Option | Purpose |
|--------|---------|
| `--rate KBPS` | Data rate, e.g. `250`, `500`, `1000` (**required**) |
| `--secs N` | Expected sectors/track (guessed from rate + rpm for standard formats if omitted) |
| `--rpm N` | Fix the spindle speed instead of tracking the live measurement |
| `--encoding mfm\|fm` | Track encoding (default `mfm`) |
| `--cyls N` / `--heads N` | Geometry limits |
| `--drive` | Which drive to diagnose |
| `--gen-tg43` | Auto-drive pin 2 as a TG43 signal for 8-inch drives |

Run `gw diag --help` for the full list.

### Getting started

`gw diag` currently runs on **Windows only**: it uses the Windows `msvcrt`
console API for live keyboard input. A macOS/Linux key-input path has not
been written yet, and the command will exit with an error on those platforms.
Contributions to port it are welcome (see
[`src/greaseweazle/tools/diag/__init__.py`](src/greaseweazle/tools/diag/__init__.py)).

#### Windows

Pick whichever matches what you have installed.

**Option A, run from source with `gw-diag.bat` (no compiler needed).** The
speed-up extension is optional at runtime, so you can skip building it and
run the pure-Python code directly. The included `gw-diag.bat` launcher does
the setup for you: it writes the version stub, sets the environment, and
installs the four runtime packages on first run. Requires only
[Python 3.8 or newer](https://www.python.org/downloads/windows/) on your PATH:

```
git clone -b diag https://github.com/misterblack1/greaseweazle.git
cd greaseweazle
gw-diag.bat --rate 500
```

Run `gw-diag.bat` on its own (no arguments) for full help on every option
and the meaning of each field in the output line.

**Option B, install with pipx (needs a C compiler).** Requires
[Python 3.8 or newer](https://www.python.org/downloads/windows/) and the
[Microsoft C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/),
because the install compiles a small optional speed-up extension:

```
pip install pipx
pipx install git+https://github.com/misterblack1/greaseweazle@diag
gw diag --rate 500
```

#### macOS and Linux

The `gw diag` command does not run on these platforms yet (see above). The
rest of the Greaseweazle tools from this fork install and work normally with:

```
pipx install git+https://github.com/misterblack1/greaseweazle@diag
```

### Status

`gw diag` is intended to be offered upstream. Like the rest of Greaseweazle,
it is released into the public domain. See [COPYING](COPYING).

[upstream]: https://github.com/keirf/greaseweazle

---

This repository contains the host tools for controlling Greaseweazle:
an [Open Source][designfiles] USB device capable of reading and
writing raw data on nearly any type of floppy disk.

For more info see the following links:

* [Download the Greaseweazle software][Downloads]
* [Purchase a Greaseweazle][rmb]
* [Read the GitHub wiki](https://github.com/keirf/greaseweazle/wiki)
* [Greaseweazle firmware repository][firmware]

## Installation

**Windows:** Simply [download][Downloads] and unzip the latest release
of the host tools. You can now open a CMD window and run the `gw.exe` tool
from inside the unzipped release folder.

**macOS, Linux:** You can install the latest host tools release directly
from GitHub using Python Pipx:
```
pipx install git+https://github.com/keirf/greaseweazle@latest
```
See the [software installation wiki page][siwp] for more details.

## Usage

Type `gw --help` for on-line help.

Read the [GitHub wiki](https://github.com/keirf/greaseweazle/wiki)
for more detailed usage instructions.

## Redistribution

Greaseweazle source code, and all binary releases, are freely redistributable
in any form. Please see the [license](COPYING).

[designfiles]: https://github.com/keirf/greaseweazle/wiki/Design-Files
[firmware]: https://github.com/keirf/greaseweazle-firmware
[rmb]: https://github.com/keirf/greaseweazle/wiki/Purchase-a-Greaseweazle
[Downloads]: https://github.com/keirf/greaseweazle/wiki/Download-Host-Tools
[siwp]: https://github.com/keirf/greaseweazle/wiki/Software-Installation

[ci-badge]: https://github.com/keirf/greaseweazle/workflows/CI/badge.svg
[downloads-badge]: https://img.shields.io/github/downloads/keirf/greaseweazle/total
[version-badge]: https://img.shields.io/github/v/release/keirf/greaseweazle
