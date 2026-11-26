/* OpenGP5 simulator: a whole firmware on the computer (tools/simulate.py builds and runs this; docs/platform/simulator.md).
 *
 * The control half (the firmware's nds32 C files with the platform's parameters, UI kit and text) is this program; the DSP half is a
 * library (sim_dsp.c) loaded beside it. The simulation runs block by block, as the pedal does:
 *   every 10 ms   fw_tick(), and the PARA hold timer (0.6 s)
 *   every block   the control half's main loop (parameters to the DSP, fw_draw), the DSP's fw_process(), the
 *                 output level, and the audio link: the DSP's words go to the control half's on_block(), whose
 *                 words reach the DSP LINK_DELAY blocks later, as on the pedal
 *   events        controls, knob, parameters, presets and screenshots from a script, at their times
 *
 *   sim <dsp library> <events> <in.raw> <out.raw> <shot dir> <seconds> <store file or -> <files dir or ->
 * in.raw: int32 mono; out.raw: int32 left, right. Events, one per line, times in ms (simulate.py writes them):
 *   <ms> foot_down | foot_up | para_down | para_up | turn <n> | knob <0..255> | set <id> <value> |
 *        preset_load <n> | preset_save <n> | shot <name> | storage_changed
 * Prints what happens (LEDs, log lines, the DSP's load) to stderr. */
#define _DEFAULT_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <dlfcn.h>
#include <dirent.h>
#include <errno.h>
#include <unistd.h>
#ifdef SIM_LIVE
#include <stdatomic.h>
#endif
#include "gp5_firmware.h"
#include "gp5_fmt.h"
#include "gp5_font.h"
#include "live.h"
#ifdef GP5_HAS_PARAMS
#include "gp5_params.h"
void gp5_params_init(void);
void gp5_params_poll(void);
int gp5_params_take(uint32_t *id, uint32_t *value);
int gp5_params_set_outside(unsigned id, int32_t value);
int gp5_params_store_changed(uint32_t id);
#endif

#define LINK_DELAY 3u                                /* blocks from the control half's words to the DSP's fw_process */
#define BLOCK 64u

static uint32_t now_ms;
uint32_t gp5_ms(void) { return now_ms; }

/* ---- the firmware's callbacks: empty defaults, as the platform's ---- */
__attribute__((weak)) void fw_init(void) {}
__attribute__((weak)) int fw_draw(int force) { (void)force; return 0; }
__attribute__((weak)) void fw_tick(void) {}
__attribute__((weak)) void fw_foot_down(void) {}
__attribute__((weak)) void fw_foot_up(void) {}
__attribute__((weak)) void fw_para_down(void) {}
__attribute__((weak)) void fw_para_up(void) {}
__attribute__((weak)) void fw_para_click(void) {}
__attribute__((weak)) void fw_para_hold(void) {}
__attribute__((weak)) void fw_para_turn(int detents) { (void)detents; }
#ifdef GP50_BOARD
__attribute__((weak)) void fw_gp50_encoder_turn(unsigned encoder, int detents) { (void)encoder; (void)detents; }
__attribute__((weak)) void fw_gp50_button(unsigned button, int down) { (void)button; (void)down; }
__attribute__((weak)) void fw_gp50_expression(unsigned value) { (void)value; }
#endif
__attribute__((weak)) void fw_storage_changed(void) {}
__attribute__((weak)) void fw_dsp_ready(void) {}
__attribute__((weak)) int fw_take(uint32_t *id, uint32_t *value) { (void)id; (void)value; return 0; }
__attribute__((weak)) unsigned fw_setting_count(void) { return 0; }
__attribute__((weak)) int fw_param_get(uint32_t id, uint32_t *value) { (void)id; (void)value; return 0; }
__attribute__((weak)) int fw_param_set(uint32_t id, uint32_t value) { (void)id; (void)value; return 0; }
#ifdef GP5_FW_ID
__attribute__((weak)) const char *fw_name(void) { return GP5_FW_ID; }
#else
__attribute__((weak)) const char *fw_name(void) { return "opengp5"; }
#endif

/* ---- controls, level, LEDs ---- */
static unsigned knob = 255, level, level_owned;
static uint32_t led = GP5_LED_ON;
unsigned gp5_knob(void) { return knob; }
void gp5_level_own(int own) { level_owned = own != 0; }
void gp5_level_set(unsigned l) { level = l > 255u ? 255u : l; }
void gp5_led(uint32_t c) { led = c; }

