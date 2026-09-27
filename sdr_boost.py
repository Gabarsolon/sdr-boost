#!/usr/bin/env python3
"""
SDR Boost - brighter SDR content in Windows HDR mode
====================================================
Windows caps SDR white at 480 nits in HDR mode (SDR content brightness slider at 100).
This tool writes MHC2 color profiles whose 1D LUT - applied by the GPU to the PQ signal
after composition - scales SDR up to a white level you choose and rolls HDR highlights
off into what the panel can show.

Profiles are copies of the monitor's Windows HDR Calibration profile, so they keep its
peak, full-frame luminance and saturation setting.

Author: Gabarsolon
"""

import argparse
import ctypes
import glob
import hashlib
import math
import os
import struct
import sys
import time
import winreg
from ctypes import wintypes
from dataclasses import dataclass

__version__ = "1.0.0"

# next to the script, or next to the exe when frozen by PyInstaller (not its temp unpack dir)
HERE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, "profiles")
COLOR_DIR = os.path.join(os.environ["WINDIR"], r"System32\spool\drivers\color")

SDR_WHITE_IN = 480.0   # SDR white with the Windows slider at 100
LUT_SIZE = 4096        # MHC2 maximum
IDENTITY = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0)

# ---------------------------------------------------------------- PQ (SMPTE ST 2084)
M1, M2 = 2610 / 16384, 2523 / 4096 * 128
C1, C2, C3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32


def pq_to_nits(e: float) -> float:
    p = max(e, 0.0) ** (1 / M2)
    return 10000 * (max(p - C1, 0.0) / (C2 - C3 * p)) ** (1 / M1)


def nits_to_pq(n: float) -> float:
    y = (max(n, 0.0) / 10000) ** M1
    return ((C1 + C2 * y) / (1 + C3 * y)) ** M2


# ---------------------------------------------------------------- tone curve
def boost_curve(white: float, peak_in: float, peak_out: float, soft: float = 0.0):
    """Returns a nits -> nits function.

    Up to SDR white (480 in) the signal is scaled to `white`. `soft` > 0 bends that part
    into (1 - e^(-soft*r)) / (1 - e^-soft), lifting darks and mids relative to white.
    Above SDR white an exponential shoulder continues with the same slope and lands on
    `peak_out` at `peak_in`; anything brighter clips there.
    """
    white = min(white, peak_out)
    if soft > 0:
        norm = 1 - math.exp(-soft)
        sdr = lambda r: (1 - math.exp(-soft * r)) / norm
        top_slope = soft * math.exp(-soft) / norm
    else:
        sdr, top_slope = (lambda r: r), 1.0

    span_in, span_out = peak_in - SDR_WHITE_IN, peak_out - white
    if span_out <= 0:  # white at the panel peak: no headroom, HDR above SDR white clips
        return lambda n: min(white * sdr(n / SDR_WHITE_IN), peak_out)

    s = white / SDR_WHITE_IN * top_slope * span_in / span_out  # shoulder slope needed at its start
    k = None
    if s > 1:
        lo, hi = 1e-9, 200.0  # solve k / (1 - e^-k) = s
        for _ in range(200):
            k = (lo + hi) / 2
            if k / (1 - math.exp(-k)) < s:
                lo = k
            else:
                hi = k

    def f(n: float) -> float:
        if n <= SDR_WHITE_IN:
            return white * sdr(n / SDR_WHITE_IN)
        if n >= peak_in:
            return peak_out
        t = (n - SDR_WHITE_IN) / span_in
        h = t if k is None else (1 - math.exp(-k * t)) / (1 - math.exp(-k))
        return white + span_out * h

    return f


# ---------------------------------------------------------------- ICC / MHC2
def s15(v: float) -> bytes:
    return struct.pack(">i", int(round(v * 65536)))


def rd_s15(b: bytes) -> float:
    return struct.unpack(">i", b)[0] / 65536.0


def icc_tags(d: bytes) -> list:
    n = struct.unpack(">I", d[128:132])[0]
    return [(d[132 + 12 * i:136 + 12 * i],) + struct.unpack(">II", d[136 + 12 * i:144 + 12 * i]) for i in range(n)]


def icc_tag(d: bytes, sig: bytes):
    for s, off, size in icc_tags(d):
        if s == sig:
            return d[off:off + size]
    return None


def profile_id(d: bytes) -> bytes:
    """ICC v4 profile ID: MD5 with flags, rendering intent and the ID itself zeroed."""
    b = bytearray(d)
    b[44:48] = bytes(4)
    b[64:68] = bytes(4)
    b[84:100] = bytes(16)
    return hashlib.md5(b).digest()


