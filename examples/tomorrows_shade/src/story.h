#ifndef STORY_H
#define STORY_H

#include "world.h"

enum {
    SH_DAWN, SH_WALK, SH_ARRIVE, SH_DIG, SH_ACORN, SH_MIRA, SH_NANA, SH_PLANT, SH_SPROUT, SH_YEARS,
    SH_SUNSET, SH_BENCH, SH_DUSK, SH_EMPTY, SH_GROW, SH_RETURN, SH_SHADE, SH_FIND, SH_GIFT, SH_PLANT2,
    SH_RISE, SH_END, SH_COUNT
};

void StoryInit(void);
float StoryShotStart(int shot); /* SH_COUNT gives the total length */
float StoryDuration(void);
void StoryEvaluate(float t, Frame *f);

#endif