#ifdef GP50_BOARD
static gp50_status_t status = { 3, 0, GP50_BT_UNPAIRED };
gp50_status_t gp50_status(void) { return status; }
#endif

/* ---- log ---- */
void gp5_log(const char *fmt, ...)
{
#ifdef SIM_LIVE
    (void)fmt;
#else
    char line[112];
    va_list ap;
    va_start(ap, fmt); gp5_vfmt(line, sizeof line, fmt, ap); va_end(ap);
    fprintf(stderr, "[%5u.%03u] %s\n", now_ms / 1000u, now_ms % 1000u, line);
#endif
}

/* ---- screen ---- */
static uint16_t fb[GP5_SCREEN_W * GP5_SCREEN_H];
static unsigned long blit_bytes, blit_most, blit_first;   /* bytes sent to the screen in this fw_draw call; the most in one
                                                                 (and in the first, forced one: a full draw, expected) */
void gp5_blit(unsigned x, unsigned y, unsigned w, unsigned h, const uint16_t *px)
{
    blit_bytes += 2ul * w * h;
    for (unsigned j = 0; j < h; j++)
        for (unsigned i = 0; i < w; i++)
            if (x + i < GP5_SCREEN_W && y + j < GP5_SCREEN_H) fb[(y + j) * GP5_SCREEN_W + x + i] = px[j * w + i];
}
#include "font5x7.h"
void gp5_text(unsigned line, const char *s)          /* the built-in 5x7 font, 2x, as main.c draws it: only the */
{                                                    /* characters that changed are sent again */
    static uint16_t cell[12 * 16];
    static char shown[5][13];
    uint16_t fg = gp5_color(0xFFFFFF), bg = gp5_color(0);
    int end = 0;
    if (line >= 5) return;
    for (unsigned c = 0; c < 13; c++) {
        char ch = ' ';
        if (!end) { if (s[c]) ch = s[c]; else end = 1; }
        if (ch < 0x20 || ch > GP5_FONT5X7_LAST) ch = '?';
        if (shown[line][c] == ch) continue;
        shown[line][c] = ch;
        for (unsigned y = 0; y < 16; y++)
            for (unsigned x = 0; x < 12; x++) {
                unsigned fx = x / 2, fy = y / 2;
                cell[y * 12 + x] = (fx < 5 && fy < 8 && ((font[ch - 0x20][fx] >> fy) & 1)) ? fg : bg;
            }
        gp5_blit(2 + c * 12, line * 16, 12, 16, cell);
    }
}
static uint32_t screen_pixel(uint16_t pixel)
{
    /* UI_PANEL stores byte-swapped BGR565. */
    unsigned v = (uint16_t)((pixel >> 8) | (pixel << 8));
    unsigned b = v >> 11, g = (v >> 5) & 63, r = v & 31;
    return ((r << 3 | r >> 2) << 16) | ((g << 2 | g >> 4) << 8) | (b << 3 | b >> 2);
}

void sim_screen(uint32_t *pixels, uint32_t *color)
{
    for (unsigned i = 0; i < GP5_SCREEN_W * GP5_SCREEN_H; i++) pixels[i] = screen_pixel(fb[i]);
    *color = led;
}

static void shot(const char *dir, const char *name)
{
    char p[1024];
    FILE *f;
    snprintf(p, sizeof p, "%s/%s.ppm", dir, name);
    if (!(f = fopen(p, "wb"))) { fprintf(stderr, "can't write %s\n", p); return; }
    fprintf(f, "P6 %d %d 255\n", GP5_SCREEN_W, GP5_SCREEN_H);
    for (unsigned i = 0; i < GP5_SCREEN_W * GP5_SCREEN_H; i++) {
        uint32_t pixel = screen_pixel(fb[i]);
        fputc(pixel >> 16, f); fputc((pixel >> 8) & 255, f); fputc(pixel & 255, f);
    }
    fclose(f);
}

