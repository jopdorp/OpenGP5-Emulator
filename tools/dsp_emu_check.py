#!/usr/bin/env python3
"""Boot a built image's DSP firmware in an emulator and check the platform contract, no pedal needed.

  python3 tools/dsp_emu_check.py build/GP-5_<name>.bin [--set ID=VALUE ...]

Checks: it starts (no fault), answers the version query the control half waits for, takes an output level,
processes audio blocks, its outputs are not silent for a test tone, and settings are acknowledged.
For a GP-50 raw section, pass --gp50-raw to model its UART, codecs and DMA routing.
Needs `pip install unicorn`. Timing is not modelled: use GP5_CYCLES_* on the pedal for that.
"""
import argparse, math, struct, sys
from pathlib import Path
try:
    from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
    from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_SP
except ImportError:
    sys.exit("dsp_emu_check needs the Unicorn emulator: pip install unicorn")

import sdk
import gp5tool

ADMA, ST = 0x40030000, 0x20007F00
IN_A, IN_B, OUT_L, OUT_R = 0x1001F400, 0x1001F600, 0x1001F800, 0x1001FA00
CODEC_IN, CODEC_OE, CODEC_SET, CODEC_CLR = 0x4001103C, 0x40011044, 0x40011048, 0x4001104C
SDA, SCL = 0x100, 0x200
CODEC_WRITES = "260001 260600 26051b 260100 2607c0 260900 260da0 260811 260513 101310"


