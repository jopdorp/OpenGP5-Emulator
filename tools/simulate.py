#!/usr/bin/env python3
"""Run a whole firmware on the computer: both processors' code, the audio link between them, the screen, the controls.
No pedal needed (docs/platform/simulator.md).

  python3 tools/simulate.py <firmware> [--in guitar.wav | --tone] [--out out.wav] [--script controls.txt]
                            [--seconds S] [--shots DIR] [--knob 0..255] [--store FILE] [--files DIR]

<firmware> is a folder under opengp5/firmwares/, or the path of a firmware folder elsewhere (it needs a firmware.json).
  --in WAV        the guitar (16 or 24-bit PCM, first channel); --tone: a plucked test tone instead (default)
  --out WAV       what the pedal puts out, stereo 24-bit (default build/sim/<firmware>.wav)
  --script FILE   what you do, one line each, a time in seconds first (# starts a comment):
                      0.5  foot                  press and let go of the foot switch (foot down / foot up: one edge)
                      1.0  para turn -2          turn PARA two clicks back
                      1.5  para click            push and let go (para hold: held 0.8 s; para down / para up)
                      2.0  knob 128              the volume knob, 0..255
                      2.5  set Rate 800          a parameter, as a computer sets it: by the name shown (Loop_level for
                                                 "Loop level") or params.h's; a choice by name: set Shape Square
                      3.0  preset load 2         presets as on the pedal, from 1 (preset save 2)
                      3.5  shot after-load       the screen now, as <shots>/after-load.png
  --seconds S     how long (default: the input's length; 4 s for the tone)
  --shots DIR     where screens go (default build/sim/<firmware>/); the screen at the end is end.png
  --knob N        where the volume knob starts (default 255)
  --store FILE    keep the firmware's saved settings in FILE between runs (default: start empty every time)
  --files DIR     the firmware's files (gp5_file): every file in DIR, by its name
  --check         build with the address and undefined-behaviour checkers: a crash or a bad memory access stops
                  with the line it happened on (slower)
  --gp50          160 x 128 screen and extra inputs, e.g. "left turn 2", "save click", "fs2 down"
  --host-dsp LIB  use a prebuilt native DSP library with the simulator ABI
  --live          open the GP-50 window with JACK audio ports (requires --gp50)
  --demo WAV      add a playable demo clip to the live window
The log (gp5_log on either processor) and the LEDs print as the run goes. The timing is the pedal's: 64-sample
blocks, fw_tick every 10 ms, the PARA hold at 0.6 s, the audio link's words three blocks on. The arithmetic is the
pedal's maths code compiled for the computer; cycle counts are not simulated (gp5_link.py status on the pedal).
Needs a C compiler (cc); Pillow for PNG screens (else they stay .ppm).
"""
import argparse, array, math, os, pathlib, re, shlex, struct, subprocess, sys, tempfile, wave

from sdk import HOST, PLATFORM, ROOT, SDK
import manifest
from run_effect import read_pcm_wav, read_wav, tone

GP50_ENCODERS = ("left", "middle", "right")
GP50_BUTTONS = ("drum", "exit", "global", "save", "fs2")


def firmware_dir(name):
    p = pathlib.Path(name)
    return p if (p / "firmware.json").exists() else SDK / "firmwares" / name


def param_names(fw):
    """params.h: names in order (their ids) and each choice's names"""
    if not (fw / "params.h").exists(): return [], {}, {}
    t = (fw / "params.h").read_text()
    entries = re.findall(r'\b[PC]\(\s*([A-Z0-9_]+)\s*,\s*"([^"]*)"', t)
    names = [k for k, _ in entries]
    shown = {d.upper().replace(" ", "_"): k for k, d in entries}       # "Loop level" -> LOOP_LEVEL
    choices = {k: c.split("|") for k, c in re.findall(r'\bC\(\s*([A-Z0-9_]+)\s*,\s*"[^"]*"\s*,\s*"[^"]*"\s*,\s*\d+\s*,\s*"([^"]*)"', t)}
    return names, choices, shown


