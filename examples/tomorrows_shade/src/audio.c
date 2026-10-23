#include "audio.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "raylib.h"
#include "story.h"

/* The score: a small additive piano, a string pad, a celesta, a cello and a
 * reverb, sequenced against the story's shot times, plus wind, birds,
 * crickets and water. Everything is rendered once into a stereo buffer. */

#define SR 44100
#define PI2 6.28318530718f

static float *gL, *gR;          /* dry/wet mix, then interleaved into gOut */
static float *gOut;
static int gFrames;
static AudioStream gStream;
static volatile int gPos;
static volatile int gPaused = 1;
static int gStreamReady;
static unsigned gRng = 0x1234567u;

static float frand(void) { gRng = gRng * 1664525u + 1013904223u; return (float)(gRng >> 8) / 16777216.0f; }
static float mtof(float m) { return 440.0f * powf(2.0f, (m - 69.0f) / 12.0f); }
static float sat(float x) { return x < 0 ? 0 : (x > 1 ? 1 : x); }
static float sstep(float a, float b, float x) { float t = sat((x - a) / (b - a)); return t * t * (3 - 2 * t); }

static void mixAt(int i, float l, float r)
{
    if (i >= 0 && i < gFrames) { gL[i] += l; gR[i] += r; }
}

/* ------------------------------------------------------------------------ */
/* instruments                                                               */

static void Piano(float t0, float dur, int midi, float vel)
{
    float f0 = mtof((float)midi);
    float t60 = 7.5f * powf(0.5f, (midi - 48) / 14.0f);          /* bass rings longer */
    if (t60 > 9.0f) t60 = 9.0f;
    if (t60 < 1.2f) t60 = 1.2f;
    float len = fminf(dur + 0.35f, t60 * 0.8f) + 0.3f;
    int n0 = (int)(t0 * SR), n = (int)(len * SR);
    float pan = sat(0.5f + (midi - 62) / 60.0f);
    float gl = cosf(pan * 1.5708f), gr = sinf(pan * 1.5708f);
    const int H = 7;
    float cr[H], ci[H], wr[H], wi[H], amp[H], dec[H];
    float bright = 0.35f + 0.65f * vel;
    for (int k = 0; k < H; k++) {
        float h = (float)(k + 1);
        float fk = f0 * h * sqrtf(1.0f + 0.00035f * h * h) * (1.0f + (frand() - 0.5f) * 0.0006f);
        if (fk > 15000.0f) { amp[k] = 0; wr[k] = 1; wi[k] = 0; cr[k] = 1; ci[k] = 0; dec[k] = 1; continue; }
        float w = PI2 * fk / SR;
        wr[k] = cosf(w); wi[k] = sinf(w);
        float ph = frand() * PI2;
        cr[k] = cosf(ph); ci[k] = sinf(ph);
        amp[k] = powf(h, -1.15f) * (k == 0 ? 1.0f : powf(bright, (float)k * 0.55f));
        float tk = t60 / (1.0f + 0.55f * k);
        dec[k] = expf(-6.9f / (tk * SR));
    }
    float relStart = (dur + 0.25f) * SR, relRate = expf(-6.9f / (0.35f * SR));
    float env = 1.0f, rel = 1.0f;
    float gain = 0.16f * vel * powf(f0 / 262.0f, -0.25f);
    float nz = 0.0f;
    for (int i = 0; i < n; i++) {
        float s = 0.0f;
        for (int k = 0; k < H; k++) {
            float nr = cr[k] * wr[k] - ci[k] * wi[k];
            ci[k] = cr[k] * wi[k] + ci[k] * wr[k];
            cr[k] = nr;
            s += ci[k] * amp[k];
            amp[k] *= dec[k];
        }
        if ((i & 1023) == 0) {
            /* renormalise the rotating phasors */
            for (int k = 0; k < H; k++) { float m = 1.0f / sqrtf(cr[k] * cr[k] + ci[k] * ci[k]); cr[k] *= m; ci[k] *= m; }
        }
        float att = i < 120 ? i / 120.0f : 1.0f;
        if (i > relStart) rel *= relRate;
        /* felt hammer thump */
        if (i < 600) { nz = nz * 0.9f + (frand() - 0.5f) * 0.1f; s += nz * (1.0f - i / 600.0f) * 0.6f; }
        float v = s * att * env * rel * gain;
        mixAt(n0 + i, v * gl, v * gr);
        if (rel < 0.0005f) break;
    }
}

