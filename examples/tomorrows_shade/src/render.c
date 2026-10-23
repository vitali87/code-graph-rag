#include "render.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "grass.h"
#include "particles.h"
#include "rlgl.h"

#define GL_ZERO 0
#define GL_ONE 1
#define GL_SRC_ALPHA 0x0302
#define GL_ONE_MINUS_SRC_ALPHA 0x0303
#define GL_FUNC_ADD 0x8006

#define BLOOM_LEVELS 6

typedef struct {
    Shader sh;
    int loc[160];
} Prog;

static RenderConfig gCfg;
static char gBase[512];

static Prog gScene, gMix, gDof, gBright, gDown, gUp, gRays, gFinal;
static RenderTexture2D gSceneA, gSceneB, gSceneMix, gDofRT, gRaysRT, gOut, gGround;
static RenderTexture2D gBloom[BLOOM_LEVELS];
static Texture2D gNoise, gHeight;
static Font gFontRegular, gFontItalic, gFontTitle;
static int gFrameCounter;

/* ------------------------------------------------------------------------ */

static RenderTexture2D LoadTarget(int w, int h, int format, int depth)
{
    RenderTexture2D t = { 0 };
    t.id = rlLoadFramebuffer();
    rlEnableFramebuffer(t.id);
    t.texture.id = rlLoadTexture(NULL, w, h, format, 1);
    t.texture.width = w;
    t.texture.height = h;
    t.texture.format = format;
    t.texture.mipmaps = 1;
    rlFramebufferAttach(t.id, t.texture.id, RL_ATTACHMENT_COLOR_CHANNEL0, RL_ATTACHMENT_TEXTURE2D, 0);
    if (depth) {
        t.depth.id = rlLoadTextureDepth(w, h, true);
        t.depth.width = w;
        t.depth.height = h;
        t.depth.format = 19;
        t.depth.mipmaps = 1;
        rlFramebufferAttach(t.id, t.depth.id, RL_ATTACHMENT_DEPTH, RL_ATTACHMENT_RENDERBUFFER, 0);
    }
    if (!rlFramebufferComplete(t.id)) TraceLog(LOG_WARNING, "RENDER: framebuffer %u incomplete", t.id);
    rlDisableFramebuffer();
    SetTextureFilter(t.texture, TEXTURE_FILTER_BILINEAR);
    SetTextureWrap(t.texture, TEXTURE_WRAP_CLAMP);
    return t;
}

static char *ReadText(const char *rel)
{
    char path[1024];
    snprintf(path, sizeof(path), "%s%s", gBase, rel);
    char *txt = LoadFileText(path);
    if (!txt) {
        TraceLog(LOG_ERROR, "RENDER: cannot read %s", path);
        exit(1);
    }
    return txt;
}

static Prog LoadProg(const char *fsFile, const char **names, int count)
{
    Prog p = { 0 };
    char *fs = ReadText(fsFile);
    p.sh = LoadShaderFromMemory(NULL, fs);
    UnloadFileText(fs);
    if (p.sh.id == rlGetShaderIdDefault()) {
        TraceLog(LOG_ERROR, "RENDER: shader %s failed to compile", fsFile);
        exit(1);
    }
    for (int i = 0; i < count && i < 160; i++) p.loc[i] = GetShaderLocation(p.sh, names[i]);
    return p;
}

/* noise texture whose green channel is the red channel shifted by (37,17):
 * lets the shader build smooth 3D value noise from one bilinear fetch */
static Texture2D MakeNoiseTexture(void)
{
    const int N = 256;
    const unsigned char *r = NoiseTable();
    Color *px = malloc(sizeof(Color) * N * N);
    for (int y = 0; y < N; y++)
        for (int x = 0; x < N; x++) {
            int x2 = (x - 37) & (N - 1), y2 = (y - 17) & (N - 1);
            px[y * N + x] = (Color){ r[y * N + x], r[y2 * N + x2], 0, 255 };
        }
    Image img = { px, N, N, 1, PIXELFORMAT_UNCOMPRESSED_R8G8B8A8 };
    Texture2D t = LoadTextureFromImage(img);
    SetTextureFilter(t, TEXTURE_FILTER_BILINEAR);
    SetTextureWrap(t, TEXTURE_WRAP_REPEAT);
    free(px);
    return t;
}

/* terrain height baked to a float texture: marching reads one texel instead of
 * evaluating the analytic hills at every step */