/* ---- storage: records in memory (and a file between runs), files from a folder ---- */
#define NREC 128
struct store_record { uint32_t id; unsigned len; uint8_t d[GP5_STORE_MAX]; };
static struct store_record recs[NREC];
static unsigned nrec;
static const char *store_path;
static int store_dirty;
#ifdef SIM_LIVE
static struct {
    struct store_record records[NREC];
    unsigned count;
    atomic_bool full;
} store_snapshot;
static int store_retry;
#endif
unsigned gp5_store_load(uint32_t id, void *dst, unsigned max)
{
    for (unsigned i = 0; i < nrec; i++)
        if (recs[i].id == id) { memcpy(dst, recs[i].d, recs[i].len < max ? recs[i].len : max); return recs[i].len; }
    return 0;
}
int gp5_store_save(uint32_t id, const void *src, unsigned len)
{
    unsigned i;
    if (len > GP5_STORE_MAX) return -1;
    for (i = 0; i < nrec && recs[i].id != id; i++) {}
    if (i == NREC) return -1;
    if (i == nrec) nrec++;
    recs[i].id = id; recs[i].len = len; memcpy(recs[i].d, src, len);
    store_dirty = store_path != NULL;
    return 0;
}

static int write_store(const struct store_record *records, unsigned count)
{
    FILE *existing = fopen(store_path, "r+b");
    if (existing) fclose(existing);
    else if (errno != ENOENT) {
        fprintf(stderr, "Could not write emulator patches to %s\n", store_path);
        return -1;
    }
    char *temporary = malloc(strlen(store_path) + sizeof ".tmp.XXXXXX");
    if (!temporary) return -1;
    sprintf(temporary, "%s.tmp.XXXXXX", store_path);
    int fd = mkstemp(temporary);
    FILE *file = fd < 0 ? NULL : fdopen(fd, "wb");
    int failed = !file;
    if (file) {
        failed = fwrite(&count, sizeof count, 1, file) != 1 ||
                 fwrite(records, sizeof records[0], count, file) != count ||
                 fflush(file) != 0 || fsync(fd) != 0;
        if (fclose(file) != 0) failed = 1;
    } else if (fd >= 0) close(fd);
    if (!failed) failed = rename(temporary, store_path) != 0;
    if (failed) {
        if (fd >= 0) unlink(temporary);
        fprintf(stderr, "Could not save emulator patches to %s\n", store_path);
    }
    free(temporary);
    return failed ? -1 : 0;
}

int sim_store_open(const char *path)
{
    if (!path || !strcmp(path, "-")) return 0;
    store_path = path;
    FILE *file = fopen(path, "rb");
    if (!file) {
        if (errno == ENOENT) return 0;
    } else {
        int valid = fread(&nrec, sizeof nrec, 1, file) == 1 && nrec <= NREC &&
                    fread(recs, sizeof recs[0], nrec, file) == nrec;
        for (unsigned i = 0; valid && i < nrec; ++i) valid = recs[i].len <= GP5_STORE_MAX;
        fclose(file);
        if (valid) return 0;
    }
    fprintf(stderr, "Could not read emulator patches from %s\n", path);
    return -1;
}

#ifdef SIM_LIVE
static void publish_store(void)
{
    if (!store_dirty || atomic_load_explicit(&store_snapshot.full, memory_order_acquire)) return;
    memcpy(store_snapshot.records, recs, nrec * sizeof recs[0]);
    store_snapshot.count = nrec;
    store_dirty = 0;
    atomic_store_explicit(&store_snapshot.full, 1, memory_order_release);
}

void sim_store_flush(void)
{
    if (!atomic_load_explicit(&store_snapshot.full, memory_order_acquire)) return;
    store_retry = write_store(store_snapshot.records, store_snapshot.count) != 0;
    atomic_store_explicit(&store_snapshot.full, 0, memory_order_release);
}

int sim_store_close(void)
{
    if (!store_retry && !store_dirty && !atomic_load_explicit(&store_snapshot.full, memory_order_relaxed)) return 0;
    return write_store(recs, nrec);
}
#endif
static const char *files_dir;
#define NFILE 64
static struct { char name[32]; uint8_t *d; uint32_t len; } files[NFILE];
static unsigned nfile;
static void load_files(void)
{
    DIR *d;
    struct dirent *e;
    if (!files_dir || !(d = opendir(files_dir))) return;
    while ((e = readdir(d)) && nfile < NFILE) {
        char p[1024]; FILE *f; long n;
        if (e->d_name[0] == '.' || strlen(e->d_name) > 31) continue;
        snprintf(p, sizeof p, "%s/%s", files_dir, e->d_name);
        if (!(f = fopen(p, "rb"))) continue;
        fseek(f, 0, SEEK_END); n = ftell(f); fseek(f, 0, SEEK_SET);
        files[nfile].d = malloc(n > 0 ? (size_t)n : 1u);
        if (n >= 0 && fread(files[nfile].d, 1, (size_t)n, f) == (size_t)n) {
            strcpy(files[nfile].name, e->d_name); files[nfile].len = (uint32_t)n; nfile++;
        }
        fclose(f);
    }
    closedir(d);
}
const uint8_t *gp5_file(uint32_t firm, const char *name, uint32_t *len)
{
    (void)firm;
    for (unsigned i = 0; i < nfile; i++) if (!strcmp(files[i].name, name)) { if (len) *len = files[i].len; return files[i].d; }
    return 0;
}
int gp5_file_check(uint32_t firm, const char *name) { return gp5_file(firm, name, 0) != 0; }
unsigned gp5_file_each(uint32_t firm, void (*fn)(const char *, const uint8_t *, uint32_t, void *), void *ctx)
{
    (void)firm;
    for (unsigned i = 0; i < nfile; i++) fn(files[i].name, files[i].d, files[i].len, ctx);
    return nfile;
}

