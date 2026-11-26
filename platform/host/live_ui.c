#include <SDL.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "live.h"
#include "font5x7.h"

enum { WIDTH = 912, HEIGHT = 520, LCD_W = 160, LCD_H = 128, KNOB_Y = 161, DRAG_STEP = 8 };
enum { MOUSE = 1, KEY = 2 };

static const uint32_t ink = 0xe8edf3, muted = 0xa0abb9, accent = 0x73d2c3;
static const SDL_Rect lcd = { 24, 74, LCD_W * 3, LCD_H * 3 };
static const SDL_Rect demo = { 715, 416, 172, 42 };
static const SDL_Rect expression = { 600, 296, 256, 17 };
static const struct {
    enum sim_control control;
    const char *label, *keys;
    SDL_Keycode decrease, increase;
} knobs[] = {
    { SIM_LEFT, "LEFT", "Q / W", SDLK_q, SDLK_w },
    { SIM_MIDDLE, "MIDDLE", "E / R", SDLK_e, SDLK_r },
    { SIM_RIGHT, "RIGHT", "T / Y", SDLK_t, SDLK_y },
    { SIM_PARA, "PARA", "A / S", SDLK_a, SDLK_s },
};
static const struct {
    enum sim_control control;
    SDL_Rect rect;
    const char *label;
    SDL_Keycode key;
} buttons[] = {
    { SIM_FS1,        { 531, 251, 110, 42 }, "FS1 [1]", SDLK_1 },
    { SIM_FS2,        { 653, 251, 110, 42 }, "FS2 [2]", SDLK_2 },
    { SIM_PARA_PRESS, { 775, 251, 112, 42 }, "PARA PUSH", SDLK_RETURN },
    { SIM_DRUM,       { 531, 316, 172, 40 }, "DRUM [D]", SDLK_d },
    { SIM_EXIT,       { 715, 316, 172, 40 }, "EXIT [X]", SDLK_x },
    { SIM_GLOBAL,     { 531, 367, 172, 40 }, "GLOBAL [G]", SDLK_g },
    { SIM_SAVE,       { 715, 367, 172, 40 }, "SAVE [V]", SDLK_v },
};

struct ui_state {
    int rotation[SDL_arraysize(knobs)];
    unsigned pressed[SDL_arraysize(buttons)];
    int knob, button, delta, click, expression;
    bool dragged, demo_playing, expression_drag;
};

static void color(SDL_Renderer *r, uint32_t rgb)
{
    SDL_SetRenderDrawColor(r, (uint8_t)(rgb >> 16), (uint8_t)(rgb >> 8), (uint8_t)rgb, 255);
}

static void fill(SDL_Renderer *r, int x, int y, int w, int h, uint32_t rgb)
{
    SDL_Rect rect = { x, y, w, h };
    color(r, rgb);
    SDL_RenderFillRect(r, &rect);
}

static void text(SDL_Renderer *r, int x, int y, int scale, uint32_t rgb, const char *s)
{
    color(r, rgb);
    while (*s && x + 5 * scale < WIDTH) {
        unsigned char c = (unsigned char)*s++;
        if (c < 0x20 || c > GP5_FONT5X7_LAST) c = '?';
        for (int col = 0; col < 5; col++)
            for (int row = 0; row < 7; row++)
                if (font[c - 0x20][col] & (1 << row)) {
                    SDL_Rect px = { x + col * scale, y + row * scale, scale, scale };
                    SDL_RenderFillRect(r, &px);
                }
        x += 6 * scale;
    }
}

static void center_text(SDL_Renderer *r, int x, int y, int scale, uint32_t rgb, const char *s)
{
    text(r, x - (int)strlen(s) * 3 * scale, y, scale, rgb, s);
}

static void disk(SDL_Renderer *r, int x, int y, int radius, uint32_t rgb)
{
    color(r, rgb);
    for (int dy = -radius; dy <= radius; dy++) {
        int dx = (int)sqrt((double)(radius * radius - dy * dy));
        SDL_RenderDrawLine(r, x - dx, y + dy, x + dx, y + dy);
    }
}

static int knob_x(int index) { return 576 + index * 88; }

static uint32_t led_color(uint32_t led)
{
    if (!(led & 0x01000000u)) return led ? accent : 0x963f44;
    return (((led & 255u) * 255u / 32u) << 16)
         | ((((led >> 8) & 255u) * 255u / 32u) << 8)
         | (((led >> 16) & 255u) * 255u / 32u);
}