static float polyblep(float t, float dt)
{
    if (t < dt) { t /= dt; return t + t - t * t - 1.0f; }
    if (t > 1.0f - dt) { t = (t - 1.0f) / dt; return t * t + t + t + 1.0f; }
    return 0.0f;
}

/* bowed string section voice: detuned saws, gentle vibrato, low-pass */
static void Strings(float t0, float t1, int midi, float level, float attack, float release, float bright)
{
    float f0 = mtof((float)midi);
    int n0 = (int)(t0 * SR), n = (int)((t1 - t0 + release) * SR);
    float ph[3] = { frand(), frand(), frand() };
    float det[3] = { 1.0f, 1.0045f, 0.9958f };
    float lp1 = 0, lp2 = 0;
    float cutoff = 900.0f + 2600.0f * bright + f0 * 1.5f;
    float a = 1.0f - expf(-PI2 * cutoff / SR);
    float pan = sat(0.5f + (frand() - 0.5f) * 0.6f);
    float gl = cosf(pan * 1.5708f), gr = sinf(pan * 1.5708f);
    float vibPh = frand() * PI2;
    for (int i = 0; i < n; i++) {
        float t = (float)i / SR;
        float env = sstep(0.0f, attack, t) * (1.0f - sstep(t1 - t0, t1 - t0 + release, t));
        if (env <= 0.0f && t > attack) continue;
        float vib = 1.0f + 0.0035f * sinf(vibPh + t * 5.2f * PI2) * sstep(0.3f, 1.5f, t);
        float s = 0.0f;
        for (int k = 0; k < 3; k++) {
            float dt = f0 * det[k] * vib / SR;
            ph[k] += dt;
            if (ph[k] >= 1.0f) ph[k] -= 1.0f;
            s += (2.0f * ph[k] - 1.0f) - polyblep(ph[k], dt);
        }
        lp1 += a * (s - lp1);
        lp2 += a * (lp1 - lp2);
        float v = lp2 * env * level * 0.035f;
        mixAt(n0 + i, v * gl, v * gr);
    }
}

/* celesta / music box: FM bell */
static void Bell(float t0, int midi, float vel)
{
    float f = mtof((float)midi);
    int n0 = (int)(t0 * SR), n = (int)(2.8f * SR);
    float pan = frand();
    float gl = cosf(pan * 1.5708f), gr = sinf(pan * 1.5708f);
    float pc = 0, pm = 0;
    for (int i = 0; i < n; i++) {
        float t = (float)i / SR;
        float idx = 2.2f * expf(-t * 6.0f);
        pm += PI2 * f * 3.5f / SR;
        pc += PI2 * f / SR;
        float s = sinf(pc + idx * sinf(pm));
        float env = expf(-t * 2.4f) * (i < 60 ? i / 60.0f : 1.0f);
        float v = s * env * vel * 0.05f;
        mixAt(n0 + i, v * gl, v * gr);
    }
}

/* ------------------------------------------------------------------------ */
/* harmony                                                                   */

typedef struct { int n; int notes[6]; } Chord;

static const Chord C_D = { 5, { 50, 57, 62, 66, 69 } };
static const Chord C_Acs = { 5, { 49, 57, 61, 64, 69 } };
static const Chord C_Bm = { 5, { 47, 54, 59, 62, 66 } };
static const Chord C_G = { 5, { 43, 50, 55, 59, 62 } };
static const Chord C_Dfs = { 5, { 42, 57, 62, 66, 69 } };
static const Chord C_Em7 = { 5, { 40, 47, 55, 59, 62 } };
static const Chord C_Asus = { 5, { 45, 52, 57, 62, 64 } };
static const Chord C_A = { 5, { 45, 52, 57, 61, 64 } };
static const Chord C_Fsm = { 5, { 42, 49, 54, 57, 61 } };

static const Chord *PROG_A[8] = { &C_D, &C_Acs, &C_Bm, &C_G, &C_Dfs, &C_Em7, &C_Asus, &C_D };
static const Chord *PROG_B[8] = { &C_G, &C_Dfs, &C_Em7, &C_A, &C_Bm, &C_Fsm, &C_G, &C_Asus };
static const Chord *PROG_M[4] = { &C_Bm, &C_G, &C_D, &C_A };