static Texture2D MakeHeightTexture(void)
{
    const int N = 2048;
    float *h = malloc(sizeof(float) * N * N);
    for (int y = 0; y < N; y++)
        for (int x = 0; x < N; x++)
            h[y * N + x] = terrainHeight(-512.0f + (x + 0.5f) * 0.5f, -512.0f + (y + 0.5f) * 0.5f);
    Image img = { h, N, N, 1, PIXELFORMAT_UNCOMPRESSED_R32 };
    Texture2D t = LoadTextureFromImage(img);
    SetTextureFilter(t, TEXTURE_FILTER_BILINEAR);
    SetTextureWrap(t, TEXTURE_WRAP_CLAMP);
    free(h);
    return t;
}

/* ------------------------------------------------------------------------ */

enum {
    S_RES, S_OFFSET, S_TIME, S_CAMPOS, S_CAMFWD, S_CAMRIGHT, S_CAMUP, S_TANHALF, S_CLIP,
    S_SUNDIR, S_SUNCOL, S_SKYZEN, S_SKYHOR, S_FOGCOL, S_AMB, S_MOONDIR,
    S_FOGDEN, S_NIGHT, S_CLOUD, S_OVERCAST, S_SNOW, S_DRY, S_LUSH, S_WET,
    S_LEAFCOL, S_LEAFCOL2, S_LEAFAMT, S_WIND, S_PATH, S_NOISE, S_HEIGHT,
    S_TREEPOS, S_SEGCOUNT, S_SEGA, S_SEGB, S_GROUPCOUNT, S_GROUPBOUND, S_GROUPRANGE,
    S_BLOBCOUNT, S_BLOB, S_CANOPYBOUND, S_WOODBOUND, S_SPROUT, S_GROWTH,
    S_CHARCOUNT, S_J, S_CSTYLE, S_CFACE, S_CGAZE, S_CBOUND, S_CCOL, S_CAUX,
    S_BENCH, S_BENCHAGE, S_CANE, S_ACORN, S_CAN, S_CANTS, S_TROWEL, S_MOUND, S_MOUND2,
    S_SWING, S_SWINGSHOW, S_FENCE, S_SCARFBENCH, S_PLANTEDCOUNT, S_PLANTED, S_SPROUT2, S_ROCK, S_BGCOUNT, S_BGBOUND, S_BGRANGE, S_HULLC, S_HULLR, S_CEXT,
    S_COUNT
};

static const char *gSceneNames[S_COUNT] = {
    "uRes", "uOffset", "uTime", "uCamPos", "uCamFwd", "uCamRight", "uCamUp", "uTanHalf", "uClip",
    "uSunDir", "uSunCol", "uSkyZen", "uSkyHor", "uFogCol", "uAmb", "uMoonDir",
    "uFogDen", "uNight", "uCloud", "uOvercast", "uSnow", "uDry", "uLush", "uWet",
    "uLeafCol", "uLeafCol2", "uLeafAmt", "uWind", "uPath", "uNoise", "uHeight",
    "uTreePos", "uSegCount", "uSegA", "uSegB", "uGroupCount", "uGroupBound", "uGroupRange",
    "uBlobCount", "uBlob", "uCanopyBound", "uWoodBound", "uSprout", "uGrowth",
    "uCharCount", "uJ", "uCStyle", "uCFace", "uCGaze", "uCBound", "uCCol", "uCAux",
    "uBench", "uBenchAge", "uCane", "uAcorn", "uCan", "uCanTS", "uTrowel", "uMound", "uMound2",
    "uSwing", "uSwingShow", "uFence", "uScarfBench", "uPlantedCount", "uPlanted", "uSprout2", "uRock", "uBlobGroupCount", "uBlobGroupBound", "uBlobGroupRange", "uCanopyHullC", "uCanopyHullR", "uCanopyExt",
};

enum { P_RES, P_SRC, P_SRC2, P_SRC3, P_TEXEL, P_MIX, P_SUN, P_STRENGTH, P_FOCUS, P_APERTURE,
       P_EXPOSURE, P_GRADE, P_SAT, P_CONTRAST, P_VIGNETTE, P_WARMTH, P_FADE, P_TIME, P_BLOOMSTR, P_RAYSTR, P_SCALE, P_SRCRES, P_WHITE, P_COUNT };
static const char *gPostNames[P_COUNT] = { "uRes", "uSrc", "uSrc2", "uSrc3", "uTexel", "uMix", "uSun", "uStrength", "uFocus", "uAperture",
    "uExposure", "uGrade", "uSat", "uContrast", "uVignette", "uWarmth", "uFade", "uTime", "uBloomStr", "uRayStr", "uScale", "uSrcRes", "uWhite" };