static void redraw(SDL_Renderer *r, SDL_Texture *screen, const char *title,
                   const struct ui_state *ui, unsigned rate)
{
    uint32_t pixels[LCD_W * LCD_H], led;
    char audio[80];
    sim_live_screen(pixels, &led);
    SDL_UpdateTexture(screen, NULL, pixels, LCD_W * sizeof pixels[0]);
    color(r, 0x14181e);
    SDL_RenderClear(r);
    fill(r, 20, 70, 488, 392, 0x303843);
    SDL_RenderCopy(r, screen, NULL, &lcd);
    text(r, 24, 24, 3, ink, title);
    text(r, 531, 76, 2, ink, "Controls");
    text(r, 531, 104, 2, muted, "Scroll or drag to turn");
    for (int i = 0; i < (int)SDL_arraysize(knobs); i++) {
        int x = knob_x(i);
        double angle = ui->rotation[i] * (6.283185307179586 / 16.0);
        disk(r, x, KNOB_Y, 25, ui->knob == i ? accent : 0x536173);
        disk(r, x, KNOB_Y, 22, 0x2b3440);
        disk(r, x + (int)(15.0 * sin(angle)), KNOB_Y - (int)(15.0 * cos(angle)), 2, ink);
        text(r, x - 39, KNOB_Y - 6, 2, muted, "-");
        text(r, x + 29, KNOB_Y - 6, 2, muted, "+");
        center_text(r, x, 198, 2, ink, knobs[i].label);
        center_text(r, x, 219, 1, muted, knobs[i].keys);
    }
    for (unsigned i = 0; i < SDL_arraysize(buttons); i++) {
        SDL_Rect button = buttons[i].rect;
        bool down = ui->pressed[i] != 0;
        fill(r, button.x, button.y, button.w, button.h, down ? accent : 0x2b3440);
        center_text(r, button.x + button.w / 2, button.y + (button.h - 14) / 2, 2,
                    down ? 0x14221f : ink, buttons[i].label);
    }
    text(r, 531, 300, 1, muted, "Expression");
    fill(r, expression.x, 302, expression.w, 4, 0x536173);
    if (ui->expression >= 0) {
        char value[12];
        snprintf(value, sizeof value, "%u", (unsigned)ui->expression);
        disk(r, expression.x + ui->expression, 304, 5, accent);
        text(r, 863, 300, 1, ink, value);
    } else {
        text(r, 863, 300, 1, muted, "--");
    }
    disk(r, 538, 426, 5, led_color(led));
    text(r, 553, 420, 2, muted, "Effect LED");
    if (sim_live_demo_available()) {
        bool playing = ui->demo_playing;
        int x = demo.x + demo.w / 2;
        fill(r, demo.x, demo.y, demo.w, demo.h, playing ? accent : 0x2b3440);
        center_text(r, x, demo.y + 7, 2, playing ? 0x14221f : ink,
                    playing ? "Stop Sample" : "Play Sample");
        center_text(r, x, demo.y + 27, 1, playing ? 0x14221f : muted, "[SPACE]");
        text(r, 531, 447, 1, muted, "Enter: PARA push");
    } else {
        text(r, 751, 424, 1, muted, "Enter: PARA push");
    }
    fill(r, 24, 474, WIDTH - 48, 1, 0x303843);
    snprintf(audio, sizeof audio, "JACK: OpenGP5 GP-50 / in, out_l, out_r | DSP %u Hz", rate);
    text(r, 24, 490, 2, muted, audio);
    SDL_RenderPresent(r);
}

static void turn(struct ui_state *ui, int index, int change)
{
    if (!change) return;
    ui->rotation[index] = (ui->rotation[index] + change % 16) % 16;
    sim_live_control(knobs[index].control, change);
}

static void press(struct ui_state *ui, int index, unsigned source, bool down)
{
    bool was = ui->pressed[index] != 0;
    if (down) ui->pressed[index] |= source;
    else ui->pressed[index] &= ~source;
    bool now = ui->pressed[index] != 0;
    if (was != now) sim_live_control(buttons[index].control, now);
}

static int hit_knob(int x, int y)
{
    if (y < 132 || y >= 234 || x < 532 || x >= 884) return -1;
    return (x - 532) / 88;
}

static void move_expression(struct ui_state *ui, int x)
{
    int position = x - expression.x;
    int value = position < 0 ? 0 : position >= expression.w ? expression.w - 1 : position;
    if (value == ui->expression) return;
    ui->expression = value;
    sim_live_control(SIM_EXPRESSION, value);
}

static void mouse_button(struct ui_state *ui, const SDL_MouseButtonEvent *event)
{
    if (event->button != SDL_BUTTON_LEFT) return;
    if (event->type == SDL_MOUSEBUTTONUP) {
        if (ui->button >= 0) press(ui, ui->button, MOUSE, false);
        if (ui->knob >= 0 && !ui->dragged) turn(ui, ui->knob, ui->click);
        if (ui->expression_drag) move_expression(ui, event->x);
        ui->expression_drag = false;
        ui->button = ui->knob = -1;
        SDL_CaptureMouse(SDL_FALSE);
        return;
    }
    SDL_Point point = { event->x, event->y };
    if (SDL_PointInRect(&point, &expression)) {
        ui->expression_drag = true;
        move_expression(ui, point.x);
        SDL_CaptureMouse(SDL_TRUE);
        return;
    }
    if (SDL_PointInRect(&point, &demo) && sim_live_demo_available()) {
        sim_live_control(SIM_DEMO, !ui->demo_playing);
        return;
    }
    for (unsigned i = 0; i < SDL_arraysize(buttons); i++) {
        if (!SDL_PointInRect(&point, &buttons[i].rect)) continue;
        ui->button = (int)i;
        press(ui, ui->button, MOUSE, true);
        SDL_CaptureMouse(SDL_TRUE);
        return;
    }
    ui->knob = hit_knob(event->x, event->y);
    if (ui->knob < 0) return;
    ui->click = event->x < knob_x(ui->knob) ? -1 : 1;
    ui->delta = 0;
    ui->dragged = false;
    SDL_CaptureMouse(SDL_TRUE);
}

