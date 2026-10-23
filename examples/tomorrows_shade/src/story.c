#include "story.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "character.h"
#include "tree.h"

/* ======================================================================== */
/* look development helpers                                                  */

typedef struct {
    float el;
    Vector3 sun, zen, hor, fog, amb;
    float fogDen;
} SkyKey;

static void SetLighting(World *w, float hour, float overcast)
{
    Lighting *L = &w->light;
    float a = (hour - 6.0f) / 12.0f * PI_F;
    float maxEl = 1.05f;
    float el = maxEl * sinf(a);
    if (hour < 6.0f || hour > 18.0f) el = 0.6f * sinf(a);
    L->sunDir = v3norm(v3(cosf(el) * cosf(a), sinf(el), cosf(el) * sinf(a) * 0.75f - 0.25f * cosf(el)));
    L->moonDir = v3norm(v3(-0.4f, 0.55f, -0.7f));
    static SkyKey keys[] = {
        { -0.35f, { 0, 0, 0 }, { 0.004f, 0.006f, 0.02f }, { 0.015f, 0.02f, 0.045f }, { 0.012f, 0.016f, 0.035f }, { 0.018f, 0.024f, 0.05f }, 0.004f },
        { -0.1f, { 0.0f, 0.0f, 0.0f }, { 0.03f, 0.045f, 0.13f }, { 0.35f, 0.18f, 0.16f }, { 0.12f, 0.1f, 0.14f }, { 0.06f, 0.06f, 0.1f }, 0.006f },
        { 0.0f, { 1.6f, 0.55f, 0.18f }, { 0.12f, 0.2f, 0.45f }, { 1.05f, 0.5f, 0.24f }, { 0.55f, 0.36f, 0.3f }, { 0.13f, 0.12f, 0.16f }, 0.007f },
        { 0.1f, { 2.7f, 1.3f, 0.55f }, { 0.12f, 0.25f, 0.6f }, { 1.0f, 0.66f, 0.42f }, { 0.66f, 0.52f, 0.45f }, { 0.19f, 0.21f, 0.3f }, 0.0032f },
        { 0.3f, { 3.0f, 2.15f, 1.3f }, { 0.1f, 0.27f, 0.72f }, { 0.72f, 0.74f, 0.76f }, { 0.58f, 0.64f, 0.74f }, { 0.24f, 0.32f, 0.46f }, 0.0022f },
        { 0.9f, { 3.3f, 3.0f, 2.6f }, { 0.07f, 0.22f, 0.7f }, { 0.52f, 0.66f, 0.88f }, { 0.5f, 0.62f, 0.82f }, { 0.25f, 0.34f, 0.52f }, 0.0018f },
    };
    int nk = sizeof(keys) / sizeof(keys[0]);
    int i = 0;
    while (i < nk - 2 && el > keys[i + 1].el) i++;
    float t = saturate((el - keys[i].el) / (keys[i + 1].el - keys[i].el));
    t = ease(t);
    SkyKey *k0 = &keys[i], *k1 = &keys[i + 1];
    L->sunCol = v3lerp(k0->sun, k1->sun, t);
    L->skyZen = v3lerp(k0->zen, k1->zen, t);
    L->skyHor = v3lerp(k0->hor, k1->hor, t);
    L->fogCol = v3lerp(k0->fog, k1->fog, t);
    L->amb = v3lerp(k0->amb, k1->amb, t);
    L->fogDen = mixf(k0->fogDen, k1->fogDen, t);
    L->night = 1.0f - smooth(-0.25f, -0.02f, el);
    L->overcast = overcast;
    L->cloud = 0.35f + 0.6f * overcast;
    if (overcast > 0.0f) {
        float lum = 0.25f + 0.75f * smooth(-0.1f, 0.4f, el);
        Vector3 grey = v3scale(v3(0.55f, 0.58f, 0.63f), lum);
        L->sunCol = v3scale(L->sunCol, 1.0f - 0.82f * overcast);
        L->skyZen = v3lerp(L->skyZen, v3scale(grey, 0.75f), overcast);
        L->skyHor = v3lerp(L->skyHor, grey, overcast);
        L->fogCol = v3lerp(L->fogCol, v3scale(grey, 0.9f), overcast);
        L->amb = v3lerp(L->amb, v3scale(grey, 0.75f), overcast);
        L->fogDen = mixf(L->fogDen, 0.009f, overcast);
    }
    w->hour = hour;
}

/* season: 0 spring, 1 summer, 2 autumn, 3 winter (cyclic) */
static void SetSeason(World *w, float season, float barren)
{
    float s = fmodf(season, 4.0f);
    if (s < 0) s += 4.0f;
    float leaf;
    if (s < 0.6f) leaf = mixf(0.35f, 1.0f, ease(s / 0.6f));
    else if (s < 2.0f) leaf = 1.0f;
    else if (s < 2.85f) leaf = mixf(1.0f, 0.15f, ease((s - 2.0f) / 0.85f));
    else if (s < 3.6f) leaf = 0.0f;
    else leaf = mixf(0.0f, 0.35f, ease((s - 3.6f) / 0.4f));
    w->leafAmt = leaf;

    Vector3 spring = rgb(0x7FAE3A), spring2 = rgb(0xB2C447);
    Vector3 summer = rgb(0x4A7A26), summer2 = rgb(0x6E9A32);
    Vector3 autumn = rgb(0xC9631C), autumn2 = rgb(0xE0A32A);
    Vector3 late = rgb(0x9B3A16), late2 = rgb(0xC27A22);
    Vector3 c1, c2;
    if (s < 0.8f) { c1 = v3lerp(spring, summer, ease(s / 0.8f)); c2 = v3lerp(spring2, summer2, ease(s / 0.8f)); }
    else if (s < 1.7f) { c1 = summer; c2 = summer2; }
    else if (s < 2.3f) { float u = ease((s - 1.7f) / 0.6f); c1 = v3lerp(summer, autumn, u); c2 = v3lerp(summer2, autumn2, u); }
    else if (s < 3.0f) { float u = ease((s - 2.3f) / 0.7f); c1 = v3lerp(autumn, late, u); c2 = v3lerp(autumn2, late2, u); }
    else { float u = ease((s - 3.6f) / 0.4f); c1 = v3lerp(late, spring, u); c2 = v3lerp(late2, spring2, u); }
    w->leafCol = c1;
    w->leafCol2 = c2;

    float snow = smooth(2.95f, 3.15f, s) * (1.0f - smooth(3.55f, 3.8f, s));
    w->snow = snow;
    float dry = 0.15f;
    if (s > 1.2f && s < 3.0f) dry = mixf(0.15f, 0.75f, smooth(1.2f, 2.2f, s));
    if (s >= 3.0f) dry = mixf(0.75f, 0.1f, smooth(3.4f, 3.9f, s));
    w->dry = fmaxf(dry, barren);
    w->season = s;
}

static void WorldDefaults(World *w, float t)
{
    memset(w, 0, sizeof(*w));
    w->t = t;
    w->anim = t;
    w->cam.fov = 40.0f;
    w->wind = 0.6f;
    w->bench = (Vector4){ 0, 0, 0, 99.0f };
    w->cane = (Vector4){ 0, 0, 0, 99.0f };
    w->trowel = (Vector4){ 0, 0, 0, 99.0f };
    w->exposure = 1.0f;
    w->grade = v3(1, 1, 1);
    w->saturation = 1.12f;
    w->contrast = 0.4f;
    w->vignette = 0.35f;
    w->warmth = 0.1f;
    w->godrays = 0.5f;
    w->bloom = 0.9f;
    SetLighting(w, 9.0f, 0.0f);
    SetSeason(w, 1.0f, 0.0f);
}

static void SetTree(World *w, float growth)
{
    Vector3 base = v3(0, terrainHeight(0, 0), 0);
    if (growth < 0.04f) {
        TreeBuild(&w->tree, base, 0.0f, 0.0f, 0.0f, w->anim);
        w->tree.sprout = saturate(growth / 0.04f);
        w->tree.growth = growth;
        return;
    }
    TreeBuild(&w->tree, base, growth, w->leafAmt, w->wind, w->anim);
}

static void Cam(World *w, Vector3 pos, Vector3 target, float fov)
{
    w->cam.pos = pos;
    w->cam.target = target;
    w->cam.fov = fov;
    /* a gentle handheld drift, scaled to how far away the subject is */
    float t = w->t;
    float k = 0.006f * fminf(v3len(v3sub(target, pos)), 6.0f);
    w->cam.shake = v3(fbm1(t * 0.31f) * k, fbm1(t * 0.27f + 5.0f) * k * 0.7f, fbm1(t * 0.29f + 9.0f) * k);
}

static CharState *AddChar(World *w, CharType type, const Pose *p, float t, float seed)
{
    if (w->charCount >= MAX_CHARS) return NULL;
    CharState *c = &w->ch[w->charCount++];
    memset(c, 0, sizeof(*c));
    Body b = BodyFor(type);
    PoseSolve(&b, p, c);
    c->blink = Blink(t, seed);
    c->smile = 0.3f;
    c->gaze = c->j[J_HEADF];
    c->scarf = type == CH_GRANDMA || type == CH_WOMAN ? 1.0f : 0.0f;
    return c;
}


/* ======================================================================== */
/* places                                                                    */

static Vector3 gTop;          /* where the acorn is planted, and the oak grows */
static Vector3 gBench;        /* bench position on the ground */
static const float kBenchYaw = -1.72f;
static Vector3 gHole2;        /* the second acorn, decades later */
static float gShotLocal;      /* local time of the shot being evaluated */