static void setF(Prog *p, int i, float v) { SetShaderValue(p->sh, p->loc[i], &v, SHADER_UNIFORM_FLOAT); }
static void setI(Prog *p, int i, int v) { SetShaderValue(p->sh, p->loc[i], &v, SHADER_UNIFORM_INT); }
static void setV2(Prog *p, int i, float x, float y) { float v[2] = { x, y }; SetShaderValue(p->sh, p->loc[i], v, SHADER_UNIFORM_VEC2); }
static void setV3(Prog *p, int i, Vector3 v) { SetShaderValue(p->sh, p->loc[i], &v, SHADER_UNIFORM_VEC3); }
static void setV4(Prog *p, int i, Vector4 v) { SetShaderValue(p->sh, p->loc[i], &v, SHADER_UNIFORM_VEC4); }
static void setTex(Prog *p, int i, Texture2D t) { SetShaderValueTexture(p->sh, p->loc[i], t); }

/* character palettes: skin, hair, top, bottom, iris */
static Vector3 CharPalette(CharType t, int k)
{
    static const int pal[4][5] = {
        { 0xD9B19A, 0xDCD8D2, 0x7880AA, 0x4E3B4A, 0x5C7A8C }, /* grandmother: silver hair, periwinkle cardigan */
        { 0xCF9C7E, 0x3A2416, 0xE3B13C, 0x3A4A7A, 0x5A3A20 }, /* girl: sunflower dress */
        { 0xC9977A, 0x3B2518, 0x3F8A88, 0xD8C8A8, 0x5A3A20 }, /* woman: teal cardigan, linen skirt */
        { 0xCC9B7E, 0x2A1A10, 0x4C7FC0, 0xB9A47C, 0x4A3018 }, /* boy: blue tee, khaki shorts */
    };
    return rgb(pal[t][k]); /* sRGB; the shader linearises all albedos */
}

Camera3D WorldCamera(const World *w)
{
    Camera3D cam = { 0 };
    cam.position = v3add(w->cam.pos, w->cam.shake);
    cam.target = v3add(w->cam.target, v3scale(w->cam.shake, 0.6f));
    Vector3 fwd = v3norm(v3sub(cam.target, cam.position));
    Vector3 right = v3norm(v3cross(fwd, v3(0, 1, 0)));
    Vector3 up = v3cross(right, fwd);
    float c = cosf(w->cam.roll), s = sinf(w->cam.roll);
    cam.up = v3add(v3scale(up, c), v3scale(right, s));
    cam.fovy = w->cam.fov;
    cam.projection = CAMERA_PERSPECTIVE;
    return cam;
}

