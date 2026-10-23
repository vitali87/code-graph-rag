#include "character.h"

#include <math.h>
#include <string.h>

/* ------------------------------------------------------------------------ */
/* footpath winding up the hill                                              */

Vector3 gPath[PATH_PTS];
#define PATH_SAMPLES 256
static Vector3 gPathS[PATH_SAMPLES];
static float gPathLen[PATH_SAMPLES];

static const float kCtrl[][2] = {
    { 36.0f, 56.0f }, { 29.0f, 44.0f }, { 25.0f, 33.0f }, { 18.0f, 24.0f },
    { 12.5f, 16.0f }, { 7.0f, 10.0f }, { 3.4f, 5.6f }, { 1.4f, 2.4f }, { 0.6f, 1.2f },
};
#define NCTRL ((int)(sizeof(kCtrl) / sizeof(kCtrl[0])))

static Vector3 CtrlPt(int i)
{
    if (i < 0) i = 0;
    if (i >= NCTRL) i = NCTRL - 1;
    return v3(kCtrl[i][0], 0, kCtrl[i][1]);
}

static Vector3 SplineAt(float u)
{
    float f = u * (NCTRL - 1);
    int i = (int)f;
    if (i >= NCTRL - 1) i = NCTRL - 2;
    float t = f - i;
    Vector3 p = catmull(CtrlPt(i - 1), CtrlPt(i), CtrlPt(i + 1), CtrlPt(i + 2), t);
    p.y = terrainHeight(p.x, p.z);
    return p;
}

void PathInit(void)
{
    float acc = 0.0f;
    for (int i = 0; i < PATH_SAMPLES; i++) {
        gPathS[i] = SplineAt((float)i / (PATH_SAMPLES - 1));
        if (i > 0) acc += v3len(v3sub(gPathS[i], gPathS[i - 1]));
        gPathLen[i] = acc;
    }
    for (int i = 0; i < PATH_PTS; i++) gPath[i] = SplineAt((float)i / (PATH_PTS - 1));
}

float PathLength(void) { return gPathLen[PATH_SAMPLES - 1]; }

Vector3 PathPos(float s)
{
    if (s <= 0.0f) return gPathS[0];
    if (s >= gPathLen[PATH_SAMPLES - 1]) return gPathS[PATH_SAMPLES - 1];
    int lo = 0, hi = PATH_SAMPLES - 1;
    while (hi - lo > 1) {
        int mid = (lo + hi) / 2;
        if (gPathLen[mid] < s) lo = mid; else hi = mid;
    }
    float t = (s - gPathLen[lo]) / fmaxf(gPathLen[hi] - gPathLen[lo], 1e-6f);
    Vector3 p = v3lerp(gPathS[lo], gPathS[hi], t);
    p.y = terrainHeight(p.x, p.z);
    return p;
}

Vector3 PathPoint(float s01) { return PathPos(s01 * PathLength()); }

float PathYaw(float s)
{
    Vector3 a = PathPos(s - 0.4f), b = PathPos(s + 0.4f);
    return atan2f(b.x - a.x, b.z - a.z);
}

/* ------------------------------------------------------------------------ */

Body BodyFor(CharType t)
{
    Body b = { 0 };
    b.type = t;
    switch (t) {
    case CH_GRANDMA:
        b = (Body){ t, 0.8f, 0.4f, 0.06f, 0.112f, 0.165f, 0.1f, 0.265f, 0.23f, 0.15f, 0.4f, 0.37f, 0.07f, 0.2f };
        break;
    case CH_GIRL:
        b = (Body){ t, 0.6f, 0.29f, 0.045f, 0.106f, 0.115f, 0.068f, 0.19f, 0.17f, 0.12f, 0.3f, 0.28f, 0.05f, 0.16f };
        break;
    case CH_WOMAN:
        b = (Body){ t, 0.91f, 0.45f, 0.07f, 0.112f, 0.17f, 0.1f, 0.29f, 0.26f, 0.18f, 0.45f, 0.43f, 0.07f, 0.23f };
        break;
    case CH_BOY:
        b = (Body){ t, 0.57f, 0.27f, 0.042f, 0.104f, 0.112f, 0.066f, 0.18f, 0.16f, 0.11f, 0.29f, 0.26f, 0.05f, 0.15f };
        break;
    }
    return b;
}

