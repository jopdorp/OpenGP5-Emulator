#!/usr/bin/env python3
"""Checks of the simulator (tools/simulate.py) on the tremolo: a script's controls, parameters, presets, saving
between runs, LEDs, screens and sound. python3 tools/test_simulate.py (needs cc)"""
import json, math, pathlib, shutil, subprocess, sys, tempfile, wave

ROOT = pathlib.Path(__file__).resolve().parents[1]
fails = 0


def check(ok, what):
    global fails
    print(("ok   " if ok else "FAIL ") + what)
    fails += not ok


def run(d, name, script, *extra, firmware="tremolo"):
    (d / f"{name}.txt").write_text(script)
    r = subprocess.run([sys.executable, str(ROOT / "tools/simulate.py"), str(firmware), "--seconds", "2", "--script", str(d / f"{name}.txt"),
                        "--out", str(d / f"{name}.wav"), "--shots", str(d / name), *extra], capture_output=True, text=True)
    if r.returncode: print(r.stdout, r.stderr)
    return r


def level(path):
    with wave.open(str(path)) as w:
        b = w.readframes(w.getnframes())
    s = [int.from_bytes(b[i:i + 3], "little", signed=True) for i in range(0, len(b), 6)]
    m = s[len(s) // 4:]                                                    # past the start
    return 10 * math.log10(sum(x * x for x in m) / len(m) / 2 ** 46 + 1e-20), s


def native_dsp(d):
    firmware = d / "native-firmware"
    shutil.copytree(ROOT / "opengp5/firmwares/tremolo", firmware, ignore=shutil.ignore_patterns("dsp"))
    (firmware / "nds32/expression.c").write_text("""
#include "gp5_params.h"
void fw_gp50_expression(unsigned value) { gp5_param_set(GP5_P_DEPTH, value * 100 / 255); }
""")
    source = d / "native.c"
    lib = d / "native.so"
    source.write_text("""
#include <stdint.h>
static int32_t link[1], depth;
unsigned sim_dsp_link_words(void) { return 0; }
unsigned sim_dsp_sample_rate(void) { return 44100; }
int32_t *sim_dsp_link(int which) { (void)which; return link; }
int sim_dsp_link_early(const int32_t *in) { (void)in; return 0; }
void sim_dsp_init(const uint32_t *ms) { (void)ms; }
int sim_dsp_command(const uint8_t *p, unsigned n) {
    if (n >= 12 && p[0] == 0x10 && p[1] == 0x7f && p[4] == 1) depth = p[8];
    return 1;
}
void sim_dsp_block(const int32_t *in, int32_t *l, int32_t *r) {
    for (unsigned i = 0; i < 64; ++i) {
        l[i] = (in[i] / 100) * depth;
        r[i] = 0;
    }
}
""")
    subprocess.run(["cc", "-fPIC", "-shared", str(source), "-o", str(lib)], check=True)
    opts = "--gp50", "--host-dsp", str(lib)
    expr = run(d, "native-expression", "0.2 expression 204\n", *opts, firmware=firmware)
    param = run(d, "native-parameter", "0.2 set Depth 80\n", *opts, firmware=firmware)
    if expr.returncode or param.returncode:
        check(False, "native DSP selection and GP-50 expression")
    else:
        with wave.open(str(d / "native-expression.wav")) as wav:
            audio = wav.readframes(wav.getnframes())
        check((d / "native-expression.wav").read_bytes() == (d / "native-parameter.wav").read_bytes()
              and any(audio[i:i + 3] != b"\0\0\0" for i in range(0, len(audio), 6))
              and all(audio[i:i + 3] == b"\0\0\0" for i in range(3, len(audio), 6)),
              "native DSP selection and GP-50 expression reach the same parameters")
    manifest = firmware / "firmware.json"
    data = json.loads(manifest.read_text())
    data.setdefault("audio", {})["sample_rate"] = 48000
    manifest.write_text(json.dumps(data))
    wrong_rate = run(d, "native-rate", "", *opts, firmware=firmware)
    check(wrong_rate.returncode != 0 and "DSP sample rate 44100 does not match firmware rate 48000" in wrong_rate.stderr,
          "native DSP rate must match the firmware")


with tempfile.TemporaryDirectory() as d:
    d = pathlib.Path(d)
    r = run(d, "plain", "0.5 shot start\n")
    check(r.returncode == 0, "runs")
    check((d / "plain/start.png").exists() or (d / "plain/start.ppm").exists(), "a shot at a time")
    check((d / "plain/end.png").exists() or (d / "plain/end.ppm").exists(), "the end screen")
    check("LEDs: mint" in r.stderr, "LEDs reported")
    r = run(d, "off", "0.2 foot\n")
    check("LEDs: dim red" in r.stderr, "the foot switch reaches the firmware")
    _, dry = level(d / "off.wav")
    _, wet = level(d / "plain.wav")
    check(any(abs(a - b) > 1000 for a, b in zip(dry[44100:], wet[44100:])), "bypass changes the sound")
    run(d, "deep", "0 set Depth 100\n0 set Shape Square\n")
    _, deep = level(d / "deep.wav")
    check(sum(1 for v in deep[44100:] if v == 0) > 1000, "set Depth 100 + Square: silent half-waves")
    r = run(d, "bad", "0 set Nope 1\n")
    check(r.returncode != 0 and "no parameter Nope" in r.stderr, "an unknown parameter is refused")
    store = d / "store.bin"
    run(d, "save", "0.1 set Rate 900\n0.2 preset save 3\n", "--store", str(store))
    r = run(d, "load", "0.1 set Rate 100\n0.2 preset load 3\n0.5 shot loaded\n", "--store", str(store))
    check(r.returncode == 0 and store.exists(), "the store is kept between runs")
    run(d, "restart", "0.5 shot after\n", "--store", str(store))
    check((d / "restart/after.png").exists() or (d / "restart/after.ppm").exists(), "a restart with the store")
    knob_script = "0.2 left turn 2\n0.4 middle turn 3\n0.6 right turn 1\n0.8 fs2 click\n"
    param_script = "0.2 set Rate 520\n0.4 set Depth 65\n0.6 set Shape Triangle\n0.8 set Shape Square\n"
    controls = run(d, "gp50-controls", knob_script, "--gp50")
    parameters = run(d, "gp50-parameters", param_script, "--gp50")
    check(controls.returncode == parameters.returncode == 0 and
          (d / "gp50-controls.wav").read_bytes() == (d / "gp50-parameters.wav").read_bytes(),
          "GP-50 encoders and FS2 reach the DSP parameters")
    native_dsp(d)
print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