static void UploadWorld(const World *w, int width, int height)
{
    Prog *p = &gScene;
    Camera3D cam = WorldCamera(w);
    Vector3 fwd = v3norm(v3sub(cam.target, cam.position));
    Vector3 right = v3norm(v3cross(fwd, cam.up));
    Vector3 up = v3cross(right, fwd);
    setV2(p, S_RES, (float)width, (float)height);
    setV2(p, S_OFFSET, 0, 0);
    setF(p, S_TIME, w->anim);
    setV3(p, S_CAMPOS, cam.position);
    setV3(p, S_CAMFWD, fwd);
    setV3(p, S_CAMRIGHT, right);
    setV3(p, S_CAMUP, up);
    setF(p, S_TANHALF, tanf(w->cam.fov * DEG2RAD * 0.5f));
    setV2(p, S_CLIP, (float)rlGetCullDistanceNear(), (float)rlGetCullDistanceFar());

    const Lighting *L = &w->light;
    setV3(p, S_SUNDIR, L->sunDir);
    setV3(p, S_SUNCOL, L->sunCol);
    setV3(p, S_SKYZEN, L->skyZen);
    setV3(p, S_SKYHOR, L->skyHor);
    setV3(p, S_FOGCOL, L->fogCol);
    setV3(p, S_AMB, L->amb);
    setV3(p, S_MOONDIR, L->moonDir);
    setF(p, S_FOGDEN, L->fogDen);
    setF(p, S_NIGHT, L->night);
    setF(p, S_CLOUD, L->cloud);
    setF(p, S_OVERCAST, L->overcast);
    setF(p, S_SNOW, w->snow);
    setF(p, S_DRY, w->dry);
    setF(p, S_LUSH, w->lush);
    setF(p, S_WET, w->wet);
    setV3(p, S_LEAFCOL, w->leafCol);
    setV3(p, S_LEAFCOL2, w->leafCol2);
    setF(p, S_LEAFAMT, w->leafAmt);
    setF(p, S_WIND, w->wind);

    float path[32];
    for (int i = 0; i < PATH_PTS; i++) { path[i * 2] = gPath[i].x; path[i * 2 + 1] = gPath[i].z; }
    SetShaderValueV(p->sh, p->loc[S_PATH], path, SHADER_UNIFORM_VEC4, 8);
    setTex(p, S_NOISE, gNoise);
    setTex(p, S_HEIGHT, gHeight);

    const TreeState *t = &w->tree;
    setV3(p, S_TREEPOS, v3(0, terrainHeight(0, 0), 0));
    setI(p, S_SEGCOUNT, t->segCount);
    if (t->segCount > 0) {
        SetShaderValueV(p->sh, p->loc[S_SEGA], t->segA, SHADER_UNIFORM_VEC4, t->segCount);
        SetShaderValueV(p->sh, p->loc[S_SEGB], t->segB, SHADER_UNIFORM_VEC4, t->segCount);
    }
    setI(p, S_GROUPCOUNT, t->groupCount);
    if (t->groupCount > 0) {
        SetShaderValueV(p->sh, p->loc[S_GROUPBOUND], t->groupBound, SHADER_UNIFORM_VEC4, t->groupCount);
        SetShaderValueV(p->sh, p->loc[S_GROUPRANGE], t->groupRange, SHADER_UNIFORM_VEC2, t->groupCount);
    }
    setI(p, S_BLOBCOUNT, t->blobCount);
    if (t->blobCount > 0) SetShaderValueV(p->sh, p->loc[S_BLOB], t->blob, SHADER_UNIFORM_VEC4, t->blobCount);
    setI(p, S_BGCOUNT, t->blobGroupCount);
    if (t->blobGroupCount > 0) {
        SetShaderValueV(p->sh, p->loc[S_BGBOUND], t->blobGroupBound, SHADER_UNIFORM_VEC4, t->blobGroupCount);
        SetShaderValueV(p->sh, p->loc[S_BGRANGE], t->blobGroupRange, SHADER_UNIFORM_VEC2, t->blobGroupCount);
    }
    setV4(p, S_CANOPYBOUND, t->canopyBound);
    setV4(p, S_HULLC, t->hullC);
    setV4(p, S_HULLR, t->hullR);
    setV4(p, S_CEXT, t->canopyExt);
    setV4(p, S_WOODBOUND, t->woodBound);
    setF(p, S_SPROUT, t->sprout);
    setF(p, S_GROWTH, t->growth);

    /* people */
    static float J[MAX_CHARS * 26 * 3];
    Vector4 style[MAX_CHARS], face[MAX_CHARS], gaze[MAX_CHARS], bound[MAX_CHARS], aux[MAX_CHARS];
    Vector3 cols[MAX_CHARS * 5];
    int n = 0;
    for (int i = 0; i < w->charCount && n < MAX_CHARS; i++) {
        const CharState *c = &w->ch[i];
        if (!c->active) continue;
        for (int k = 0; k < 26; k++) {
            Vector3 v = k < NJ ? c->j[k] : (k == 24 ? c->palmL : c->palmR);
            J[(n * 26 + k) * 3 + 0] = v.x;
            J[(n * 26 + k) * 3 + 1] = v.y;
            J[(n * 26 + k) * 3 + 2] = v.z;
        }
        style[n] = (Vector4){ (float)c->type, c->scarf, c->cane, c->sit };
        face[n] = (Vector4){ c->blink, c->smile, c->mouthOpen, c->brow };
        gaze[n] = (Vector4){ c->gaze.x, c->gaze.y, c->gaze.z, c->tear };
        Vector3 mn = c->j[0], mx = c->j[0];
        for (int k = 0; k <= J_RTOE; k++) { mn = Vector3Min(mn, c->j[k]); mx = Vector3Max(mx, c->j[k]); }
        Vector3 cen = v3scale(v3add(mn, mx), 0.5f);
        float r = v3len(v3sub(mx, cen)) + 0.32f;
        if (c->cane > 0.5f) r += 0.25f;
        bound[n] = (Vector4){ cen.x, cen.y, cen.z, r };
        aux[n] = (Vector4){ c->caneTip.x, c->caneTip.y, c->caneTip.z, c->cane };
        for (int k = 0; k < 5; k++) cols[n * 5 + k] = CharPalette(c->type, k);
        n++;
    }
    setI(p, S_CHARCOUNT, n);
    if (n > 0) {
        SetShaderValueV(p->sh, p->loc[S_J], J, SHADER_UNIFORM_VEC3, n * 26);
        SetShaderValueV(p->sh, p->loc[S_CSTYLE], style, SHADER_UNIFORM_VEC4, n);
        SetShaderValueV(p->sh, p->loc[S_CFACE], face, SHADER_UNIFORM_VEC4, n);
        SetShaderValueV(p->sh, p->loc[S_CGAZE], gaze, SHADER_UNIFORM_VEC4, n);
        SetShaderValueV(p->sh, p->loc[S_CBOUND], bound, SHADER_UNIFORM_VEC4, n);
        SetShaderValueV(p->sh, p->loc[S_CCOL], cols, SHADER_UNIFORM_VEC3, n * 5);
        SetShaderValueV(p->sh, p->loc[S_CAUX], aux, SHADER_UNIFORM_VEC4, n);
    }

    setV4(p, S_BENCH, w->bench);
    setF(p, S_BENCHAGE, w->benchAge);
    setV4(p, S_CANE, w->cane);
    setV4(p, S_ACORN, w->acorn);
    setV4(p, S_CAN, w->can);
    setV2(p, S_CANTS, w->canTilt, w->canShow);
    setV4(p, S_TROWEL, w->trowel);
    setV4(p, S_MOUND, w->mound);
    setV4(p, S_MOUND2, w->mound2);
    setV4(p, S_SWING, w->swing);
    setF(p, S_SWINGSHOW, w->swingShow);
    setV4(p, S_FENCE, w->fence);
    setV4(p, S_SCARFBENCH, w->scarfOnBench);
    setI(p, S_PLANTEDCOUNT, w->plantedCount);
    if (w->plantedCount > 0) SetShaderValueV(p->sh, p->loc[S_PLANTED], w->planted, SHADER_UNIFORM_VEC4, w->plantedCount);
    setV4(p, S_SPROUT2, w->sprout2);

    static const float rocks[6][3] = {
        { 13.5f, 13.0f, 0.7f }, { 14.4f, 13.9f, 0.42f }, { -5.0f, 7.5f, 0.55f },
        { 21.5f, 17.0f, 0.9f }, { 6.5f, -5.0f, 0.5f }, { -9.0f, -3.0f, 0.65f },
    };
    Vector4 rk[6];
    for (int i = 0; i < 6; i++)
        rk[i] = (Vector4){ rocks[i][0], terrainHeight(rocks[i][0], rocks[i][1]) - 0.2f * rocks[i][2], rocks[i][1], rocks[i][2] };
    SetShaderValueV(p->sh, p->loc[S_ROCK], rk, SHADER_UNIFORM_VEC4, 6);
}