Vector3 Fwd(float yaw) { return v3(sinf(yaw), 0.0f, cosf(yaw)); }
Vector3 Right(float yaw) { return v3(-cosf(yaw), 0.0f, sinf(yaw)); }

static Vector3 rotAxis(Vector3 v, Vector3 axis, float a)
{
    axis = v3norm(axis);
    float c = cosf(a), s = sinf(a);
    return v3add(v3add(v3scale(v, c), v3scale(v3cross(axis, v), s)), v3scale(axis, v3dot(axis, v) * (1.0f - c)));
}

/* two-bone IK; returns the middle joint, writes the reached end point */
static Vector3 SolveIK(Vector3 a, Vector3 target, float l1, float l2, Vector3 pole, Vector3 *end)
{
    Vector3 d = v3sub(target, a);
    float dist = v3len(d);
    Vector3 dir = v3norm(d);
    float maxd = (l1 + l2) * 0.999f, mind = fabsf(l1 - l2) + 0.01f;
    dist = clampf(dist, mind, maxd);
    float x = (l1 * l1 - l2 * l2 + dist * dist) / (2.0f * dist);
    float h = sqrtf(fmaxf(l1 * l1 - x * x, 0.0f));
    Vector3 perp = v3sub(pole, v3scale(dir, v3dot(pole, dir)));
    if (v3len(perp) < 1e-4f) perp = v3cross(dir, v3(1, 0, 0));
    perp = v3norm(perp);
    *end = v3add(a, v3scale(dir, dist));
    return v3add(v3add(a, v3scale(dir, x)), v3scale(perp, h));
}

