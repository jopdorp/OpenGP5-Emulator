#ifndef GP5_HOST_LIVE_H
#define GP5_HOST_LIVE_H

#include <stdbool.h>
#include <stdint.h>

enum sim_control {
    SIM_LEFT, SIM_MIDDLE, SIM_RIGHT, SIM_PARA, SIM_PARA_PRESS,
    SIM_FS1, SIM_FS2, SIM_DRUM, SIM_EXIT, SIM_GLOBAL, SIM_SAVE, SIM_VOLUME, SIM_DEMO, SIM_EXPRESSION
};

int sim_open(const char *dsp_path);
unsigned sim_rate(void);
void sim_control(enum sim_control control, int value);
void sim_block(const int32_t *in, int32_t *stereo_out);
void sim_screen(uint32_t *pixels, uint32_t *led);

int sim_live_ui(const char *title, unsigned rate);
void sim_live_control(enum sim_control control, int value);
void sim_live_screen(uint32_t *pixels, uint32_t *led);
bool sim_live_running(void);
bool sim_live_demo_available(void);
bool sim_live_demo_playing(void);

#endif