def crc8(p):
    c = 0
    for b in p:
        c ^= b
        for _ in range(8): c = ((c << 1) ^ 7) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("image")
    ap.add_argument("--set", action="append", default=[], metavar="ID=VALUE")
    ap.add_argument("--gp50-raw", action="store_true", help="read a raw GP-50 DSP section")
    ap.add_argument("--codec-nack", action="store_true", help="simulate a missing GP-50 codec")
    a = ap.parse_args()
    image = Path(a.image).read_bytes()
    if a.codec_nack and not a.gp50_raw:
        ap.error("--codec-nack requires --gp50-raw")
    if a.gp50_raw:
        if len(image) != 0xB874 or image[8:12] != b"V105" or gp5tool.check_dsp(image):
            ap.error("expected a raw GP-50 DSP section")
        sec = image
    else:
        sec = dict(gp5tool.inspect(image)[0]).get(0x63)
    if sec is None: sys.exit("the image has no dsp section")
    uart = 0x40006000 if a.gp50_raw else 0x40005000
    mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
    for base, size in ((0, 0x200000), (0x10000000, 0x20000), (0x20000000, 0x8000),
                       (0x40000000, 0x100000), (0x50000000, 0x1000), (0xE0000000, 0x100000)):
        mu.mem_map(base, size)
    mu.mem_write(0x4000, sec)
    rx, tx = bytearray(), bytearray()
    codec_frames = []
    codec_pins, codec_message, codec_bits, codec_value = 0, None, 0, 0

    def reg(addr):
        return struct.unpack("<I", mu.mem_read(addr, 4))[0]

    def rd(mu, acc, addr, size, val, _):
        if addr == uart + 0x0C: mu.mem_write(addr, struct.pack("<I", min(len(rx), 31) << 4))
        elif addr == uart + 0x00: mu.mem_write(addr, struct.pack("<I", rx.pop(0) if rx else 0))
        elif addr == ADMA + 0x00: mu.mem_write(addr, struct.pack("<I", 1))
        elif addr == ADMA + 0x04: mu.mem_write(addr, struct.pack("<I", 0xF))
        elif a.gp50_raw and addr == CODEC_IN:
            mu.mem_write(addr, struct.pack("<I", SDA if a.codec_nack else 0))

    def wr(mu, acc, addr, size, val, _):
        nonlocal codec_pins, codec_message, codec_bits, codec_value
        if addr == uart + 0x00:
            tx.append(val & 0xFF)
        if not a.gp50_raw or addr not in (CODEC_SET, CODEC_CLR):
            return
        old = codec_pins
        codec_pins = old | val if addr == CODEC_SET else old & ~val
        if old & codec_pins & SCL and (old ^ codec_pins) & SDA:
            if codec_pins & SDA:
                if codec_message is not None:
                    codec_frames.append(codec_message.hex())
                codec_message = None
            else:
                codec_message = bytearray()
                codec_bits = codec_value = 0
        elif codec_message is not None and (codec_pins & ~old & SCL) and reg(CODEC_OE) & SDA:
            codec_value = (codec_value << 1) | bool(codec_pins & SDA)
            codec_bits += 1
            if codec_bits == 8:
                codec_message.append(codec_value)
                codec_bits = codec_value = 0
    mu.hook_add(UC_HOOK_MEM_READ, rd, begin=0x40000000, end=0x400FFFFF)
    mu.hook_add(UC_HOOK_MEM_WRITE, wr, begin=0x40000000, end=0x400FFFFF)
    sp, reset = struct.unpack_from("<2I", sec, 0x400)
    mu.reg_write(UC_ARM_REG_SP, sp)
    pc = [reset]

    def run(n):
        mu.emu_start(pc[0] | 1, 0, count=n); pc[0] = mu.reg_read(UC_ARM_REG_PC)

    def st(i): return reg(ST + 4 * i)

    def send(pl, n=300000):
        tx.clear(); ln = len(pl) + 4
        rx.extend(bytes([crc8(pl), ln & 0xFF, ln >> 8, 1]) + pl); run(n); return bytes(tx)

    ok = True

    def check(name, cond, info=""):
        nonlocal ok; ok &= bool(cond); print(f"  {'ok ' if cond else 'FAIL'} {name} {info}")

    tone = [int(0.5 * 2 ** 31 * math.sin(2 * math.pi * 440 * i / 44100)) for i in range(128)]
    run(3_000_000)                                   # start-up clears the audio buffers: load the tone after it
    mu.mem_write(IN_A, struct.pack("<128i", *tone)); mu.mem_write(IN_B, struct.pack("<128i", *[0] * 128))
    check("starts without a fault", st(3) == 0, hex(st(3)) if st(3) else "")
    if a.gp50_raw:
        check("configures the GP-50 clock and module gates",
              [reg(address) for address in (0x4000E054, 0x4000E058, 0x4000E05C, 0x4000E00C)] ==
              [0x03000F00, 0x0B400000, 0x03002222, 0x1FC3F] and bool(reg(0x4000E020) & 2))
    r = send(bytes.fromhex("0060080000000000"))
    check("answers the version query", len(r) >= 16 and r[4:8] == bytes.fromhex("00600c00"),
          "version 0x%08x" % struct.unpack_from("<I", r, 12)[0] if len(r) >= 16 else r.hex())
    check("takes the output level", send(bytes.fromhex("01100c00000000000000803f")) == b"\x01\x04\x00\x14")
    for s in a.set:
        k, _, v = s.partition("=")
        check(f"setting {k} = {v} acknowledged",
              send(struct.pack("<HHIi", 0x7F10, 12, int(k, 0), int(v, 0))) == b"\x01\x04\x00\x14")
    if a.codec_nack:
        check("keeps audio off after a codec NACK", st(13) == 0 and reg(0x50000000) == 0)
    else:
        if a.gp50_raw:
            check("configures both codecs", codec_frames == CODEC_WRITES.split(), " ".join(codec_frames))
            check("routes all four DMA channels to GP-50 FIFOs",
                  [reg(ADMA + offset) for offset in (0x148, 0x168, 0x10C, 0x12C)] ==
                  [0x5000000C, 0x50000010, 0x50000014, 0x50000018])
        blocks0 = st(13); run(3_000_000)
        outl = struct.unpack("<128i", mu.mem_read(OUT_L, 512)); outr = struct.unpack("<128i", mu.mem_read(OUT_R, 512))
        check("processes audio blocks", st(13) > blocks0, f"{st(13) - blocks0} blocks")
        check("outputs are not silent for a tone", max(map(abs, outl)) > 0 and max(map(abs, outr)) > 0,
              f"peak L {max(map(abs, outl)) / 2 ** 31:.3f} R {max(map(abs, outr)) / 2 ** 31:.3f}")
    check("still no fault", st(3) == 0 and st(8) == 0, hex(st(3)) if st(3) else "")
    print("ALL OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
