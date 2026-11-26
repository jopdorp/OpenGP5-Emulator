/* JACK bridge for the live simulator. The audio thread owns the firmware. */
#include "live.h"
#include "gp5_firmware.h"

#include <jack/jack.h>
#include <samplerate.h>

#include <limits.h>
#include <math.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define FRAMES GP5_BLOCK
#define OUTPUT_FRAMES 4096u
#define CONTROL_EVENTS 256u
#define SCREEN_PIXELS (GP5_SCREEN_W * GP5_SCREEN_H)
#define SCREEN_READY 4u
#define Q31_SCALE 2147483648.0f

static struct {
    jack_port_t *in, *left, *right;
    SRC_STATE *input_src, *output_src;
    unsigned firmware_rate, graph_rate;
    atomic_uint requested_rate;
    unsigned screen_frames;
    double input_ratio, output_ratio;
    int32_t input[FRAMES];
    unsigned input_used;
    float output[OUTPUT_FRAMES][2];
    unsigned output_read, output_write;
} audio;

static atomic_bool running, audio_error;
static struct {
    int32_t *samples;
    size_t count, position;
    atomic_bool playing;
} demo;
static struct {
    enum sim_control control;
    int value;
} events[CONTROL_EVENTS];
static atomic_uint event_read, event_write;

/* Each side owns one screen. The exchange transfers ownership of the third. */
static struct {
    uint32_t pixels[SCREEN_PIXELS], led;
} screens[3];
static unsigned screen_write = 2, screen_read;
static atomic_uint screen_pending = 1;

bool sim_live_running(void)
{
    return atomic_load(&running);
}

bool sim_live_demo_available(void)
{
    return demo.count != 0;
}

bool sim_live_demo_playing(void)
{
    return atomic_load_explicit(&demo.playing, memory_order_relaxed);
}

void sim_live_control(enum sim_control control, int value)
{
    unsigned write = atomic_load_explicit(&event_write, memory_order_relaxed);
    unsigned read = atomic_load_explicit(&event_read, memory_order_acquire);
    if (write - read == CONTROL_EVENTS) return;
    events[write % CONTROL_EVENTS].control = control;
    events[write % CONTROL_EVENTS].value = value;
    atomic_store_explicit(&event_write, write + 1, memory_order_release);
}

void sim_live_screen(uint32_t *pixels, uint32_t *led)
{
    if (atomic_load_explicit(&screen_pending, memory_order_acquire) & SCREEN_READY)
        screen_read = atomic_exchange_explicit(&screen_pending, screen_read, memory_order_acq_rel) & ~SCREEN_READY;
    memcpy(pixels, screens[screen_read].pixels, sizeof screens[screen_read].pixels);
    *led = screens[screen_read].led;
}

static void publish_screen(void)
{
    sim_screen(screens[screen_write].pixels, &screens[screen_write].led);
    screen_write = atomic_exchange_explicit(&screen_pending, screen_write | SCREEN_READY, memory_order_acq_rel) & ~SCREEN_READY;
}

static void apply_controls(void)
{
    unsigned read = atomic_load_explicit(&event_read, memory_order_relaxed);
    unsigned write = atomic_load_explicit(&event_write, memory_order_acquire);
    while (read != write) {
        unsigned slot = read++ % CONTROL_EVENTS;
        if (events[slot].control == SIM_DEMO) {
            bool play = events[slot].value != 0;
            if (!sim_live_demo_available() || play == sim_live_demo_playing()) continue;
            demo.position = 0;
            atomic_store_explicit(&demo.playing, play, memory_order_relaxed);
        } else {
            sim_control(events[slot].control, events[slot].value);
        }
    }
    atomic_store_explicit(&event_read, read, memory_order_release);
}

static bool queue_output(const float *samples, unsigned frames)
{
    if (frames > OUTPUT_FRAMES - (audio.output_write - audio.output_read)) return false;
    for (unsigned i = 0; i < frames; i++) {
        float *next = audio.output[audio.output_write++ % OUTPUT_FRAMES];
        next[0] = samples[2 * i];
        next[1] = samples[2 * i + 1];
    }
    return true;
}

static bool resample(SRC_STATE *src, double ratio, const float *input, unsigned frames,
                     unsigned channels, bool (*consume)(const float *, unsigned))
{
    float converted[2 * FRAMES];
    SRC_DATA data = {
        .data_in = input, .input_frames = frames,
        .data_out = converted, .output_frames = FRAMES, .src_ratio = ratio,
    };
    for (;;) {
        if (src_process(src, &data) || !consume(converted, (unsigned)data.output_frames_gen)) return false;
        data.data_in += data.input_frames_used * channels;
        data.input_frames -= data.input_frames_used;
        if (!data.input_frames && data.output_frames_gen < data.output_frames) return true;
        if (!data.input_frames_used && !data.output_frames_gen) return false;
    }
}