void PoseSolve(const Body *b, const Pose *p, CharState *o)
{
    Vector3 up0 = v3(0, 1, 0);
    Vector3 fwd = Fwd(p->yaw), right = Right(p->yaw);
    Vector3 pel = p->root;
    /* spine */
    Vector3 up = v3norm(v3add(v3add(v3scale(up0, cosf(p->lean)), v3scale(fwd, sinf(p->lean))), v3scale(right, sinf(p->side))));
    Vector3 cfwd = rotAxis(fwd, up, p->twist);
    cfwd = v3norm(v3sub(cfwd, v3scale(up, v3dot(cfwd, up))));
    Vector3 cright = v3cross(cfwd, up);
    float stoop = b->type == CH_GRANDMA ? 0.12f : 0.0f;
    Vector3 belly = v3add(pel, v3add(v3scale(up, b->spine * 0.5f), v3scale(cfwd, 0.01f)));
    Vector3 chestUp = v3norm(v3add(up, v3scale(cfwd, stoop)));
    Vector3 chest = v3add(belly, v3scale(chestUp, b->spine * 0.5f));
    Vector3 neck = v3add(chest, v3scale(chestUp, b->neck * 0.5f));
    o->j[J_PELVIS] = pel;
    o->j[J_BELLY] = belly;
    o->j[J_CHEST] = chest;
    o->j[J_NECK] = neck;

    /* head */
    Vector3 hf = cfwd;
    float hpitch = p->headPitch + (b->type == CH_GRANDMA ? 0.08f : 0.0f);
    if (p->lookW > 0.0f) {
        Vector3 headApprox = v3add(neck, v3scale(chestUp, b->neck + b->headOff));
        Vector3 want = v3norm(v3sub(p->look, headApprox));
        /* limit how far the head can turn */
        float d = v3dot(want, cfwd);
        if (d < 0.1f) want = v3norm(v3add(want, v3scale(cfwd, 0.1f - d + 0.2f)));
        hf = v3norm(v3lerp(cfwd, want, p->lookW));
    }
    hf = rotAxis(hf, up0, p->headYaw);
    Vector3 hr = v3norm(v3cross(hf, up0));
    hf = rotAxis(hf, hr, -hpitch);
    Vector3 hu = v3cross(hr, hf);
    hr = rotAxis(hr, hf, p->headRoll);
    hu = rotAxis(hu, hf, p->headRoll);
    Vector3 neckTop = v3add(neck, v3scale(v3norm(v3add(chestUp, hu)), b->neck * 0.5f));
    Vector3 head = v3add(neckTop, v3add(v3scale(hu, b->headOff), v3scale(hf, 0.012f)));
    o->j[J_HEAD] = head;
    o->j[J_HEADR] = hr;
    o->j[J_HEADU] = hu;
    o->j[J_HEADF] = hf;

    /* arms */
    Vector3 lsho = v3add(chest, v3add(v3scale(cright, -b->shoulderW), v3scale(chestUp, -0.02f)));
    Vector3 rsho = v3add(chest, v3add(v3scale(cright, b->shoulderW), v3scale(chestUp, -0.02f)));
    o->j[J_LSHO] = lsho;
    o->j[J_RSHO] = rsho;
    Vector3 end;
    Vector3 poleL = v3len(p->elbowL) > 0.01f ? p->elbowL : v3add(v3scale(cfwd, -0.6f), v3add(v3scale(cright, -0.4f), v3scale(up0, -0.3f)));
    Vector3 poleR = v3len(p->elbowR) > 0.01f ? p->elbowR : v3add(v3scale(cfwd, -0.6f), v3add(v3scale(cright, 0.4f), v3scale(up0, -0.3f)));
    o->j[J_LELB] = SolveIK(lsho, p->handL, b->upperArm, b->foreArm, poleL, &end);
    o->j[J_LWRI] = end;
    Vector3 hdl = v3len(p->handDirL) > 0.01f ? v3norm(p->handDirL) : v3norm(v3sub(end, o->j[J_LELB]));
    o->j[J_LTIP] = v3add(end, v3scale(hdl, b->handLen));
    o->j[J_RELB] = SolveIK(rsho, p->handR, b->upperArm, b->foreArm, poleR, &end);
    o->j[J_RWRI] = end;
    Vector3 hdr = v3len(p->handDirR) > 0.01f ? v3norm(p->handDirR) : v3norm(v3sub(end, o->j[J_RELB]));
    o->j[J_RTIP] = v3add(end, v3scale(hdr, b->handLen));
    o->palmL = v3len(p->palmL) > 0.01f ? v3norm(p->palmL) : v3norm(v3cross(hdl, cfwd));
    o->palmR = v3len(p->palmR) > 0.01f ? v3norm(p->palmR) : v3norm(v3cross(cfwd, hdr));

    /* legs */
    Vector3 hipC = v3add(pel, v3scale(up, -0.04f));
    Vector3 lhip = v3add(hipC, v3scale(right, -b->hipW));
    Vector3 rhip = v3add(hipC, v3scale(right, b->hipW));
    o->j[J_LHIP] = lhip;
    o->j[J_RHIP] = rhip;
    Vector3 kpL = v3len(p->kneeL) > 0.01f ? p->kneeL : fwd;
    Vector3 kpR = v3len(p->kneeR) > 0.01f ? p->kneeR : fwd;
    o->j[J_LKNE] = SolveIK(lhip, p->footL, b->thigh, b->shin, kpL, &end);
    o->j[J_LANK] = end;
    Vector3 tl = v3len(p->toeL) > 0.01f ? v3norm(p->toeL) : fwd;
    o->j[J_LTOE] = v3add(end, v3scale(tl, b->footLen * 0.8f));
    o->j[J_LTOE].y -= b->ankleH * 0.5f;
    o->j[J_RKNE] = SolveIK(rhip, p->footR, b->thigh, b->shin, kpR, &end);
    o->j[J_RANK] = end;
    Vector3 tr = v3len(p->toeR) > 0.01f ? v3norm(p->toeR) : fwd;
    o->j[J_RTOE] = v3add(end, v3scale(tr, b->footLen * 0.8f));
    o->j[J_RTOE].y -= b->ankleH * 0.5f;

    o->caneTip = v3add(v3add(o->j[J_RWRI], v3scale(fwd, 0.06f)), v3scale(right, 0.04f));
    o->caneTip.y = terrainHeight(o->caneTip.x, o->caneTip.z);
    o->type = b->type;
    o->active = 1;
}

