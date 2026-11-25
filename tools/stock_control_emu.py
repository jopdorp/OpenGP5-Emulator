#!/usr/bin/env python3
"""Run an NDS32 app from reset in the Andes simulator and check startup landmarks."""

import argparse
from collections import namedtuple
from hashlib import sha256
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import zipfile

from sdk import SDK
import htfw
from gp5_proto import CLASS_CMD, build_sysex


ANDES = SDK / "fw-build/andes/v3s/nds32le-elf-mculib-v3s/bin"
SIM = ("target sim --environment operating --memory-region 0x0,0x200000 "
       "--memory-region 0x20000000,0x50000 --memory-region 0x40000000,0x100000")
StockBoot = namedtuple("StockBoot", "sha256 tlb efuse pll audio_pll measure calibrated frame iret task adc board gpio_oe")
STOCK = {
    "GP-5": StockBoot("a518f44c8f6a899775c1a69636cda7c34c2bbae6afed23e4d6eca88defca61e5",
                      tlb=0x61d16, efuse=0x62f02, pll=0x61750, audio_pll=0x61846,
                      measure=0x615bc, calibrated=0x615f6, frame=0x5eeb6, iret=0x5eede,
                      task=0x4bc1c, adc=0x604cc, board=0x4bc2c, gpio_oe=0x01418280),
    "GP-50": StockBoot("7ac580be0ea153e15404f1282d6f7480a7781f0e494391daa206b923246575b0",
                       tlb=0x5dd88, efuse=0x5ef74, pll=0x5d7c2, audio_pll=0x5d8b8,
                       measure=0x5d62e, calibrated=0x5d668, frame=0x5b3b6, iret=0x5b3de,
                       task=0x4b5e6, adc=0x5c53e, board=0x4b5fc, gpio_oe=0x11430080),
}
STOCK_RADIO = {
    "GP-5": (0x4bc6c, 0x6139c, 0x85ad8, 0x5ff28),
    "GP-50": (0x4b63c, 0x5d40e, 0x81c80, 0x5c0a4),
}
StockCold = namedtuple("StockCold", "ipc frame iret tick idle yield_at bluetooth delay run tx rx rx_caller "
                       "version_at version catalog panel_window spi panel_done rows gpio_a gpio_b ir_count writes")
STOCK_COLD = {
    "GP-5": StockCold(ipc=0x11b5c, frame=0x11eac, iret=0x11ed4, tick=0x5fb78, idle=0x5fe2c, yield_at=0x5f000,
                      bluetooth=0x58608, delay=0x5fd78, run=0x23b08, tx=0x48814, rx=0x629ea, rx_caller=0x146d0,
                      version_at=0x46dcc, version=0x01010006, catalog=(0x47000, 0x47020, 0x47060),
                      panel_window=0x481c4, spi=0x66a36, panel_done=0x418d4, rows=80, gpio_a=0x20000, gpio_b=0x4c,
                      ir_count=40, writes=((0x1000, 16), (0x1001, 12), (0x6003, 8), (0, 368), (0x6003, 8))),
    "GP-50": StockCold(ipc=0x11b5a, frame=0x11e6c, iret=0x11e94, tick=0x5bdb6, idle=0x5bf9c, yield_at=0x5b4b8,
                       bluetooth=0x55dbc, delay=0x5bf60, run=0x27914, tx=0x48712, rx=0x5ea5c, rx_caller=0x14a62,
                       version_at=0x4604c, version=0x01010005, catalog=(0x46212, 0x46238),
                       panel_window=0x47d74, spi=0x62a0a, panel_done=0x2792c, rows=128, gpio_a=0x8000, gpio_b=0x10,
                       ir_count=41, writes=((0x1000, 17), (0x1001, 12), (0, 368))),
}


def command(andes, tool, *args):
    result = subprocess.run([andes / f"nds32le-elf-{tool}", *args], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10)
    return result.stdout


def checkpoints(andes, elf, commands, expected, sim=SIM):
    with TemporaryDirectory() as directory:
        path = Path(directory) / "startup.gdb"
        path.write_text("\n".join([sim, "load", *commands]) + "\n")
        output = command(andes, "gdb", "-nx", "-batch", elf, "-x", path)
    actual = re.findall(r"^CHECK ([a-z]+) (\d+) (0x[0-9a-f]+) (0x[0-9a-f]+)$", output, re.M)
    if [label for label, _, _, _ in actual] != expected or any(
        correct != "1" or not 0x20000000 <= int(stack, 16) < 0x20050000
        for _, correct, _, stack in actual
    ):
        raise ValueError(f"startup checkpoint failed\n{output}")
    for label, _, pc, stack in actual:
        print(f"{label} reached at {pc} with SP {stack}")