static void FullscreenQuad(int w, int h)
{
    rlDisableColorBlend();
    DrawRectangle(0, 0, w, h, WHITE);
    rlDrawRenderBatchActive();
    rlEnableColorBlend();
}

static void RenderWorld(const World *w, RenderTexture2D *target)
{
    int W = target->texture.width, H = target->texture.height;
    BeginTextureMode(*target);
    ClearBackground(BLACK);
    rlEnableDepthTest();
    rlEnableDepthMask();
    BeginShaderMode(gScene.sh);
    UploadWorld(w, W, H);
    FullscreenQuad(W, H);
    EndShaderMode();
    EndTextureMode();

    /* keep a copy of the lit ground for the grass to sample */
    BeginTextureMode(gGround);
    BeginShaderMode(gMix.sh);
    setTex(&gMix, P_SRC, target->texture);
    setTex(&gMix, P_SRC2, target->texture);
    setF(&gMix, P_MIX, 0.0f);
    setV2(&gMix, P_RES, (float)W, (float)H);
    FullscreenQuad(W, H);
    EndShaderMode();
    EndTextureMode();

    /* 3D overlays (grass, particles), depth tested against the raymarched depth */
    BeginTextureMode(*target);
    rlEnableDepthTest();
    rlEnableDepthMask();
    Camera3D cam = WorldCamera(w);
    BeginMode3D(cam);
    GrassDraw(w, cam, gGround.texture, W, H);
    rlDrawRenderBatchActive();
    rlSetBlendFactorsSeparate(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA, GL_ZERO, GL_ONE, GL_FUNC_ADD, GL_FUNC_ADD);
    rlSetBlendMode(RL_BLEND_CUSTOM_SEPARATE);
    ParticlesDraw(w, cam, 0);
    rlSetBlendFactorsSeparate(GL_SRC_ALPHA, GL_ONE, GL_ZERO, GL_ONE, GL_FUNC_ADD, GL_FUNC_ADD);
    rlSetBlendMode(RL_BLEND_CUSTOM_SEPARATE);
    ParticlesDraw(w, cam, 1);
    rlDrawRenderBatchActive();
    rlSetBlendMode(RL_BLEND_ALPHA);
    EndMode3D();
    rlDisableDepthTest();
    EndTextureMode();
}