static int32_t q31(float sample)
{
    if (isnan(sample)) return 0;
    if (sample >= 1.0f) return INT32_MAX;
    if (sample <= -1.0f) return INT32_MIN;
    return (int32_t)(sample * Q31_SCALE);
}

static bool load_demo(const char *path, double ratio)
{
    if (!isfinite(ratio) || !src_is_valid_ratio(ratio)) return false;
    FILE *file = fopen(path, "rb");
    if (!file) return false;
    long bytes = fseek(file, 0, SEEK_END) == 0 ? ftell(file) : -1;
    if (bytes > 0 && bytes % sizeof *demo.samples == 0 && fseek(file, 0, SEEK_SET) == 0) {
        demo.samples = malloc(bytes);
        if (demo.samples && fread(demo.samples, 1, bytes, file) == (size_t)bytes)
            demo.count = bytes / sizeof *demo.samples;
    }
    fclose(file);
    if (!demo.count || ratio == 1.0) return demo.count != 0;

    double frames = ceil(demo.count * ratio);
    size_t limit = SIZE_MAX / sizeof(float);
    if (limit > INT_MAX) limit = INT_MAX;
    if (frames > limit || demo.count > limit) return false;
    float *input = malloc(demo.count * sizeof *input);
    float *output = malloc((size_t)frames * sizeof *output);
    SRC_DATA data = {
        .data_in = input, .input_frames = demo.count,
        .data_out = output, .output_frames = (long)frames,
        .src_ratio = ratio, .end_of_input = 1,
    };
    bool ok = false;
    if (input && output) {
        src_int_to_float_array(demo.samples, input, (int)demo.count);
        if (!src_simple(&data, SRC_SINC_FASTEST, 1) && data.output_frames_gen > 0) {
            int32_t *converted = realloc(demo.samples, (size_t)data.output_frames_gen * sizeof *converted);
            if (converted) {
                demo.samples = converted;
                demo.count = data.output_frames_gen;
                for (size_t i = 0; i < demo.count; i++) demo.samples[i] = q31(output[i]);
                ok = true;
            }
        }
    }
    free(output);
    free(input);
    return ok;
}

static bool feed_firmware(const float *samples, unsigned frames)
{
    bool playing = sim_live_demo_playing();
    for (unsigned i = 0; i < frames; i++) {
        if (playing) {
            audio.input[audio.input_used++] = demo.samples[demo.position++];
            if (demo.position == demo.count) {
                playing = false;
                atomic_store_explicit(&demo.playing, false, memory_order_relaxed);
            }
        } else {
            audio.input[audio.input_used++] = q31(samples[i]);
        }
        if (audio.input_used != FRAMES) continue;
        int32_t words[2 * FRAMES];
        float output[2 * FRAMES];
        sim_block(audio.input, words);
        audio.input_used = 0;
        for (unsigned j = 0; j < 2 * FRAMES; j++)
            output[j] = (float)words[j] / Q31_SCALE;
        bool ok = audio.output_ratio == 1.0
            ? queue_output(output, FRAMES)
            : resample(audio.output_src, audio.output_ratio, output, FRAMES, 2, queue_output);
        if (!ok) return false;
    }
    return true;
}

static bool configure_rate(unsigned rate)
{
    if (!rate) return false;
    double ratio = (double)rate / audio.firmware_rate;
    if (!src_is_valid_ratio(ratio) || !src_is_valid_ratio(1.0 / ratio)) return false;
    /* Prefill covers block assembly and filter lookahead across quantum changes. */
    unsigned delay = FRAMES;
    if (rate != audio.firmware_rate) delay += (unsigned)(3 * FRAMES * ratio + 1);
    if (delay + FRAMES >= OUTPUT_FRAMES) return false;
    if (src_reset(audio.input_src) || src_reset(audio.output_src) ||
        src_set_ratio(audio.input_src, 1.0 / ratio) || src_set_ratio(audio.output_src, ratio))
        return false;
    audio.graph_rate = rate;
    audio.input_ratio = 1.0 / ratio;
    audio.output_ratio = ratio;
    audio.input_used = 0;
    audio.output_read = 0;
    audio.output_write = delay;
    memset(audio.output, 0, delay * sizeof audio.output[0]);
    return true;
}