static Vector3 lerp3(Vector3 a, Vector3 b, float t) { return v3lerp(a, b, t); }

Pose PoseLerp(const Pose *a, const Pose *b, float t)
{
    Pose r;
    r.root = lerp3(a->root, b->root, t);
    float dy = b->yaw - a->yaw;
    while (dy > PI_F) dy -= TAU_F;
    while (dy < -PI_F) dy += TAU_F;
    r.yaw = a->yaw + dy * t;
    r.lean = mixf(a->lean, b->lean, t);
    r.side = mixf(a->side, b->side, t);
    r.twist = mixf(a->twist, b->twist, t);
    r.look = lerp3(a->look, b->look, t);
    r.lookW = mixf(a->lookW, b->lookW, t);
    r.headPitch = mixf(a->headPitch, b->headPitch, t);
    r.headYaw = mixf(a->headYaw, b->headYaw, t);
    r.headRoll = mixf(a->headRoll, b->headRoll, t);
    r.handL = lerp3(a->handL, b->handL, t);
    r.handR = lerp3(a->handR, b->handR, t);
    r.handDirL = lerp3(a->handDirL, b->handDirL, t);
    r.handDirR = lerp3(a->handDirR, b->handDirR, t);
    r.palmL = lerp3(a->palmL, b->palmL, t);
    r.palmR = lerp3(a->palmR, b->palmR, t);
    r.elbowL = lerp3(a->elbowL, b->elbowL, t);
    r.elbowR = lerp3(a->elbowR, b->elbowR, t);
    r.footL = lerp3(a->footL, b->footL, t);
    r.footR = lerp3(a->footR, b->footR, t);
    r.toeL = lerp3(a->toeL, b->toeL, t);
    r.toeR = lerp3(a->toeR, b->toeR, t);
    r.kneeL = lerp3(a->kneeL, b->kneeL, t);
    r.kneeR = lerp3(a->kneeR, b->kneeR, t);
    return r;
}

static Vector3 groundAt(Vector3 p, float lift)
{
    return v3(p.x, terrainHeight(p.x, p.z) + lift, p.z);
}

void ArmsRelaxed(const Body *b, Pose *p, float t)
{
    Vector3 fwd = Fwd(p->yaw), right = Right(p->yaw);
    float armLen = b->upperArm + b->foreArm;
    Vector3 chest = v3add(p->root, v3scale(v3(0, 1, 0), b->spine));
    float breath = 0.006f * sinf(t * 1.6f);
    p->handL = v3add(chest, v3add(v3scale(right, -b->shoulderW - 0.04f), v3add(v3scale(fwd, 0.05f), v3(0, -armLen * 0.97f + breath, 0))));
    p->handR = v3add(chest, v3add(v3scale(right, b->shoulderW + 0.04f), v3add(v3scale(fwd, 0.05f), v3(0, -armLen * 0.97f + breath, 0))));
    p->handDirL = v3add(v3(0, -1, 0), v3scale(fwd, 0.15f));
    p->handDirR = v3add(v3(0, -1, 0), v3scale(fwd, 0.15f));
    p->palmL = right;
    p->palmR = v3scale(right, -1.0f);
}

void HandReach(const Body *b, Pose *p, int right, Vector3 target, Vector3 palm, float w)
{
    if (right) {
        p->handR = v3lerp(p->handR, target, w);
        if (v3len(palm) > 0.01f) p->palmR = v3lerp(p->palmR, palm, w);
    } else {
        p->handL = v3lerp(p->handL, target, w);
        if (v3len(palm) > 0.01f) p->palmL = v3lerp(p->palmL, palm, w);
    }
}