void RenderInit(RenderConfig cfg, const char *baseDir)
{
    gCfg = cfg;
    snprintf(gBase, sizeof(gBase), "%s", baseDir);
    rlSetClipPlanes(0.05, 2000.0);

    gScene = LoadProg("shaders/scene.fs", gSceneNames, S_COUNT);
    gMix = LoadProg("shaders/mix.fs", gPostNames, P_COUNT);
    gDof = LoadProg("shaders/dof.fs", gPostNames, P_COUNT);
    gBright = LoadProg("shaders/bright.fs", gPostNames, P_COUNT);
    gDown = LoadProg("shaders/down.fs", gPostNames, P_COUNT);
    gUp = LoadProg("shaders/up.fs", gPostNames, P_COUNT);
    gRays = LoadProg("shaders/rays.fs", gPostNames, P_COUNT);
    gFinal = LoadProg("shaders/final.fs", gPostNames, P_COUNT);

    int fmt = PIXELFORMAT_UNCOMPRESSED_R16G16B16A16;
    gSceneA = LoadTarget(cfg.intW, cfg.intH, fmt, 1);
    gSceneB = LoadTarget(cfg.intW, cfg.intH, fmt, 1);
    gSceneMix = LoadTarget(cfg.intW, cfg.intH, fmt, 0);
    gGround = LoadTarget(cfg.intW, cfg.intH, fmt, 0);
    gDofRT = LoadTarget(cfg.intW, cfg.intH, fmt, 0);
    gRaysRT = LoadTarget(cfg.intW / 2, cfg.intH / 2, fmt, 0);
    int bw = cfg.intW / 2, bh = cfg.intH / 2;
    for (int i = 0; i < BLOOM_LEVELS; i++) {
        gBloom[i] = LoadTarget(bw > 1 ? bw : 1, bh > 1 ? bh : 1, fmt, 0);
        bw /= 2;
        bh /= 2;
    }
    gOut = LoadTarget(cfg.outW, cfg.outH, PIXELFORMAT_UNCOMPRESSED_R8G8B8A8, 0);
    gNoise = MakeNoiseTexture();
    gHeight = MakeHeightTexture();

    char path[1024];
    snprintf(path, sizeof(path), "%sassets/fonts/EBGaramond12-Regular.ttf", gBase);
    gFontRegular = LoadFontEx(path, 96, NULL, 0);
    gFontTitle = LoadFontEx(path, 192, NULL, 0);
    snprintf(path, sizeof(path), "%sassets/fonts/EBGaramond12-Italic.ttf", gBase);
    gFontItalic = LoadFontEx(path, 96, NULL, 0);
    Font *fonts[3] = { &gFontRegular, &gFontItalic, &gFontTitle };
    for (int i = 0; i < 3; i++) {
        GenTextureMipmaps(&fonts[i]->texture);
        SetTextureFilter(fonts[i]->texture, TEXTURE_FILTER_TRILINEAR);
    }
    ParticlesInit();
    GrassInit(gBase);
}

void RenderShutdown(void)
{
    UnloadShader(gScene.sh);
}

static void DrawTextShadowed(Font font, const char *text, Vector2 pos, float size, float spacing, Color col, float alpha)
{
    Color sh = (Color){ 0, 0, 0, (unsigned char)(150 * alpha) };
    for (int i = 0; i < 8; i++) {
        float a = i * 0.785f;
        Vector2 o = { pos.x + cosf(a) * size * 0.04f, pos.y + sinf(a) * size * 0.04f + size * 0.03f };
        DrawTextEx(font, text, o, size, spacing, (Color){ sh.r, sh.g, sh.b, (unsigned char)(sh.a * 0.25f) });
    }
    col.a = (unsigned char)(col.a * alpha);
    DrawTextEx(font, text, pos, size, spacing, col);
}

static void DrawCentered(Font font, const char *text, float cy, float size, float spacing, Color col, float alpha)
{
    Vector2 m = MeasureTextEx(font, text, size, spacing);
    DrawTextShadowed(font, text, (Vector2){ (gCfg.outW - m.x) * 0.5f, cy - m.y * 0.5f }, size, spacing, col, alpha);
}

