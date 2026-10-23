#include "tree.h"

#include <math.h>
#include <string.h>

/* The oak is grown once at full maturity as a hierarchy of curved limbs, then
 * every frame it is "replayed" at the requested growth: the whole form is
 * scaled (tall and slender when young, broad when old), finer branches only
 * appear as the tree ages, and the trunk thickens. */

#define MAX_BRANCHES 96

typedef struct {
    int parent;
    float attach;      /* 0..1 along the parent */
    Vector3 dir;
    float len, r0, r1;
    int nseg;
    float bend;        /* upward curvature per segment */
    int depth;
    int group;
    float appear;      /* growth at which it starts to appear */
    float leaf;        /* foliage blob radius at the tip (0 = none) */
    float phase;
} Branch;

static Branch gB[MAX_BRANCHES];
static int gBC;
static unsigned gSeed;

static float rnd(void)
{
    gSeed = gSeed * 1664525u + 1013904223u;
    return (float)((gSeed >> 8) & 0xFFFFFF) / 16777215.0f;
}
static float rr(float a, float b) { return a + (b - a) * rnd(); }

static Vector3 dirFrom(float azim, float elev)
{
    return v3(cosf(elev) * cosf(azim), sinf(elev), cosf(elev) * sinf(azim));
}

static int addBranch(int parent, float attach, Vector3 dir, float len, float r0, float r1, int nseg, float bend, int depth, int group, float appear, float leaf)
{
    if (gBC >= MAX_BRANCHES) return -1;
    Branch *b = &gB[gBC];
    b->parent = parent; b->attach = attach; b->dir = v3norm(dir); b->len = len;
    b->r0 = r0; b->r1 = r1; b->nseg = nseg; b->bend = bend; b->depth = depth;
    b->group = group; b->appear = appear; b->leaf = leaf; b->phase = rnd() * 6.28f;
    return gBC++;
}

/* deviate a direction by a random cone angle */
static Vector3 deviate(Vector3 d, float minA, float maxA, float upBias)
{
    Vector3 t = fabsf(d.y) < 0.9f ? v3cross(d, v3(0, 1, 0)) : v3cross(d, v3(1, 0, 0));
    t = v3norm(t);
    Vector3 bt = v3cross(d, t);
    float a = rr(minA, maxA), r = rr(0.0f, 6.2831f);
    Vector3 o = v3add(v3scale(t, cosf(r)), v3scale(bt, sinf(r)));
    Vector3 nd = v3add(v3scale(d, cosf(a)), v3scale(o, sinf(a)));
    nd.y += upBias;
    return v3norm(nd);
}