static int process(jack_nframes_t frames, void *unused)
{
    (void)unused;
    const float *input = jack_port_get_buffer(audio.in, frames);
    float *left = jack_port_get_buffer(audio.left, frames);
    float *right = jack_port_get_buffer(audio.right, frames);
    if (!sim_live_running()) goto silence;
    unsigned rate = atomic_load_explicit(&audio.requested_rate, memory_order_acquire);
    if (rate != audio.graph_rate && !configure_rate(rate)) goto error;

    apply_controls();
    for (jack_nframes_t at = 0; at < frames;) {
        unsigned chunk = frames - at > FRAMES ? FRAMES : frames - at;
        bool ok = audio.input_ratio == 1.0
            ? feed_firmware(input + at, chunk)
            : resample(audio.input_src, audio.input_ratio, input + at, chunk, 1, feed_firmware);
        if (!ok || audio.output_write - audio.output_read < chunk) goto error;
        for (unsigned i = 0; i < chunk; i++, at++) {
            const float *next = audio.output[audio.output_read++ % OUTPUT_FRAMES];
            left[at] = next[0];
            right[at] = next[1];
        }
    }
    audio.screen_frames += frames;
    if (audio.screen_frames >= rate / 30) {
        audio.screen_frames = 0;
        publish_screen();
    }
    return 0;

error:
    atomic_store(&audio_error, true);
    atomic_store(&running, false);
silence:
    memset(left, 0, frames * sizeof *left);
    memset(right, 0, frames * sizeof *right);
    return 0;
}

static int rate_changed(jack_nframes_t rate, void *unused)
{
    (void)unused;
    atomic_store_explicit(&audio.requested_rate, rate, memory_order_release);
    return 0;
}

static void shutdown(void *unused)
{
    (void)unused;
    atomic_store(&running, false);
}

int main(int argc, char **argv)
{
    int next = 3;
    const char *store = NULL;
    if (argc > next && !strcmp(argv[next], "--store") && argc > next + 1) {
        store = argv[next + 1];
        next += 2;
    }
    if (argc != next && argc != next + 2) {
        fprintf(stderr, "usage: %s dsp.so title [--store FILE] [demo.raw demo_rate]\n", argv[0]);
        return 2;
    }
    if (sim_store_open(store)) return 1;
    if (sim_open(argv[1])) return 1;
    audio.firmware_rate = sim_rate();
    if (argc == next + 2 && !load_demo(argv[next], audio.firmware_rate / strtod(argv[next + 1], NULL))) {
        fprintf(stderr, "Could not load the demo sample\n");
        free(demo.samples);
        return 1;
    }
    jack_status_t status;
    jack_client_t *client = jack_client_open("OpenGP5 GP-50", JackNoStartServer | JackUseExactName, &status);
    if (!client) {
        fprintf(stderr, "Could not open the JACK client (status 0x%x)\n", (unsigned)status);
        free(demo.samples);
        return 1;
    }

    int result = 1;
    int src_error_code;
    audio.input_src = src_new(SRC_SINC_FASTEST, 1, &src_error_code);
    if (audio.input_src) audio.output_src = src_new(SRC_SINC_FASTEST, 2, &src_error_code);
    if (!audio.input_src || !audio.output_src) {
        fprintf(stderr, "Could not initialize the audio resampler: %s\n", src_strerror(src_error_code));
        goto close;
    }

    unsigned rate = jack_get_sample_rate(client);
    atomic_store(&audio.requested_rate, rate);
    if (!configure_rate(rate)) {
        fprintf(stderr, "Could not convert JACK rate %u to firmware rate %u\n", rate, audio.firmware_rate);
        goto close;
    }
    audio.in = jack_port_register(client, "in", JACK_DEFAULT_AUDIO_TYPE, JackPortIsInput, 0);
    audio.left = jack_port_register(client, "out_l", JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, 0);
    audio.right = jack_port_register(client, "out_r", JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, 0);
    if (!audio.in || !audio.left || !audio.right || jack_set_process_callback(client, process, NULL) ||
        jack_set_sample_rate_callback(client, rate_changed, NULL)) {
        fprintf(stderr, "Could not register JACK ports and callbacks\n");
        goto close;
    }
    jack_on_shutdown(client, shutdown, NULL);
    publish_screen();
    atomic_store(&running, true);
    if (jack_activate(client)) {
        fprintf(stderr, "Could not activate the JACK client\n");
        goto close;
    }

    fprintf(stderr, "%s at %u Hz (firmware %u Hz)\n", jack_get_client_name(client), rate, audio.firmware_rate);
    result = sim_live_ui(argv[2], audio.firmware_rate);
    if (atomic_load(&audio_error)) {
        fprintf(stderr, "The JACK audio stream stopped processing\n");
        result = 1;
    }

close:
    atomic_store(&running, false);
    jack_client_close(client);
    apply_controls();
    if (sim_store_close()) result = 1;
    src_delete(audio.output_src);
    src_delete(audio.input_src);
    free(demo.samples);
    return result;
}