static Vector3 G(float x, float z) { return v3(x, terrainHeight(x, z), z); }
static Vector3 up(Vector3 p, float h) { return v3(p.x, p.y + h, p.z); }
static float yawTo(Vector3 from, Vector3 to) { return atan2f(to.x - from.x, to.z - from.z); }

/* ======================================================================== */
/* captions                                                                  */

typedef struct {
    int shot;
    float t0, t1;
    CharType who;
    const char *text;
} Line;

static const Line gLines[] = {
    { SH_ACORN, 1.6f, 4.2f, CH_GIRL, "\"What is it, Grandma?\"" },
    { SH_ACORN, 5.0f, 10.0f, CH_GRANDMA, "\"An oak tree. It just doesn't know it yet.\"" },
    { SH_MIRA, 0.8f, 4.6f, CH_GIRL, "\"When will it be big enough to sit under?\"" },
    { SH_MIRA, 5.4f, 7.6f, CH_GIRL, "\"But then you won't...\"" },
    { SH_NANA, 1.2f, 4.4f, CH_GRANDMA, "\"Oh... not for a long, long time.\"" },
    { SH_NANA, 5.2f, 8.8f, CH_GRANDMA, "\"It isn't for me, sweetheart.\"" },
    { SH_BENCH, 1.0f, 4.4f, CH_GIRL, "\"It's still too small to give any shade.\"" },
    { SH_BENCH, 5.4f, 9.4f, CH_GRANDMA, "\"Shade was never for the one who plants the tree.\"" },
    { SH_BENCH, 10.2f, 14.6f, CH_GRANDMA, "\"It's a gift for someone you may never meet.\"" },
    { SH_SHADE, 8.5f, 12.0f, CH_WOMAN, "\"Thank you, Grandma.\"" },
    { SH_GIFT, 1.0f, 3.6f, CH_BOY, "\"Mama, what's this?\"" },
    { SH_GIFT, 5.4f, 10.6f, CH_WOMAN, "\"An oak tree. It just doesn't know it yet.\"" },
};
#define NLINES ((int)(sizeof(gLines) / sizeof(gLines[0])))

static int gCurShot;

static float Speaking(CharType who, float lt)
{
    for (int i = 0; i < NLINES; i++) {
        const Line *l = &gLines[i];
        if (l->shot == gCurShot && l->who == who && lt > l->t0 + 0.1f && lt < l->t1 - 0.5f)
            return Talk(lt, (float)i);
    }
    return 0.0f;
}

/* ======================================================================== */
/* actors                                                                    */

static CharState *Actor(World *w, CharType type, Pose p, float lt, float seed)
{
    CharState *c = AddChar(w, type, &p, lt, seed);
    if (!c) return NULL;
    c->mouthOpen = Speaking(type, lt);
    return c;
}

static void Face(CharState *c, float smile, float brow) { if (c) { c->smile = smile; c->brow = brow; } }
static void Gaze(CharState *c, Vector3 at) { if (c) c->gaze = v3norm(v3sub(at, c->j[J_HEAD])); }

static Vector3 HeadOf(const Body *b, const Pose *p)
{
    CharState tmp;
    PoseSolve(b, p, &tmp);
    return tmp.j[J_HEAD];
}

/* two people walking side by side up the path, holding hands */
static void WalkTogether(World *w, CharType bigT, CharType smallT, float s, float lt, CharState **big, CharState **small)
{
    Body bb = BodyFor(bigT), bs = BodyFor(smallT);
    float stepBig = bigT == CH_GRANDMA ? 0.27f : 0.34f;
    Pose pb = PoseWalk(&bb, s, lt, stepBig, 0.3f);
    Pose ps = PoseWalk(&bs, s + 0.05f, lt, 0.24f, -0.3f);
    /* meet in the middle: big person's left hand, small person's right hand */
    float yaw = PathYaw(s);
    Vector3 r = Right(yaw), f = Fwd(yaw);
    Vector3 mid = v3scale(v3add(pb.root, ps.root), 0.5f);
    float hy = ps.root.y + bs.spine - (bs.upperArm + bs.foreArm) * 0.62f;
    float bounce = 0.015f * sinf(s / (2.0f * stepBig) * TAU_F * 2.0f);
    Vector3 hold = v3add(v3(mid.x, hy + bounce, mid.z), v3scale(f, 0.06f));
    pb.handL = v3add(hold, v3scale(r, 0.03f));
    ps.handR = v3add(hold, v3scale(r, -0.03f));
    pb.handDirL = v3add(f, v3add(v3scale(r, -0.7f), v3(0, -0.5f, 0)));
    ps.handDirR = v3add(f, v3add(v3scale(r, 0.7f), v3(0, -0.2f, 0)));
    pb.palmL = v3scale(r, -1.0f);
    ps.palmR = r;
    pb.elbowL = v3add(v3scale(r, -0.4f), v3(0, -1.0f, 0));
    ps.elbowR = v3add(v3scale(r, 0.4f), v3(0, -1.0f, 0));
    *big = Actor(w, bigT, pb, lt, 1.0f);
    *small = Actor(w, smallT, ps, lt, 2.0f);
}

/* ======================================================================== */
/* world states                                                              */

static void Early(World *w, float hour, float season)
{
    SetLighting(w, hour, 0.0f);
    SetSeason(w, season, 0.82f);
    w->leafAmt = 0.0f;
    w->lush = 0.0f;
    w->wind = 0.7f;
    SetTree(w, 0.0f);
}

static void Late(World *w, float hour)
{
    SetLighting(w, hour, 0.0f);
    SetSeason(w, 1.15f, 0.0f);
    w->lush = 1.0f;
    w->wind = 0.55f;
    SetTree(w, 1.0f);
    w->bench = (Vector4){ gBench.x, gBench.y, gBench.z, kBenchYaw };
    w->benchAge = 0.85f;
    Vector3 a = TreeSwingAnchor(gTop, 1.0f);
    w->swing = (Vector4){ a.x, a.y, a.z, 0.0f };
    w->swingShow = a.y - terrainHeight(a.x, a.z) - 0.5f;
    w->pBirds = 1.0f;
}

static void Grade(World *w, float exposure, float warmth, float sat)
{
    w->exposure = exposure;
    w->warmth = warmth;
    w->saturation = sat;
}

/* ======================================================================== */
/* shots                                                                     */

/* 1. Dawn over the barren hill. Title. */
static void ShotDawn(World *w, float lt)
{
    Early(w, 6.15f + lt * 0.01f, 1.7f);
    w->light.fogDen *= 0.75f;
    w->light.cloud = 0.6f;
    float u = easeInOut(lt / 13.0f);
    Vector3 p0 = v3(-62.0f, 14.0f, 34.0f), p1 = v3(-44.0f, 11.5f, 23.0f);
    Cam(w, v3lerp(p0, p1, u), v3add(gTop, v3(0, 5.5f + u, 0)), 32.0f);
    w->godrays = 0.9f;
    w->pBirds = 1.0f;
    Grade(w, 0.78f, 0.15f, 1.2f);
    w->contrast = 0.55f;
    w->bloom = 0.45f;
}

/* 2. Grandmother and Mira walk up the path, hand in hand. */
static void ShotWalk(World *w, float lt)
{
    Early(w, 6.55f + lt * 0.01f, 1.7f);
    float s = 27.5f + lt * 0.58f;
    CharState *g, *m;
    WalkTogether(w, CH_GRANDMA, CH_GIRL, s, lt, &g, &m);
    Face(g, 0.35f, 0.0f);
    Face(m, 0.45f, 0.2f);
    Vector3 c = PathPos(s - 7.0f);
    Cam(w, v3add(c, v3(3.0f + lt * 0.08f, 1.5f, 2.5f)), up(PathPos(s + 2.0f), 0.9f), 30.0f);
    w->godrays = 0.6f;
    w->pDust = 0.6f;
    Grade(w, 1.0f, 0.3f, 1.1f);
}

/* 3. They reach the empty hilltop. */
static void ShotArrive(World *w, float lt)
{
    Early(w, 6.9f, 1.7f);
    float L = PathLength();
    float s = fminf(L - 6.0f + lt * 0.58f, L - 1.2f);
    float walking = s < L - 1.25f ? 1.0f : 0.0f;
    Body bg = BodyFor(CH_GRANDMA), bm = BodyFor(CH_GIRL);
    CharState *g, *m;
    if (walking > 0.5f) {
        WalkTogether(w, CH_GRANDMA, CH_GIRL, s, lt, &g, &m);
    } else {
        float st = lt - (L - 1.25f - (L - 6.0f)) / 0.58f;
        float yaw = PathYaw(L - 1.2f);
        Vector3 pg = v3add(PathPos(L - 1.2f), v3scale(Right(yaw), 0.3f));
        Vector3 pm = v3add(PathPos(L - 1.15f), v3scale(Right(yaw), -0.3f));
        pg.y = terrainHeight(pg.x, pg.z);
        pm.y = terrainHeight(pm.x, pm.z);
        Pose a = PoseStand(&bg, pg, yaw - 0.4f * ease(st / 2.0f), lt);
        a.look = up(G(-6.0f, -8.0f), 2.0f);
        a.lookW = ease(st / 1.5f) * 0.8f;
        Pose b = PoseStand(&bm, pm, yaw + 0.5f * ease((st - 1.0f) / 1.5f), lt);
        b.look = HeadOf(&bg, &a);
        b.lookW = ease((st - 1.0f) / 1.0f);
        g = Actor(w, CH_GRANDMA, a, lt, 1.0f);
        m = Actor(w, CH_GIRL, b, lt, 2.0f);
        Gaze(g, a.look);
        Gaze(m, b.look);
    }
    Face(g, 0.4f, 0.1f);
    Face(m, 0.4f, 0.3f);
    Cam(w, up(G(-1.6f, -2.2f), 1.45f), up(PathPos(L - 3.0f), 0.8f), 34.0f);
    w->godrays = 0.5f;
    Grade(w, 1.0f, 0.3f, 1.1f);
}