def mluc(text: str) -> bytes:
    s = text.encode("utf-16-be")
    return b"mluc" + bytes(4) + struct.pack(">II", 1, 12) + b"enUS" + struct.pack(">II", len(s), 28) + s


@dataclass
class Calibration:
    path: str
    peak: float
    black: float
    full_frame: float
    saturation: bool  # matrix differs from identity


def read_calibration(path: str) -> Calibration:
    d = open(path, "rb").read()
    m = icc_tag(d, b"MHC2")
    if not m:
        raise SystemExit(f"{os.path.basename(path)} has no MHC2 tag, so it isn't an HDR calibration profile.")
    lumi = icc_tag(d, b"lumi")
    om = struct.unpack(">I", m[20:24])[0]
    mat = [rd_s15(m[om + 4 * i:om + 4 * i + 4]) for i in range(12)] if om else list(IDENTITY)
    return Calibration(path, rd_s15(m[16:20]), rd_s15(m[12:16]),
                       rd_s15(lumi[12:16]) if lumi else 0.0,
                       any(abs(a - b) > 1e-3 for a, b in zip(mat, IDENTITY)))


def build_profile(template: bytes, name: str, lut: list, neutral: bool) -> bytes:
    """Copy of `template` with a new description and an MHC2 tag carrying `lut` on all channels."""
    tags = icc_tags(template)
    old = icc_tag(template, b"MHC2")
    om = struct.unpack(">I", old[20:24])[0]
    matrix = b"".join(s15(v) for v in IDENTITY) if neutral or not om else old[om:om + 48]
    lut_blob = b"sf32" + bytes(4) + b"".join(s15(v) for v in lut)
    lut_offsets = [84 + i * len(lut_blob) for i in range(3)]  # 36-byte header + 48-byte matrix
    mhc2 = (b"MHC2" + bytes(4) + struct.pack(">I", len(lut)) + old[12:20]
            + struct.pack(">IIII", 36, *lut_offsets) + matrix + lut_blob * 3)
    replaced = {b"MHC2": mhc2, b"desc": mluc(name)}

    # header + tag table, then 4-byte aligned tag data; tags that shared data keep sharing it
    pos = 128 + 4 + 12 * len(tags)
    placed, table, blobs = {}, [], []
    for sig, off, size in tags:
        key = sig if sig in replaced else (off, size)
        if key not in placed:
            data = replaced.get(sig, template[off:off + size])
            pad = -pos % 4
            blobs += [bytes(pad), data]
            pos += pad
            placed[key] = (pos, len(data))
            pos += len(data)
        table.append(sig + struct.pack(">II", *placed[key]))
    out = bytearray(template[:128]) + struct.pack(">I", len(tags)) + b"".join(table) + b"".join(blobs)
    out += bytes(-len(out) % 4)
    out[0:4] = struct.pack(">I", len(out))
    out[24:36] = struct.pack(">6H", *time.localtime()[:6])
    out[84:100] = profile_id(out)
    return bytes(out)


# ---------------------------------------------------------------- displays
class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("size", ctypes.c_uint32), ("adapterId", LUID), ("id", ctypes.c_uint32)]


class TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [("header", DEVICE_INFO_HEADER), ("flags", ctypes.c_uint32), ("outputTechnology", ctypes.c_int),
                ("edidManufactureId", ctypes.c_uint16), ("edidProductCodeId", ctypes.c_uint16),
                ("connectorInstance", ctypes.c_uint32), ("monitorFriendlyDeviceName", ctypes.c_wchar * 64),
                ("monitorDevicePath", ctypes.c_wchar * 128)]


class ADVANCED_COLOR_INFO(ctypes.Structure):
    _fields_ = [("header", DEVICE_INFO_HEADER), ("value", ctypes.c_uint32), ("colorEncoding", ctypes.c_int),
                ("bitsPerColorChannel", ctypes.c_uint32)]


class SDR_WHITE_LEVEL(ctypes.Structure):
    _fields_ = [("header", DEVICE_INFO_HEADER), ("SDRWhiteLevel", ctypes.c_uint32)]


@dataclass
class Display:
    name: str
    path: str
    luid: LUID
    source_id: int
    hdr: bool
    sdr_white: float  # nits