void TreeInit(unsigned seed)
{
    gSeed = seed;
    gBC = 0;
    /* trunk */
    int trunk = addBranch(-1, 0.0f, v3(0.05f, 1.0f, 0.02f), 3.3f, 0.72f, 0.5f, 3, 0.0f, 0, 0, 0.0f, 0.0f);
    /* root flare */
    for (int i = 0; i < 5; i++) {
        float a = i * 1.2566f + rr(-0.3f, 0.3f);
        addBranch(trunk, 0.1f, v3(cosf(a), -0.3f, sinf(a)), 1.6f, 0.34f, 0.05f, 2, -0.08f, 1, 0, 0.3f, 0.0f);
    }
    /* main limbs, spreading wide like an old field oak */
    int nl = 6;
    for (int i = 0; i < nl; i++) {
        float az = i * (6.2831f / nl) + rr(-0.3f, 0.3f);
        float el = rr(0.42f, 0.85f);
        float attach = i == 0 ? 0.7f : rr(0.84f, 1.0f);
        int limb = addBranch(trunk, attach, dirFrom(az, el), rr(4.8f, 6.0f), 0.36f, 0.15f, 3, rr(-0.04f, 0.04f), 1, 1 + i, 0.06f, 0.0f);
        /* sub branches */
        for (int k = 0; k < 3; k++) {
            float at = 0.36f + 0.32f * k + rr(-0.05f, 0.05f);
            if (at > 1.0f) at = 1.0f;
            Vector3 d = deviate(gB[limb].dir, 0.35f, 0.8f, 0.3f);
            int sub = addBranch(limb, at, d, rr(2.4f, 3.4f), 0.15f, 0.06f, 2, 0.05f, 2, 7 + i * 3 + k, 0.14f + 0.03f * k, rr(1.5f, 1.9f));
            int nt = 2 + (rnd() > 0.6f);
            for (int m = 0; m < nt; m++) {
                Vector3 td = deviate(gB[sub].dir, 0.4f, 0.95f, 0.25f);
                addBranch(sub, 0.5f + 0.5f * m / (nt - 1 > 0 ? nt - 1 : 1), td, rr(1.3f, 2.0f), 0.06f, 0.022f, 1, 0.02f, 3, 7 + i * 3 + k, 0.3f + 0.05f * m, rr(1.4f, 1.9f));
            }
        }
    }
    /* a crown leader */
    int lead = addBranch(trunk, 1.0f, v3(0.0f, 1.0f, 0.1f), 4.2f, 0.26f, 0.08f, 2, 0.0f, 1, 25, 0.0f, 2.0f);
    for (int k = 0; k < 2; k++) {
        Vector3 d = deviate(gB[lead].dir, 0.5f, 0.8f, 0.1f);
        addBranch(lead, 0.55f + 0.45f * k, d, rr(2.0f, 2.6f), 0.09f, 0.03f, 1, 0.0f, 2, 25, 0.2f, rr(1.7f, 2.0f));
    }
}

typedef struct {
    Vector3 pts[5];
    float rad[5];
    int n;
    float grown;
} Chain;

static Chain gC[MAX_BRANCHES];

static Vector3 chainPoint(const Chain *c, float t, float *radius)
{
    if (c->n <= 0) { *radius = 0; return c->pts[0]; }
    float f = t * c->n;
    int i = (int)f;
    if (i >= c->n) i = c->n - 1;
    float u = f - i;
    *radius = mixf(c->rad[i], c->rad[i + 1], u);
    return v3lerp(c->pts[i], c->pts[i + 1], u);
}

static float growthScaleY(float g) { return 0.035f + 0.965f * powf(g, 0.85f); }
static float growthScaleXZ(float g) { return 0.02f + 0.98f * powf(g, 1.25f); }
static float thickness(float g) { return 0.03f + 0.97f * powf(g, 1.6f); }

