#ifndef RENDER_H
#define RENDER_H

#include "world.h"

typedef struct {
    int outW, outH;       /* final image size */
    int intW, intH;       /* internal render size (supersampled) */
} RenderConfig;

void RenderInit(RenderConfig cfg, const char *baseDir);
void RenderShutdown(void);
/* renders the frame into the returned LDR texture (outW x outH, upright GL rows) */
RenderTexture2D *RenderFrame(const Frame *f);
Camera3D WorldCamera(const World *w);

#endif