/* the theme, in beats over PROG_A: {bar, beat, length, midi} */
typedef struct { float bar, beat, len; int midi; } MNote;
static const MNote THEME[] = {
    { 0, 0, 2, 78 }, { 0, 2, 1, 76 }, { 0, 3, 1, 74 },
    { 1, 0, 3, 76 }, { 1, 3, 1, 69 },
    { 2, 0, 2, 74 }, { 2, 2, 1, 73 }, { 2, 3, 1, 71 },
    { 3, 0, 3, 69 }, { 3, 3, 1, 74 },
    { 4, 0, 1, 66 }, { 4, 1, 1, 67 }, { 4, 2, 1, 69 }, { 4, 3, 1, 74 },
    { 5, 0, 2, 71 }, { 5, 2, 1, 69 }, { 5, 3, 1, 67 },
    { 6, 0, 2, 76 }, { 6, 2, 1, 74 }, { 6, 3, 1, 73 },
    { 7, 0, 4, 74 },
};
#define NTHEME ((int)(sizeof(THEME) / sizeof(THEME[0])))

typedef struct {
    float t0, t1;
    const Chord **prog;
    int progLen;
    float bpm;
    float arp;       /* 0 none, 1 = quarter notes, 2 = eighths */
    int melody;      /* play the theme */
    float melOct;    /* octave shift of the melody */
    float strings;   /* level at start */
    float strings1;  /* level at end */
    float bells;
    float bass;
    float vol;
    float vol1;
    float hold;      /* sustained block chords instead of arpeggios */
} Section;

static void Arrange(const Section *s)
{
    float beat = 60.0f / s->bpm;
    float bar = beat * 4.0f;
    int nbars = (int)ceilf((s->t1 - s->t0) / bar);
    for (int b = 0; b < nbars; b++) {
        float tb = s->t0 + b * bar;
        if (tb >= s->t1) break;
        const Chord *c = s->prog[b % s->progLen];
        float u = (tb - s->t0) / fmaxf(s->t1 - s->t0, 1e-3f);
        float vol = s->vol + (s->vol1 - s->vol) * u;
        float barEnd = fminf(tb + bar, s->t1);
        /* piano accompaniment */
        if (s->hold > 0.0f) {
            for (int k = 0; k < c->n; k++) Piano(tb + k * 0.025f, bar * 0.95f, c->notes[k], vol * (k == 0 ? 0.55f : 0.38f) * s->hold);
        } else if (s->arp > 0.0f) {
            int steps = s->arp >= 2.0f ? 8 : 4;
            static const int pat[8] = { 0, 2, 3, 4, 3, 2, 4, 3 };
            for (int k = 0; k < steps; k++) {
                float tt = tb + k * bar / steps;
                if (tt >= s->t1) break;
                int idx = steps == 8 ? pat[k] : (k == 0 ? 0 : pat[k * 2 - 1]);
                int note = c->notes[idx] + (idx == 0 ? 0 : 12);
                float v = vol * (k == 0 ? 0.6f : 0.36f + 0.06f * frand());
                Piano(tt + (frand() - 0.5f) * 0.012f, k == 0 ? bar : bar / steps * 2.2f, note, v);
            }
        }
        /* bass */
        if (s->bass > 0.0f) Strings(tb, barEnd, c->notes[0] - 12, s->bass * vol * 1.4f, 0.4f, 1.0f, 0.1f);
        /* strings */
        float sl = s->strings + (s->strings1 - s->strings) * u;
        if (sl > 0.01f) {
            for (int k = 1; k < c->n; k++) Strings(tb - 0.05f, barEnd + 0.1f, c->notes[k] + 12 * (k > 2), sl * vol, bar * 0.45f, bar * 0.6f, 0.25f + 0.5f * sl);
        }
        /* celesta sparkles */
        if (s->bells > 0.0f) {
            for (int k = 0; k < 8; k++) {
                if (frand() < s->bells * 0.45f) {
                    int note = c->notes[1 + (int)(frand() * (c->n - 1))] + 24;
                    Bell(tb + k * bar / 8.0f, note, vol * (0.5f + 0.5f * frand()));
                }
            }
        }
        /* melody */
        if (s->melody) {
            int themeBar = b % 8;
            for (int k = 0; k < NTHEME; k++) {
                if ((int)THEME[k].bar != themeBar) continue;
                float tt = tb + THEME[k].beat * beat;
                if (tt >= s->t1 - 0.2f) continue;
                Piano(tt + 0.01f, THEME[k].len * beat, THEME[k].midi + (int)(s->melOct * 12), vol * 0.85f);
            }
        }
    }
}

