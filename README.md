# OpenGP5 Emulator

Run OpenGP5 effects on your computer, or exercise selected startup and
peripheral behavior of the stock Valeton GP-5 and GP-50 firmware.

```sh
git clone --recurse-submodules https://github.com/jopdorp/OpenGP5-Emulator.git
cd OpenGP5-Emulator
```

## Play an OpenGP5 effect

The live GP-50 window runs on Linux with Python 3, a C compiler, SDL2 2.0.18
or later, JACK, and libsamplerate. On Debian or Ubuntu, install
`build-essential python3 pkg-config libsdl2-dev libjack-jackd2-dev libsamplerate0-dev`
and run the command below under PipeWire with `pw-jack`. With a JACK server,
run the Python command directly.

```sh
pw-jack python3 tools/simulate.py tremolo --gp50 --live
```

Connect `OpenGP5 GP-50:out_l` and `out_r` to your speakers or recorder in a
patchbay. Connect a guitar source to `OpenGP5 GP-50:in` or add
`--demo guitar.wav` to the command and press **Space** or click **Play Sample**.
Scroll or drag to turn the knobs. Replace `tremolo` with the path to any
compatible OpenGP5 firmware source directory. Add `--store patches.store`
to reopen saved patches in the next live or scripted session. In firmware with a
status display, F6/F7 change the simulated battery, F8 toggles charging, and
F9 cycles Bluetooth off, unpaired, and paired.

The scripted simulator runs without JACK or SDL2. The default input is a
plucked tone. It saves a stereo WAV and screenshots under `build/sim/`.

```sh
python3 tools/simulate.py tremolo --gp50
python3 tools/test_simulate.py
```

See the [simulator manual](docs/platform/simulator.md) for the control script,
file storage, and audio options.

## Run the stock firmware checks

Supply an official Valeton GP-5 **V1.0.6** or GP-50 **V1.0.5** update ZIP or
BIN. The control emulator uses the Andes NDS32 GDB simulator, objcopy and
linker. Run `opengp5/release/setup-toolchain.sh` to install the pinned tools
on a supported host, or pass `--andes-bin /path/to/bin` to use an existing
installation. On amd64 Linux the Andes binaries need a 32-bit C library.
Install Unicorn 2.1 or later in your Python environment for DSP and cold
startup checks.

```sh
python3 tools/stock_control_emu.py --stock /path/to/valeton-update.zip --stock-cold
python3 tools/stock_dsp_emu.py /path/to/valeton-update.zip --runtime
```

The stock harnesses check modeled startup, LCD traffic, UART, and audio DMA.
They run separately from the live desktop window, which runs OpenGP5
firmware source. See the [GP-50 bring-up notes](https://github.com/jopdorp/OpenGP5/blob/3ca158ba4bb66d44c797d6ba1701768d6b7bf7f6/docs/platform/gp50-port.md)
for the emulated peripherals, alternate check modes, and hardware coverage.

The simulator and firmware check harnesses were extracted from
[OpenGP5 at a9c3a71](https://github.com/jopdorp/OpenGP5/tree/a9c3a713d5136b166b1ad5e2d862a7b3b6ec3760).
The pinned `opengp5/` submodule provides the platform runtime, protocol
parsers, and example firmware. Vendor firmware and demo audio are supplied
locally. See [LICENSE](LICENSE) and the font licenses in OpenGP5.