def events(script, names, choices, shown, gp50=False):
    """the script's lines -> (ms, event) pairs, sorted"""
    out = []
    for n, raw in enumerate(script.splitlines(), 1):
        line = raw.split("#", 1)[0].split()
        if not line: continue
        def bad(why): sys.exit(f"script line {n}: {why}: {raw.strip()}")
        try: t = round(float(line[0]) * 1000)
        except ValueError: bad("starts with a time in seconds")
        w = [x.lower() for x in line[1:]]
        if w[:1] == ["foot"]:
            if len(w) == 1: out += [(t, "foot_down"), (t + 100, "foot_up")]
            elif w[1] in ("down", "up"): out.append((t, f"foot_{w[1]}"))
            else: bad("foot, foot down or foot up")
        elif w[:1] == ["para"] and len(w) >= 2:
            if w[1] == "click": out += [(t, "para_down"), (t + 100, "para_up")]
            elif w[1] == "hold": out += [(t, "para_down"), (t + 800, "para_up")]
            elif w[1] in ("down", "up"): out.append((t, f"para_{w[1]}"))
            elif w[1] == "turn" and len(w) == 3 and re.match(r"^[+-]?\d+$", w[2]): out.append((t, f"turn {int(w[2])}"))
            else: bad("para click | hold | down | up | turn N")
        elif gp50 and w and w[0] in GP50_ENCODERS:
            if len(w) != 3 or w[1] != "turn" or not re.fullmatch(r"[+-]?\d+", w[2]):
                bad(f"{w[0]} turn N")
            encoder = GP50_ENCODERS.index(w[0])
            out.append((t, f"encoder {encoder} {int(w[2])}"))
        elif gp50 and w and w[0] in GP50_BUTTONS:
            button = GP50_BUTTONS.index(w[0])
            if len(w) == 2 and w[1] == "click":
                out += [(t, f"button {button} 1"), (t + 100, f"button {button} 0")]
            elif len(w) == 2 and w[1] in ("down", "up"):
                out.append((t, f"button {button} {int(w[1] == 'down')}"))
            else: bad(f"{w[0]} click | down | up")
        elif gp50 and w[:1] == ["expression"]:
            if len(w) != 2 or not re.fullmatch(r"[0-9]{1,3}", w[1]) or int(w[1]) > 255:
                bad("expression N (0..255)")
            out.append((t, f"expression {int(w[1])}"))
        elif w[:1] == ["knob"] and len(w) == 2 and w[1].isdigit(): out.append((t, f"knob {int(w[1])}"))
        elif w[:1] == ["set"] and len(w) == 3:
            k, v = line[2].upper(), line[3]
            k = k if k in names else shown.get(k, k)                         # the code's name or the one shown
            if k in names:
                low = [c.lower() for c in choices.get(k, [])]
                if v.lower() in low: v = str(low.index(v.lower()))
                k = names.index(k)
            elif re.match(r"^\d+$", k): k = int(k)
            else: bad(f"no parameter {line[2]} (params.h has {', '.join(names) or 'none'}; or their names as shown, spaces as _)")
            try: out.append((t, f"set {k} {int(v, 0)}"))
            except ValueError: bad("set NAME VALUE (a number, or a choice's name)")
        elif w[:1] == ["preset"] and len(w) == 3 and w[1] in ("load", "save") and w[2].isdigit() and int(w[2]) >= 1:
            out.append((t, f"preset_{w[1]} {int(w[2]) - 1}"))
        elif w[:1] == ["shot"] and len(w) == 2 and re.match(r"^[A-Za-z0-9_.-]+$", line[2]): out.append((t, f"shot {line[2]}"))
        elif w == ["storage", "changed"]: out.append((t, "storage_changed"))
        else: bad("not understood")
    return sorted(out, key=lambda e: e[0])