/* ---- the audio link ---- */
static void (*link_cb)(const int32_t *in, int32_t *out);
volatile uint32_t gp5_link_blocks;
void gp5_link_start(void (*on_block)(const int32_t *in, int32_t *out)) { link_cb = on_block; }

/* ---- the DSP half ---- */
static void (*dsp_init)(const uint32_t *);
static int (*dsp_command)(const uint8_t *, unsigned);
static void (*dsp_block)(const int32_t *, int32_t *, int32_t *);
static int32_t *(*dsp_link)(int);
static unsigned (*dsp_link_words)(void), (*dsp_rate)(void);
static int (*dsp_link_early)(const int32_t *);
static void put32(uint8_t *p, uint32_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24); }
static void send_setting(uint32_t id, uint32_t v)
{
    uint8_t p[12] = { 0x10, 0x7F, 12, 0 };
    put32(p + 4, id); put32(p + 8, v);
    dsp_command(p, sizeof p);
}

/* one exchange of the audio link: the DSP's words to the control half's on_block(), its words into the delay line */
static void exchange(unsigned words, uint32_t block, int32_t *ctl_in, int32_t *ctl_out, int32_t (*delay)[512])
{
    memcpy(ctl_in, dsp_link(1), words * 4u);
    memset(ctl_out, 0, 512u * 4u);
    link_cb(ctl_in, ctl_out);
    memcpy(delay[block % (LINK_DELAY + 1u)], ctl_out, words * 4u);
    gp5_link_blocks++;
}

static const char *led_text(uint32_t c, char *b)
{
    if (c == GP5_LED_BYPASS) return "dim red";
    if (c == GP5_LED_ON) return "mint";
    snprintf(b, 32, "rgb(%u, %u, %u)", (unsigned)(c & 255u), (unsigned)((c >> 8) & 255u), (unsigned)((c >> 16) & 255u));
    return b;
}

static unsigned words, rate, para_down, para_held, force = 1;
static uint32_t para_t0, next_tick = 10, blocks;
static int early;

int sim_open(const char *dsp_path)
{
    void *lib = dlopen(dsp_path, RTLD_NOW | RTLD_LOCAL);
    if (!lib) { fprintf(stderr, "%s\n", dlerror()); return 2; }
    *(void **)&dsp_init = dlsym(lib, "sim_dsp_init"); *(void **)&dsp_command = dlsym(lib, "sim_dsp_command");
    *(void **)&dsp_block = dlsym(lib, "sim_dsp_block"); *(void **)&dsp_link = dlsym(lib, "sim_dsp_link");
    *(void **)&dsp_link_words = dlsym(lib, "sim_dsp_link_words"); *(void **)&dsp_link_early = dlsym(lib, "sim_dsp_link_early"); *(void **)&dsp_rate = dlsym(lib, "sim_dsp_sample_rate");
    if (!dsp_init || !dsp_link_early || !dsp_command || !dsp_block || !dsp_link || !dsp_link_words || !dsp_rate) { fprintf(stderr, "not a sim_dsp library\n"); return 2; }
    words = dsp_link_words(); rate = dsp_rate(); early = dsp_link_early(0);
    if (rate != GP5_SAMPLE_RATE) {
        fprintf(stderr, "DSP sample rate %u does not match firmware rate %u\n", rate, GP5_SAMPLE_RATE);
        return 2;
    }
    if (words > 512u) words = 512u;
#ifdef GP5_HAS_PARAMS
    gp5_params_init();
#endif
    dsp_init(&now_ms);
    fw_init();
    fw_dsp_ready();
    return 0;
}

unsigned sim_rate(void) { return rate; }