/* positions around the planting hole */
static Vector3 NanaKneel(void) { return G(gTop.x + 0.05f, gTop.z - 0.6f); }
static Vector3 MiraSquat(void) { return G(gTop.x - 0.5f, gTop.z + 0.12f); }

static Pose NanaKneelPose(float lt)
{
    Body b = BodyFor(CH_GRANDMA);
    return PoseKneel(&b, NanaKneel(), 0.0f, lt);
}

static Pose MiraSquatPose(float lt)
{
    Body b = BodyFor(CH_GIRL);
    return PoseSquat(&b, MiraSquat(), yawTo(MiraSquat(), gTop), lt);
}

/* 4. Grandmother kneels and digs; Mira crouches to watch. */
static void ShotDig(World *w, float lt)
{
    Early(w, 7.2f, 1.7f);
    Body bg = BodyFor(CH_GRANDMA), bm = BodyFor(CH_GIRL);
    float k = easeInOut((lt - 0.5f) / 2.2f);
    Pose stand = PoseStand(&bg, NanaKneel(), 0.0f, lt);
    Pose kneel = NanaKneelPose(lt);
    Pose pg = PoseLerp(&stand, &kneel, k);
    /* digging strokes with the trowel */
    float dig = k * smooth(3.0f, 3.5f, lt);
    float stroke = sinf(lt * 3.2f);
    Vector3 holeP = up(gTop, 0.03f);
    Vector3 hand = v3add(holeP, v3(0.02f, 0.06f + 0.05f * (0.5f + 0.5f * stroke), 0.12f - 0.04f * stroke));
    HandReach(&bg, &pg, 1, hand, v3(0, -1, 0), dig);
    pg.handDirR = v3lerp(pg.handDirR, v3(0.1f, -0.6f, -1.0f), dig);
    HandReach(&bg, &pg, 0, v3add(pg.root, v3(0.18f, 0.02f, 0.3f)), v3(0, -1, 0), k * 0.8f);
    pg.look = holeP;
    pg.lookW = k;
    CharState *g = Actor(w, CH_GRANDMA, pg, lt, 1.0f);
    float q = easeInOut((lt - 1.2f) / 1.8f);
    Pose ms = PoseStand(&bm, MiraSquat(), yawTo(MiraSquat(), gTop), lt);
    Pose mq = MiraSquatPose(lt);
    Pose pm = PoseLerp(&ms, &mq, q);
    pm.look = lt < 7.0f ? holeP : HeadOf(&bg, &pg);
    pm.lookW = 1.0f;
    CharState *m = Actor(w, CH_GIRL, pm, lt, 2.0f);
    Gaze(g, holeP);
    Gaze(m, pm.look);
    Face(g, 0.45f, 0.0f);
    Face(m, 0.35f, 0.5f);
    if (g && dig > 0.1f) {
        Vector3 hw = g->j[J_RWRI], hd = v3norm(v3sub(g->j[J_RTIP], g->j[J_RWRI]));
        Vector3 tp = v3add(hw, v3scale(hd, 0.1f));
        w->trowel = (Vector4){ tp.x, tp.y, tp.z, atan2f(hd.x, hd.z) };
    }
    w->mound = (Vector4){ gTop.x + 0.06f, gTop.y - 0.04f, gTop.z + 0.12f, 0.16f * smooth(3.5f, 8.0f, lt) };
    Cam(w, up(G(1.75f, 2.35f), 0.95f), up(G(-0.15f, -0.1f), 0.42f), 32.0f);
    w->cam.aperture = 6.0f;
    w->cam.focus = 2.9f;
    w->godrays = 0.4f;
    Grade(w, 1.0f, 0.28f, 1.1f);
}

/* the acorn held up in grandmother's palm */
static void AcornInPalm(World *w, Pose *pg, const Body *bg, CharState **g, float lt, float raise, Vector3 toward)
{
    Vector3 chest = v3add(pg->root, v3(0, bg->spine * 0.75f, 0));
    Vector3 dir = v3norm(v3sub(toward, chest));
    dir.y = 0;
    dir = v3norm(dir);
    Vector3 hand = v3add(chest, v3add(v3scale(dir, 0.3f), v3(0, -0.12f, 0)));
    HandReach(bg, pg, 1, hand, v3(0, 1, 0), raise);
    pg->handDirR = v3lerp(pg->handDirR, v3add(dir, v3(0, 0.1f, 0)), raise);
    pg->elbowR = v3(0.3f, -1.0f, -0.2f);
    *g = Actor(w, CH_GRANDMA, *pg, lt, 1.0f);
    Vector3 a = hand;
    if (*g) {
        Vector3 wr = (*g)->j[J_RWRI], tip = (*g)->j[J_RTIP];
        a = v3add(v3lerp(wr, tip, 0.42f), v3scale((*g)->palmR, 0.026f));
    }
    /* the position is always valid (cameras frame it); it only shows once the palm is up */
    w->acorn = (Vector4){ a.x, a.y, a.z, raise > 0.5f ? 1.0f : 0.0f };
}

/* 5. "What is it?" — the acorn. */
static void ShotAcorn(World *w, float lt)
{
    Early(w, 7.4f, 1.7f);
    Body bg = BodyFor(CH_GRANDMA), bm = BodyFor(CH_GIRL);
    Pose pg = NanaKneelPose(lt);
    Pose pm = MiraSquatPose(lt);
    Vector3 mHead = HeadOf(&bm, &pm);
    float raise = easeInOut((lt - 0.3f) / 1.4f);
    pg.look = lt < 4.5f ? up(gTop, 0.5f) : mHead;
    pg.lookW = 0.8f;
    pg.headRoll = 0.1f * ease((lt - 5.0f) / 2.0f);
    CharState *g;
    AcornInPalm(w, &pg, &bg, &g, lt, raise, mHead);
    Vector3 acorn = v3(w->acorn.x, w->acorn.y, w->acorn.z);
    pm.look = lt < 4.8f ? acorn : HeadOf(&bg, &pg);
    pm.lookW = 1.0f;
    pm.headRoll = -0.12f * ease((lt - 1.8f) / 1.0f);
    CharState *m = Actor(w, CH_GIRL, pm, lt, 2.0f);
    Gaze(g, pg.look);
    Gaze(m, pm.look);
    Face(g, 0.55f, 0.15f);
    Face(m, mixf(0.25f, 0.65f, ease((lt - 8.0f) / 1.5f)), 0.6f);
    w->mound = (Vector4){ gTop.x + 0.06f, gTop.y - 0.04f, gTop.z + 0.12f, 0.16f };
    Cam(w, up(G(0.95f, 1.75f), 0.95f), v3add(acorn, v3(-0.12f, 0.12f, -0.1f)), 30.0f);
    w->cam.aperture = 9.0f;
    w->cam.focus = v3len(v3sub(acorn, w->cam.pos));
    Grade(w, 1.0f, 0.28f, 1.1f);
}

/* 6. Close on Mira. */
static void ShotMira(World *w, float lt)
{
    Early(w, 7.55f, 1.7f);
    Body bg = BodyFor(CH_GRANDMA), bm = BodyFor(CH_GIRL);
    Pose pg = NanaKneelPose(lt);
    Pose pm = MiraSquatPose(lt);
    Vector3 gHead = HeadOf(&bg, &pg);
    pm.look = gHead;
    pm.lookW = 1.0f;
    pm.headRoll = -0.08f;
    pm.headPitch = -0.05f * ease((lt - 5.0f) / 1.0f);
    CharState *g;
    AcornInPalm(w, &pg, &bg, &g, lt, 1.0f, HeadOf(&bm, &pm));
    CharState *m = Actor(w, CH_GIRL, pm, lt, 2.0f);
    Gaze(m, gHead);
    Gaze(g, HeadOf(&bm, &pm));
    float sad = ease((lt - 5.2f) / 1.5f);
    Face(m, mixf(0.35f, 0.0f, sad), mixf(0.5f, 1.0f, sad));
    w->mound = (Vector4){ gTop.x + 0.06f, gTop.y - 0.04f, gTop.z + 0.12f, 0.16f };
    Vector3 head = m ? m->j[J_HEAD] : up(MiraSquat(), 0.7f);
    Vector3 camp = v3add(head, v3(1.25f, 0.06f, -0.45f));
    Cam(w, camp, v3add(head, v3(0.03f, -0.03f, 0.05f)), 24.0f);
    w->cam.aperture = 11.0f;
    w->cam.focus = v3len(v3sub(head, camp));
    Grade(w, 1.0f, 0.3f, 1.08f);
}