def active_displays() -> list:
    user32 = ctypes.windll.user32
    n_paths, n_modes = ctypes.c_uint32(), ctypes.c_uint32()
    if user32.GetDisplayConfigBufferSizes(2, ctypes.byref(n_paths), ctypes.byref(n_modes)):  # QDC_ONLY_ACTIVE_PATHS
        raise SystemExit("GetDisplayConfigBufferSizes failed")
    paths = ctypes.create_string_buffer(72 * n_paths.value)   # DISPLAYCONFIG_PATH_INFO
    modes = ctypes.create_string_buffer(64 * n_modes.value)   # DISPLAYCONFIG_MODE_INFO
    if user32.QueryDisplayConfig(2, ctypes.byref(n_paths), paths, ctypes.byref(n_modes), modes, None):
        raise SystemExit("QueryDisplayConfig failed")
    raw, out = paths.raw, []
    for i in range(n_paths.value):
        p = raw[72 * i:72 * i + 72]
        source_id = struct.unpack("<I", p[8:12])[0]
        lo, hi, target_id = struct.unpack("<IiI", p[20:32])  # targetInfo follows the 20-byte sourceInfo
        luid = LUID(lo, hi)

        def query(info_struct, info_type):
            s = info_struct()
            s.header = DEVICE_INFO_HEADER(info_type, ctypes.sizeof(info_struct), luid, target_id)
            return s if user32.DisplayConfigGetDeviceInfo(ctypes.byref(s)) == 0 else None

        tn = query(TARGET_DEVICE_NAME, 2)
        ac = query(ADVANCED_COLOR_INFO, 9)
        sw = query(SDR_WHITE_LEVEL, 11)
        out.append(Display(tn.monitorFriendlyDeviceName if tn else f"Display {i + 1}",
                           tn.monitorDevicePath if tn else "", luid, source_id,
                           bool(ac and ac.value & 2), sw.SDRWhiteLevel * 80 / 1000 if sw else 0.0))
    return out


def pick_display(match) -> Display:
    displays = active_displays()
    if match:
        found = [d for d in displays if match.lower() in (d.name + " " + d.path).lower()]
    else:
        found = [d for d in displays if d.hdr] or displays
    if len(found) != 1:
        listing = "\n".join(f"  {d.name}  (HDR {'on' if d.hdr else 'off'})  {d.path}" for d in displays)
        raise SystemExit(f"Pick a display with --monitor (matched {len(found)}):\n{listing}")
    return found[0]


def reg_value(hive, key: str, name: str):
    try:
        with winreg.OpenKey(hive, key) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def device_key(d: Display) -> str:
    """HKLM Enum key of the monitor, from its device path \\\\?\\DISPLAY#HWID#INSTANCE#{guid}."""
    _, hwid, inst, _ = d.path.split("#")
    return rf"SYSTEM\CurrentControlSet\Enum\DISPLAY\{hwid}\{inst}"