void sim_control(enum sim_control control, int value)
{
    switch (control) {
    case SIM_FS1:
        if (value) fw_foot_down(); else fw_foot_up();
        break;
    case SIM_PARA_PRESS:
        if (value) {
            para_down = 1;
            para_held = 0;
            para_t0 = now_ms;
            fw_para_down();
        } else {
            if (para_down && !para_held) fw_para_click();
            para_down = 0;
            fw_para_up();
        }
        break;
    case SIM_PARA: fw_para_turn(value); break;
    case SIM_VOLUME: knob = (unsigned)(value < 0 ? 0 : value > 255 ? 255 : value); break;
#ifdef GP50_BOARD
    case SIM_LEFT: case SIM_MIDDLE: case SIM_RIGHT:
        fw_gp50_encoder_turn(control - SIM_LEFT, value);
        break;
    case SIM_DRUM: case SIM_EXIT: case SIM_GLOBAL: case SIM_SAVE:
        fw_gp50_button(control - SIM_DRUM, value);
        break;
    case SIM_FS2: fw_gp50_button(4, value); break;
    case SIM_EXPRESSION: fw_gp50_expression((unsigned)(value < 0 ? 0 : value > 255 ? 255 : value)); break;
    case SIM_BATTERY_STEP: {
        int bars = status.battery + value;
        status.battery = bars < 0 ? 0 : bars > 4 ? 4 : bars;
        break;
    }
    case SIM_CHARGE_TOGGLE: status.charging = !status.charging; break;
    case SIM_BT_CYCLE: status.bluetooth = (status.bluetooth + 1) % (GP50_BT_PAIRED + 1); break;
#endif
    default: break;
    }
}

void sim_block(const int32_t *in, int32_t *out)
{
#ifdef SIM_LIVE
    publish_store();
#endif
    static int32_t l[BLOCK], r[BLOCK];
    static int32_t ctl_in[512], ctl_out[512], delay[LINK_DELAY + 1][512], zero[512];
    static int32_t g_now;

    while (next_tick <= now_ms) {
        fw_tick();
        if (para_down && !para_held && now_ms - para_t0 >= 600u) { para_held = 1; fw_para_hold(); }
        next_tick += 10u;
    }

    uint32_t id, v;
#ifdef GP5_HAS_PARAMS
    gp5_params_poll();
    while (gp5_params_take(&id, &v)) send_setting(id, v);
#endif
    while (fw_take(&id, &v)) send_setting(id, v);
    for (unsigned k = 0; k < 8u; k++) {          /* one call is one main-loop pass on the pedal */
        int forced = force;
        blit_bytes = 0;
        int more = fw_draw(force);
        force = 0;
        if (forced) { if (blit_bytes > blit_first) blit_first = blit_bytes; }
        else if (blit_bytes > blit_most) blit_most = blit_bytes;
        if (!more) break;
    }

    if (link_cb) memcpy(dsp_link(0), blocks >= LINK_DELAY ? delay[(blocks - LINK_DELAY) % (LINK_DELAY + 1u)] : zero, words * 4u);
    if (link_cb && early) { dsp_link_early(in); exchange(words, blocks, ctl_in, ctl_out, delay); }
    dsp_block(in, l, r);

    /* The pedal glides its output level by at most 4096 Q16 units per block. */
    unsigned lv = level_owned ? level : knob;
    int32_t g = (int32_t)(lv * 65536u / 255u), d = g - g_now;
    if (d > 4096) d = 4096; else if (d < -4096) d = -4096;
    for (unsigned i = 0; i < BLOCK; i++) {
        int32_t gi = g_now + (d * (int32_t)(i + 1)) / 64;
        out[2 * i] = (int32_t)(((int64_t)l[i] * gi) >> 16);
        out[2 * i + 1] = (int32_t)(((int64_t)r[i] * gi) >> 16);
    }
    g_now += d;
    if (link_cb && !early) exchange(words, blocks, ctl_in, ctl_out, delay);
    blocks++;
    now_ms = (uint32_t)((uint64_t)blocks * BLOCK * 1000u / rate);
}