/* 7. Close on grandmother. */
static void ShotNana(World *w, float lt)
{
    Early(w, 7.65f, 1.7f);
    Body bg = BodyFor(CH_GRANDMA), bm = BodyFor(CH_GIRL);
    Pose pg = NanaKneelPose(lt);
    Pose pm = MiraSquatPose(lt);
    Vector3 mHead = HeadOf(&bm, &pm);
    pg.look = mHead;
    pg.lookW = 0.9f;
    pg.headRoll = 0.12f;
    pg.headPitch = 0.04f * sinf(lt * 0.7f);
    CharState *g;
    AcornInPalm(w, &pg, &bg, &g, lt, 1.0f - ease((lt - 6.0f) / 2.0f), mHead);
    CharState *m = Actor(w, CH_GIRL, pm, lt, 2.0f);
    Gaze(g, mHead);
    Gaze(m, HeadOf(&bg, &pg));
    Face(g, mixf(0.5f, 0.8f, ease((lt - 4.5f) / 2.0f)), 0.3f);
    if (g) g->tear = 0.4f * ease((lt - 5.0f) / 2.0f);
    w->mound = (Vector4){ gTop.x + 0.06f, gTop.y - 0.04f, gTop.z + 0.12f, 0.16f };
    Vector3 head = g ? g->j[J_HEAD] : up(NanaKneel(), 1.0f);
    Vector3 camp = v3add(head, v3(-0.48f, -0.05f, 1.25f));
    Cam(w, camp, v3add(head, v3(-0.05f, -0.04f, 0.0f)), 24.0f);
    w->cam.aperture = 11.0f;
    w->cam.focus = v3len(v3sub(head, camp));
    Grade(w, 1.0f, 0.32f, 1.08f);
}

/* 8. Planting: the acorn goes into the earth, Mira waters it. */
static void ShotPlant(World *w, float lt)
{
    Early(w, 7.9f, 1.7f);
    Body bg = BodyFor(CH_GRANDMA), bm = BodyFor(CH_GIRL);
    Pose pg = NanaKneelPose(lt);
    Vector3 hole = up(gTop, 0.02f);
    float place = ease(lt / 1.6f) * (1.0f - ease((lt - 3.2f) / 1.0f));
    float pat = smooth(2.2f, 2.6f, lt) * (1.0f - smooth(5.0f, 5.6f, lt));
    Vector3 handR = v3add(hole, v3(0.0f, 0.05f + 0.04f * fabsf(sinf(lt * 6.0f)) * pat, 0.06f));
    HandReach(&bg, &pg, 1, handR, v3(0, -1, 0), fmaxf(place, pat));
    HandReach(&bg, &pg, 0, v3add(hole, v3(0.1f, 0.05f + 0.04f * fabsf(sinf(lt * 6.0f + 1.5f)) * pat, 0.1f)), v3(0, -1, 0), pat);
    pg.look = hole;
    pg.lookW = 1.0f;
    CharState *g = Actor(w, CH_GRANDMA, pg, lt, 1.0f);
    if (g && lt < 2.0f) {
        Vector3 a = v3lerp(g->j[J_RWRI], g->j[J_RTIP], 0.45f);
        a = v3add(a, v3scale(g->palmR, 0.02f));
        if (lt > 1.6f) a = hole;
        w->acorn = (Vector4){ a.x, a.y, a.z, 1.0f };
    }
    /* Mira stands with the watering can */
    Vector3 mp = G(gTop.x - 0.62f, gTop.z + 0.32f);
    float yaw = yawTo(mp, gTop);
    Pose pm = PoseStand(&bm, mp, yaw, lt);
    float tilt = smooth(4.5f, 6.0f, lt) * (1.0f - smooth(10.0f, 11.0f, lt));
    Vector3 f = Fwd(yaw), r = Right(yaw);
    Vector3 canHand = v3add(pm.root, v3add(v3scale(f, 0.2f), v3add(v3scale(r, 0.08f), v3(0, 0.12f + 0.06f * tilt, 0))));
    HandReach(&bm, &pm, 1, canHand, v3scale(r, -1.0f), 1.0f);
    pm.handDirR = v3add(f, v3(0, -0.2f, 0));
    HandReach(&bm, &pm, 0, v3add(canHand, v3add(v3scale(r, -0.1f), v3scale(f, 0.08f))), v3(0, 0, 0), tilt);
    pm.look = hole;
    pm.lookW = 1.0f;
    pm.lean = 0.12f * tilt;
    CharState *m = Actor(w, CH_GIRL, pm, lt, 2.0f);
    Gaze(g, hole);
    Gaze(m, hole);
    Face(g, 0.55f, 0.1f);
    Face(m, 0.6f, 0.3f);
    if (m) {
        Vector3 cp = v3add(m->j[J_RWRI], v3(0, -0.12f, 0));
        w->can = (Vector4){ cp.x, cp.y, cp.z, yaw };
        w->canTilt = 0.15f + 0.65f * tilt;
        w->canShow = 1.0f;
        w->pWater = tilt;
    }
    float mound = smooth(2.0f, 3.5f, lt);
    w->mound = (Vector4){ gTop.x, gTop.y - 0.05f, gTop.z, 0.2f * mound + 0.0001f };
    w->wet = smooth(6.0f, 9.0f, lt);
    Cam(w, up(G(1.9f, 2.6f), 1.25f), up(G(-0.25f, -0.05f), 0.45f), 30.0f);
    Grade(w, 1.0f, 0.28f, 1.1f);
}

/* 9. Time-lapse macro: the first shoot breaks the soil. */
static void ShotSprout(World *w, float lt)
{
    float h = 6.8f + lt * 1.35f;
    Early(w, h, 1.8f + lt * 0.02f);
    w->anim = gShotLocal * 6.0f;
    SetTree(w, 0.04f * ease((lt - 1.0f) / 6.5f));
    w->mound = (Vector4){ gTop.x, gTop.y - 0.05f, gTop.z, 0.2f };
    w->wet = 1.0f - smooth(0.0f, 3.0f, lt) * 0.7f;
    Vector3 tgt = up(gTop, 0.06f + 0.06f * ease(lt / 8.0f));
    Cam(w, up(G(0.36f, 0.5f), 0.1f), tgt, 34.0f);
    w->cam.aperture = 14.0f;
    w->cam.focus = v3len(v3sub(tgt, w->cam.pos));
    w->godrays = 0.3f;
    Grade(w, 1.0f, 0.2f, 1.12f);
}

