#ifndef PARTICLES_H
#define PARTICLES_H

#include "world.h"

void ParticlesInit(void);
/* pass 0 = alpha blended, pass 1 = additive (glows) */
void ParticlesDraw(const World *w, Camera3D cam, int pass);

#endif