def check(label, target):
    return f'printf "CHECK {label} %d %#x %#x\\n", $pc == {target}, $pc, $sp'


def tlb_fixture(address):
    # GDB's flat memory map already covers these regions. Replace the unsupported
    # target-write instruction with two 16-bit NOPs only in simulator memory.
    return f"set {{unsigned int}} {address:#x} = 0x00920092"


def gp50_warm_handoff():
    # Enter the stock dispatcher with an already-decoded request. USB emission,
    # the inter-task GPIO update and timer delays are outside this simulation.
    ack = build_sysex(CLASS_CMD, 0, b"\x14\x08\x00")
    expect_ack = " && ".join(
        f"*(unsigned int *)($r0 + {i}) == {int.from_bytes(ack[i:i + 4], 'little'):#x}"
        for i in range(0, len(ack), 4)
    )
    commands = [
        "set {unsigned short} 0x2004ff00 = 0x6f11",
        "set $r6 = 0x2004ff00", "set {unsigned int} ($sp + 4) = 0", "set $pc = 0x42c64",
        "tbreak *0x42b7e", "continue", check("ack", f"0x42b7e && $r1 == {len(ack)} && {expect_ack}"),
        "set $r0 = 1", "set $pc = $lp",
    ]
    for label in ["delayone", "delaytwo"]:
        commands.extend([
            "tbreak *0x4796a", "continue", "set $pc = $lp",
            "tbreak *0x5bf60", "continue", check(label, "0x5bf60 && $r0 == 100"), "set $pc = $lp",
        ])
    commands.extend([
        "tbreak *0x49734", "continue",
        check("handoff", "0x49734 && $r6 == 0 && (*(unsigned int *)0x4000100c & 0x22) == 0x22"),
    ])
    return commands


def stock_radio(model, flash, flash_base):
    settings, entropy, clock, wait = STOCK_RADIO[model]
    # Use the verified stock preset section and erased user storage. Reads use
    # the stock driver, with the NOR data register fed from the emulated image.
    # The erase and program operations update only this simulator's memory.
    commands = [f"restore {flash} binary {flash_base:#x}", f"set $flash_base = {flash_base:#x}", r"""
define read_stock_nor
  set $offset = $r10 + $r9 - $r6
  if $offset < $flash_base || $offset + 4 > 0x200000
    printf "unmodeled NOR read at %#x\n", $offset
    quit 1
  end
  set {unsigned int}0x40007000 = *(unsigned int *)$offset
end
set $flash_reads = 0
"""]
    for read in (0x1104e, 0x1105e):
        commands.extend([
            f"break *{read:#x}", "commands", "silent",
            "read_stock_nor",
            "set $flash_reads = $flash_reads + 1", "continue", "end",
        ])
    commands.append(r"""
break *0x11482
commands
  silent
  if $r0 != 1 || $r1 < 0x190 || $r1 >= 0x200
    printf "unmodeled NOR erase at sector %#x\n", $r1
    quit 1
  end
  set $address = $r1 << 12
  set $end = $address + 0x1000
  while $address < $end
    set {unsigned int}$address = 0xffffffff
    set $address = $address + 4
  end
  set $r0 = 0
  set $pc = $lp
  continue
end
break *0x11124
commands
  silent
  if $r0 < 0x190000 || $r0 + $r2 > 0x200000
    printf "unmodeled NOR program at %#x\n", $r0
    quit 1
  end
  set $i = 0
  while $i < $r2
    set {unsigned char}($r0+$i) = *(unsigned char *)($r0+$i) & *(unsigned char *)($r1+$i)
    set $i = $i + 1
  end
  set $r0 = 0
  set $pc = $lp
  continue
end
""")
    commands.extend([
        f"tbreak *{settings:#x}", "continue",
        check("settings", f"{settings:#x} && $flash_reads == 50 && *(unsigned int *)0x1f3000 == 0x3142564d"),
        f"tbreak *{entropy:#x}", "continue", check("entropy", hex(entropy)),
        "set {unsigned int}0x40021068 = *(unsigned int *)0x40021068 | 4",
        "set {unsigned int}0x4002106c = 0xb583d921",
        # Supply completion bits polled by the stock calibration code.
        "set {unsigned short}0x40009154 = 1",
        "set {unsigned short}0x40009008 = 3",
        "set {unsigned short}0x40009564 = 8",
        "set {unsigned short}0x400095ec = 1",
        "set $clock_reads = 0",
        f"break *{clock:#x}", "commands", "silent",
        "set {unsigned int}0x4000801c = *(unsigned int *)0x4000801c & 0x7fffffff",
        "set $clock_reads = $clock_reads + 1", "continue", "end",
        f"tbreak *{wait:#x}", "continue",
        check("radio", f"{wait:#x} && $clock_reads == 2 && *(unsigned int *)0x1ff00c == 0x352d5047"),
    ])
    return commands