static bool handle(SDL_Renderer *r, struct ui_state *ui, const SDL_Event *event)
{
    switch (event->type) {
    case SDL_QUIT:
        return false;
    case SDL_KEYDOWN:
    case SDL_KEYUP: {
        SDL_Keycode key = event->key.keysym.sym;
        bool down = event->type == SDL_KEYDOWN;
        if (key == SDLK_ESCAPE && down) return false;
        if (key == SDLK_SPACE && down && !event->key.repeat && sim_live_demo_available())
            sim_live_control(SIM_DEMO, !ui->demo_playing);
        for (unsigned i = 0; i < SDL_arraysize(buttons); i++)
            if (buttons[i].key == key) press(ui, (int)i, KEY, down);
        for (unsigned i = 0; down && i < SDL_arraysize(knobs); i++) {
            if (knobs[i].decrease == key) turn(ui, (int)i, -1);
            if (knobs[i].increase == key) turn(ui, (int)i, 1);
        }
        break;
    }
    case SDL_MOUSEBUTTONDOWN:
    case SDL_MOUSEBUTTONUP:
        mouse_button(ui, &event->button);
        break;
    case SDL_MOUSEMOTION:
        if (ui->expression_drag) {
            move_expression(ui, event->motion.x);
        } else if (ui->knob >= 0) {
            ui->delta -= event->motion.yrel;
            int steps = ui->delta / DRAG_STEP;
            if (steps) ui->dragged = true;
            ui->delta %= DRAG_STEP;
            turn(ui, ui->knob, steps);
        }
        break;
    case SDL_MOUSEWHEEL: {
        int x, y;
        float lx, ly;
        SDL_GetMouseState(&x, &y);
        SDL_RenderWindowToLogical(r, x, y, &lx, &ly);
        int index = hit_knob((int)lx, (int)ly);
        if (index >= 0) turn(ui, index, event->wheel.direction == SDL_MOUSEWHEEL_FLIPPED ?
                                        -event->wheel.y : event->wheel.y);
        break;
    }
    case SDL_WINDOWEVENT:
        if (event->window.event == SDL_WINDOWEVENT_CLOSE) return false;
        if (event->window.event == SDL_WINDOWEVENT_FOCUS_LOST) {
            for (unsigned i = 0; i < SDL_arraysize(buttons); i++) press(ui, (int)i, MOUSE | KEY, false);
            ui->button = ui->knob = -1;
            ui->expression_drag = false;
            SDL_CaptureMouse(SDL_FALSE);
        }
        break;
    }
    return true;
}

int sim_live_ui(const char *title, unsigned rate)
{
    if (SDL_Init(SDL_INIT_VIDEO) < 0) {
        fprintf(stderr, "SDL video: %s\n", SDL_GetError());
        return 1;
    }
    SDL_SetHint(SDL_HINT_RENDER_SCALE_QUALITY, "0");
    SDL_Window *window = SDL_CreateWindow(title, SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED,
                                         WIDTH, HEIGHT, SDL_WINDOW_RESIZABLE | SDL_WINDOW_ALLOW_HIGHDPI);
    SDL_Renderer *renderer = window ? SDL_CreateRenderer(window, -1, 0) : NULL;
    SDL_Texture *screen = renderer ? SDL_CreateTexture(renderer, SDL_PIXELFORMAT_RGB888,
                                    SDL_TEXTUREACCESS_STREAMING, LCD_W, LCD_H) : NULL;
    int result = 0;
    if (!screen || SDL_RenderSetLogicalSize(renderer, WIDTH, HEIGHT) < 0) {
        fprintf(stderr, "SDL display: %s\n", SDL_GetError());
        result = 1;
    } else {
        SDL_SetWindowMinimumSize(window, WIDTH * 2 / 3, HEIGHT * 2 / 3);
        struct ui_state ui = { .button = -1, .knob = -1, .expression = -1 };
        bool show = true;
        while (show && sim_live_running()) {
            SDL_Event event;
            while (show && SDL_PollEvent(&event)) show = handle(renderer, &ui, &event);
            sim_store_flush();
            if (show) {
                ui.demo_playing = sim_live_demo_playing();
                redraw(renderer, screen, title, &ui, rate);
                SDL_Delay(16);
            }
        }
        for (unsigned i = 0; i < SDL_arraysize(buttons); i++) press(&ui, (int)i, MOUSE | KEY, false);
    }
    SDL_DestroyTexture(screen);
    SDL_DestroyRenderer(renderer);
    SDL_DestroyWindow(window);
    SDL_QuitSubSystem(SDL_INIT_VIDEO);
    return result;
}