/* 10. Years go by: seasons turn, the sapling grows, they keep visiting. */
static void ShotYears(World *w, float lt)
{
    const float D = 24.0f;
    float u = lt / D;
    float season = 1.95f + lt * 0.17f;
    /* days spin between visits and slow down during them */
    float visit[4][2] = { { 2.0f, 5.0f }, { 7.2f, 10.2f }, { 12.0f, 15.0f }, { 17.0f, 21.5f } };
    float hour = 6.0f + lt * 9.0f;
    int inVisit = -1;
    for (int i = 0; i < 4; i++) {
        if (lt > visit[i][0] && lt < visit[i][1]) inVisit = i;
    }
    if (inVisit >= 0) hour = 10.5f + (lt - visit[inVisit][0]) * 0.8f;
    else hour = fmodf(hour, 24.0f);
    float barren = mixf(0.82f, 0.45f, u);
    SetLighting(w, hour, 0.0f);
    SetSeason(w, season, barren);
    w->anim = gShotLocal * 4.0f;
    float growth = mixf(0.045f, 0.15f, ease(u));
    SetTree(w, growth);
    w->pLeaves = (w->season > 2.0f && w->season < 2.9f) ? 1.0f : 0.0f;
    w->pSnow = w->snow > 0.3f ? 1.0f : 0.0f;
    w->pPetals = (w->season < 0.7f) ? 0.6f : 0.0f;
    Body bg = BodyFor(CH_GRANDMA), bm = BodyFor(CH_GIRL);
    if (lt > 3.2f) w->fence = (Vector4){ gTop.x, gTop.y, gTop.z, 1.0f };
    if (lt > 11.0f) {
        w->bench = (Vector4){ gBench.x, gBench.y, gBench.z, kBenchYaw };
        w->benchAge = 0.0f;
    }
    float vl = inVisit >= 0 ? lt - visit[inVisit][0] : 0.0f;
    /* visits follow the seasons: autumn fence, winter, spring bench, summer watering */
    int scene = inVisit < 0 ? -1 : (inVisit + 1) % 4;
    if (scene == 0) {
        /* summer: Mira waters, grandmother watches leaning on her cane */
        Vector3 mp = G(gTop.x - 0.55f, gTop.z + 0.5f);
        float yaw = yawTo(mp, gTop);
        Pose pm = PoseStand(&bm, mp, yaw, vl);
        Vector3 f = Fwd(yaw), r = Right(yaw);
        Vector3 ch = v3add(pm.root, v3add(v3scale(f, 0.22f), v3add(v3scale(r, 0.08f), v3(0, 0.17f, 0))));
        HandReach(&bm, &pm, 1, ch, v3scale(r, -1.0f), 1.0f);
        pm.look = up(gTop, 0.3f);
        pm.lookW = 1.0f;
        CharState *m = Actor(w, CH_GIRL, pm, vl, 2.0f);
        if (m) {
            Vector3 cp = v3add(m->j[J_RWRI], v3(0, -0.12f, 0));
            w->can = (Vector4){ cp.x, cp.y, cp.z, yaw };
            w->canTilt = 0.8f;
            w->canShow = 1.0f;
            w->pWater = 1.0f;
            Gaze(m, up(gTop, 0.2f));
            Face(m, 0.6f, 0.2f);
        }
        Vector3 gp = G(gTop.x + 0.75f, gTop.z + 0.9f);
        Pose pg = PoseStand(&bg, gp, yawTo(gp, mp), vl);
        HandReach(&bg, &pg, 1, v3add(pg.root, v3add(v3scale(Fwd(pg.yaw), 0.18f), v3add(v3scale(Right(pg.yaw), 0.2f), v3(0, -0.05f, 0)))), v3(0, 0, 0), 1.0f);
        pg.look = up(mp, 0.9f);
        pg.lookW = 0.8f;
        CharState *g = Actor(w, CH_GRANDMA, pg, vl, 1.0f);
        if (g) { g->cane = 1.0f; Face(g, 0.6f, 0.0f); Gaze(g, up(mp, 0.9f)); }
    } else if (scene == 1) {
        /* autumn: grandmother ties a little fence around the sapling */
        Pose pg = PoseKneel(&bg, G(gTop.x + 0.15f, gTop.z + 0.62f), PI_F + 0.2f, vl);
        HandReach(&bg, &pg, 1, up(G(gTop.x + 0.1f, gTop.z + 0.3f), 0.35f + 0.05f * sinf(vl * 4.0f)), v3(0, 0, -1), 1.0f);
        HandReach(&bg, &pg, 0, up(G(gTop.x + 0.28f, gTop.z + 0.22f), 0.38f), v3(0, 0, -1), 1.0f);
        pg.look = up(gTop, 0.35f);
        pg.lookW = 1.0f;
        CharState *g = Actor(w, CH_GRANDMA, pg, vl, 1.0f);
        Face(g, 0.5f, 0.0f);
        Gaze(g, up(gTop, 0.35f));
        Vector3 mp = G(gTop.x - 0.7f, gTop.z + 0.55f);
        Pose pm = PoseStand(&bm, mp, yawTo(mp, gTop) + 0.3f, vl);
        pm.look = up(gTop, 0.4f);
        pm.lookW = 1.0f;
        CharState *m = Actor(w, CH_GIRL, pm, vl, 2.0f);
        Face(m, 0.4f, 0.3f);
        Gaze(m, up(gTop, 0.4f));
    } else if (scene == 2) {
        /* winter: standing together in the snow */
        Vector3 gp = G(gTop.x + 1.0f, gTop.z + 0.45f), mp = G(gTop.x + 0.55f, gTop.z + 0.8f);
        Pose pg = PoseStand(&bg, gp, yawTo(gp, gTop), vl);
        Pose pm = PoseStand(&bm, mp, yawTo(mp, gTop), vl);
        Vector3 hold = v3scale(v3add(v3add(gp, mp), v3(0, 1.0f, 0)), 0.5f);
        hold.y = mp.y + 0.55f;
        HandReach(&bg, &pg, 0, hold, v3(0, 0, 0), 1.0f);
        HandReach(&bm, &pm, 1, hold, v3(0, 0, 0), 1.0f);
        pg.look = up(gTop, 0.6f);
        pg.lookW = 0.8f;
        pm.look = up(gTop, 0.6f);
        pm.lookW = 0.8f;
        CharState *g = Actor(w, CH_GRANDMA, pg, vl, 1.0f);
        CharState *m = Actor(w, CH_GIRL, pm, vl, 2.0f);
        Face(g, 0.5f, 0.0f);
        Face(m, 0.5f, 0.2f);
        Gaze(g, up(gTop, 0.6f));
        Gaze(m, up(gTop, 0.6f));
        if (g) g->cane = 1.0f;
    } else if (scene == 3) {
        /* spring: someone has put up a bench; they sit side by side */
        Pose pg = PoseSit(&bg, gBench, kBenchYaw, 0.28f, vl);
        Pose pm = PoseSit(&bm, gBench, kBenchYaw, -0.25f, vl);
        pg.look = up(gTop, 1.0f);
        pg.lookW = 0.6f;
        pm.look = HeadOf(&bg, &pg);
        pm.lookW = 0.7f;
        CharState *g = Actor(w, CH_GRANDMA, pg, vl, 1.0f);
        CharState *m = Actor(w, CH_GIRL, pm, vl, 2.0f);
        Face(g, 0.6f, 0.0f);
        Face(m, 0.6f, 0.3f);
        Gaze(g, up(gTop, 1.0f));
        Gaze(m, pm.look);
        if (g) g->sit = 1.0f;
        w->cane = (Vector4){ gBench.x + 0.75f, gBench.y, gBench.z - 0.1f, kBenchYaw + 1.3f };
    }
    Cam(w, up(G(0.6f, 5.0f), 1.45f), up(gTop, 0.7f + growth * 3.0f), 36.0f);
    Grade(w, 1.0f - 0.3f * w->snow, 0.22f - 0.3f * w->snow, 1.1f);
}

static Pose SitPose(CharType t, float offset, float lt)
{
    Body b = BodyFor(t);
    return PoseSit(&b, gBench, kBenchYaw, offset, lt);
}

/* 11. Sunset from behind the bench. */
static void ShotSunset(World *w, float lt)
{
    SetLighting(w, 17.62f + lt * 0.006f, 0.0f);
    SetSeason(w, 5.55f, 0.4f);
    w->lush = 0.15f;
    SetTree(w, 0.16f);
    w->fence = (Vector4){ gTop.x, gTop.y, gTop.z, 1.0f };
    w->bench = (Vector4){ gBench.x, gBench.y, gBench.z, kBenchYaw };
    w->benchAge = 0.1f;
    Pose pg = SitPose(CH_GRANDMA, 0.28f, lt), pm = SitPose(CH_GIRL, -0.25f, lt);
    pg.look = up(G(-30.0f, 0.0f), 3.0f);
    pg.lookW = 0.6f;
    pm.look = up(G(-30.0f, 0.0f), 3.0f);
    pm.lookW = 0.6f;
    CharState *g = Actor(w, CH_GRANDMA, pg, lt, 1.0f);
    CharState *m = Actor(w, CH_GIRL, pm, lt, 2.0f);
    if (g) { g->sit = 1.0f; Face(g, 0.5f, 0.0f); }
    if (m) Face(m, 0.4f, 0.2f);
    w->cane = (Vector4){ gBench.x + 0.78f, gBench.y, gBench.z - 0.1f, kBenchYaw + 1.3f };
    float u = ease(lt / 10.0f);
    Cam(w, up(v3lerp(G(gBench.x + 3.4f, gBench.z + 1.3f), G(gBench.x + 2.7f, gBench.z + 1.0f), u), 1.15f), up(G(gBench.x - 6.0f, gBench.z - 1.4f), 0.95f), 36.0f);
    w->godrays = 0.65f;
    w->bloom = 0.6f;
    w->pDust = 0.6f;
    Grade(w, 0.92f, 0.4f, 1.15f);
    w->contrast = 0.5f;
}

/* 12. The talk on the bench. The scarf is passed on. */
static void ShotBench(World *w, float lt)
{
    SetLighting(w, 17.7f + lt * 0.006f, 0.0f);
    SetSeason(w, 5.55f, 0.4f);
    w->lush = 0.15f;
    SetTree(w, 0.16f);
    w->fence = (Vector4){ gTop.x, gTop.y, gTop.z, 1.0f };
    w->bench = (Vector4){ gBench.x, gBench.y, gBench.z, kBenchYaw };
    w->benchAge = 0.1f;
    Body bg = BodyFor(CH_GRANDMA), bm = BodyFor(CH_GIRL);
    Pose pg = SitPose(CH_GRANDMA, 0.28f, lt), pm = SitPose(CH_GIRL, -0.25f, lt);
    Vector3 gh = HeadOf(&bg, &pg), mh = HeadOf(&bm, &pm);
    pm.look = lt < 5.0f ? up(gTop, 1.2f) : gh;
    pm.lookW = 1.0f;
    pg.look = lt < 5.0f ? up(gTop, 1.2f) : (lt < 10.0f ? up(G(-20.0f, -2.0f), 2.0f) : mh);
    pg.lookW = 0.8f;
    pg.headRoll = lt > 10.0f ? 0.1f : 0.0f;
    /* at the end grandmother wraps her scarf around Mira's shoulders */
    float wrap = smooth(15.0f, 16.5f, lt) * (1.0f - smooth(18.0f, 19.0f, lt));
    if (wrap > 0.0f) {
        Vector3 mn = v3add(mh, v3(0, -0.14f, 0));
        HandReach(&bg, &pg, 0, v3add(mn, v3(0, 0.0f, 0.06f)), v3(0, 0, 0), wrap);
        HandReach(&bg, &pg, 1, v3add(mn, v3(0.06f, -0.02f, -0.05f)), v3(0, 0, 0), wrap);
        pg.twist = -0.35f * wrap;
    }
    CharState *g = Actor(w, CH_GRANDMA, pg, lt, 1.0f);
    CharState *m = Actor(w, CH_GIRL, pm, lt, 2.0f);
    int passed = lt > 16.2f;
    if (g) { g->sit = 1.0f; g->scarf = passed ? 0.0f : 1.0f; Face(g, lt > 9.5f ? 0.75f : 0.5f, 0.1f); Gaze(g, pg.look); }
    if (m) { m->scarf = passed ? 1.0f : 0.0f; Face(m, lt < 5.0f ? 0.2f : (lt > 15.0f ? 0.7f : 0.35f), 0.4f); Gaze(m, pm.look); }
    w->cane = (Vector4){ gBench.x + 0.78f, gBench.y, gBench.z - 0.1f, kBenchYaw + 1.3f };
    Vector3 mid = v3scale(v3add(gh, mh), 0.5f);
    Vector3 bcp = v3add(mid, v3add(v3scale(Fwd(kBenchYaw), 2.1f), v3add(v3scale(Right(kBenchYaw), -0.6f), v3(0, -0.1f, 0))));
    Cam(w, bcp, v3add(mid, v3(0.0f, -0.1f, 0.0f)), 30.0f);
    w->cam.aperture = 7.0f;
    w->cam.focus = v3len(v3sub(mid, bcp));
    w->godrays = 0.6f;
    Grade(w, 1.0f, 0.5f, 1.15f);
}