def stock_cold(model, wire, requests):
    # Deliver stock FreeRTOS switches and ticks explicitly because Andes sim
    # lacks the interrupt controller and IRET. Defer the packed Bluetooth task
    # after calibration so its alignment faults do not prevent the LCD check.
    cold = STOCK_COLD[model]
    gp50 = model == "GP-50"
    catalog_count = cold.ir_count + 80 + (0 if gp50 else 8)
    stop = 0x27960 if gp50 else cold.panel_done
    catalog_breaks = "\n".join(f"break *{address:#x}" for address in cold.catalog)
    catalog_events = " || ".join(f"$event == {address:#x}" for address in cold.catalog)
    # GP-50 waits for a Run event and renders its full first UI frame before
    # the final checkpoint. GP-5 starts its application task automatically.
    run_setup = "break *0x4ab3e" if gp50 else ""
    run_event = "if $event == 0x4ab3e\n    set {unsigned short}$r0 = 0x0201\n  end" if gp50 else ""
    ui_setup = "tbreak *0x27960\nbreak *0x405b8" if gp50 else ""
    ui_event = r"""
  if $event == 0x405b8
    if *(unsigned short *)$r1 == 0 && *(unsigned short *)($r1 + 4) == 159 && *(unsigned short *)($r1 + 6) == $ui_bottom
      set $ui_bottom = (int)*(unsigned short *)($r1 + 2) - 1
    end
  end""" if gp50 else ""
    user_slots = """
      if $cmd == 0x7005 && $slot >= 72 && $slot < 80
        set $record = $slot + 49
      end""" if not gp50 else ""
    # The GP-5 sends a data reply followed by a four-byte ACK for 0x7005.
    user_ack = """
      if $cmd == 0x7005
        set $pending = $pending + 4
      end""" if not gp50 else ""
    app_check = check("app", "0x27960 && $ui_bottom == -1 && $ui_bytes == 40960") if gp50 else ""
    panel_check = check("panel", f"{cold.panel_done:#x} && $catalog == {catalog_count} && "
                                f"$acks == {len(cold.writes)} && $lcd == 2 && $windows == {cold.rows}")
    return [f"restore {wire} binary 0x90000000", rf"""
define switch_stock_task
  set $resume = $pc
  set $saved_r0 = $r0
  set $saved_r1 = $r1
  set $pc = 0x11aac
  si
  set $psw = $r0
  set $r0 = $resume
  set $r1 = $psw
  set $pc = {cold.ipc:#x}
  si 2
  set $r0 = ($psw & ~1) + 2
  set $pc = 0x11ab2
  si
  set $r0 = $saved_r0
  set $r1 = $saved_r1
  set $pc = 0x10038
  tbreak *{cold.frame:#x}
  continue
  set $task_pc = $r1
  set $task_psw = $r2
  tbreak *{cold.iret:#x}
  continue
  set $task_r0 = $r0
  set $r0 = $task_psw
  set $pc = 0x11ab2
  si
  set $r0 = $task_r0
  set $pc = $task_pc
end
define tick_stock_idle
  set $tick_lp = $lp
  set $tick_r0 = $r0
  set $tick_r1 = $r1
  set $tick_r2 = $r2
  set $tick_r3 = $r3
  set $tick_r4 = $r4
  set $tick_r5 = $r5
  set $tick_r15 = $r15
  set $woken = 0
  while !$woken && $ticks < 950
    set $pc = {cold.tick:#x}
    set $lp = 0x100f0
    tbreak *0x100f0
    continue
    set $woken = $r0
    set $ticks = $ticks + 1
  end
  set $pc = {cold.idle:#x}
  set $lp = $tick_lp
  set $r0 = $tick_r0
  set $r1 = $tick_r1
  set $r2 = $tick_r2
  set $r3 = $tick_r3
  set $r4 = $tick_r4
  set $r5 = $tick_r5
  set $r15 = $tick_r15
end
set $ticks = 0
set $switches = 0
set $pending = 0
set $catalog = 0
set $acks = 0
set $rows = 0
set $windows = 0
set $lcd = 0
set $ui_bottom = 127
set $ui_bytes = 0
set {{unsigned int}}0x40010000 = {cold.gpio_a:#x}
set {{unsigned int}}0x40010030 = {cold.gpio_b:#x}
set {{unsigned int}}0x40003014 = 0x4000
break *{cold.yield_at:#x}
set $yield_bp = $bpnum
break *{cold.idle:#x}
{run_setup}
tbreak *{cold.run:#x}
break *{cold.tx:#x} if $r1 >= 12 && $r0 != 0
break *{cold.rx:#x} if $lp == {cold.rx_caller:#x} && $pending
tbreak *{cold.version_at:#x}
{catalog_breaks}
break *0x11050 if ($r7 & 3) != 0
break *{cold.panel_window:#x}
break *{cold.spi:#x}
tbreak *{cold.panel_done:#x}
{ui_setup}
continue
while $ticks < 950 && $switches < 9000
  set $event = (unsigned int)$pc
  if $event == {cold.yield_at:#x} || $event == {cold.idle:#x}
    if $event == {cold.idle:#x}
      tick_stock_idle
    end
    switch_stock_task
    set $switches = $switches + 1
    if $pc == {cold.bluetooth:#x}
      set $lp = $pc
      set $r0 = 1000
      set $pc = {cold.delay:#x}
    end
    if $pc == {cold.yield_at:#x}
      disable $yield_bp
      si
      enable $yield_bp
    end
  end
  {run_event}
  if $event == {cold.run:#x}
    {check("run", hex(cold.run))}
  end
  if $event == {cold.tx:#x}
    set $frame = *(unsigned int *)($r0 + 4)
    set $cmd = *(unsigned short *)($frame + 4)
    set $size = *(unsigned short *)($frame + 6)
    set $slot = *(unsigned int *)($frame + 8)
    set $record = -1
    if *(unsigned short *)($frame + 1) != $r1 || $size + 4 != $r1
      printf "unexpected stock UART length for %#x\n", $cmd
      quit 1
    end
    if *(unsigned char *)($frame + 3) == 1
      if $cmd == 0x6000 && $slot == 0
        set $record = 0
      end
      if $cmd == 0x2002 && $slot < {cold.ir_count}
        set $record = $slot + 1
      end
      if $cmd == 0x7002 && $slot < 80
        set $record = $slot + {cold.ir_count + 1}
      end{user_slots}
    end
    if *(unsigned char *)($frame + 3) == 0
""" + "\n".join(
        f"      if $cmd == {opcode:#x} && $size == {length} && $acks == {index}\n"
        f"        dump binary memory {requests[index]} $frame ($frame + $r1)\n"
        f"        set $record = {catalog_count + 1}\n        set $acks = $acks + 1\n      end"
        for index, (opcode, length) in enumerate(cold.writes)
    ) + rf"""
    end
    if $record >= 0
      set $reply = 0x90000000 + $record * 0x100
      set $pending = *(unsigned char *)($reply + 1) | (*(unsigned char *)($reply + 2) << 8){user_ack}
    end
  end
  if $event == {cold.rx:#x}
    set $buffer = *(unsigned int *)($r3 + 0x18)
    set $capacity = *(unsigned short *)($r3 + 4)
    set $write = *(unsigned short *)($r3 + 8)
    set $i = 0
    while $i < $pending
      set {{unsigned char}}($buffer + (($write + $i) % $capacity)) = *(unsigned char *)($reply + $i)
      set $i = $i + 1
    end
    set {{unsigned short}}($r3 + 8) = ($write + $pending) % $capacity
    set $pending = 0
  end
  if $event == {cold.version_at:#x}
    {check("version", f"{cold.version_at:#x} && *(unsigned int *)($r0 + 4) == {cold.version:#x}")}
  end
  if {catalog_events}
    set $catalog = $catalog + 1
  end
  if $event == 0x1104e || $event == 0x1105e
    read_stock_nor
  end
  if $event == 0x11050
    set $i = 0
    while $i < 4
      set {{unsigned char}}($r7 + $i) = $r0 >> ($i * 8)
      set $i = $i + 1
    end
    set $r7 = $r7 + 4
    set $pc = 0x11052
  end
  if $event == {cold.panel_window:#x} && $r0 == 0 && $r1 == 159 && $r2 == $windows && $r3 == $windows + 1
    set $windows = $windows + 1
  end
  if $event == {cold.spi:#x}
    if $r1 == 320
      set $rows = $rows + 1
    end
    if $lcd == 2 && $r1 > 320
      set $ui_bytes = $ui_bytes + $r1
    end
    if $r1 == 1 && (*(unsigned int *)0x40010004 & 0x80) == 0
      if *(unsigned char *)$r0 == 0x11 && $lcd == 0
        set $lcd = 1
      end
      if *(unsigned char *)$r0 == 0x29 && $lcd == 1 && $rows == {cold.rows}
        set $lcd = 2
      end
    end
  end
  if $event == {cold.panel_done:#x}
    {panel_check}
  end
  {ui_event}
  if $event == {stop:#x}
    loop_break
  end
  continue
end
{app_check}"""]