/* ------------------------------------------------------------------------ */
/* ambience                                                                  */

static void Wind(float t0, float t1, float level, float gust)
{
    int n0 = (int)(t0 * SR), n1 = (int)(t1 * SR);
    float aL = 0, bL = 0, aR = 0, bR = 0;
    for (int i = n0; i < n1 && i < gFrames; i++) {
        float t = (float)i / SR;
        float fade = sstep(t0, t0 + 2.0f, t) * (1.0f - sstep(t1 - 2.0f, t1, t));
        float g = 0.5f + 0.5f * sinf(t * 0.37f) * sinf(t * 0.23f + 1.0f) * gust;
        /* airy band of noise whose centre rises with each gust */
        float hi = 0.05f + 0.06f * g, lo = 0.012f;
        float wl = frand() * 2.0f - 1.0f, wr = frand() * 2.0f - 1.0f;
        aL += hi * (wl - aL); bL += lo * (aL - bL);
        aR += hi * (wr - aR); bR += lo * (aR - bR);
        float v = level * fade * (0.25f + 0.75f * g) * 0.22f;
        mixAt(i, (aL - bL) * v, (aR - bR) * v);
    }
}

static void Bird(float t0, float pan, float pitch)
{
    int n0 = (int)(t0 * SR);
    int notes = 2 + (int)(frand() * 5);
    float t = 0.0f;
    float gl = cosf(pan * 1.5708f), gr = sinf(pan * 1.5708f);
    for (int k = 0; k < notes; k++) {
        float dur = 0.05f + 0.09f * frand();
        float f0 = pitch * (0.85f + 0.4f * frand()), f1 = f0 * (0.7f + 0.7f * frand());
        int n = (int)(dur * SR);
        float ph = 0.0f;
        for (int i = 0; i < n; i++) {
            float u = (float)i / n;
            float f = f0 + (f1 - f0) * u + 300.0f * sinf(u * 40.0f);
            ph += PI2 * f / SR;
            float env = sinf(u * 3.14159f);
            float v = sinf(ph) * env * env * 0.012f;
            mixAt(n0 + (int)(t * SR) + i, v * gl, v * gr);
        }
        t += dur + 0.03f + 0.08f * frand();
    }
}

static void Birds(float t0, float t1, float density)
{
    for (float t = t0; t < t1; t += 0.4f + frand() * 2.5f / density) Bird(t, frand(), 2600.0f + 2400.0f * frand());
}

static void Crickets(float t0, float t1, float level)
{
    int n0 = (int)(t0 * SR), n1 = (int)(t1 * SR);
    float ph = 0.0f;
    for (int i = n0; i < n1 && i < gFrames; i++) {
        float t = (float)i / SR;
        float fade = sstep(t0, t0 + 3.0f, t) * (1.0f - sstep(t1 - 2.0f, t1, t));
        ph += PI2 * 4700.0f / SR;
        float chirp = fmodf(t * 2.6f, 1.0f) < 0.18f ? 1.0f : 0.0f;
        float pulse = 0.5f + 0.5f * sinf(t * PI2 * 55.0f);
        float v = sinf(ph) * chirp * pulse * level * fade * 0.01f;
        float v2 = sinf(ph * 1.07f + 1.0f) * (fmodf(t * 2.3f + 0.4f, 1.0f) < 0.15f ? 1.0f : 0.0f) * pulse * level * fade * 0.007f;
        mixAt(i, v + v2 * 0.3f, v * 0.4f + v2);
    }
}