/* 13. Dusk: two small figures on a hill, first stars, fireflies. */
static void ShotDusk(World *w, float lt)
{
    SetLighting(w, 18.15f + lt * 0.035f, 0.0f);
    SetSeason(w, 5.55f, 0.4f);
    w->lush = 0.15f;
    SetTree(w, 0.16f);
    w->fence = (Vector4){ gTop.x, gTop.y, gTop.z, 1.0f };
    w->bench = (Vector4){ gBench.x, gBench.y, gBench.z, kBenchYaw };
    Pose pg = SitPose(CH_GRANDMA, 0.28f, lt), pm = SitPose(CH_GIRL, -0.12f, lt);
    pg.look = up(G(-30.0f, 0.0f), 4.0f);
    pg.lookW = 0.5f;
    pm.lean = 0.2f;
    pm.side = 0.12f;
    CharState *g = Actor(w, CH_GRANDMA, pg, lt, 1.0f);
    CharState *m = Actor(w, CH_GIRL, pm, lt, 2.0f);
    if (g) { g->sit = 1.0f; g->scarf = 0.0f; }
    if (m) m->scarf = 1.0f;
    w->cane = (Vector4){ gBench.x + 0.78f, gBench.y, gBench.z - 0.1f, kBenchYaw + 1.3f };
    float u = lt / 11.0f;
    Cam(w, up(G(16.0f - u * 1.5f, 15.0f), 1.6f), up(G(0.6f, 1.2f), 1.5f), 24.0f);
    w->pFireflies = smooth(2.0f, 6.0f, lt);
    w->godrays = 0.3f;
    Grade(w, 1.05f, 0.25f, 1.1f);
}

/* 14. Autumn, overcast. The bench is empty; the cane leans against it.
 *     Mira, wearing the red scarf, stands by the young tree alone. */
static void ShotEmpty(World *w, float lt)
{
    SetLighting(w, 11.0f, 0.85f);
    SetSeason(w, 6.55f, 0.4f);
    w->wind = 1.2f;
    SetTree(w, 0.2f);
    w->fence = (Vector4){ gTop.x, gTop.y, gTop.z, 1.0f };
    w->bench = (Vector4){ gBench.x, gBench.y, gBench.z, kBenchYaw };
    w->benchAge = 0.2f;
    w->cane = (Vector4){ gBench.x + 0.78f, gBench.y, gBench.z - 0.1f, kBenchYaw + 1.3f };
    Body bm = BodyFor(CH_GIRL);
    Vector3 mp = G(gTop.x + 0.28f, gTop.z + 0.42f);
    float yaw = yawTo(mp, gTop);
    Pose pm = PoseStand(&bm, mp, yaw, lt);
    float touch = smooth(3.0f, 5.0f, lt);
    HandReach(&bm, &pm, 1, up(gTop, 0.62f + 0.02f * sinf(lt)), v3scale(Fwd(yaw), -1.0f), touch);
    pm.look = up(gTop, 1.5f);
    pm.lookW = 0.6f + 0.4f * touch;
    CharState *m = Actor(w, CH_GIRL, pm, lt, 2.0f);
    if (m) { m->scarf = 1.0f; Face(m, 0.1f + 0.25f * smooth(9.0f, 12.0f, lt), 0.5f); Gaze(m, up(gTop, 1.5f)); }
    w->pLeaves = 1.0f;
    float u = easeInOut(lt / 15.0f);
    Cam(w, up(v3lerp(G(-3.6f, 5.6f), G(-2.6f, 4.3f), u), 1.25f), up(G(0.5f, 1.4f), 0.75f), 34.0f);
    Grade(w, 1.05f, -0.1f, 0.8f);
    w->godrays = 0.0f;
}

/* 15. The great growing: decades pass in seconds. */
static void ShotGrow(World *w, float lt)
{
    const float D = 26.0f;
    float u = lt / D;
    float k = easeInOut(u);
    /* time races, then gently comes to rest on a summer morning */
    float speed = 1.0f - smooth(0.7f, 1.0f, u);
    float years = 9.0f * (u - 0.3f * u * u * u);
    float season = 6.6f + years * 4.0f;
    float targetSeason = 1.15f + 4.0f * 12.0f;
    season = mixf(season, targetSeason, smooth(0.86f, 1.0f, u));
    float hour = 11.0f + 70.0f * (u - 0.45f * u * u);
    hour = fmodf(hour, 24.0f);
    hour = mixf(hour, 9.5f, smooth(0.8f, 1.0f, u));
    float overcast = 0.85f * (1.0f - smooth(0.0f, 0.15f, u));
    SetLighting(w, hour, overcast);
    float barren = mixf(0.4f, 0.0f, smooth(0.1f, 0.7f, u));
    SetSeason(w, season, barren);
    w->lush = smooth(0.3f, 0.9f, u);
    w->anim = gShotLocal * (1.0f + 6.0f * speed);
    float growth = mixf(0.2f, 1.0f, smooth(0.02f, 0.92f, u));
    SetTree(w, growth);
    w->bench = (Vector4){ gBench.x, gBench.y, gBench.z, kBenchYaw };
    w->benchAge = mixf(0.2f, 0.85f, k);
    if (u < 0.08f) w->cane = (Vector4){ gBench.x + 0.78f, gBench.y, gBench.z - 0.1f, kBenchYaw + 1.3f };
    if (u < 0.35f) w->fence = (Vector4){ gTop.x, gTop.y, gTop.z, 1.0f };
    if (u > 0.8f) {
        Vector3 a = TreeSwingAnchor(gTop, growth);
        w->swing = (Vector4){ a.x, a.y, a.z, 0.0f };
        w->swingShow = a.y - terrainHeight(a.x, a.z) - 0.5f;
    }
    w->pSnow = w->snow > 0.3f ? 1.0f : 0.0f;
    w->pLeaves = (w->season > 2.0f && w->season < 2.9f) ? 1.0f : 0.0f;
    Vector3 c0 = up(G(-3.0f, 5.2f), 1.3f), c1 = up(G(-14.0f, 19.0f), 4.5f);
    Cam(w, v3lerp(c0, c1, k), up(gTop, mixf(1.0f, 5.0f, k)), mixf(34.0f, 38.0f, k));
    Grade(w, 1.0f, mixf(0.0f, 0.25f, k), mixf(0.85f, 1.12f, k));
    w->godrays = 0.5f * k;
}

/* 16. Decades later: Mira, grown up, walks up the path with her son. */
static void ShotReturn(World *w, float lt)
{
    Late(w, 8.4f + lt * 0.01f);
    float s = 27.0f + lt * 0.75f;
    CharState *a, *b;
    WalkTogether(w, CH_WOMAN, CH_BOY, s, lt, &a, &b);
    Face(a, 0.45f, 0.0f);
    Face(b, 0.6f, 0.3f);
    Vector3 c = PathPos(s - 7.0f);
    Cam(w, v3add(c, v3(3.0f + lt * 0.08f, 1.5f, 2.5f)), up(PathPos(s + 2.0f), 0.9f), 30.0f);
    w->pDust = 0.4f;
    w->pSeeds = 0.4f;
    Grade(w, 1.0f, 0.2f, 1.12f);
}

/* 17. In the shade of the great oak. */
static void ShotShade(World *w, float lt)
{
    Late(w, 10.6f + lt * 0.01f);
    Body bb = BodyFor(CH_BOY);
    Pose pw = SitPose(CH_WOMAN, 0.1f, lt);
    pw.lean = -0.04f;
    pw.look = up(gTop, 4.0f);
    pw.look = up(gTop, 2.5f);
    pw.lookW = smooth(2.0f, 4.0f, lt) * 0.3f;
    CharState *wm = Actor(w, CH_WOMAN, pw, lt, 3.0f);
    if (wm) {
        float closed = smooth(6.0f, 6.6f, lt) * (1.0f - smooth(13.0f, 13.5f, lt));
        wm->blink = fmaxf(wm->blink, closed);
        Face(wm, mixf(0.4f, 0.8f, smooth(7.0f, 9.0f, lt)), 0.2f);
        Gaze(wm, up(gTop, 4.0f));
        wm->sit = 1.0f;
        wm->tear = 0.6f * smooth(9.0f, 11.0f, lt);
    }
    /* the boy on the swing */
    Vector3 a = TreeSwingAnchor(gTop, 1.0f);
    float ang = 0.45f * sinf(lt * 1.9f);
    float len = w->swingShow;
    Vector3 seat = v3add(a, v3(0, -len * cosf(ang), -len * sinf(ang)));
    w->swing.w = ang;
    Pose pb = PoseSit(&bb, v3add(seat, v3(0, -0.5f + 0.01f, 0)), 0.0f, 0.0f, lt);
    pb.root = v3add(seat, v3(0, 0.07f, -0.02f));
    pb.handL = v3add(seat, v3(0.21f, 0.45f, 0.0f));
    pb.handR = v3add(seat, v3(-0.21f, 0.45f, 0.0f));
    pb.footL = v3add(seat, v3(0.08f, -0.3f, 0.25f + 0.1f * sinf(lt * 1.9f + 1.0f)));
    pb.footR = v3add(seat, v3(-0.08f, -0.3f, 0.25f + 0.1f * sinf(lt * 1.9f + 1.0f)));
    pb.lean = -0.15f * sinf(lt * 1.9f);
    pb.look = up(gBench, 1.0f);
    pb.lookW = 0.5f;
    CharState *bo = Actor(w, CH_BOY, pb, lt, 4.0f);
    if (bo) Face(bo, 0.9f, 0.4f);
    Vector3 head = wm ? wm->j[J_HEAD] : up(gBench, 1.1f);
    float u = easeInOut(lt / 15.0f);
    Vector3 fw = Fwd(kBenchYaw);
    Vector3 cp = v3add(head, v3add(v3scale(fw, mixf(2.7f, 1.9f, u)), v3add(v3scale(Right(kBenchYaw), -0.7f), v3(0, 0.02f, 0))));
    Cam(w, cp, v3add(head, v3(0, -0.18f, 0)), 30.0f);
    w->cam.aperture = 8.0f;
    w->cam.focus = v3len(v3sub(head, cp));
    w->pDust = 1.0f;
    w->godrays = 0.8f;
    Grade(w, 1.0f, 0.22f, 1.12f);
}