void TreeBuild(TreeState *ts, Vector3 base, float growth, float leafAmt, float wind, float time)
{
    memset(ts, 0, sizeof(*ts));
    ts->growth = growth;
    float g = clampf(growth, 0.0f, 1.0f);
    ts->sprout = 0.0f;
    if (g <= 0.0f) return;
    float sy = growthScaleY(g), sxz = growthScaleXZ(g), th = thickness(g);

    /* replay branches */
    for (int i = 0; i < gBC; i++) {
        Branch *b = &gB[i];
        Chain *c = &gC[i];
        c->n = 0;
        float span = 0.25f + 0.1f * b->depth;
        float grown = b->depth == 0 ? 1.0f : saturate((g - b->appear) / span);
        if (b->parent >= 0 && gC[b->parent].grown <= 0.0f) grown = 0.0f;
        c->grown = grown;
        if (grown <= 0.0f) continue;
        Vector3 start;
        float pr = b->r0;
        if (b->parent < 0) start = v3(0, -0.3f, 0);
        else start = chainPoint(&gC[b->parent], b->attach, &pr);
        float rscale = th * (0.35f + 0.65f * grown);
        float r0 = fminf(b->r0 * rscale, pr * 0.95f);
        if (b->parent < 0) r0 = b->r0 * rscale;
        float r1 = b->r1 * rscale * (0.6f + 0.4f * grown);
        if (r1 > r0) r1 = r0;
        Vector3 d = b->dir;
        float segLen = b->len * grown / b->nseg;
        c->pts[0] = start;
        c->rad[0] = r0;
        Vector3 p = start;
        for (int k = 0; k < b->nseg; k++) {
            d.y += b->bend;
            d = v3norm(d);
            /* scale: tall-and-slender when young */
            Vector3 step = v3(d.x * segLen * sxz, d.y * segLen * sy, d.z * segLen * sxz);
            /* gentle wind sway grows towards the tips */
            float sw = wind * 0.035f * b->depth * (k + 1) * sinf(time * (1.1f + 0.2f * b->depth) + b->phase);
            step.x += sw * sxz;
            step.z += sw * 0.4f * sxz;
            p = v3add(p, step);
            c->pts[k + 1] = p;
            c->rad[k + 1] = mixf(r0, r1, (float)(k + 1) / b->nseg);
        }
        c->n = b->nseg;
    }

    /* emit segments ordered by group */
    int groups = 0;
    for (int i = 0; i < gBC; i++) if (gB[i].group + 1 > groups) groups = gB[i].group + 1;
    ts->groupCount = 0;
    ts->segCount = 0;
    Vector3 wmin = v3(1e9f, 1e9f, 1e9f), wmax = v3(-1e9f, -1e9f, -1e9f);
    for (int gi = 0; gi < groups && gi < MAX_GROUPS; gi++) {
        int s0 = ts->segCount;
        Vector3 gmin = v3(1e9f, 1e9f, 1e9f), gmax = v3(-1e9f, -1e9f, -1e9f);
        float rmax = 0.0f;
        for (int i = 0; i < gBC; i++) {
            if (gB[i].group != gi || gC[i].n == 0) continue;
            Chain *c = &gC[i];
            for (int k = 0; k < c->n && ts->segCount < MAX_SEGS; k++) {
                Vector3 a = v3add(base, c->pts[k]);
                Vector3 b = v3add(base, c->pts[k + 1]);
                if (c->rad[k] < 0.003f) continue;
                ts->segA[ts->segCount] = (Vector4){ a.x, a.y, a.z, c->rad[k] };
                ts->segB[ts->segCount] = (Vector4){ b.x, b.y, b.z, fmaxf(c->rad[k + 1], 0.002f) };
                ts->segCount++;
                gmin = Vector3Min(gmin, Vector3Min(a, b));
                gmax = Vector3Max(gmax, Vector3Max(a, b));
                rmax = fmaxf(rmax, c->rad[k]);
            }
        }
        if (ts->segCount > s0) {
            Vector3 cen = v3scale(v3add(gmin, gmax), 0.5f);
            float r = v3len(v3sub(gmax, cen)) + rmax + 0.05f;
            ts->groupBound[ts->groupCount] = (Vector4){ cen.x, cen.y, cen.z, r };
            ts->groupRange[ts->groupCount * 2] = (float)s0;
            ts->groupRange[ts->groupCount * 2 + 1] = (float)ts->segCount;
            ts->groupCount++;
            wmin = Vector3Min(wmin, v3sub(cen, v3(r, r, r)));
            wmax = Vector3Max(wmax, v3add(cen, v3(r, r, r)));
        }
    }
    if (ts->segCount > 0) {
        Vector3 cen = v3scale(v3add(wmin, wmax), 0.5f);
        ts->woodBound = (Vector4){ cen.x, cen.y, cen.z, v3len(v3sub(wmax, cen)) * 0.75f };
    }

    /* foliage, emitted per limb group so the shader can skip distant clusters */
    ts->blobCount = 0;
    ts->blobGroupCount = 0;
    if (leafAmt > 0.01f) {
        Vector3 cmin = v3(1e9f, 1e9f, 1e9f), cmax = v3(-1e9f, -1e9f, -1e9f);
        float sl = sqrtf(sy * sxz);
        for (int gi = 0; gi < groups && gi < MAX_GROUPS; gi++) {
            int b0 = ts->blobCount;
            for (int i = 0; i < gBC && ts->blobCount < MAX_BLOBS; i++) {
                Branch *b = &gB[i];
                Chain *c = &gC[i];
                if (c->n == 0 || b->group != gi) continue;
                /* foliage sits at tips; if the children have not grown yet the branch carries it */
                float leaf = b->leaf;
                int hasKids = 0;
                for (int k = 0; k < gBC; k++) if (gB[k].parent == i && gC[k].grown > 0.15f) hasKids = 1;
                if (leaf <= 0.0f && b->depth >= 1 && b->group != 0 && !hasKids) leaf = 1.5f;
                if (b->depth == 0 && g < 0.12f) leaf = 1.6f; /* sapling crown */
                if (b->depth == 1 && b->group != 0 && leaf <= 0.0f) leaf = 2.1f; /* inner mass */
                if (leaf <= 0.0f) continue;
                float r = leaf * sl * leafAmt * (0.4f + 0.6f * c->grown);
                if (g < 0.12f) r = fmaxf(r, 0.25f * leafAmt * sy * 3.0f);
                if (r < 0.03f) continue;
                Vector3 tip = v3add(base, c->pts[c->n]);
                tip.y += r * 0.25f;
                ts->blob[ts->blobCount++] = (Vector4){ tip.x, tip.y, tip.z, r };
                /* a second puff halfway along leafy sub-branches for fullness */
                if (b->depth == 2 && ts->blobCount < MAX_BLOBS) {
                    Vector3 mid = v3add(base, v3lerp(c->pts[0], c->pts[c->n], 0.55f));
                    mid.y += r * 0.35f;
                    ts->blob[ts->blobCount++] = (Vector4){ mid.x, mid.y, mid.z, r * 0.85f };
                }
            }
            if (ts->blobCount > b0) {
                Vector3 gmin = v3(1e9f, 1e9f, 1e9f), gmax = v3(-1e9f, -1e9f, -1e9f);
                for (int i = b0; i < ts->blobCount; i++) {
                    Vector4 bb = ts->blob[i];
                    gmin = Vector3Min(gmin, v3(bb.x - bb.w, bb.y - bb.w, bb.z - bb.w));
                    gmax = Vector3Max(gmax, v3(bb.x + bb.w, bb.y + bb.w, bb.z + bb.w));
                }
                Vector3 cen = v3scale(v3add(gmin, gmax), 0.5f);
                int k = ts->blobGroupCount++;
                ts->blobGroupBound[k] = (Vector4){ cen.x, cen.y, cen.z, v3len(v3sub(gmax, cen)) * 0.85f + 0.5f };
                ts->blobGroupRange[k * 2] = (float)b0;
                ts->blobGroupRange[k * 2 + 1] = (float)ts->blobCount;
                cmin = Vector3Min(cmin, gmin);
                cmax = Vector3Max(cmax, gmax);
            }
        }
        if (ts->blobCount > 0) {
            Vector3 cen = v3scale(v3add(cmin, cmax), 0.5f);
            ts->canopyBound = (Vector4){ cen.x, cen.y, cen.z, v3len(v3sub(cmax, cen)) * 0.8f + 0.6f };
            Vector3 ext = v3scale(v3sub(cmax, cmin), 0.5f);
            ts->canopyExt = (Vector4){ ext.x, ext.y, ext.z, 0.0f };
            float dome = smooth(0.3f, 0.7f, g) * leafAmt;
            ts->hullC = (Vector4){ cen.x, cen.y + ext.y * 0.22f, cen.z, 0.0f };
            ts->hullR = (Vector4){ ext.x * 0.8f * dome, ext.y * 0.74f * dome, ext.z * 0.8f * dome, sl };
            if (dome < 0.05f) ts->hullR.x = ts->hullR.y = ts->hullR.z = 0.0f;
        }
    }
}

Vector3 TreeSwingAnchor(Vector3 base, float growth)
{
    /* the first limb is the low one the swing hangs from; pick a point 55% along it */
    float r;
    for (int i = 0; i < gBC; i++) {
        if (gB[i].depth == 1 && gB[i].group == 1) {
            Vector3 p = chainPoint(&gC[i], 0.5f, &r);
            return v3add(base, v3(p.x, p.y - r, p.z));
        }
    }
    return v3add(base, v3(2.5f, 4.0f, 0.0f));
}