static void BasePose(Pose *p, const Body *b, Vector3 ground, float yaw)
{
    memset(p, 0, sizeof(*p));
    p->root = v3add(ground, v3(0, b->pelvisH, 0));
    p->yaw = yaw;
    Vector3 fwd = Fwd(yaw), right = Right(yaw);
    p->look = v3add(p->root, v3add(v3scale(fwd, 5.0f), v3(0, b->spine + 0.2f, 0)));
    p->lookW = 0.0f;
    p->footL = groundAt(v3add(ground, v3scale(right, -b->hipW * 1.1f)), b->ankleH);
    p->footR = groundAt(v3add(ground, v3scale(right, b->hipW * 1.1f)), b->ankleH);
    p->toeL = v3rotY(fwd, 0.15f);
    p->toeR = v3rotY(fwd, -0.15f);
}

Pose PoseStand(const Body *b, Vector3 ground, float yaw, float t)
{
    Pose p;
    BasePose(&p, b, ground, yaw);
    float sway = 0.008f * sinf(t * 0.7f);
    p.root = v3add(p.root, v3scale(Right(yaw), sway));
    p.root.y += 0.004f * sinf(t * 1.6f) - 0.01f;
    p.lean = 0.03f + 0.01f * sinf(t * 1.6f + 1.0f);
    if (b->type == CH_GRANDMA) p.lean += 0.06f;
    ArmsRelaxed(b, &p, t);
    return p;
}

/* Walk with planted feet. s = distance along the footpath (metres). */
Pose PoseWalk(const Body *b, float s, float t, float stepLen, float lateral)
{
    Pose p;
    float yawOffset = 0.0f;
    Vector3 pos = PathPos(s);
    float yaw = PathYaw(s) + yawOffset;
    pos = v3add(pos, v3scale(Right(yaw), lateral));
    pos.y = terrainHeight(pos.x, pos.z);
    BasePose(&p, b, pos, yaw);
    float stride = 2.0f * stepLen;
    float ph = s / stride;
    Vector3 up = v3(0, 1, 0);

    for (int side = 0; side < 2; side++) {
        float fp = ph + (side == 0 ? 0.0f : 0.5f);
        float k = floorf(fp), u = fp - k;
        float sMid0 = (k + 0.3f - (side == 0 ? 0.0f : 0.5f)) * stride;
        float sMid1 = sMid0 + stride;
        float lat = (side == 0 ? -1.0f : 1.0f) * b->hipW * 0.95f + lateral;
        Vector3 p0 = PathPos(sMid0), p1 = PathPos(sMid1);
        Vector3 r0 = Right(PathYaw(sMid0) + yawOffset), r1 = Right(PathYaw(sMid1) + yawOffset);
        p0 = groundAt(v3add(p0, v3scale(r0, lat)), b->ankleH);
        p1 = groundAt(v3add(p1, v3scale(r1, lat)), b->ankleH);
        Vector3 foot;
        Vector3 toe = Fwd(yaw);
        if (u < 0.6f) {
            foot = p0;
            /* heel lifts towards the end of stance */
            float lift = smooth(0.4f, 0.6f, u);
            foot.y += lift * 0.03f;
            toe = v3add(toe, v3scale(up, -0.35f * lift));
        } else {
            float w = (u - 0.6f) / 0.4f;
            float e = easeInOut(w);
            foot = v3lerp(p0, p1, e);
            foot.y = mixf(p0.y, p1.y, e) + sinf(w * PI_F) * stepLen * 0.22f;
            toe = v3add(toe, v3scale(up, 0.3f * sinf(w * PI_F) - 0.1f));
        }
        if (side == 0) { p.footL = foot; p.toeL = toe; }
        else { p.footR = foot; p.toeR = toe; }
    }

    /* pelvis: bob and sway following the stance leg */
    float bob = 0.018f * cosf(TAU_F * 2.0f * (ph - 0.3f));
    float sway = 0.022f * cosf(TAU_F * (ph - 0.3f));
    p.root = v3add(pos, v3(0, b->pelvisH - 0.025f + bob, 0));
    p.root = v3add(p.root, v3scale(Right(yaw), -sway));
    p.twist = -0.12f * cosf(TAU_F * ph);
    p.lean = 0.07f;
    if (b->type == CH_GRANDMA) p.lean = 0.16f;
    if (b->type == CH_GIRL || b->type == CH_BOY) p.root.y += 0.01f * fabsf(sinf(TAU_F * ph));

    /* arms swing against the legs */
    Vector3 fwd = Fwd(yaw), right = Right(yaw);
    float armLen = b->upperArm + b->foreArm;
    Vector3 chest = v3add(p.root, v3(0, b->spine, 0));
    float amp = (b->type == CH_GRANDMA) ? 0.1f : 0.2f;
    float swL = -cosf(TAU_F * ph) * amp, swR = cosf(TAU_F * ph) * amp;
    p.handL = v3add(chest, v3add(v3scale(right, -b->shoulderW - 0.05f), v3add(v3scale(fwd, 0.04f + swL), v3(0, -armLen * 0.93f + fabsf(swL) * 0.2f, 0))));
    p.handR = v3add(chest, v3add(v3scale(right, b->shoulderW + 0.05f), v3add(v3scale(fwd, 0.04f + swR), v3(0, -armLen * 0.93f + fabsf(swR) * 0.2f, 0))));
    p.handDirL = v3add(v3(0, -1, 0), v3scale(fwd, 0.25f + swL));
    p.handDirR = v3add(v3(0, -1, 0), v3scale(fwd, 0.25f + swR));
    p.palmL = right;
    p.palmR = v3scale(right, -1.0f);
    p.look = v3add(p.root, v3add(v3scale(fwd, 6.0f), v3(0, b->spine, 0)));
    return p;
}