static void Water(float t0, float t1, float level)
{
    int n0 = (int)(t0 * SR), n1 = (int)(t1 * SR);
    float hp = 0, lp = 0;
    for (int i = n0; i < n1 && i < gFrames; i++) {
        float t = (float)i / SR;
        float fade = sstep(t0, t0 + 0.4f, t) * (1.0f - sstep(t1 - 0.5f, t1, t));
        float wn = frand() * 2.0f - 1.0f;
        lp += 0.12f * (wn - lp);
        hp += 0.02f * (lp - hp);
        float bub = 0.6f + 0.4f * sinf(t * 37.0f + sinf(t * 13.0f) * 3.0f);
        float v = (lp - hp) * fade * level * bub * 0.06f;
        mixAt(i, v, v * 0.9f);
    }
}

/* ------------------------------------------------------------------------ */
/* reverb (Freeverb)                                                         */

typedef struct { float *buf; int len, idx; float store; } Comb;
typedef struct { float *buf; int len, idx; } Allpass;

static float CombTick(Comb *c, float in, float fb, float damp)
{
    float out = c->buf[c->idx];
    c->store = out * (1.0f - damp) + c->store * damp;
    c->buf[c->idx] = in + c->store * fb;
    if (++c->idx >= c->len) c->idx = 0;
    return out;
}

static float AllpassTick(Allpass *a, float in)
{
    float b = a->buf[a->idx];
    float out = -in + b;
    a->buf[a->idx] = in + b * 0.5f;
    if (++a->idx >= a->len) a->idx = 0;
    return out;
}

static void Reverb(float wet)
{
    static const int ct[8] = { 1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617 };
    static const int at[4] = { 556, 441, 341, 225 };
    Comb cl[8], cr[8];
    Allpass al[4], ar[4];
    for (int i = 0; i < 8; i++) {
        cl[i] = (Comb){ calloc(ct[i], sizeof(float)), ct[i], 0, 0 };
        cr[i] = (Comb){ calloc(ct[i] + 23, sizeof(float)), ct[i] + 23, 0, 0 };
    }
    for (int i = 0; i < 4; i++) {
        al[i] = (Allpass){ calloc(at[i], sizeof(float)), at[i], 0 };
        ar[i] = (Allpass){ calloc(at[i] + 23, sizeof(float)), at[i] + 23, 0 };
    }
    const float fb = 0.86f, damp = 0.35f;
    for (int n = 0; n < gFrames; n++) {
        float in = (gL[n] + gR[n]) * 0.015f;
        float ol = 0, orr = 0;
        for (int i = 0; i < 8; i++) { ol += CombTick(&cl[i], in, fb, damp); orr += CombTick(&cr[i], in, fb, damp); }
        for (int i = 0; i < 4; i++) { ol = AllpassTick(&al[i], ol); orr = AllpassTick(&ar[i], orr); }
        gL[n] = gL[n] * (1.0f - wet * 0.4f) + ol * wet * 3.0f;
        gR[n] = gR[n] * (1.0f - wet * 0.4f) + orr * wet * 3.0f;
    }
    for (int i = 0; i < 8; i++) { free(cl[i].buf); free(cr[i].buf); }
    for (int i = 0; i < 4; i++) { free(al[i].buf); free(ar[i].buf); }
}

/* ------------------------------------------------------------------------ */