static Vector3 BoyFindSpot(void) { return G(-1.6f, 7.6f); }

/* 18. At the sunny edge of the shade, the boy finds an acorn in the grass. */
static void ShotFind(World *w, float lt)
{
    Late(w, 11.2f);
    Body bb = BodyFor(CH_BOY);
    Vector3 sp = BoyFindSpot();
    float yaw = -PI_F * 0.5f + 0.25f;
    Pose sq = PoseSquat(&bb, sp, yaw, lt);
    Vector3 f = Fwd(yaw);
    Vector3 pick = up(v3add(sp, v3scale(f, 0.3f)), 0.03f);
    float reach = smooth(1.0f, 2.0f, lt) * (1.0f - smooth(3.5f, 4.2f, lt));
    HandReach(&bb, &sq, 1, pick, v3(0, -1, 0), reach);
    float show = smooth(4.2f, 5.0f, lt);
    Vector3 showP = v3add(sq.root, v3add(v3scale(f, 0.24f), v3(0, 0.32f, 0)));
    HandReach(&bb, &sq, 1, showP, v3(0, 1, 0), show);
    sq.look = show > 0.5f ? showP : pick;
    sq.lookW = 1.0f;
    CharState *bo = Actor(w, CH_BOY, sq, lt, 4.0f);
    if (bo) {
        Face(bo, 0.4f + 0.5f * show, 0.6f * show);
        Gaze(bo, sq.look);
        if (lt > 3.5f) {
            Vector3 a = v3add(v3lerp(bo->j[J_RWRI], bo->j[J_RTIP], 0.45f), v3scale(bo->palmR, 0.022f));
            w->acorn = (Vector4){ a.x, a.y, a.z, 1.0f };
        } else {
            w->acorn = (Vector4){ pick.x, pick.y - 0.015f, pick.z, 1.0f };
        }
    }
    /* Mira on the bench, in the shade behind */
    Pose pw = SitPose(CH_WOMAN, 0.1f, lt);
    pw.look = up(sp, 0.5f);
    pw.lookW = 0.7f;
    CharState *wm = Actor(w, CH_WOMAN, pw, lt, 3.0f);
    if (wm) { wm->sit = 1.0f; Face(wm, 0.6f, 0.2f); Gaze(wm, up(sp, 0.5f)); }
    Vector3 tgt = up(v3add(sp, v3scale(f, 0.15f)), 0.32f);
    Vector3 cp = v3add(tgt, v3(0.4f, 0.55f, 1.8f));
    Cam(w, cp, v3add(tgt, v3(0.0f, 0.1f, -0.4f)), 30.0f);
    w->cam.aperture = 8.0f;
    w->cam.focus = v3len(v3sub(tgt, cp));
    w->pDust = 0.6f;
    w->pSeeds = 0.5f;
    Grade(w, 1.0f, 0.25f, 1.12f);
}

/* 19. "Mama, what's this?" — the words come back. */
static void ShotGift(World *w, float lt)
{
    Late(w, 11.4f);
    Body bb = BodyFor(CH_BOY), bw = BodyFor(CH_WOMAN);
    Pose pw = SitPose(CH_WOMAN, 0.1f, lt);
    Vector3 fwB = Fwd(kBenchYaw);
    Vector3 bp = v3add(G(gBench.x, gBench.z), v3add(v3scale(fwB, 0.75f), v3scale(Right(kBenchYaw), 0.05f)));
    bp.y = terrainHeight(bp.x, bp.z);
    Pose pb = PoseStand(&bb, bp, kBenchYaw + PI_F, lt);
    Vector3 wh = HeadOf(&bw, &pw);
    Vector3 showP = v3add(pb.root, v3add(v3scale(fwB, -0.22f), v3(0, 0.33f, 0)));
    HandReach(&bb, &pb, 1, showP, v3(0, 1, 0), 1.0f);
    pb.handDirR = v3add(v3scale(fwB, -1.0f), v3(0, 0.1f, 0));
    pb.look = lt < 4.5f ? wh : showP;
    pb.lookW = 1.0f;
    CharState *bo = Actor(w, CH_BOY, pb, lt, 4.0f);
    pw.lean = 0.25f;
    Vector3 acornP = showP;
    if (bo) {
        acornP = v3add(v3lerp(bo->j[J_RWRI], bo->j[J_RTIP], 0.45f), v3scale(bo->palmR, 0.022f));
        w->acorn = (Vector4){ acornP.x, acornP.y, acornP.z, 1.0f };
    }
    pw.look = lt < 5.0f ? acornP : HeadOf(&bb, &pb);
    pw.lookW = 1.0f;
    CharState *wm = Actor(w, CH_WOMAN, pw, lt, 3.0f);
    if (wm) {
        wm->sit = 1.0f;
        Face(wm, mixf(0.45f, 0.85f, smooth(4.0f, 6.0f, lt)), 0.4f);
        Gaze(wm, pw.look);
        wm->tear = smooth(5.0f, 8.0f, lt);
    }
    if (bo) { Face(bo, 0.7f, 0.5f); Gaze(bo, pb.look); }
    Vector3 mid = v3scale(v3add(wh, HeadOf(&bb, &pb)), 0.5f);
    Vector3 side = Right(kBenchYaw);
    Vector3 cp = v3add(mid, v3add(v3scale(side, -1.9f), v3add(v3scale(fwB, 0.5f), v3(0, -0.05f, 0))));
    Cam(w, cp, v3add(mid, v3(0, -0.1f, 0)), 30.0f);
    w->cam.aperture = 8.0f;
    w->cam.focus = v3len(v3sub(mid, cp));
    w->pDust = 1.0f;
    Grade(w, 1.0f, 0.25f, 1.12f);
}

/* 20. Planting again, a little way down the hill. */
static void ShotPlant2(World *w, float lt)
{
    Late(w, 16.7f + lt * 0.02f);
    Body bw = BodyFor(CH_WOMAN), bb = BodyFor(CH_BOY);
    Vector3 kp = G(gHole2.x + 0.05f, gHole2.z - 0.5f);
    Pose pw = PoseKneel(&bw, kp, 0.0f, lt);
    Vector3 hole = up(gHole2, 0.02f);
    float pat = smooth(1.0f, 1.5f, lt) * (1.0f - smooth(4.0f, 4.5f, lt));
    HandReach(&bw, &pw, 1, v3add(hole, v3(0.0f, 0.05f + 0.04f * fabsf(sinf(lt * 6.0f)) * pat, 0.06f)), v3(0, -1, 0), pat);
    HandReach(&bw, &pw, 0, v3add(hole, v3(0.1f, 0.05f + 0.04f * fabsf(sinf(lt * 6.0f + 1.5f)) * pat, 0.1f)), v3(0, -1, 0), pat);
    pw.look = hole;
    pw.lookW = 1.0f;
    CharState *wm = Actor(w, CH_WOMAN, pw, lt, 3.0f);
    if (wm) { Face(wm, 0.7f, 0.1f); Gaze(wm, hole); }
    Vector3 bp = G(gHole2.x - 0.6f, gHole2.z + 0.3f);
    float yaw = yawTo(bp, gHole2);
    Pose pb = PoseStand(&bb, bp, yaw, lt);
    float tilt = smooth(4.5f, 6.0f, lt);
    Vector3 f = Fwd(yaw), r = Right(yaw);
    Vector3 canHand = v3add(pb.root, v3add(v3scale(f, 0.2f), v3add(v3scale(r, 0.08f), v3(0, 0.12f + 0.06f * tilt, 0))));
    HandReach(&bb, &pb, 1, canHand, v3scale(r, -1.0f), 1.0f);
    HandReach(&bb, &pb, 0, v3add(canHand, v3add(v3scale(r, -0.1f), v3scale(f, 0.08f))), v3(0, 0, 0), tilt);
    pb.look = hole;
    pb.lookW = 1.0f;
    CharState *bo = Actor(w, CH_BOY, pb, lt, 4.0f);
    if (bo) {
        Face(bo, 0.8f, 0.3f);
        Gaze(bo, hole);
        Vector3 cp = v3add(bo->j[J_RWRI], v3(0, -0.12f, 0));
        w->can = (Vector4){ cp.x, cp.y, cp.z, yaw };
        w->canTilt = 0.15f + 0.65f * tilt;
        w->canShow = 1.0f;
        w->pWater = tilt;
    }
    w->mound2 = (Vector4){ gHole2.x, gHole2.y - 0.05f, gHole2.z, 0.2f * smooth(0.8f, 2.5f, lt) + 0.0001f };
    Cam(w, up(G(gHole2.x + 1.2f, gHole2.z + 2.4f), 1.0f), up(G(gHole2.x - 0.1f, gHole2.z - 0.2f), 0.45f), 32.0f);
    w->pSeeds = 0.6f;
    Grade(w, 1.0f, 0.4f, 1.14f);
}