static void DrawOverlay(const Frame *f)
{
    int W = gCfg.outW, H = gCfg.outH;
    float u = H / 1080.0f;
    /* letterbox to ~2.2:1 */
    float bar = f->letterbox * (H - W / 2.2f) * 0.5f;
    if (bar > 0.5f) {
        DrawRectangle(0, 0, W, (int)ceilf(bar), BLACK);
        DrawRectangle(0, H - (int)ceilf(bar), W, (int)ceilf(bar) + 1, BLACK);
    }
    if (f->title && f->titleAlpha > 0.001f) {
        float a = f->titleAlpha;
        if (f->titleStyle == 0) {
            DrawCentered(gFontTitle, f->title, H * 0.42f, 132 * u, 6 * u, (Color){ 250, 240, 222, 255 }, a);
            if (f->subtitle) DrawCentered(gFontItalic, f->subtitle, H * 0.42f + 105 * u, 42 * u, 2 * u, (Color){ 240, 228, 205, 255 }, a * 0.9f);
        } else if (f->titleStyle == 1) {
            /* epigraph: multi line, italic */
            const char *s = f->title;
            char line[256];
            int lines = 1;
            for (const char *c = s; *c; c++) if (*c == '\n') lines++;
            float size = 50 * u, lh = size * 1.35f;
            float y = H * 0.46f - (lines - 1) * lh * 0.5f;
            while (*s) {
                int n = 0;
                while (s[n] && s[n] != '\n' && n < 255) n++;
                memcpy(line, s, n);
                line[n] = 0;
                DrawCentered(gFontItalic, line, y, size, 1.5f * u, (Color){ 245, 236, 220, 255 }, a);
                y += lh;
                s += n;
                if (*s == '\n') s++;
            }
            if (f->subtitle) DrawCentered(gFontRegular, f->subtitle, y + lh * 0.5f, 34 * u, 4 * u, (Color){ 220, 205, 180, 255 }, a * 0.85f);
        } else {
            DrawCentered(gFontRegular, f->title, H * 0.5f, 64 * u, 8 * u, (Color){ 245, 236, 220, 255 }, a);
        }
    }
    if (f->caption && f->captionAlpha > 0.001f) {
        float size = 46 * u;
        float y = H - bar - 70 * u;
        DrawCentered(gFontItalic, f->caption, y, size, 1.0f * u, (Color){ 255, 248, 232, 255 }, f->captionAlpha);
    }
}