Pose PoseSit(const Body *b, Vector3 bench, float benchYaw, float offset, float t)
{
    Pose p;
    Vector3 fwd = Fwd(benchYaw), right = Right(benchYaw);
    Vector3 seat = v3add(bench, v3add(v3scale(right, offset), v3scale(fwd, 0.0f)));
    BasePose(&p, b, seat, benchYaw);
    p.root = v3add(seat, v3(0, 0.47f + 0.06f, 0));
    p.root = v3add(p.root, v3scale(fwd, -0.02f));
    p.lean = 0.02f + 0.006f * sinf(t * 1.6f);
    if (b->type == CH_GRANDMA) p.lean = 0.1f;
    float legReach = b->thigh + b->shin;
    Vector3 fl = v3add(seat, v3add(v3scale(fwd, b->thigh * 0.95f), v3scale(right, -b->hipW * 1.2f)));
    Vector3 fr = v3add(seat, v3add(v3scale(fwd, b->thigh * 0.95f), v3scale(right, b->hipW * 1.2f)));
    fl = groundAt(fl, b->ankleH);
    fr = groundAt(fr, b->ankleH);
    float hang = p.root.y - 0.05f - legReach * 0.98f;
    if (fl.y < hang) {
        /* too short to reach the ground: legs dangle and swing */
        float sw = 0.12f * sinf(t * 2.2f);
        fl = v3add(p.root, v3add(v3scale(fwd, b->thigh * 0.9f + sw), v3add(v3scale(right, -b->hipW), v3(0, -b->shin * 0.95f - 0.04f, 0))));
        fr = v3add(p.root, v3add(v3scale(fwd, b->thigh * 0.9f - sw), v3add(v3scale(right, b->hipW), v3(0, -b->shin * 0.95f - 0.04f, 0))));
    }
    p.footL = fl;
    p.footR = fr;
    p.toeL = v3rotY(fwd, 0.2f);
    p.toeR = v3rotY(fwd, -0.2f);
    /* hands resting on the lap */
    Vector3 lap = v3add(p.root, v3add(v3scale(fwd, b->thigh * 0.55f), v3(0, 0.07f, 0)));
    p.handL = v3add(lap, v3scale(right, -0.07f));
    p.handR = v3add(lap, v3scale(right, 0.07f));
    p.handDirL = v3add(fwd, v3add(v3scale(right, 0.6f), v3(0, -0.3f, 0)));
    p.handDirR = v3add(fwd, v3add(v3scale(right, -0.6f), v3(0, -0.3f, 0)));
    p.palmL = v3(0, -1, 0);
    p.palmR = v3(0, -1, 0);
    p.elbowL = v3add(v3scale(right, -1.0f), v3scale(fwd, -0.3f));
    p.elbowR = v3add(right, v3scale(fwd, -0.3f));
    p.look = v3add(p.root, v3add(v3scale(fwd, 8.0f), v3(0, 0.6f, 0)));
    return p;
}