/* 21. The camera rises: a hill crowned by an old oak, a new one just planted,
 *     and young trees on every hill around. */
static void ShotRise(World *w, float lt)
{
    Late(w, 17.05f + lt * 0.01f);
    float u = easeInOut(lt / 20.0f);
    Body bw = BodyFor(CH_WOMAN), bb = BodyFor(CH_BOY);
    Vector3 hole = up(gHole2, 0.02f);
    Vector3 wp = G(gHole2.x + 0.35f, gHole2.z + 0.55f), bp = G(gHole2.x - 0.25f, gHole2.z + 0.6f);
    Pose pw = PoseStand(&bw, wp, yawTo(wp, gHole2), lt);
    Pose pb = PoseStand(&bb, bp, yawTo(bp, gHole2), lt);
    Vector3 hold = v3scale(v3add(wp, bp), 0.5f);
    hold.y = bp.y + 0.55f;
    HandReach(&bw, &pw, 1, hold, v3(0, 0, 0), 1.0f);
    HandReach(&bb, &pb, 0, hold, v3(0, 0, 0), 1.0f);
    pw.look = up(gTop, 5.0f);
    pw.lookW = 0.7f * smooth(4.0f, 7.0f, lt);
    pb.look = HeadOf(&bw, &pw);
    pb.lookW = smooth(5.0f, 6.0f, lt);
    Actor(w, CH_WOMAN, pw, lt, 3.0f);
    Actor(w, CH_BOY, pb, lt, 4.0f);
    w->mound2 = (Vector4){ gHole2.x, gHole2.y - 0.05f, gHole2.z, 0.2f };
    w->sprout2 = (Vector4){ gHole2.x, gHole2.y, gHole2.z, 0.0f };
    (void)hole;
    /* young trees spreading over the surrounding hills */
    static const float spots[][2] = {
        { -44, -26 }, { -48, -32 }, { -40, -22 }, { 50, -40 }, { 56, -48 }, { 46, -50 },
        { 30, 60 }, { 38, 64 }, { -38, 38 }, { -44, 44 }, { -34, 44 }, { 26, -18 },
        { -22, 22 }, { 18, 34 }, { -28, -8 }, { 60, -36 }, { -52, -20 }, { 34, 54 },
        { -12, -26 }, { 8, -30 }, { 42, 20 }, { -60, 10 }, { 64, 4 }, { -6, 40 },
    };
    w->plantedCount = 24;
    for (int i = 0; i < 24; i++) {
        float x = spots[i][0], z = spots[i][1];
        float sz = 0.9f + 0.9f * hash1((float)i * 3.7f);
        w->planted[i] = (Vector4){ x, terrainHeight(x, z) - 0.05f, z, sz * (0.6f + 0.4f * smooth(0.0f, 1.0f, u * 1.4f - hash1((float)i) * 0.3f)) };
    }
    Vector3 c0 = up(G(gHole2.x + 1.6f, gHole2.z + 3.0f), 1.1f);
    Vector3 c1 = v3(-34.0f, gTop.y + 26.0f, 58.0f);
    Vector3 t0 = up(gHole2, 0.6f), t1 = up(gTop, 1.0f);
    Cam(w, v3lerp(c0, c1, u), v3lerp(t0, t1, ease(u * 1.3f)), mixf(32.0f, 40.0f, u));
    w->pSeeds = 1.0f;
    w->pBirds = 1.0f;
    w->godrays = 0.8f;
    Grade(w, 1.0f, 0.45f, 1.15f);
}

/* 22. End card over black. */
static void ShotEnd(World *w, float lt)
{
    Late(w, 17.3f);
    Cam(w, v3(-34.0f, gTop.y + 26.0f, 58.0f), up(gTop, 1.0f), 40.0f);
    w->exposure = 0.0f;
}

/* ======================================================================== */

typedef void (*ShotFn)(World *, float);
typedef struct {
    float dur;
    ShotFn fn;
    float xfade;   /* dissolve from the previous shot */
} Shot;

static Shot gShots[SH_COUNT] = {
    [SH_DAWN] = { 13.0f, ShotDawn, 0.0f },
    [SH_WALK] = { 13.0f, ShotWalk, 1.2f },
    [SH_ARRIVE] = { 10.0f, ShotArrive, 0.0f },
    [SH_DIG] = { 10.0f, ShotDig, 0.0f },
    [SH_ACORN] = { 11.0f, ShotAcorn, 0.0f },
    [SH_MIRA] = { 8.0f, ShotMira, 0.0f },
    [SH_NANA] = { 9.5f, ShotNana, 0.0f },
    [SH_PLANT] = { 11.0f, ShotPlant, 0.0f },
    [SH_SPROUT] = { 8.0f, ShotSprout, 1.5f },
    [SH_YEARS] = { 24.0f, ShotYears, 1.5f },
    [SH_SUNSET] = { 10.0f, ShotSunset, 1.5f },
    [SH_BENCH] = { 18.0f, ShotBench, 0.0f },
    [SH_DUSK] = { 11.0f, ShotDusk, 1.0f },
    [SH_EMPTY] = { 15.0f, ShotEmpty, 0.0f },
    [SH_GROW] = { 26.0f, ShotGrow, 0.0f },
    [SH_RETURN] = { 12.0f, ShotReturn, 1.5f },
    [SH_SHADE] = { 15.0f, ShotShade, 1.0f },
    [SH_FIND] = { 7.0f, ShotFind, 0.0f },
    [SH_GIFT] = { 12.0f, ShotGift, 0.0f },
    [SH_PLANT2] = { 9.0f, ShotPlant2, 1.0f },
    [SH_RISE] = { 20.0f, ShotRise, 0.0f },
    [SH_END] = { 14.0f, ShotEnd, 0.0f },
};
static float gStart[SH_COUNT + 1];

void StoryInit(void)
{
    PathInit();
    TreeInit(7u);
    gTop = G(0.0f, 0.0f);
    gBench = G(1.25f, 2.25f);
    gHole2 = G(-3.6f, 6.9f);
    float t = 0.0f;
    for (int i = 0; i < SH_COUNT; i++) { gStart[i] = t; t += gShots[i].dur; }
    gStart[SH_COUNT] = t;
}

float StoryDuration(void) { return gStart[SH_COUNT]; }
float StoryShotStart(int shot) { return gStart[shot < 0 ? 0 : (shot > SH_COUNT ? SH_COUNT : shot)]; }

static void EvalShot(int i, float t, World *w)
{
    float lt = t - gStart[i];
    gCurShot = i;
    gShotLocal = lt;
    WorldDefaults(w, t);
    gShots[i].fn(w, lt);
}

void StoryEvaluate(float t, Frame *f)
{
    memset(f, 0, sizeof(*f));
    int i = 0;
    while (i < SH_COUNT - 1 && t >= gStart[i + 1]) i++;
    float lt = t - gStart[i];
    if (gShots[i].xfade > 0.0f && lt < gShots[i].xfade && i > 0) {
        EvalShot(i - 1, t, &f->a);
        EvalShot(i, t, &f->b);
        f->mix = lt / gShots[i].xfade;
    } else {
        EvalShot(i, t, &f->a);
        f->mix = 0.0f;
    }
    f->letterbox = 1.0f;

    /* fades */
    float total = gStart[SH_COUNT];
    float fade = 1.0f - smooth(0.0f, 3.0f, t);
    float duskEnd = gStart[SH_DUSK + 1];
    fade = fmaxf(fade, smooth(duskEnd - 2.5f, duskEnd, t) * (1.0f - smooth(duskEnd, duskEnd + 2.0f, t)));
    float riseEnd = gStart[SH_END];
    fade = fmaxf(fade, smooth(riseEnd - 3.0f, riseEnd, t));
    f->fade = fade;

    /* title over the dawn */
    if (i == SH_DAWN) {
        f->title = "Tomorrow's Shade";
        f->subtitle = "a short film";
        f->titleAlpha = smooth(3.5f, 5.5f, lt) * (1.0f - smooth(10.0f, 12.0f, lt));
        f->titleStyle = 0;
    }
    if (i == SH_END) {
        f->title = "\"A society grows great when old men plant trees\nwhose shade they know they shall never sit in.\"";
        f->subtitle = "- Greek proverb -";
        f->titleAlpha = smooth(1.0f, 3.0f, lt) * (1.0f - smooth(9.0f, 11.0f, lt));
        f->titleStyle = 1;
        if (lt > 10.5f) {
            f->title = "Plant something today.";
            f->subtitle = NULL;
            f->titleAlpha = smooth(10.8f, 11.8f, lt) * (1.0f - smooth(13.0f, 14.0f, lt));
            f->titleStyle = 2;
        }
    }
    /* captions */
    for (int k = 0; k < NLINES; k++) {
        const Line *l = &gLines[k];
        if (l->shot != i) continue;
        if (lt >= l->t0 && lt <= l->t1) {
            f->caption = l->text;
            f->captionAlpha = smooth(l->t0, l->t0 + 0.35f, lt) * (1.0f - smooth(l->t1 - 0.35f, l->t1, lt));
        }
    }
    (void)total;
}