static void Compose(void)
{
#define T(s) StoryShotStart(s)
    float end = T(SH_COUNT);
    const float bpm = 64.0f;
    Section secs[] = {
        /* dawn and the walk: solo piano, the theme enters as they climb */
        { 0.5f, T(SH_ARRIVE), PROG_A, 8, bpm, 2, 1, -1, 0.0f, 0.0f, 0, 0, 0.55f, 0.7f, 0 },
        /* the hilltop, digging, the acorn */
        { T(SH_ARRIVE), T(SH_MIRA), PROG_B, 8, bpm, 1, 0, 0, 0.0f, 0.35f, 0, 0, 0.55f, 0.6f, 0 },
        /* the conversation: held chords, strings rise on "It isn't for me" */
        { T(SH_MIRA), T(SH_PLANT), PROG_A, 8, bpm, 0, 0, 0, 0.35f, 0.9f, 0, 0.3f, 0.5f, 0.75f, 0.6f },
        { T(SH_PLANT), T(SH_SPROUT), PROG_A, 8, bpm, 1, 1, -1, 0.5f, 0.4f, 0, 0.3f, 0.65f, 0.6f, 0 },
        /* the first shoot: celesta */
        { T(SH_SPROUT), T(SH_YEARS), PROG_A + 7, 1, bpm, 0, 0, 0, 0.6f, 0.6f, 1.0f, 0, 0.6f, 0.6f, 0 },
        /* years: arpeggios turning like seasons */
        { T(SH_YEARS), T(SH_SUNSET), PROG_B, 8, bpm, 2, 0, 0, 0.45f, 0.65f, 0.3f, 0.4f, 0.6f, 0.7f, 0 },
        /* sunset and the talk on the bench: the theme, full */
        { T(SH_SUNSET), T(SH_DUSK), PROG_A, 8, bpm, 2, 1, 0, 0.6f, 0.9f, 0, 0.6f, 0.7f, 0.8f, 0 },
        /* dusk */
        { T(SH_DUSK), T(SH_EMPTY) - 1.0f, PROG_A + 6, 2, bpm, 0, 0, 0, 0.8f, 0.0f, 0.2f, 0.3f, 0.6f, 0.3f, 0.5f },
        /* the empty bench: sparse, minor */
        { T(SH_EMPTY) + 1.0f, T(SH_GROW), PROG_M, 4, bpm * 0.8f, 1, 0, 0, 0.0f, 0.2f, 0, 0, 0.42f, 0.5f, 0 },
        /* decades pass: a long crescendo resolving into the summer morning */
        { T(SH_GROW), T(SH_RETURN), PROG_M, 4, bpm, 2, 0, 0, 0.2f, 1.0f, 0.6f, 0.7f, 0.5f, 1.0f, 0 },
        /* the return: the opening theme, now with strings */
        { T(SH_RETURN), T(SH_SHADE), PROG_A, 8, bpm, 2, 1, -1, 0.5f, 0.5f, 0.1f, 0.4f, 0.75f, 0.7f, 0 },
        /* the shade, the acorn found, the words come back */
        { T(SH_SHADE), T(SH_GIFT), PROG_B, 8, bpm, 1, 0, 0, 0.35f, 0.45f, 0.2f, 0.3f, 0.55f, 0.6f, 0 },
        { T(SH_GIFT), T(SH_PLANT2), PROG_A, 8, bpm, 0, 1, 0, 0.45f, 0.85f, 0.2f, 0.4f, 0.6f, 0.8f, 0.5f },
        /* planting again and the camera rising: everything together */
        { T(SH_PLANT2), T(SH_END), PROG_A, 8, bpm, 2, 1, 0, 0.7f, 1.0f, 0.5f, 0.8f, 0.75f, 1.0f, 0 },
        /* end card: a last phrase */
        { T(SH_END), end - 1.0f, PROG_A + 4, 4, bpm * 0.85f, 1, 0, 0, 0.5f, 0.0f, 0.3f, 0.2f, 0.55f, 0.3f, 0 },
    };
    for (size_t i = 0; i < sizeof(secs) / sizeof(secs[0]); i++) Arrange(&secs[i]);
    /* a final low D under the end card */
    Piano(end - 9.0f, 7.0f, 38, 0.5f);
    Piano(end - 9.0f, 7.0f, 50, 0.4f);

    /* ambience */
    Wind(0.0f, T(SH_DIG), 0.5f, 1.0f);
    Wind(T(SH_DIG), T(SH_SUNSET), 0.25f, 0.6f);
    Wind(T(SH_SUNSET), T(SH_EMPTY), 0.18f, 0.5f);
    Wind(T(SH_EMPTY), T(SH_GROW) + 4.0f, 0.9f, 1.0f);
    Wind(T(SH_GROW), T(SH_RETURN), 0.35f, 1.0f);
    Wind(T(SH_RETURN), end, 0.2f, 0.6f);
    Birds(T(SH_DAWN) + 4.0f, T(SH_SPROUT), 0.8f);
    Birds(T(SH_YEARS) + 2.0f, T(SH_YEARS) + 8.0f, 0.6f);
    Birds(T(SH_RETURN), T(SH_END) - 2.0f, 1.2f);
    Crickets(T(SH_DUSK) - 2.0f, T(SH_EMPTY), 1.0f);
    Water(T(SH_PLANT) + 4.6f, T(SH_PLANT) + 10.5f, 1.0f);
    Water(T(SH_PLANT2) + 4.6f, T(SH_PLANT2) + 9.0f, 1.0f);
#undef T
}