RenderTexture2D *RenderFrame(const Frame *f)
{
    gFrameCounter++;
    RenderTexture2D *scene = &gSceneA;
    if (f->mix < 0.999f) RenderWorld(&f->a, &gSceneA);
    if (f->mix > 0.001f) {
        RenderWorld(&f->b, &gSceneB);
        if (f->mix >= 0.999f) {
            scene = &gSceneB;
        } else {
            BeginTextureMode(gSceneMix);
            BeginShaderMode(gMix.sh);
            setTex(&gMix, P_SRC, gSceneA.texture);
            setTex(&gMix, P_SRC2, gSceneB.texture);
            setF(&gMix, P_MIX, f->mix);
            setV2(&gMix, P_RES, (float)gCfg.intW, (float)gCfg.intH);
            FullscreenQuad(gCfg.intW, gCfg.intH);
            EndShaderMode();
            EndTextureMode();
            scene = &gSceneMix;
        }
    }
    /* grading follows the dominant shot */
    const World *g = f->mix > 0.5f ? &f->b : &f->a;

    /* depth of field */
    RenderTexture2D *src = scene;
    if (g->cam.aperture > 0.0f) {
        BeginTextureMode(gDofRT);
        BeginShaderMode(gDof.sh);
        setTex(&gDof, P_SRC, scene->texture);
        setF(&gDof, P_FOCUS, g->cam.focus);
        setF(&gDof, P_APERTURE, g->cam.aperture * (gCfg.intH / 1080.0f));
        setV2(&gDof, P_RES, (float)gCfg.intW, (float)gCfg.intH);
        FullscreenQuad(gCfg.intW, gCfg.intH);
        EndShaderMode();
        EndTextureMode();
        src = &gDofRT;
    }

    /* bloom: bright pass then a mip chain down and back up */
    BeginTextureMode(gBloom[0]);
    BeginShaderMode(gBright.sh);
    setTex(&gBright, P_SRC, src->texture);
    setV2(&gBright, P_SRCRES, (float)gCfg.intW, (float)gCfg.intH);
    setV2(&gBright, P_RES, (float)gBloom[0].texture.width, (float)gBloom[0].texture.height);
    FullscreenQuad(gBloom[0].texture.width, gBloom[0].texture.height);
    EndShaderMode();
    EndTextureMode();
    for (int i = 1; i < BLOOM_LEVELS; i++) {
        BeginTextureMode(gBloom[i]);
        BeginShaderMode(gDown.sh);
        setTex(&gDown, P_SRC, gBloom[i - 1].texture);
        setV2(&gDown, P_SRCRES, (float)gBloom[i - 1].texture.width, (float)gBloom[i - 1].texture.height);
        setV2(&gDown, P_RES, (float)gBloom[i].texture.width, (float)gBloom[i].texture.height);
        FullscreenQuad(gBloom[i].texture.width, gBloom[i].texture.height);
        EndShaderMode();
        EndTextureMode();
    }
    for (int i = BLOOM_LEVELS - 2; i >= 0; i--) {
        BeginTextureMode(gBloom[i]);
        BeginShaderMode(gUp.sh);
        setTex(&gUp, P_SRC, gBloom[i + 1].texture);
        setV2(&gUp, P_SRCRES, (float)gBloom[i + 1].texture.width, (float)gBloom[i + 1].texture.height);
        setV2(&gUp, P_RES, (float)gBloom[i].texture.width, (float)gBloom[i].texture.height);
        rlSetBlendFactors(GL_ONE, GL_ONE, GL_FUNC_ADD);
        rlSetBlendMode(RL_BLEND_CUSTOM);
        DrawRectangle(0, 0, gBloom[i].texture.width, gBloom[i].texture.height, WHITE);
        rlDrawRenderBatchActive();
        rlSetBlendMode(RL_BLEND_ALPHA);
        EndShaderMode();
        EndTextureMode();
    }

    /* god rays from the sun through gaps in the scene */
    Camera3D cam = WorldCamera(g);
    Vector3 fwd = v3norm(v3sub(cam.target, cam.position));
    Vector3 right = v3norm(v3cross(fwd, cam.up));
    Vector3 up = v3cross(right, fwd);
    Vector3 sd = g->light.sunDir;
    float sf = v3dot(sd, fwd);
    float th = tanf(g->cam.fov * DEG2RAD * 0.5f);
    float aspect = (float)gCfg.intW / gCfg.intH;
    Vector2 sunUV = { 0.5f, 0.5f };
    float rayStr = 0.0f;
    if (sf > 0.05f) {
        sunUV.x = 0.5f + 0.5f * (v3dot(sd, right) / sf) / (th * aspect);
        sunUV.y = 0.5f + 0.5f * (v3dot(sd, up) / sf) / th;
        rayStr = g->godrays * saturate((sf - 0.05f) * 3.0f) * smooth(-0.05f, 0.08f, sd.y);
    }
    BeginTextureMode(gRaysRT);
    BeginShaderMode(gRays.sh);
    setTex(&gRays, P_SRC, scene->texture);
    setV2(&gRays, P_SUN, sunUV.x, sunUV.y);
    setF(&gRays, P_STRENGTH, rayStr);
    setV3(&gRays, P_GRADE, g->light.sunCol);
    setV2(&gRays, P_RES, (float)gRaysRT.texture.width, (float)gRaysRT.texture.height);
    FullscreenQuad(gRaysRT.texture.width, gRaysRT.texture.height);
    EndShaderMode();
    EndTextureMode();

    /* final composite, tone mapping and grading */
    BeginTextureMode(gOut);
    BeginShaderMode(gFinal.sh);
    setTex(&gFinal, P_SRC, src->texture);
    setTex(&gFinal, P_SRC2, gBloom[0].texture);
    setTex(&gFinal, P_SRC3, gRaysRT.texture);
    setV2(&gFinal, P_SRCRES, (float)gCfg.intW, (float)gCfg.intH);
    setV2(&gFinal, P_RES, (float)gCfg.outW, (float)gCfg.outH);
    setF(&gFinal, P_EXPOSURE, g->exposure);
    setV3(&gFinal, P_GRADE, g->grade);
    setF(&gFinal, P_SAT, g->saturation);
    setF(&gFinal, P_CONTRAST, g->contrast);
    setF(&gFinal, P_VIGNETTE, g->vignette);
    setF(&gFinal, P_WARMTH, g->warmth);
    setF(&gFinal, P_FADE, f->fade);
    setF(&gFinal, P_WHITE, f->whiteFade);
    setF(&gFinal, P_TIME, (float)gFrameCounter);
    setF(&gFinal, P_BLOOMSTR, g->bloom);
    setF(&gFinal, P_RAYSTR, 1.0f);
    setF(&gFinal, P_SCALE, (float)gCfg.intW / gCfg.outW);
    FullscreenQuad(gCfg.outW, gCfg.outH);
    EndShaderMode();
    DrawOverlay(f);
    EndTextureMode();
    return &gOut;
}
