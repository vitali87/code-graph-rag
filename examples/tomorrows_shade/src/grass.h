#ifndef GRASS_H
#define GRASS_H

#include "world.h"

void GrassInit(const char *baseDir);
/* draws blades around the camera; `ground` holds the lit raymarched image
 * (alpha = view depth) used to colour each blade from the soil beneath it */
void GrassDraw(const World *w, Camera3D cam, Texture2D ground, int width, int height);

#endif