def build(fw, m, d, check=False, gp50=False, live=False, host_dsp=None):
    plat = PLATFORM
    flags = manifest.flags(m, fw) + (["-DGP50_BOARD=1"] if gp50 else [])
    sources, libraries = [], []
    if live:
        try:
            libraries = shlex.split(subprocess.check_output(
                ["pkg-config", "--cflags", "--libs", "jack", "samplerate", "sdl2"], text=True))
        except (FileNotFoundError, subprocess.CalledProcessError):
            sys.exit("live mode needs pkg-config and the JACK, libsamplerate, and SDL2 development libraries")
        flags.append("-DSIM_LIVE")
        sources = [str(HOST / f"{name}.c") for name in ("live", "live_ui")]
    lib, exe = host_dsp or d / "sim_dsp.so", d / "sim"
    cc = ["cc", "-std=gnu11" if live else "-std=gnu99", "-Wall", "-Wno-unused-function", "-Werror=implicit-function-declaration", "-fno-builtin", "-g", *flags,
          *(["-fsanitize=address,undefined", "-fno-sanitize=shift-base", "-fno-omit-frame-pointer"] if check else [])]
    if host_dsp is None:
        subprocess.run([*cc, "-O2", "-fPIC", "-shared", "-fvisibility=hidden", "-DGP5_HOST", f"-I{fw / 'dsp'}", f"-I{fw}",
                        f"-I{plat / 'dsp'}", str(HOST / "sim_dsp.c"), str(plat / "dsp/gp5_math.c"), "-o", str(lib)], check=True)
    ctl = sorted((fw / "nds32").glob("*.c"))
    math_inline = any('#include "gp5_math.c"' in c.read_text(errors="replace") for c in ctl)
    subprocess.run([*cc, "-O1", "-DGP5_HOST", "-DUI_PANEL", "-DGP5_CTL_SIDE", f"-I{plat / 'nds32'}", f"-I{HOST / 'include'}",
                    f"-I{plat / 'dsp'}", f"-I{fw}", f"-I{fw / 'nds32'}", str(HOST / "sim.c"), str(plat / "nds32/gp5_params.c"),
                    str(plat / "nds32/gp5_ui.c"), str(plat / "nds32/text.c"), *map(str, ctl), *sources,
                    *([] if math_inline else [str(plat / "dsp/gp5_math.c")]), "-lm", *(["-ldl"] if sys.platform.startswith("linux") else []),
                    *libraries, "-o", str(exe)], check=True)
    return lib, exe


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("firmware")
    ap.add_argument("--in", dest="inp"); ap.add_argument("--tone", action="store_true")
    ap.add_argument("--out"); ap.add_argument("--script"); ap.add_argument("--seconds", type=float)
    ap.add_argument("--shots"); ap.add_argument("--knob", type=int, default=255)
    ap.add_argument("--store"); ap.add_argument("--files")
    ap.add_argument("--gp50", action="store_true", help="simulate the GP-50 screen size and inputs")
    ap.add_argument("--host-dsp", type=pathlib.Path, metavar="LIB", help="use a prebuilt native DSP library")
    ap.add_argument("--live", action="store_true", help="open a GP-50 window and JACK audio ports")
    ap.add_argument("--demo", metavar="WAV", help="enable sample playback in the live window")
    ap.add_argument("--check", action="store_true", help="build with the address and undefined-behaviour checkers")
    a = ap.parse_args()
    if a.demo and not a.live:
        ap.error("--demo requires --live")
    if a.host_dsp is not None:
        a.host_dsp = a.host_dsp.resolve()
        if not a.host_dsp.is_file():
            ap.error(f"--host-dsp: no such library: {a.host_dsp}")
    fw = firmware_dir(a.firmware).resolve()
    try: m = manifest.load(fw)
    except manifest.ManifestError as e: sys.exit(str(e))
    if not m: sys.exit(f"{fw}: no firmware.json (the simulator runs OpenGP5 1.0 firmwares)")
    if a.live:
        if not a.gp50:
            ap.error("--live requires --gp50")
        if any((a.inp, a.out, a.script, a.seconds, a.shots, a.tone, a.store, a.files, a.knob != 255)):
            ap.error("--live takes input from the window and JACK. Use offline options without --live.")
        with tempfile.TemporaryDirectory(prefix="opengp5-live-") as d:
            d = pathlib.Path(d)
            demo_args = []
            if a.demo:
                try:
                    samples, rate = read_pcm_wav(a.demo)
                except (OSError, EOFError, wave.Error) as e:
                    ap.error(f"{a.demo}: {e}")
                if not samples:
                    ap.error(f"{a.demo}: empty demo clip")
                raw = d / "demo.raw"
                raw.write_bytes(samples.tobytes())
                demo_args = [str(raw), str(rate)]
            lib, exe = build(fw, m, d, a.check, a.gp50, a.live, a.host_dsp)
            return subprocess.call([str(exe), str(lib), m["name"], *demo_args])
    rate = m["audio"]["sample_rate"]
    names, choices, shown = param_names(fw)
    script = pathlib.Path(a.script).read_text() if a.script else ""
    ev = [(0, f"knob {max(0, min(255, a.knob))}")] + events(script, names, choices, shown, a.gp50)
    samples = read_wav(a.inp) if a.inp else tone()
    seconds = a.seconds or (len(samples) / rate)
    out = pathlib.Path(a.out or ROOT / "build/sim" / f"{m['id']}.wav")
    shots = pathlib.Path(a.shots or ROOT / "build/sim" / m["id"])
    out.parent.mkdir(parents=True, exist_ok=True); shots.mkdir(parents=True, exist_ok=True)
    for old in shots.glob("*.p[np][gm]"): old.unlink()
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        lib, exe = build(fw, m, d, a.check, a.gp50, host_dsp=a.host_dsp)
        (d / "events.txt").write_text("".join(f"{t} {e}\n" for t, e in ev))
        (d / "in.raw").write_bytes(samples.tobytes())
        env = dict(os.environ, ASAN_OPTIONS=os.environ.get("ASAN_OPTIONS", "abort_on_error=0"))   # (--check: report, no crash report)
        r = subprocess.run([str(exe), str(lib), str(d / "events.txt"), str(d / "in.raw"), str(d / "out.raw"), str(shots),
                            f"{seconds}", str(pathlib.Path(a.store).resolve()) if a.store else "-",
                            str(pathlib.Path(a.files).resolve()) if a.files else "-"], env=env)
        if r.returncode:
            sig = -r.returncode if r.returncode < 0 else r.returncode - 256 if r.returncode > 128 else 0
            sys.exit(f"the firmware crashed (signal {abs(sig)}): run again with --check to find where"
                     if r.returncode < 0 or r.returncode > 128 else f"the simulation failed ({r.returncode})")
        pcm = array.array("i"); pcm.frombytes((d / "out.raw").read_bytes())
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<i", v)[1:] for v in pcm))
    peak = max((abs(v) for v in pcm), default=0) / 2 ** 31
    try:
        from PIL import Image
        for p in sorted(shots.glob("*.ppm")):
            with Image.open(p) as screen:
                screen.resize((screen.width * 4, screen.height * 4), Image.NEAREST).save(p.with_suffix(".png"))
            p.unlink()
        kind = "png"
    except ImportError:
        kind = "ppm (pip install pillow for PNG)"
    print(f"{m['name']} {m['version']}: {seconds:.2f} s at {rate} Hz -> {out} (peak {20 * math.log10(peak) if peak else -999:.1f} dBFS); "
          f"screens in {shots} ({kind})")


if __name__ == "__main__":
    sys.exit(main())