def edid_peak(d: Display):
    """Max luminance from the CTA-861 HDR static metadata block, with any CRU override applied."""
    params = device_key(d) + r"\Device Parameters"
    edid = reg_value(winreg.HKEY_LOCAL_MACHINE, params, "EDID")
    if not edid:
        return None
    edid = bytearray(edid)
    for block in range(len(edid) // 128):
        override = reg_value(winreg.HKEY_LOCAL_MACHINE, params + r"\EDID_OVERRIDE", str(block))
        if override:
            edid[128 * block:128 * block + 128] = override[:128]
    for block in range(1, len(edid) // 128):
        b = edid[128 * block:128 * block + 128]
        if b[0] != 0x02:  # CTA-861 extension
            continue
        i = 4
        while i < b[2]:
            tag, length = b[i] >> 5, b[i] & 31
            if tag == 7 and b[i + 1] == 6 and length >= 4:  # extended tag 6: HDR static metadata
                return 50 * 2 ** (b[i + 4] / 32)
            i += length + 1
    return None


def associated_profiles(d: Display) -> list:
    """Advanced color (HDR) profiles linked to this monitor for the current user."""
    driver = reg_value(winreg.HKEY_LOCAL_MACHINE, device_key(d), "Driver")
    if not driver:
        return []
    key = r"Software\Microsoft\Windows NT\CurrentVersion\ICM\ProfileAssociations\Display" + "\\" + driver
    return list(reg_value(winreg.HKEY_CURRENT_USER, key, "ICMProfileAC") or [])


# ---------------------------------------------------------------- Windows color profile management
SCOPE_CURRENT_USER, CPT_ICC, CPST_EXTENDED_DISPLAY_COLOR_MODE = 1, 0, 8

mscms = ctypes.WinDLL("mscms", use_last_error=True)
mscms.InstallColorProfileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
mscms.InstallColorProfileW.restype = wintypes.BOOL
mscms.UninstallColorProfileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.BOOL]
mscms.UninstallColorProfileW.restype = wintypes.BOOL
mscms.ColorProfileAddDisplayAssociation.argtypes = [ctypes.c_int, wintypes.LPCWSTR, LUID, ctypes.c_uint32,
                                                    wintypes.BOOL, wintypes.BOOL]
mscms.ColorProfileAddDisplayAssociation.restype = ctypes.c_long
mscms.ColorProfileRemoveDisplayAssociation.argtypes = [ctypes.c_int, wintypes.LPCWSTR, LUID, ctypes.c_uint32,
                                                       wintypes.BOOL]
mscms.ColorProfileRemoveDisplayAssociation.restype = ctypes.c_long
mscms.ColorProfileGetDisplayDefault.argtypes = [ctypes.c_int, LUID, ctypes.c_uint32, ctypes.c_int, ctypes.c_int,
                                                ctypes.POINTER(ctypes.c_void_p)]
mscms.ColorProfileGetDisplayDefault.restype = ctypes.c_long


def active_profile(d: Display):
    out = ctypes.c_void_p()
    hr = mscms.ColorProfileGetDisplayDefault(SCOPE_CURRENT_USER, d.luid, d.source_id, CPT_ICC,
                                             CPST_EXTENDED_DISPLAY_COLOR_MODE, ctypes.byref(out))
    if hr or not out.value:
        return None
    name = ctypes.wstring_at(out.value)
    ctypes.windll.kernel32.LocalFree(out)
    return name


def link(d: Display, name: str, make_active: bool):
    hr = mscms.ColorProfileAddDisplayAssociation(SCOPE_CURRENT_USER, name, d.luid, d.source_id, make_active, True)
    if hr:
        raise SystemExit(f"Windows refused to link {name} (HRESULT 0x{hr & 0xFFFFFFFF:08X})")


def unlink(d: Display, name: str):
    hr = mscms.ColorProfileRemoveDisplayAssociation(SCOPE_CURRENT_USER, name, d.luid, d.source_id, True)
    if hr:
        print(f"  note: unlinking {name} returned HRESULT 0x{hr & 0xFFFFFFFF:08X}")


def install(path: str):
    if not mscms.InstallColorProfileW(None, path):
        raise SystemExit(f"Could not install {os.path.basename(path)} (error {ctypes.get_last_error()})")


def uninstall(name: str):
    if not mscms.UninstallColorProfileW(None, os.path.join(COLOR_DIR, name), True):
        raise SystemExit(f"Could not delete {name} from the color store (error {ctypes.get_last_error()})")


def calibration_for(d: Display, base) -> Calibration:
    if base:
        path = base if os.path.isfile(base) else os.path.join(COLOR_DIR, base)
    else:
        found = [os.path.join(COLOR_DIR, n) for n in associated_profiles(d)
                 if n.startswith("HDR Calibrated Profile") and os.path.isfile(os.path.join(COLOR_DIR, n))]
        if not found:
            raise SystemExit("No Windows HDR Calibration profile is linked to this monitor.\n"
                             "Run the Windows HDR Calibration app first, or pass --base FILE.")
        path = max(found, key=os.path.getmtime)
    return read_calibration(path)


def known_profiles(d: Display) -> list:
    """HDR profiles linked to the monitor, plus any installed SDR Boost profile."""
    boosts = sorted(os.path.basename(p) for p in glob.glob(os.path.join(COLOR_DIR, "*SDR Boost*.icc")))
    return list(dict.fromkeys(associated_profiles(d) + boosts))


def resolve(d: Display, query: str) -> str:
    """Finds a profile by full name, by what follows "SDR Boost" (e.g. "1400 soft"), or by a unique fragment."""
    names = known_profiles(d)
    q = query.lower().removesuffix(".icc").strip()
    stem = lambda n: n.lower().removesuffix(".icc")
    exact = [n for n in names if q in (stem(n), stem(n).split("sdr boost ")[-1])]
    if len(exact) == 1:
        return exact[0]
    partial = [n for n in names if q in stem(n)]
    if len(partial) == 1:
        return partial[0]
    hits = exact or partial
    raise SystemExit(f'"{query}" matches {len(hits)} profiles' + (":\n  " + "\n  ".join(hits) if hits else "."))


# ---------------------------------------------------------------- boost profiles
def peak_in_for(d: Display, cal: Calibration) -> float:
    """Brightest signal games aim for: the calibrated or the EDID peak."""
    return max(cal.peak, edid_peak(d) or 0.0)


def boost_for(d: Display, cal: Calibration, white: float, soft: float, neutral: bool, peak_in: float = 0.0):
    """Curve, effective white level and profile name for a boost built from `cal`."""
    if white <= SDR_WHITE_IN:
        raise SystemExit("White level must be above 480 nits (Windows already does 480).")
    if not 0 <= soft <= 3:
        raise SystemExit("Softness must be between 0 and 3.")
    white = min(white, cal.peak)
    f = boost_curve(white, peak_in or peak_in_for(d, cal), cal.peak, soft)
    name = f"{d.name} SDR Boost " + (f"{white:.0f}" if white < cal.peak else f"Max {cal.peak:.0f}")
    if soft:
        name += " Soft" if soft == 1 else f" Soft {soft:g}"
    if neutral:
        name += " Neutral"
    return f, white, name


def boost_notes(cal: Calibration, requested: float, white: float) -> list:
    notes = []
    if requested > cal.peak:
        notes.append(f"Capped at the calibrated peak ({cal.peak:.0f} nits); more would only clip the brightest shades.")
    if white >= cal.peak:
        notes.append("SDR white sits at the panel peak, so HDR highlights have no room left and clip.")
    elif white > cal.full_frame > 0:
        notes.append(f"Above the panel's full-screen brightness ({cal.full_frame:.0f} nits): big bright areas get held back.")
    return notes


def create_profile(d: Display, cal: Calibration, white: float, soft: float, neutral: bool, make_active: bool):
    """Builds, installs and links a boost profile. Returns (file name, whether it is now active)."""
    f, _, name = boost_for(d, cal, white, soft, neutral)
    lut = [nits_to_pq(f(pq_to_nits(i / (LUT_SIZE - 1)))) for i in range(LUT_SIZE)]
    file = name + ".icc"
    os.makedirs(PROFILE_DIR, exist_ok=True)
    path = os.path.join(PROFILE_DIR, file)
    with open(cal.path, "rb") as src, open(path, "wb") as dst:
        dst.write(build_profile(src.read(), name, lut, neutral))

    was_active = active_profile(d) == file
    if os.path.exists(os.path.join(COLOR_DIR, file)):  # replacing an earlier build with the same name
        if was_active:
            link(d, os.path.basename(cal.path), True)
        unlink(d, file)
        uninstall(file)
    install(path)
    make_active = make_active or was_active
    link(d, file, make_active)
    return file, make_active


def remove_profile(d: Display, name: str, base=None):
    """Unlinks and deletes an SDR Boost profile. If it was active, switches to the calibration
    profile first and returns that profile's name."""
    if "SDR Boost" not in name:
        raise SystemExit(f"{name} isn't an SDR Boost profile; leaving it alone.")
    switched = None
    if active_profile(d) == name:
        switched = os.path.basename(calibration_for(d, base).path)
        link(d, switched, True)
    unlink(d, name)
    uninstall(name)
    return switched


# ---------------------------------------------------------------- commands
SDR_PROBES = (("dark bg", 0.013), ("mid grey", 0.18), ("light grey", 0.5), ("white", 1.0))


def fmt(v: float) -> str:
    return f"{v:.1f}" if v < 100 else f"{v:.0f}"


def plan(args, d: Display) -> Calibration:
    cal = calibration_for(d, args.base)
    f, white, name = boost_for(d, cal, args.white, args.soft, args.neutral)
    hdr = (800, 1000, 1500, round(cal.peak))
    print(name)
    print(f"  from {os.path.basename(cal.path)}: peak {cal.peak:.0f}, full-screen {cal.full_frame:.0f} nits, "
          f"saturation boost {'dropped' if args.neutral else 'on' if cal.saturation else 'off'}")
    print()
    print(" " * 9 + "".join(f"{label:>11}" for label, _ in SDR_PROBES) + "  |" + "".join(f"{'HDR ' + str(h):>10}" for h in hdr))
    print(f"{'normal':9}" + "".join(f"{fmt(r * SDR_WHITE_IN):>11}" for _, r in SDR_PROBES) + "  |"
          + "".join(f"{fmt(min(h, cal.peak)):>10}" for h in hdr))
    print(f"{'boosted':9}" + "".join(f"{fmt(f(r * SDR_WHITE_IN)):>11}" for _, r in SDR_PROBES) + "  |"
          + "".join(f"{fmt(f(h)):>10}" for h in hdr))
    print("  (nits; dark bg = a dark-mode window background)")
    notes = boost_notes(cal, args.white, white)
    if notes:
        print("\n" + "\n".join("  " + n for n in notes))
    return cal


def cmd_status(args):
    d = pick_display(args.monitor)
    print(f"Display:          {d.name}")
    if d.hdr:
        print(f"HDR:              on, SDR content brightness {d.sdr_white:.0f} nits (slider {(d.sdr_white - 80) / 4:.0f})")
    else:
        print("HDR:              off (boost profiles only apply with HDR on)")
    print(f"Active profile:   {active_profile(d) or '-'}")
    try:
        c = calibration_for(d, args.base)
        print(f"Calibration:      {os.path.basename(c.path)}")
        print(f"                  peak {c.peak:.0f}, full-screen {c.full_frame:.0f}, black {c.black:g} nits, "
              f"saturation boost {'on' if c.saturation else 'off'}")
    except SystemExit as e:
        print(f"Calibration:      {e}")
    e = edid_peak(d)
    print(f"EDID peak:        {f'{e:.0f} nits' if e else '-'}")


def cmd_list(args):
    d = pick_display(args.monitor)
    active = active_profile(d)
    linked = associated_profiles(d)
    for n in known_profiles(d):
        note = "" if n in linked else "   (installed, not linked to this monitor)"
        print(f" {'*' if n == active else ' '} {n}{note}")
    print("\n * = active")


def cmd_preview(args):
    plan(args, pick_display(args.monitor))


def cmd_create(args):
    d = pick_display(args.monitor)
    cal = plan(args, d)
    file, make_active = create_profile(d, cal, args.white, args.soft, args.neutral, args.apply)
    print(f"\nInstalled {file}" + (" and made it active." if make_active else "."))
    if not make_active:
        print(f'Turn it on with:  sdr-boost apply "{file[:-4]}"   (or Settings > Display > Color profile)')


def cmd_apply(args):
    d = pick_display(args.monitor)
    link(d, resolve(d, args.name), True)
    print(f"Active: {active_profile(d)}")


def cmd_normal(args):
    d = pick_display(args.monitor)
    link(d, os.path.basename(calibration_for(d, args.base).path), True)
    print(f"Active: {active_profile(d)}")


def cmd_remove(args):
    d = pick_display(args.monitor)
    name = resolve(d, args.name)
    switched = remove_profile(d, name, args.base)
    if switched:
        print(f"Switched to {switched} first.")
    print(f"Removed {name}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--monitor", help="pick the display by (part of) its name or device path")
    common.add_argument("--base", help="calibration profile to build from (default: the monitor's newest one)")

    ap = argparse.ArgumentParser(prog="sdr-boost", description="Brighter SDR in Windows HDR mode via MHC2 color profiles.")
    ap.add_argument("--version", action="version", version=f"sdr-boost {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")
    sub.add_parser("status", parents=[common], help="HDR state, active profile, calibration")
    sub.add_parser("list", parents=[common], help="this monitor's HDR profiles (* = active)")
    for cmd, text in (("preview", "show what a boost would do, change nothing"),
                      ("create", "build, install and link a boost profile")):
        p = sub.add_parser(cmd, parents=[common], help=text)
        p.add_argument("white", type=float, help="SDR white in nits (Windows max is 480; capped at the calibrated peak)")
        p.add_argument("--soft", type=float, default=0.0,
                       help="0 = normal contrast, 1 = brighter darks/mids with gentler whites, up to 3 = flatter")
        p.add_argument("--neutral", action="store_true", help="drop the saturation boost from HDR Calibration")
        if cmd == "create":
            p.add_argument("--apply", action="store_true", help="make it the active profile right away")
    p = sub.add_parser("apply", parents=[common], help="make a profile active")
    p.add_argument("name", help='e.g. 1400, "1400 soft", or a full profile name')
    sub.add_parser("normal", parents=[common], help="switch back to the HDR Calibration profile")
    p = sub.add_parser("remove", parents=[common], help="unlink and delete an SDR Boost profile")
    p.add_argument("name")

    args = ap.parse_args()
    {"status": cmd_status, "list": cmd_list, "preview": cmd_preview, "create": cmd_create,
     "apply": cmd_apply, "normal": cmd_normal, "remove": cmd_remove}[args.cmd](args)


if __name__ == "__main__":
    main()