def stock(andes, image, warm_handoff=False, radio=False, cold=False):
    model, app = htfw.read_section(image, 0x62)
    boot = STOCK.get(model)
    if boot is None or sha256(app).hexdigest() != boot.sha256:
        raise ValueError(f"unrecognized {model} stock control section")
    if warm_handoff and model != "GP-50":
        raise ValueError("warm handoff replay requires the GP-50 stock image")
    with TemporaryDirectory() as directory:
        stub = Path(directory)
        (stub / "app.bin").write_bytes(app)
        command(andes, "objcopy", "-I", "binary", "-O", "elf32-nds32le", "-B", "nds32",
                "--rename-section", ".data=.text,alloc,load,readonly,code,contents", stub / "app.bin", stub / "app.o")
        command(andes, "ld", "-Ttext", "0x10000", "-e", "0x10000", stub / "app.o", "-o", stub / "app.elf")
        print(model, "stock control app")
        commands = [
            f"tbreak *{boot.tlb:#x}", "run", check("tlb", hex(boot.tlb)), tlb_fixture(boot.tlb),
            f"tbreak *{boot.efuse:#x}", "continue", check("efuse", hex(boot.efuse)),
            "set {unsigned int} 0x40023010 = 2",
            "set {unsigned int} 0x40021078 = 1",
            f"tbreak *{boot.pll:#x}", "continue", check("pll", hex(boot.pll)),
            "set {unsigned int} 0x40026034 = 1",
            f"tbreak *{boot.audio_pll:#x}", "continue", check("audiopll", hex(boot.audio_pll)),
            "set {unsigned int} 0x40032034 = 1",
            f"tbreak *{boot.measure:#x}", "continue",
            # The 24 MHz reference is divided by 750 for this measurement.
            # Signal completion and supply one count, yielding 32 kHz.
            "set {unsigned int} 0x4002105c = 1",
            "set {unsigned int} 0x40021060 = 1",
            f"tbreak *{boot.calibrated:#x}", "continue",
            check("calibrated", f"{boot.calibrated:#x} && $r0 == 32000"),
            # Capture the first task's IPC/IPSW as the stock scheduler loads
            # them. The simulator lacks IRET and cannot read them over GDB.
            f"tbreak *{boot.frame:#x}", "continue",
            "set $task_pc = $r1", "set $task_psw = $r2",
            f"tbreak *{boot.iret:#x}", "continue", check("scheduler", hex(boot.iret)),
            # Execute the stock mtsr r0,psw once, then resume with the restored
            # R0 and IPC. This models only the initial task dispatch.
            "set $task_r0 = $r0", "set $r0 = $task_psw", "set $pc = 0x11ab2", "si",
            "set $r0 = $task_r0", "set $pc = $task_pc", check("task", hex(boot.task)),
            f"tbreak *{boot.adc:#x}", "continue", check("adc", hex(boot.adc)),
            "set {unsigned int} 0x4002800c = 3",  # ADC calibration complete
            f"tbreak *{boot.board:#x}", "continue",
            check("board", f"{boot.board:#x} && *(unsigned int *)0x40010018 == {boot.gpio_oe:#x}"),
        ]
        expected = ["tlb", "efuse", "pll", "audiopll", "calibrated", "scheduler", "task", "adc", "board"]
        sim = SIM
        if radio or cold:
            _, presets = htfw.read_section(image, 0x65)
            flash = stub / "flash.bin"
            flash_base = (0x166000 if model == "GP-50" else 0x13f000) if cold else 0x190000
            flash.write_bytes(b"\xff" * (0x190000 - flash_base) + presets.ljust(0x70000, b"\xff"))
            sim += " --memory-region 0x80000000,0x10000"
            commands.extend(stock_radio(model, flash, flash_base))
            expected.extend(["settings", "entropy", "radio"])
        if cold:
            from stock_dsp_emu import catalog_replies, replay_uart

            replies = catalog_replies(image)
            wire = stub / "wire.bin"
            ack = b"\x01\x04\x00\x14"
            wire.write_bytes(b"".join(reply.ljust(0x100, b"\0") for reply in replies.values()) + ack)
            requests = [stub / f"uart-{index}.bin" for index in range(len(STOCK_COLD[model].writes))]
            sim += " --memory-region 0x90000000,0x10000"
            commands.extend(stock_cold(model, wire, requests))
            expected.extend(["run", "version", "panel"])
            if model == "GP-50":
                expected.append("app")
        if warm_handoff:
            commands.extend(gp50_warm_handoff())
            expected.extend(["ack", "delayone", "delaytwo", "handoff"])
        checkpoints(andes, stub / "app.elf", commands, expected, sim)
        if cold:
            if replay_uart(image, [path.read_bytes() for path in requests]) != [ack] * len(requests):
                raise ValueError("the stock DSP did not acknowledge the captured startup commands")
            print(f"Stock DSP accepted {len(requests)} captured startup writes")


