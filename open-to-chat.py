#!/usr/bin/env python3
"""
open-to-chat: make the Ollama macOS app open on CHAT, not the Apps page.

Problem: after a full quit (menu bar -> Quit Ollama) or a Mac restart, the
Ollama app (0.34.x) always opens on the Apps store page. Closing only the
window (app stays alive) reopens on Chat fine. As of v0.35.0 there is no
setting for this and no upstream fix.

Root cause (found by static analysis of the 0.34.2 binary, no guesswork):
the app's embedded web UI routes "/" and "/onboarding" to a new Chat when
settings.OnboardingVersion >= 1, and the ONLY route that renders the Apps
page is "/connect" - it has no gate. The Go host navigates the fresh webview
straight to "/connect" on cold start, and the saved LastHomeView setting
("chat") is never consulted at boot. On warm reopen the window simply keeps
its last URL, which is why the last tab survives warm restarts only.

Fix: replace the 8-byte Go nav-table string "/connect" with "/c/new#/"
(same length; the trailing fragment is ignored by the router). Every
Go-side window open - cold start included - now lands on Chat. The in-app
sidebar "Apps" link is part of the JS bundle and still works.

Safety:
- reads/writes only the app binary in your /Applications copy
- takes a one-time per-version backup before the first patch
- --revert restores it, but ONLY if the installed binary is still the one
  this tool produced (it refuses to downgrade a newer app over it)
- --check is a read-only status probe
- if the pattern for your Ollama version is not found, the tool refuses and
  changes nothing (offsets are re-scanned every run, never hardcoded)

Tested on Ollama 0.34.2 (macOS 14+, Apple Silicon). The scan-based approach
may keep working on other 0.34.x/0.35.x builds; if the internal strings
change, the tool tells you and touches nothing.

Exit codes: 0 ok/patched, 1 refused/error, 3 --check: unpatched (update
replaced the binary), 4 --check: unknown binary shape.

License: MIT. Not affiliated with Ollama. If upstream adds a real setting,
use that instead and uninstall this with --revert.
"""

import shutil
import struct
import subprocess
import sys
from pathlib import Path

APP = Path("/Applications/Ollama.app")
BIN = APP / "Contents/MacOS/Ollama"
PLIST = APP / "Contents/Info.plist"
BACKUP_DIR = Path.home() / ".open-to-chat"

OLD = b"/connect"
NEW = b"/c/new#/"  # exactly 8 bytes; fragment ignored by the router
assert len(OLD) == len(NEW)

# The Go nav table is NUL-separated. Primary anchor includes a menu string;
# the minimal anchor survives if the menu string is ever reworded.
ANCHORS = [
    b"Use Ollama models\x00ollama\x00/connect\x00connect\x00/apps\x00apps\x00",
    b"/connect\x00connect\x00/apps\x00",
]


def app_version() -> str:
    try:
        out = subprocess.run(
            ["defaults", "read", str(PLIST), "CFBundleShortVersionString"],
            capture_output=True, text=True, timeout=10,
        )
        v = out.stdout.strip()
        return v if v else "unknown"
    except Exception:
        return "unknown"