Pose PoseKneel(const Body *b, Vector3 ground, float yaw, float t)
{
    Pose p;
    BasePose(&p, b, ground, yaw);
    Vector3 fwd = Fwd(yaw), right = Right(yaw);
    /* left knee down, right foot forward */
    p.root = v3add(ground, v3add(v3(0, b->shin + b->ankleH * 0.4f + 0.05f, 0), v3scale(fwd, -0.05f)));
    p.footL = groundAt(v3add(ground, v3add(v3scale(fwd, -b->shin * 0.95f), v3scale(right, -b->hipW))), b->ankleH * 0.9f);
    p.toeL = v3add(v3scale(fwd, -0.4f), v3(0, -1.0f, 0));
    p.kneeL = v3add(fwd, v3(0, -0.6f, 0));
    p.footR = groundAt(v3add(ground, v3add(v3scale(fwd, b->thigh * 0.62f), v3scale(right, b->hipW * 1.5f))), b->ankleH);
    p.toeR = fwd;
    p.kneeR = v3add(fwd, v3(0, 0.6f, 0));
    p.lean = 0.42f + 0.02f * sinf(t * 1.5f);
    ArmsRelaxed(b, &p, t);
    return p;
}

Pose PoseSquat(const Body *b, Vector3 ground, float yaw, float t)
{
    Pose p;
    BasePose(&p, b, ground, yaw);
    Vector3 fwd = Fwd(yaw), right = Right(yaw);
    p.root = v3add(ground, v3add(v3(0, b->pelvisH * 0.42f, 0), v3scale(fwd, -0.1f)));
    p.footL = groundAt(v3add(ground, v3add(v3scale(fwd, 0.05f), v3scale(right, -b->hipW * 1.6f))), b->ankleH);
    p.footR = groundAt(v3add(ground, v3add(v3scale(fwd, 0.05f), v3scale(right, b->hipW * 1.6f))), b->ankleH);
    p.kneeL = v3add(fwd, v3scale(right, -0.3f));
    p.kneeR = v3add(fwd, v3scale(right, 0.3f));
    p.toeL = v3rotY(fwd, 0.3f);
    p.toeR = v3rotY(fwd, -0.3f);
    p.lean = 0.5f + 0.02f * sinf(t * 1.7f);
    /* forearms on the knees */
    Vector3 knees = v3add(p.root, v3add(v3scale(fwd, b->thigh * 0.85f), v3(0, b->thigh * 0.35f, 0)));
    p.handL = v3add(knees, v3add(v3scale(right, 0.02f), v3scale(fwd, 0.06f)));
    p.handR = v3add(knees, v3add(v3scale(right, -0.02f), v3scale(fwd, 0.06f)));
    p.handDirL = v3add(fwd, v3scale(right, 0.5f));
    p.handDirR = v3add(fwd, v3scale(right, -0.5f));
    p.palmL = v3(0, -1, 0);
    p.palmR = v3(0, -1, 0);
    p.elbowL = v3add(v3scale(right, -1.0f), v3(0, -0.5f, 0));
    p.elbowR = v3add(right, v3(0, -0.5f, 0));
    return p;
}

float Blink(float t, float seed)
{
    float period = 3.7f + 1.3f * hash1(seed);
    float k = floorf((t + seed * 3.0f) / period);
    float local = (t + seed * 3.0f) - k * period;
    float when = period * (0.3f + 0.5f * hash1(k + seed * 17.0f));
    float d = fabsf(local - when);
    return saturate(1.0f - d / 0.08f);
}

float Talk(float t, float seed)
{
    float v = 0.5f + 0.5f * sinf(t * 17.0f + seed) * sinf(t * 7.3f + seed * 2.0f);
    return 0.15f + 0.45f * v;
}
