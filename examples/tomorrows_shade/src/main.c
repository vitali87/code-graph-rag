/*
 * Tomorrow's Shade
 * A short animated film about planting trees whose shade you will never sit in.
 *
 * Everything — land, sky, the oak, the people, the music — is generated
 * procedurally at run time. raylib provides the window, the GPU pipeline,
 * text, and audio streaming; the world itself is raymarched in shaders/scene.fs.
 *
 *   ./tomorrows_shade                       play the film
 *   ./tomorrows_shade --record film.mp4     render a video with ffmpeg
 *   ./tomorrows_shade --still 42.0 a.png    render a single frame
 *
 * Options: --size WxH (output), --ss F (supersampling), --fps N,
 *          --from T, --to T, --scale F (real-time render scale)
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "audio.h"
#include "raylib.h"
#include "render.h"
#include "rlgl.h"
#include "story.h"
#include "tree.h"
#include "world.h"

typedef struct {
    const char *record;
    const char *stillPath[64];
    float stillTime[64];
    int stills;
    int w, h;
    float ss;
    float scale;
    float fps;
    float from, to;
    int noAudio;
} Options;

static double Now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

static const char *BaseDir(void)
{
    if (FileExists("shaders/scene.fs")) return "";
    static char buf[1024];
    snprintf(buf, sizeof(buf), "%s", GetApplicationDirectory());
    return buf;
}

static void SavePNG(RenderTexture2D *rt, const char *path)
{
    Image img = LoadImageFromTexture(rt->texture);
    ImageFlipVertical(&img);
    ExportImage(img, path);
    UnloadImage(img);
}

static int Record(const Options *o)
{
    int W = o->w, H = o->h;
    float dur = StoryDuration();
    float t0 = o->from, t1 = o->to > 0 ? o->to : dur;
    int frames = (int)((t1 - t0) * o->fps + 0.5f);
    char wav[512] = { 0 };
    if (!o->noAudio) {
        snprintf(wav, sizeof(wav), "%s.wav", o->record);
        AudioBuild(dur);
        if (!AudioWriteWav(wav)) wav[0] = 0;
    }
    char cmd[2048];
    if (wav[0])
        snprintf(cmd, sizeof(cmd),
                 "ffmpeg -y -loglevel error -f rawvideo -pix_fmt rgba -s %dx%d -r %g -i - -ss %.3f -i \"%s\" "
                 "-vf vflip -c:v libx264 -preset slow -crf 16 -pix_fmt yuv420p -c:a aac -b:a 192k -shortest \"%s\"",
                 W, H, o->fps, t0, wav, o->record);
    else
        snprintf(cmd, sizeof(cmd),
                 "ffmpeg -y -loglevel error -f rawvideo -pix_fmt rgba -s %dx%d -r %g -i - "
                 "-vf vflip -c:v libx264 -preset slow -crf 16 -pix_fmt yuv420p \"%s\"",
                 W, H, o->fps, o->record);
    FILE *ff = popen(cmd, "w");
    if (!ff) {
        fprintf(stderr, "cannot start ffmpeg\n");
        return 1;
    }
    double start = Now();
    Frame f;
    for (int i = 0; i < frames; i++) {
        float t = t0 + i / o->fps;
        StoryEvaluate(t, &f);
        RenderTexture2D *rt = RenderFrame(&f);
        unsigned char *px = rlReadTexturePixels(rt->texture.id, W, H, rt->texture.format);
        fwrite(px, 1, (size_t)W * H * 4, ff);
        RL_FREE(px);
        if (i % 10 == 0 || i == frames - 1) {
            double el = Now() - start;
            double eta = el / (i + 1) * (frames - i - 1);
            fprintf(stderr, "\rframe %d/%d  t=%.2fs  %.2fs/frame  eta %dm%02ds   ", i + 1, frames, t, el / (i + 1), (int)eta / 60, (int)eta % 60);
            fflush(stderr);
        }
    }
    pclose(ff);
    if (wav[0]) remove(wav);
    fprintf(stderr, "\nwrote %s\n", o->record);
    return 0;
}

static void Play(const Options *o)
{
    float dur = StoryDuration();
    float t = o->from;
    int paused = 0;
    if (!o->noAudio) {
        BeginDrawing();
        ClearBackground(BLACK);
        DrawText("composing the score...", 24, GetScreenHeight() - 40, 20, (Color){ 200, 190, 170, 255 });
        EndDrawing();
        AudioBuild(dur);
        AudioStart(t);
    }
    Frame f;
    while (!WindowShouldClose()) {
        if (IsKeyPressed(KEY_SPACE)) { paused = !paused; AudioPause(paused); }
        if (IsKeyPressed(KEY_RIGHT)) { t += 10.0f; AudioSeek(t); }
        if (IsKeyPressed(KEY_LEFT)) { t -= 10.0f; if (t < 0) t = 0; AudioSeek(t); }
        if (IsKeyPressed(KEY_R)) { t = 0; AudioSeek(t); }
        if (IsKeyPressed(KEY_F) || IsKeyPressed(KEY_F11)) ToggleFullscreen();
        if (!paused) {
            /* follow the music when it is playing so picture and sound stay in sync */
            float at = AudioTime();
            if (at >= 0.0f && at < dur) t = at;
            else t += GetFrameTime() > 0.1f ? 0.1f : GetFrameTime();
        }
        if (t > dur + 2.0f) { t = 0; AudioSeek(t); }
        StoryEvaluate(t, &f);
        RenderTexture2D *rt = RenderFrame(&f);
        BeginDrawing();
        ClearBackground(BLACK);
        int sw = GetScreenWidth(), sh = GetScreenHeight();
        float sc = (float)sw / rt->texture.width;
        if (rt->texture.height * sc > sh) sc = (float)sh / rt->texture.height;
        float dw = rt->texture.width * sc, dh = rt->texture.height * sc;
        DrawTexturePro(rt->texture, (Rectangle){ 0, 0, (float)rt->texture.width, -(float)rt->texture.height },
                       (Rectangle){ (sw - dw) * 0.5f, (sh - dh) * 0.5f, dw, dh }, (Vector2){ 0, 0 }, 0, WHITE);
        if (paused) {
            DrawRectangle(0, sh - 6, sw, 6, (Color){ 40, 40, 40, 200 });
            DrawRectangle(0, sh - 6, (int)(sw * t / dur), 6, (Color){ 230, 200, 140, 255 });
            DrawText("paused - space: play  left/right: seek  r: restart  f: fullscreen", 12, sh - 30, 18, (Color){ 230, 220, 200, 220 });
        }
        EndDrawing();
    }
    AudioStop();
}