void AudioBuild(float duration)
{
    if (gOut) return;
    gFrames = (int)((duration + 2.0f) * SR);
    gL = calloc(gFrames, sizeof(float));
    gR = calloc(gFrames, sizeof(float));
    Compose();
    Reverb(0.32f);
    /* gentle master limiting and normalisation */
    float peak = 1e-6f;
    for (int i = 0; i < gFrames; i++) {
        gL[i] = tanhf(gL[i] * 1.2f);
        gR[i] = tanhf(gR[i] * 1.2f);
        peak = fmaxf(peak, fmaxf(fabsf(gL[i]), fabsf(gR[i])));
    }
    float g = 0.89f / peak;
    gOut = malloc(sizeof(float) * 2 * gFrames);
    for (int i = 0; i < gFrames; i++) {
        float fade = 1.0f;
        float t = (float)i / SR;
        if (t > duration - 3.0f) fade = sat((duration - t) / 3.0f);
        gOut[i * 2] = gL[i] * g * fade;
        gOut[i * 2 + 1] = gR[i] * g * fade;
    }
    free(gL);
    free(gR);
    gL = gR = NULL;
}

const float *AudioSamples(int *frameCount, int *sampleRate)
{
    *frameCount = gFrames;
    *sampleRate = SR;
    return gOut;
}

int AudioWriteWav(const char *path)
{
    if (!gOut) return 0;
    FILE *f = fopen(path, "wb");
    if (!f) return 0;
    int dataBytes = gFrames * 2 * 2;
    unsigned char hdr[44];
    memcpy(hdr, "RIFF", 4);
    int v = 36 + dataBytes;
    memcpy(hdr + 4, &v, 4);
    memcpy(hdr + 8, "WAVEfmt ", 8);
    v = 16; memcpy(hdr + 16, &v, 4);
    short s = 1; memcpy(hdr + 20, &s, 2);
    s = 2; memcpy(hdr + 22, &s, 2);
    v = SR; memcpy(hdr + 24, &v, 4);
    v = SR * 4; memcpy(hdr + 28, &v, 4);
    s = 4; memcpy(hdr + 32, &s, 2);
    s = 16; memcpy(hdr + 34, &s, 2);
    memcpy(hdr + 36, "data", 4);
    memcpy(hdr + 40, &dataBytes, 4);
    fwrite(hdr, 1, 44, f);
    short *pcm = malloc(sizeof(short) * 2 * 4096);
    for (int i = 0; i < gFrames * 2; i += 8192) {
        int n = gFrames * 2 - i < 8192 ? gFrames * 2 - i : 8192;
        for (int k = 0; k < n; k++) pcm[k] = (short)(fmaxf(-1.0f, fminf(1.0f, gOut[i + k])) * 32767.0f);
        fwrite(pcm, sizeof(short), n, f);
    }
    free(pcm);
    fclose(f);
    return 1;
}

static void StreamCallback(void *buffer, unsigned int frames)
{
    float *out = (float *)buffer;
    for (unsigned int i = 0; i < frames; i++) {
        if (gPaused || !gOut || gPos >= gFrames) { out[i * 2] = out[i * 2 + 1] = 0.0f; continue; }
        out[i * 2] = gOut[gPos * 2];
        out[i * 2 + 1] = gOut[gPos * 2 + 1];
        gPos++;
    }
}

void AudioStart(float atTime)
{
    if (!gStreamReady) {
        InitAudioDevice();
        if (!IsAudioDeviceReady()) return;
        SetAudioStreamBufferSizeDefault(4096);
        gStream = LoadAudioStream(SR, 32, 2);
        SetAudioStreamCallback(gStream, StreamCallback);
        PlayAudioStream(gStream);
        gStreamReady = 1;
    }
    AudioSeek(atTime);
    gPaused = 0;
}

void AudioPause(int paused) { gPaused = paused; }
void AudioSeek(float t) { int p = (int)(t * SR); gPos = p < 0 ? 0 : p; }
float AudioTime(void) { return gStreamReady ? (float)gPos / SR : -1.0f; }

void AudioStop(void)
{
    if (!gStreamReady) return;
    StopAudioStream(gStream);
    UnloadAudioStream(gStream);
    CloseAudioDevice();
    gStreamReady = 0;
}