def open_elf_tlb(andes, elf, gp50=False):
    disassembly = command(andes, "objdump", "-d", elf)
    cache = disassembly.partition("<gp5_cache_on>:\n")[2].partition("\n\n")[0]
    match = re.search(r"^\s*([a-f0-9]+):[^\n]*\btlbop\b[^\n]*targetwrite", cache, re.M)
    if not match or "ex9.it" in disassembly:
        raise ValueError("expected an OpenGP5 app.elf linked without EX9 relaxation")
    if gp50 and "<gp50_controls_init>:" not in disassembly:
        raise ValueError("expected the GP-50 emulator app.elf")
    return int(match[1], 16)


def open_app(andes, elf):
    tlb = open_elf_tlb(andes, elf)
    print("OpenGP5 control app, emulator build")
    checkpoints(andes, elf, [
        f"tbreak *{tlb:#x}", "run", check("tlb", hex(tlb)), tlb_fixture(tlb),
        "tbreak *main", "continue", check("main", "(unsigned int) &main"),
    ], ["tlb", "main"])


def gp50_app(andes, elf):
    tlb = open_elf_tlb(andes, elf, gp50=True)
    # The simulator needs oscillator/eFuse/PLL readiness from the board.
    # SPI is intercepted so the display sees transfers without a modeled FIFO.
    script = rf"""set pagination off
{SIM}
load
tbreak *{tlb:#x}
run
{tlb_fixture(tlb)}
set {{unsigned int}} 0x40023010 = 2
set {{unsigned int}} 0x40021078 = 1
set {{unsigned int}} 0x40026034 = 1
set {{unsigned int}} 0x40032034 = 1
set {{unsigned int}} 0x40010000 = 0x8000
set {{unsigned int}} 0x40010030 = 0x10
break *delay_ms
commands
  silent
  printf "DELAY %u\n", $r0
  set $pc = $lp
  continue
end
break *gp5_spim_send
commands
  silent
  printf "SPI %u %u", (*(unsigned int *)0x40010004 >> 7) & 1, $r1
  if $r1 <= 16
    set $i = 0
    while $i < $r1
      printf " %02x", *(unsigned char *)($r0 + $i)
      set $i = $i + 1
    end
  end
  printf "\n"
  set $pc = $lp
  continue
end
break *usb_midi_init
continue
set $a_outputs = *(unsigned int *)0x40010018 & 0x11030080
set $b_outputs = *(unsigned int *)0x40010048 & 0x60
set $backlight = (*(unsigned int *)0x400100f4 & 0xf) == 0xa && *(unsigned int *)0x4002f024 == 3999
printf "BOARD %d\n", $pc == (unsigned int)&usb_midi_init && gp5_stage == 8 && $a_outputs == 0x11030080 && $b_outputs == 0x60 && $backlight && gp50_control_input == 0xffff
"""
    with TemporaryDirectory() as directory:
        path = Path(directory) / "startup.gdb"
        path.write_text(script)
        trace = command(andes, "gdb", "-nx", "-batch", elf, "-x", path).splitlines()

    # Command/data-select, byte count and data, with each pixel row sent as a burst.
    spi = [line[4:] for line in trace if line.startswith("SPI ")]
    init = (
        "0 1 11", "0 1 b1", "1 3 05 3a 3a", "0 1 b2", "1 3 05 3a 3a",
        "0 1 b3", "1 6 05 3a 3a 05 3a 3a", "0 1 b4", "1 1 03",
        "0 1 c0", "1 3 62 02 04", "0 1 c1", "1 1 c0", "0 1 c2", "1 2 0d 00",
        "0 1 c3", "1 2 8d 6a", "0 1 c4", "1 2 8d ee", "0 1 c5", "1 1 12",
        "0 1 e0", "1 16 03 1b 12 11 3f 3a 32 34 2f 2b 30 3a 00 01 02 05",
        "0 1 e1", "1 16 03 1b 12 11 32 2f 2a 2f 2e 2c 35 3f 00 00 01 05",
        "0 1 36", "1 1 68", "0 1 3a", "1 1 55", "0 1 2c",
    )
    column = ["0 1 2a", "1 4 00 00 00 9f", "0 1 2b"]
    expected = [*init, *column, "1 4 00 00 00 7f", "0 1 2c", *(["1 320"] * 128),
                "0 1 29", *column, "1 4 00 18 00 67", "0 1 2c", *(["1 320"] * 80)]
    if spi != expected:
        raise ValueError("unexpected panel initialization or pixel window")
    delays = [int(line.split()[1]) for line in trace if line.startswith("DELAY ")]
    if "BOARD 1" not in trace or delays[:6] != [1, 1, 130, 120, 10, 100]:
        raise ValueError("unexpected board registers or panel reset timings")
    print("GP-50 reached USB initialization with the 160×128 panel, startup logo and backlight")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--stock", type=Path, help="official Valeton update ZIP or BIN")
    source.add_argument("--open-elf", type=Path, help="OpenGP5 app.elf linked without EX9 relaxation")
    source.add_argument("--gp50-app", type=Path, help="GP-50 app.elf from the emulator build")
    parser.add_argument("--andes-bin", type=Path, default=ANDES)
    stock_mode = parser.add_mutually_exclusive_group()
    stock_mode.add_argument("--gp50-warm-handoff", action="store_true",
                            help="replay the stock GP-50 update-mode request up to the factory-loader jump")
    stock_mode.add_argument("--stock-radio", action="store_true",
                            help="continue stock startup through modeled settings flash and Bluetooth radio setup")
    stock_mode.add_argument("--stock-cold", action="store_true",
                            help="run stock startup through the LCD using stock DSP UART replies")
    args = parser.parse_args()
    if args.gp50_warm_handoff and not args.stock:
        parser.error("--gp50-warm-handoff requires --stock")
    if (args.stock_radio or args.stock_cold) and not args.stock:
        parser.error("stock startup replay requires --stock")
    try:
        if args.stock:
            stock(args.andes_bin, args.stock, args.gp50_warm_handoff, args.stock_radio, args.stock_cold)
        elif args.gp50_app:
            gp50_app(args.andes_bin, args.gp50_app)
        else:
            open_app(args.andes_bin, args.open_elf)
    except (OSError, ValueError, ImportError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        detail = getattr(exc, "stdout", "") or ""
        parser.exit(1, f"error: {exc}\n{detail}")


if __name__ == "__main__":
    main()