int main(int argc, char **argv)
{
    Options o = { 0 };
    o.w = 1280;
    o.h = 720;
    o.ss = 1.0f;
    o.scale = 1.0f;
    o.fps = 24.0f;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--record") && i + 1 < argc) o.record = argv[++i];
        else if (!strcmp(argv[i], "--still") && i + 2 < argc && o.stills < 64) {
            o.stillTime[o.stills] = (float)atof(argv[++i]);
            o.stillPath[o.stills++] = argv[++i];
        } else if (!strcmp(argv[i], "--size") && i + 1 < argc) sscanf(argv[++i], "%dx%d", &o.w, &o.h);
        else if (!strcmp(argv[i], "--ss") && i + 1 < argc) o.ss = (float)atof(argv[++i]);
        else if (!strcmp(argv[i], "--scale") && i + 1 < argc) o.scale = (float)atof(argv[++i]);
        else if (!strcmp(argv[i], "--fps") && i + 1 < argc) o.fps = (float)atof(argv[++i]);
        else if (!strcmp(argv[i], "--from") && i + 1 < argc) o.from = (float)atof(argv[++i]);
        else if (!strcmp(argv[i], "--to") && i + 1 < argc) o.to = (float)atof(argv[++i]);
        else if (!strcmp(argv[i], "--no-audio")) o.noAudio = 1;
        else {
            printf("usage: %s [--record out.mp4] [--still T out.png] [--size WxH] [--ss F] [--scale F] [--fps N] [--from T] [--to T] [--no-audio]\n", argv[0]);
            return 1;
        }
    }
    int offline = o.record || o.stills;
    SetTraceLogLevel(LOG_WARNING);
    if (offline) SetConfigFlags(FLAG_WINDOW_HIDDEN);
    else SetConfigFlags(FLAG_WINDOW_RESIZABLE | FLAG_VSYNC_HINT);
    InitWindow(offline ? 320 : o.w, offline ? 180 : o.h, "Tomorrow's Shade");
    SetExitKey(KEY_ESCAPE);

    RenderConfig cfg;
    cfg.outW = o.w;
    cfg.outH = o.h;
    float internal = offline ? o.ss : o.scale;
    cfg.intW = (int)(o.w * internal);
    cfg.intH = (int)(o.h * internal);
    RenderInit(cfg, BaseDir());
    StoryInit();

    int rc = 0;
    if (o.stills) {
        Frame f;
        for (int i = 0; i < o.stills; i++) {
            double s = Now();
            StoryEvaluate(o.stillTime[i], &f);
            RenderTexture2D *rt = RenderFrame(&f);
            SavePNG(rt, o.stillPath[i]);
            fprintf(stderr, "still t=%.2f -> %s (%.2fs)\n", o.stillTime[i], o.stillPath[i], Now() - s);
        }
    } else if (o.record) {
        rc = Record(&o);
    } else {
        Play(&o);
    }
    RenderShutdown();
    CloseWindow();
    return rc;
}