#ifndef SIM_LIVE
int main(int argc, char **argv)
{
    FILE *ev, *fin, *fout;
    int32_t in[BLOCK], out[2 * BLOCK];
    char line[256], name[200];
    long next_ms = -1; char next_op[32] = ""; long a1 = 0, a2 = 0;
    uint32_t shown_led = 0xFFFFFFFFu;
    if (argc != 9) { fprintf(stderr, "usage: sim <dsp library> <events> <in.raw> <out.raw> <shot dir> <seconds> <store|-> <files|->\n"); return 2; }
    if (sim_store_open(argv[7])) return 2;
    ev = fopen(argv[2], "r"); fin = fopen(argv[3], "rb"); fout = fopen(argv[4], "wb");
    if (!ev || !fin || !fout) { fprintf(stderr, "can't open the events, in or out file\n"); return 2; }
    if (strcmp(argv[8], "-")) { files_dir = argv[8]; load_files(); }
    if (sim_open(argv[1])) return 2;
    uint32_t total = (uint32_t)(atof(argv[6]) * rate / BLOCK + 0.5);

    while (blocks < total) {
        /* events due */
        for (;;) {
            if (next_ms < 0) {
                if (!fgets(line, sizeof line, ev)) { next_ms = 0x7FFFFFFF; break; }
                name[0] = 0; a1 = a2 = 0;
                if (sscanf(line, "%ld %31s", &next_ms, next_op) != 2) { next_ms = -1; continue; }
                if (!strcmp(next_op, "shot")) sscanf(line, "%*ld %*s %199s", name);
                else sscanf(line, "%*ld %*s %ld %ld", &a1, &a2);
            }
            if (next_ms > (long)now_ms) break;
            if (!strcmp(next_op, "foot_down")) sim_control(SIM_FS1, 1);
            else if (!strcmp(next_op, "foot_up")) sim_control(SIM_FS1, 0);
            else if (!strcmp(next_op, "para_down")) sim_control(SIM_PARA_PRESS, 1);
            else if (!strcmp(next_op, "para_up")) sim_control(SIM_PARA_PRESS, 0);
            else if (!strcmp(next_op, "turn")) sim_control(SIM_PARA, (int)a1);
#ifdef GP50_BOARD
            else if (!strcmp(next_op, "encoder")) sim_control(SIM_LEFT + a1, (int)a2);
            else if (!strcmp(next_op, "button")) sim_control(a1 == 4 ? SIM_FS2 : SIM_DRUM + a1, (int)a2);
            else if (!strcmp(next_op, "expression")) sim_control(SIM_EXPRESSION, (int)a1);
            else if (!strcmp(next_op, "battery")) status.battery = (int)a1;
            else if (!strcmp(next_op, "charging")) status.charging = (int)a1;
            else if (!strcmp(next_op, "bluetooth")) status.bluetooth = (unsigned)a1;
#endif
            else if (!strcmp(next_op, "knob")) sim_control(SIM_VOLUME, (int)a1);
#ifdef GP5_HAS_PARAMS
            else if (!strcmp(next_op, "set")) { if (a1 < GP5_NPARAMS) gp5_params_set_outside((unsigned)a1, (int32_t)a2); else fw_param_set((uint32_t)a1, (uint32_t)a2); }
            else if (!strcmp(next_op, "preset_load")) gp5_preset_load((unsigned)a1);
            else if (!strcmp(next_op, "preset_save")) gp5_preset_save((unsigned)a1);
#else
            else if (!strcmp(next_op, "set")) fw_param_set((uint32_t)a1, (uint32_t)a2);
#endif
            else if (!strcmp(next_op, "storage_changed")) fw_storage_changed();
            else if (!strcmp(next_op, "shot")) { while (fw_draw(force)) force = 0; force = 0; shot(argv[5], name); }
            else fprintf(stderr, "unknown event '%s'\n", next_op);
            next_ms = -1;
        }
        size_t got = fread(in, sizeof in[0], BLOCK, fin);
        if (got < BLOCK) memset(in + got, 0, (BLOCK - got) * sizeof in[0]);
        uint32_t ms = now_ms;
        sim_block(in, out);
        if (led != shown_led) {
            char b[32];
            fprintf(stderr, "[%5u.%03u] LEDs: %s\n", ms / 1000u, ms % 1000u, led_text(led, b));
            shown_led = led;
        }
        fwrite(out, sizeof out[0], 2 * BLOCK, fout);
    }
    while (fw_draw(force)) force = 0;
    shot(argv[5], "end");
    fprintf(stderr, "screen: at most %lu bytes sent in one fw_draw call while running (one main-loop pass; the whole screen is %u;"
                    " the first full draw: %lu)\n", blit_most, 2u * GP5_SCREEN_W * GP5_SCREEN_H, blit_first);
    int result = store_dirty && write_store(recs, nrec) != 0;
    fclose(fout);
    return result;
}
#endif