def app_running() -> bool:
    try:
        out = subprocess.run(["ps", "axo", "comm="], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return False
    return "Ollama.app/Contents/MacOS/Ollama" in out


def find_patch_offsets(data: bytes) -> list[int]:
    """Offsets of the nav-table '/connect' (JS bundle copies and unrelated
    strings never match the anchors, so this is precise or empty)."""
    offs = []
    for anchor in ANCHORS:
        start = 0
        while True:
            i = data.find(anchor, start)
            if i < 0:
                break
            target = i + anchor.index(OLD)
            if data[target : target + 8] == OLD:
                offs.append(target)
            start = i + 1
    return sorted(set(offs))


def _thin_code_region(s: bytes, base: int) -> tuple[int, int] | None:
    """(abs_start, abs_end) of one thin Mach-O slice's code region: from the
    end of load commands to the start of the code-signature blob. Codesigning
    never touches this region; any real rebuild changes it."""
    if len(s) < 32 or struct.unpack_from("<I", s, 0)[0] != 0xFEEDFACF:
        return None
    ncmds, sizeofcmds = struct.unpack_from("<II", s, 16)
    cmds_end = 32 + sizeofcmds
    sig_off = None
    off = 32
    for _ in range(ncmds):
        if off + 8 > len(s):
            return None
        cmd, cmdsize = struct.unpack_from("<II", s, off)
        if cmdsize < 8 or off + cmdsize > len(s):
            return None
        if cmd == 0x1D:  # LC_CODE_SIGNATURE
            sig_off = struct.unpack_from("<I", s, off + 8)[0]
        off += cmdsize
    if sig_off is None or sig_off > len(s) or cmds_end > sig_off:
        return None
    return (base + cmds_end, base + sig_off)


def identity_regions(data: bytes) -> list[tuple[int, int]] | None:
    """Code regions used as the revert identity anchor, for both universal
    (FAT, magic 0xCAFEBABE big-endian) and thin binaries. Returns None when
    the shape is unrecognized (caller must then refuse, not guess)."""
    shape = None
    if len(data) >= 4:
        shape = struct.unpack_from("<I", data, 0)[0]
    if shape == 0xFEEDFACF:  # thin 64-bit
        r = _thin_code_region(data, 0)
        return [r] if r else None
    if len(data) >= 8 and struct.unpack_from(">I", data, 0)[0] == 0xCAFEBABE:  # FAT
        narch = struct.unpack_from(">I", data, 4)[0]
        regions = []
        for i in range(narch):
            base = 8 + i * 20  # fat_arch: cputype, cpusubtype, offset, size, align
            coff, csize = struct.unpack_from(">II", data, base + 8)
            r = _thin_code_region(data[coff : coff + csize], coff)
            if r is None:
                return None
            regions.append(r)
        return regions
    return None


def codesign_resign() -> bool:
    """Ad-hoc re-sign so macOS will still launch the modified app."""
    try:
        r = subprocess.run(
            ["codesign", "--force", "--sign", "-", str(APP)],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            v = subprocess.run(
                ["codesign", "--verify", "--deep", "--strict", str(APP)],
                capture_output=True, text=True, timeout=120,
            )
            return v.returncode == 0
    except Exception:
        pass
    return False


def backup_path(version: str) -> Path:
    return BACKUP_DIR / f"Ollama-{version}.pristine.bin"


def main() -> int:
    argv = sys.argv[1:]
    if not BIN.exists():
        print(f"Ollama app not found at {BIN}")
        print("Install Ollama for macOS first: https://ollama.com/download")
        return 1

    version = app_version()
    data = BIN.read_bytes()

    if "--check" in argv:
        if find_patch_offsets(data):
            print(f"unpatched (Ollama {version})")
            return 3
        if data.count(NEW) >= 1:
            print(f"patched ok (Ollama {version})")
            return 0
        print(f"unknown binary shape (Ollama {version}) - the tool does not recognize it")
        return 4

    if app_running():
        print("Ollama is RUNNING. Quit it fully first: menu bar icon -> Quit Ollama.")
        return 1

    if "--revert" in argv or "--undo" in argv:
        bp = backup_path(version)
        if not bp.exists():
            print(f"no backup for version {version} at {bp}; nothing to revert")
            print("(backups are per-version; if you updated Ollama, the old backup")
            print(" lives beside this file under ~/.open-to-chat/)")
            return 1
        pristine = bp.read_bytes()
        expected = bytearray(pristine)
        for o in find_patch_offsets(pristine):
            expected[o : o + 8] = NEW
        expected = bytes(expected)

        cur_regions = identity_regions(data)
        exp_regions = identity_regions(expected)
        same = (
            cur_regions is not None
            and exp_regions is not None
            and len(cur_regions) == len(exp_regions)
            and all(
                ce - cs == ee - es and data[cs:ce] == expected[es:ee]
                for (cs, ce), (es, ee) in zip(cur_regions, exp_regions)
            )
        )
        if not same:
            print("REFUSED to revert: the installed binary is not the one this")
            print("backup produced (app updated or modified elsewhere). Restoring")
            print("now would replace it with the older backed-up version.")
            print(f"If you really want that, by hand:\n  cp '{bp}' '{BIN}'")
            return 1
        BIN.write_bytes(pristine)
        print(f"reverted to pristine Ollama {version} (identity check passed)")
        if codesign_resign():
            print("re-signed ok")
        else:
            print("auto re-sign failed; run: codesign --force --sign - /Applications/Ollama.app")
        return 0

    # ---- apply ----
    if version != "0.34.2":
        print(f"note: tested on Ollama 0.34.2; you have {version}. The patch is")
        print("scan-based and refuses anything it does not recognize, so trying")
        print("is safe - but check for an upstream fix first if this is a new version.")

    offs = find_patch_offsets(data)
    if not offs:
        if data.count(NEW) >= 1:
            print("patch already applied (nothing to do)")
            return 0
        print("REFUSED: the nav-table pattern for this Ollama build was not found.")
        print("The app's internals likely changed. Nothing was modified.")
        print("Check for an upstream fix, then open an issue with your version:")
        print("  https://github.com/Qiueyed/ollama-open-to-chat/issues")
        return 1

    if version == "0.34.2" and len(offs) != 2:
        print(f"note: expected 2 nav tables on 0.34.2 (one per CPU slice), found {len(offs)}; patching what was found")

    bp = backup_path(version)
    if not bp.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(BIN, bp)
        print(f"backup written: {bp} ({bp.stat().st_size} bytes)")

    buf = bytearray(data)
    for o in offs:
        buf[o : o + 8] = NEW
    try:
        BIN.write_bytes(bytes(buf))
    except PermissionError:
        print("EPERM: macOS blocks writing another app's binary from this terminal.")
        print("Fix: System Settings -> Privacy & Security -> App Management ->")
        print("     add/enable the terminal app you run this from (or use sudo).")
        return 1

    print(f"patched {len(offs)} nav table(s): /connect -> /c/new#/")

    check = BIN.read_bytes()
    ok = not find_patch_offsets(check) and check.count(NEW) >= 1
    print("verify:", "ok" if ok else "FAILED (restore from backup!)")
    if not ok:
        return 1

    if codesign_resign():
        print("re-signed ok (ad-hoc)")
    else:
        print("auto re-sign failed; run: codesign --force --sign - /Applications/Ollama.app")

    print("\nDone. Open Ollama - it should open on Chat now.")
    print("Revert anytime: python3 open-to-chat.py --revert (with Ollama quit).")
    print("If 'launch at login' stops working, toggle it off/on in Ollama Settings.")
    print("If macOS ever blocks the app after an OS update: xattr -dr com.apple.quarantine /Applications/Ollama.app")
    return 0


if __name__ == "__main__":
    sys.exit(main())
