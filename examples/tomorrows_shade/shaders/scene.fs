#version 330
// Tomorrow's Shade — raymarched world.
// Everything visible in the film (land, sky, the oak, the people, the props)
// is a signed distance field evaluated here, lit with soft shadows, ambient
// occlusion, subsurface scattering and atmospheric scattering.

out vec4 fragColor;

uniform vec2 uRes;
uniform vec2 uOffset;            // pixel offset of this tile (for tiled rendering)
uniform float uTime;
uniform vec3 uCamPos, uCamFwd, uCamRight, uCamUp;
uniform float uTanHalf;
uniform vec2 uClip;

uniform vec3 uSunDir, uSunCol, uSkyZen, uSkyHor, uFogCol, uAmb, uMoonDir;
uniform float uFogDen, uNight, uCloud, uOvercast;
uniform float uSnow, uDry, uLush, uWet;
uniform vec3 uLeafCol, uLeafCol2;
uniform float uLeafAmt, uWind;
uniform vec4 uPath[8];

uniform sampler2D uNoise;
uniform sampler2D uHeight;        // terrainMid baked on a 0.5 m grid over +-512 m

// --- oak -------------------------------------------------------------------
uniform vec3 uTreePos;
uniform int uSegCount;
uniform vec4 uSegA[140];
uniform vec4 uSegB[140];
uniform int uGroupCount;
uniform vec4 uGroupBound[28];
uniform vec2 uGroupRange[28];
uniform int uBlobCount;
uniform vec4 uBlob[100];
uniform int uBlobGroupCount;
uniform vec4 uBlobGroupBound[28];
uniform vec2 uBlobGroupRange[28];
uniform vec4 uCanopyBound, uWoodBound;
uniform vec4 uCanopyHullC, uCanopyHullR; // hull ellipsoid: centre, radii (w = canopy scale)
uniform vec4 uCanopyExt;                  // half extents of the canopy box around uCanopyBound.xyz
uniform float uSprout, uGrowth;

// --- people ----------------------------------------------------------------
uniform int uCharCount;
uniform vec3 uJ[104];            // 4 characters x 26 joints
uniform vec4 uCStyle[4];         // type, scarf, cane, sit
uniform vec4 uCFace[4];          // blink, smile, mouthOpen, brow
uniform vec4 uCGaze[4];          // gaze dir (world), tear
uniform vec4 uCBound[4];         // bounding sphere
uniform vec3 uCCol[20];          // skin, hair, top, bottom, accent
uniform vec4 uCAux[4];           // cane tip xyz, show

// --- props -----------------------------------------------------------------
uniform vec4 uBench;
uniform float uBenchAge;
uniform vec4 uCane;
uniform vec4 uAcorn;
uniform vec4 uCan;
uniform vec2 uCanTS;             // tilt, show
uniform vec4 uTrowel;
uniform vec4 uMound, uMound2;
uniform vec4 uSwing;
uniform float uSwingShow;
uniform vec4 uFence;
uniform vec4 uScarfBench;
uniform int uPlantedCount;
uniform vec4 uPlanted[24];
uniform vec4 uSprout2;
uniform vec4 uRock[6];

#define MAT_TERRAIN 1.0
#define MAT_BARK 2.0
#define MAT_LEAF 3.0
#define MAT_ROCK 4.0
#define MAT_WOOD 5.0
#define MAT_SOIL 6.0
#define MAT_ACORN 7.0
#define MAT_CAN 8.0
#define MAT_SPROUT 9.0
#define MAT_ROPE 10.0
#define MAT_IRON 11.0
#define MAT_CANE 12.0
#define MAT_SCARFOBJ 13.0
#define MAT_DTREE 14.0
#define MAT_DTRUNK 15.0
#define MAT_FENCE 16.0
#define MAT_CHAR 100.0

#define WATER_Y -2.2

// ===========================================================================
// noise
// ===========================================================================
float hash12(vec2 p) { vec3 p3 = fract(vec3(p.xyx) * 0.1031); p3 += dot(p3, p3.yzx + 33.33); return fract((p3.x + p3.y) * p3.z); }
float hash13(vec3 p3) { p3 = fract(p3 * 0.1031); p3 += dot(p3, p3.zyx + 31.32); return fract((p3.x + p3.y) * p3.z); }
float hash11(float p) { p = fract(p * 0.1031); p *= p + 33.33; p *= p + p; return fract(p); }

float noise3(vec3 x)
{
    vec3 p = floor(x);
    vec3 f = fract(x);
    f = f * f * (3.0 - 2.0 * f);
    vec2 uv = (p.xy + vec2(37.0, 17.0) * p.z) + f.xy;
    vec2 rg = textureLod(uNoise, (uv + 0.5) / 256.0, 0.0).yx;
    return mix(rg.x, rg.y, f.z);
}

float noise2(vec2 x)
{
    vec2 p = floor(x);
    vec2 f = fract(x);
    f = f * f * (3.0 - 2.0 * f);
    return textureLod(uNoise, (p + f + 0.5) / 256.0, 0.0).x;
}

float noise1(float x) { float i = floor(x), f = fract(x); f = f * f * (3.0 - 2.0 * f); return mix(hash11(i), hash11(i + 1.0), f); }

float fbm2(vec2 p)
{
    float a = 0.5, s = 0.0;
    for (int i = 0; i < 5; i++) { s += a * noise2(p); p = mat2(1.6, 1.2, -1.2, 1.6) * p; a *= 0.5; }
    return s;
}

float fbm3(vec3 p)
{
    float s = 0.5 * noise3(p);
    p = p * 2.03 + vec3(1.7, 9.2, 3.1); s += 0.25 * noise3(p);
    p = p * 2.01 + vec3(8.3, 2.8, 5.4); s += 0.125 * noise3(p);
    return s / 0.875;
}

// ===========================================================================
// sdf primitives
// ===========================================================================
float smin(float a, float b, float k) { float h = max(k - abs(a - b), 0.0) / k; return min(a, b) - h * h * k * 0.25; }
float smax(float a, float b, float k) { return -smin(-a, -b, k); }

float sdSphere(vec3 p, float r) { return length(p) - r; }
float sdEllipsoid(vec3 p, vec3 r) { float k0 = length(p / r); float k1 = length(p / (r * r)); return k0 * (k0 - 1.0) / max(k1, 1e-6); }
float sdCapsule(vec3 p, vec3 a, vec3 b, float r) { vec3 pa = p - a, ba = b - a; float h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0); return length(pa - ba * h) - r; }
float sdCone2(vec3 p, vec3 a, vec3 b, float ra, float rb)
{
    vec3 pa = p - a, ba = b - a;
    float h = clamp(dot(pa, ba) / max(dot(ba, ba), 1e-8), 0.0, 1.0);
    return length(pa - ba * h) - mix(ra, rb, h);
}
// segment parameter variant (returns distance, writes h)
float sdCone2h(vec3 p, vec3 a, vec3 b, float ra, float rb, out float h)
{
    vec3 pa = p - a, ba = b - a;
    h = clamp(dot(pa, ba) / max(dot(ba, ba), 1e-8), 0.0, 1.0);
    return length(pa - ba * h) - mix(ra, rb, h);
}
float sdBox(vec3 p, vec3 b) { vec3 q = abs(p) - b; return length(max(q, 0.0)) + min(max(q.x, max(q.y, q.z)), 0.0); }
float sdRBox(vec3 p, vec3 b, float r) { return sdBox(p, b - r) - r; }
float sdTorus(vec3 p, vec2 t) { vec2 q = vec2(length(p.xz) - t.x, p.y); return length(q) - t.y; }
float sdCylY(vec3 p, float r, float h) { vec2 d = abs(vec2(length(p.xz), p.y)) - vec2(r, h); return min(max(d.x, d.y), 0.0) + length(max(d, 0.0)); }
mat2 rot(float a) { float c = cos(a), s = sin(a); return mat2(c, -s, s, c); }

// ===========================================================================
// terrain (macro shape mirrors terrainHeight() in mathx.c)
// ===========================================================================
float gauss2(vec2 p, vec2 c, float s) { vec2 d = p - c; return exp(-dot(d, d) / (2.0 * s * s)); }

float terrainMacro(vec2 p)
{
    float r = length(p);
    float h = 7.0 * gauss2(p, vec2(0.0), 15.0);
    h += 4.5 * gauss2(p, vec2(-46.0, -28.0), 14.0);
    h += 6.0 * gauss2(p, vec2(52.0, -44.0), 18.0);
    h += 3.0 * gauss2(p, vec2(34.0, 62.0), 16.0);
    h += 3.5 * gauss2(p, vec2(-40.0, 40.0), 15.0);
    float u = 1.2 * sin(0.031 * p.x + 0.7) * cos(0.027 * p.y - 0.4) + 0.7 * sin(0.053 * p.x - 0.061 * p.y + 1.3);
    h += u * smoothstep(12.0, 40.0, r);
    float zc = -88.0 + 14.0 * sin(0.018 * p.x + 0.5);
    float dz = p.y - zc;
    h -= 4.0 * exp(-dz * dz / (2.0 * 24.0 * 24.0)) + 3.0 * exp(-dz * dz / (2.0 * 6.0 * 6.0));
    float ang = atan(p.y, p.x);
    h += 14.0 * smoothstep(150.0, 460.0, r) * (0.65 + 0.35 * sin(ang * 3.0 + 1.0) * sin(ang * 5.0 - 0.5));
    return h;
}

float pathDist(vec2 p)
{
    float d = 1e9;
    vec2 prev = uPath[0].xy;
    for (int i = 0; i < 15; i++) {
        int k = i + 1;
        vec4 pk = uPath[k / 2];
        vec2 cur = (k % 2 == 0) ? pk.xy : pk.zw;
        vec2 pa = p - prev, ba = cur - prev;
        float h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
        d = min(d, length(pa - ba * h));
        prev = cur;
    }
    return d;
}

// mid-scale relief, mirrored in C (terrainHeight) so feet and grass sit on it
float terrainMid(vec2 p)
{
    float r = length(p);
    vec2 q = p * 0.07;
    float n = 0.5 * noise2(q);
    q = mat2(1.6, 1.2, -1.2, 1.6) * q;
    n += 0.25 * noise2(q);
    q = mat2(1.6, 1.2, -1.2, 1.6) * q;
    n += 0.125 * noise2(q);
    return terrainMacro(p) + 0.4 * (n / 0.875 - 0.5) * smoothstep(1.5, 7.0, r);
}

float terrainFine(vec2 p)
{
    float r = length(p);
    float d = 0.05 * (noise2(p * 0.9) - 0.5) + 0.014 * (noise2(p * 4.1) - 0.5);
    return d * smoothstep(1.0, 5.0, r);
}

float terrainMidFast(vec2 p)
{
    vec2 uv = (p + 512.0) / 1024.0;
    if (uv.x < 0.0 || uv.y < 0.0 || uv.x > 1.0 || uv.y > 1.0) return terrainMid(p);
    return textureLod(uHeight, uv, 0.0).r;
}

// marching uses the baked height; shading normals use the analytic surface
float terrainH(vec2 p, float lod) { return lod < 1.0 ? terrainMidFast(p) + terrainFine(p) : terrainMidFast(p); }
float terrainHA(vec2 p, float lod) { return lod < 1.0 ? terrainMid(p) + terrainFine(p) : terrainMid(p); }

// ===========================================================================
// oak
// ===========================================================================
// dmax: distance of the nearest surface found so far; groups beyond it are skipped
float sdWood(vec3 p, float dmax)
{
    float d = dmax + 0.05;
    for (int g = 0; g < uGroupCount; g++) {
        vec4 gb = uGroupBound[g];
        if (length(p - gb.xyz) - gb.w > d) continue;
        int s0 = int(uGroupRange[g].x), s1 = int(uGroupRange[g].y);
        for (int i = s0; i < s1; i++) {
            vec4 a = uSegA[i], b = uSegB[i];
            vec3 pa = p - a.xyz, ba = b.xyz - a.xyz;
            float h = clamp(dot(pa, ba) / max(dot(ba, ba), 1e-8), 0.0, 1.0);
            float di = length(pa - ba * h) - mix(a.w, b.w, h);
            float k = max(0.4 * a.w, 0.004);
            if (di < d + k) d = smin(d, di, k);
        }
    }
    return d;
}

float sdCanopyBase(vec3 p)
{
    float d = 1e9;
    for (int g = 0; g < uBlobGroupCount; g++) {
        vec4 gb = uBlobGroupBound[g];
        float bd = length(p - gb.xyz) - gb.w;
        if (bd > d + 1.0) { continue; }
        int b0 = int(uBlobGroupRange[g].x), b1 = int(uBlobGroupRange[g].y);
        for (int i = b0; i < b1; i++) {
            vec4 b = uBlob[i];
            d = smin(d, length(p - b.xyz) - b.w, 0.4 * b.w);
        }
    }
    // a broad dome holds the clumps together like an old oak's crown
    if (uCanopyHullR.x > 0.0) d = smin(d, sdEllipsoid(p - uCanopyHullC.xyz, uCanopyHullR.xyz), 0.9 * uCanopyHullR.w);
    return d;
}

float canopyNoise(vec3 p)
{
    vec3 q = p + vec3(0.0, 0.0, 0.25 * uWind * uCanopyHullR.w * sin(uTime * 1.3 + p.x * 0.4));
    float n = 0.55 * noise3(q * 1.3) + 0.3 * noise3(q * 3.1 + 7.1);
    // billowy small clusters of leaves with crisp crevices between them
    float b = 1.0 - abs(noise3(q * 7.3 + 3.3) * 2.0 - 1.0);
    n += 0.2 * b * b;
    return n;
}

float sdCanopy(vec3 p, bool detail)
{
    float d = sdCanopyBase(p);
    float sc = clamp(uCanopyHullR.w, 0.12, 1.0);
    if (d < 1.6 * sc) {
        // big clumps with gaps between them, then leafy detail (scaled to the tree's size)
        vec3 q = p / sc;
        float big = noise3(q * 0.42 + vec3(11.0, 3.0, 7.0));
        d += (big - 0.45) * 1.1 * sc;
        float n = detail ? canopyNoise(q) : 0.55 * noise3(q * 1.3) + 0.45 * noise3(q * 3.1 + 7.1);
        d = d + (0.5 - n) * 0.95 * sc;
        // autumn and spring: the crown thins into scattered clusters
        float thin = 1.0 - clamp(uLeafAmt, 0.0, 1.0);
        if (thin > 0.01) d = max(d, (0.32 + 0.4 * thin) * sc - noise3(q * 2.2 + 3.0) * 0.9 * sc);
        d *= 0.5;
    }
    return d;
}

// oak seedling: a thin stem that pushes up as a hook, straightens, then opens
// a small rosette of lobed oak leaves
float oakLeaf(vec3 q, float L, float W)
{
    float t = clamp(q.z / L, 0.0, 1.0);
    float w = W * pow(sin(3.1416 * t), 0.75) * (0.78 + 0.22 * cos(t * 28.0));
    float bend = q.y - 1.6 * q.x * q.x / max(L, 1e-3) + 0.15 * q.z * t;
    float d2 = max(abs(q.x) - w, max(-q.z, q.z - L));
    return max(d2 * 0.8, abs(bend) - 0.0012);
}

vec2 sdSprout(vec3 p, vec3 base, float g)
{
    if (g <= 0.0) return vec2(1e9, 0.0);
    vec3 q = p - base;
    float h = 0.015 + 0.14 * g;
    float bound = length(q - vec3(0.0, h * 0.5, 0.0)) - (h * 0.6 + 0.09 * g + 0.02);
    if (bound > 0.02) return vec2(bound, MAT_SPROUT);
    float sway = 0.05 * sin(uTime * 1.7) * uWind * g;
    float hook = 1.0 - smoothstep(0.08, 0.35, g);
    vec3 top = vec3(sway * h + 0.02 * hook * g, h * (1.0 - 0.25 * hook), 0.0);
    vec3 mid = vec3(0.004 * sin(g * 9.0), h * 0.5, 0.0);
    float d = smin(sdCone2(q, vec3(0.0, -0.03, 0.0), mid, 0.0032, 0.0026), sdCone2(q, mid, top, 0.0026, 0.002), 0.003);
    if (hook > 0.0) {
        // the bent tip of a freshly emerged seedling
        vec3 tip = top + vec3(0.012, -0.012, 0.0) * hook;
        d = smin(d, sdCone2(q, top, tip, 0.002, 0.0028), 0.002);
    }
    float open = smoothstep(0.2, 0.8, g);
    int nl = 4;
    for (int i = 0; i < nl; i++) {
        float fi = float(i);
        float lg = smoothstep(0.12 + 0.12 * fi, 0.6 + 0.12 * fi, g);
        if (lg <= 0.0) continue;
        vec3 lq = q - (top - vec3(0.0, 0.006 * fi, 0.0));
        lq.xz = rot(fi * 2.4 + 0.6) * lq.xz;
        lq.yz = rot(-mix(1.35, 0.45, open) + 0.05 * fi) * lq.yz;
        d = min(d, oakLeaf(lq, 0.065 * lg, 0.024 * lg));
    }
    return vec2(d, MAT_SPROUT);
}

// ===========================================================================
// people
// ===========================================================================
vec3 J(int c, int i) { return uJ[c * 26 + i]; }

// head features in head-local space (x right, y up, z forward), metres
const vec3 EYE_C = vec3(0.033, 0.01, 0.066);

float eyeRadius(float child) { return 0.015 + 0.003 * child; }

vec2 sdHead(vec3 h, int c, float type)
{
    float child = (type == 1.0 || type == 3.0) ? 1.0 : 0.0;
    float old = type == 0.0 ? 1.0 : 0.0;
    vec4 face = uCFace[c];
    vec3 hx = vec3(abs(h.x), h.yz);
    float mb = MAT_CHAR + float(c) * 20.0;

    // cranium and face mass
    float d = sdEllipsoid(h - vec3(0.0, 0.018, -0.008), vec3(0.079, 0.093, 0.091));
    float jaw = sdEllipsoid(h - vec3(0.0, -0.03, 0.02), vec3(0.058 + 0.006 * child, 0.056 - 0.006 * child, 0.064));
    d = smin(d, jaw, 0.04);
    d = smin(d, sdSphere(h - vec3(0.0, -0.066 + 0.012 * child, 0.047 - 0.004 * child), 0.02 + 0.004 * child), 0.022);
    d = smin(d, sdSphere(hx - vec3(0.036, -0.022 + 0.002 * child, 0.05), 0.029 + 0.007 * child - 0.002 * old), 0.03);
    d = smin(d, sdCapsule(hx, vec3(0.0, 0.034, 0.072), vec3(0.043, 0.032, 0.066), 0.011 - 0.003 * child), 0.018);
    float er = eyeRadius(child);
    vec3 ec = EYE_C + vec3(0.0, 0.0, -0.002 * child);
    d = smax(d, -sdSphere(hx - (ec + vec3(0.0, 0.001, 0.007)), er + 0.0008), 0.005);
    // nose: small and soft
    float nz = 0.093 - 0.007 * child;
    float ny = -0.014 + 0.005 * child;
    float nose = sdCone2(h, vec3(0.0, 0.014, 0.079), vec3(0.0, ny, nz), 0.0055, 0.0085 - 0.0015 * child);
    nose = smin(nose, sdSphere(h - vec3(0.0, ny - 0.002, nz - 0.002), 0.0102 - 0.0025 * child), 0.006);
    nose = smin(nose, sdSphere(hx - vec3(0.0105 - 0.002 * child, ny - 0.005, nz - 0.009), 0.0072 - 0.0015 * child), 0.006);
    d = smin(d, nose, 0.01);
    // ears
    d = smin(d, sdEllipsoid(hx - vec3(0.078, 0.004, -0.008), vec3(0.0095, 0.025 - 0.003 * child, 0.016)), 0.006);
    // lips
    float my = -0.043 + 0.006 * child;
    float lipz = 0.073 - 0.005 * child;
    d = smin(d, sdEllipsoid(h - vec3(0.0, my + 0.0042, lipz), vec3(0.0155 - 0.002 * child, 0.0048, 0.0075)), 0.007);
    d = smin(d, sdEllipsoid(h - vec3(0.0, my - 0.0052, lipz - 0.001), vec3(0.0135 - 0.002 * child, 0.0055, 0.0075)), 0.007);
    // mouth line carved in, curving with the smile
    float smile = face.y;
    float curve = my + smile * 0.006 * (h.x * h.x) / (0.018 * 0.018) - smile * 0.0015;
    float slit = length(vec2(max(abs(h.x) - 0.016, 0.0), h.y - curve)) - 0.0008 - 0.006 * face.z * smoothstep(0.016, 0.0, abs(h.x));
    d = smax(d, -max(slit, 0.06 - h.z), 0.0025);
    vec2 res = vec2(d, mb);

    // eyes
    float eye = sdSphere(hx - ec, er);
    if (eye < res.x) res = vec2(eye, mb + 6.0);
    // eyelids: skin shell cut by the lid lines, lowering when blinking
    vec3 e = hx - ec;
    float squint = clamp(face.y - 0.4, 0.0, 1.0) * 0.6;
    float yTop = mix(0.6 * er, -1.05 * er, clamp(face.x, 0.0, 1.0));
    yTop -= 0.1 * er * old;
    float yLow = mix(-0.78 * er, -0.35 * er, squint);
    float tilt = 0.25 * e.x / er;
    float lidShell = sdSphere(e, er + 0.0012);
    float lid = max(lidShell, min(yTop - tilt * er * 0.3 - e.y, e.y - yLow));
    if (lid < res.x) res = vec2(lid, mb);

    // eyebrows
    float browY = 0.037 + 0.005 * face.w - 0.002 * child;
    float brow = sdCapsule(hx, vec3(0.015, browY, 0.084), vec3(0.047, browY - 0.003 + 0.003 * face.w, 0.074), 0.0028 + 0.0008 * (1.0 - child));
    if (brow < res.x) res = vec2(brow, mb + 1.0);

    // hair
    float hair = 1e9;
    if (type == 0.0 || type == 2.0) {
        // swept back into a bun: silver for the grandmother, dark for the grown-up girl
        float skull = sdEllipsoid(h - vec3(0.0, 0.018, -0.008), vec3(0.079, 0.093, 0.091));
        float thick = mix(0.003, 0.012, smoothstep(0.07, -0.03, h.z)) + 0.005 * smoothstep(0.05, 0.1, h.y);
        hair = skull - thick;
        float hl = 0.074 + 1.1 * h.x * h.x + (type == 2.0 ? -0.006 * smoothstep(-0.02, 0.03, h.x) : 0.0);
        hair = smax(hair, -max(0.004 - h.z, h.y - hl), 0.006);
        float lowCut = mix(0.012, -0.066, smoothstep(-0.01, -0.08, h.z));
        hair = smax(hair, lowCut - h.y, 0.01);
        vec3 bunC = type == 0.0 ? vec3(0.0, 0.062, -0.086) : vec3(0.0, -0.012, -0.096);
        vec3 bq = h - bunC;
        float bun = sdEllipsoid(bq, type == 0.0 ? vec3(0.042, 0.038, 0.04) : vec3(0.036, 0.032, 0.032));
        bun += 0.0025 * sin(atan(bq.x, bq.y) * 5.0 + length(bq) * 160.0);
        hair = smin(hair, bun, 0.018);
        // strands combed back towards the bun
        float ang = atan(h.x, h.y + 0.03);
        hair += 0.00035 * sin(ang * 70.0) + 0.0002 * sin(ang * 170.0 + h.z * 40.0);
    } else if (type == 1.0) {
        // bob with straight bangs
        hair = sdEllipsoid(h - vec3(0.0, 0.006, -0.012), vec3(0.094, 0.118, 0.104));
        hair = smax(hair, -max(max(0.022 - h.z, h.y - 0.042), abs(h.x) - 0.064), 0.008);
        hair = smax(hair, -0.064 - h.y + 0.012 * smoothstep(0.0, 0.09, h.z), 0.012);
        hair = smax(hair, sdSphere(h - vec3(0.0, 0.0, 0.0), 0.13), 0.01);
        float ang = atan(h.x, h.z);
        hair += 0.0011 * sin(ang * 60.0) + 0.0006 * sin(ang * 140.0 + h.y * 50.0);
    } else {
        // short tousled hair
        hair = sdEllipsoid(h - vec3(0.0, 0.026, -0.012), vec3(0.084, 0.1, 0.095));
        float hl = 0.06 + 1.2 * h.x * h.x;
        hair = smax(hair, -max(0.016 - h.z, h.y - hl), 0.006);
        hair = smax(hair, mix(0.01, -0.05, smoothstep(-0.02, -0.08, h.z)) - h.y, 0.008);
        hair -= 0.004 * noise3(h * 70.0);
    }
    if (hair < res.x) res = vec2(hair, mb + 1.0);

    if (type == 1.0) {
        float clip = sdEllipsoid(h - vec3(0.06, 0.066, 0.034), vec3(0.012, 0.008, 0.016));
        if (clip < res.x) res = vec2(clip, mb + 10.0);
    }
    if (type == 0.0) {
        // round spectacles
        vec3 g = hx - vec3(ec.x, ec.y + 0.001, 0.091);
        float ring = length(vec2(length(g.xy) - 0.0195, g.z)) - 0.0014;
        float bridge = sdCapsule(h, vec3(-0.0135, 0.016, 0.093), vec3(0.0135, 0.016, 0.093), 0.0012);
        float temple = sdCapsule(hx, vec3(0.052, 0.014, 0.088), vec3(0.08, 0.018, -0.005), 0.0012);
        float gl = min(ring, min(bridge, temple));
        if (gl < res.x) res = vec2(gl, mb + 7.0);
    }
    return res;
}

vec2 sdCharacter(vec3 p, int c)
{
    vec4 style = uCStyle[c];
    float type = style.x;
    float child = (type == 1.0 || type == 3.0) ? 1.0 : 0.0;
    float matBase = MAT_CHAR + float(c) * 20.0;

    vec3 pel = J(c, 0), bel = J(c, 1), che = J(c, 2), nek = J(c, 3), hed = J(c, 4);
    vec3 lsh = J(c, 5), lel = J(c, 6), lwr = J(c, 7), ltp = J(c, 8);
    vec3 rsh = J(c, 9), rel = J(c, 10), rwr = J(c, 11), rtp = J(c, 12);
    vec3 lhp = J(c, 13), lkn = J(c, 14), lan = J(c, 15), lto = J(c, 16);
    vec3 rhp = J(c, 17), rkn = J(c, 18), ran = J(c, 19), rto = J(c, 20);
    vec3 hR = J(c, 21), hU = J(c, 22), hF = J(c, 23);
    float s = child > 0.5 ? 0.78 : 1.0;

    // torso frame
    vec3 up = normalize(che - pel);
    vec3 rt = normalize(rsh - lsh);
    rt = normalize(rt - up * dot(rt, up));
    vec3 fw = cross(up, rt);
    vec3 q = p - pel;
    vec3 tl = vec3(dot(q, rt), dot(q, up), dot(q, fw));
    float torsoLen = length(che - pel);

    vec2 res = vec2(1e9, matBase + 2.0);

    // --- torso (top garment)
    float adult = 1.0 - child;
    float d = sdEllipsoid(tl - vec3(0.0, 0.02, -0.005), vec3(0.165 * adult + 0.125 * child, 0.13 * s, 0.12 * s));
    float bellyD = sdEllipsoid(tl - vec3(0.0, torsoLen * 0.45, 0.014 * s), vec3(0.142, 0.13, 0.118 + 0.01 * float(type == 0.0)) * s);
    d = smin(d, bellyD, 0.08 * s);
    float chestD = sdEllipsoid(tl - vec3(0.0, torsoLen * 0.8, 0.004), vec3(0.158, 0.13, 0.112) * s);
    d = smin(d, chestD, 0.07 * s);
    if (adult > 0.5) {
        vec3 bq = vec3(abs(tl.x), tl.yz) - vec3(0.055, torsoLen * 0.68, 0.058);
        d = smin(d, sdSphere(bq, 0.05), 0.06);
    }
    if (type == 0.0) d = smin(d, sdEllipsoid(tl - vec3(0.0, torsoLen * 0.93, -0.045), vec3(0.12, 0.09, 0.075)), 0.06); // gentle stoop
    d = smin(d, sdCapsule(p, lsh, rsh, 0.058 * s), 0.07 * s);
    res = vec2(d, matBase + 2.0);

    // --- neck
    float nk = sdCone2(p, nek - up * 0.03 * s, hed - hU * 0.06 * s, 0.047 * s, 0.04 * s);
    if (nk < res.x) res = vec2(smin(nk, res.x, 0.02), matBase + 0.0);
    else res.x = smin(res.x, nk, 0.02);

    // --- arms
    float sleeve = (type == 1.0 || type == 3.0) ? 0.45 : 1.0; // fraction of arm covered by sleeve
    for (int side = 0; side < 2; side++) {
        vec3 sh = side == 0 ? lsh : rsh;
        vec3 el = side == 0 ? lel : rel;
        vec3 wr = side == 0 ? lwr : rwr;
        vec3 tp = side == 0 ? ltp : rtp;
        float hu;
        float ua = sdCone2h(p, sh, el, 0.056 * s, 0.045 * s, hu);
        float hf;
        float fa = sdCone2h(p, el, wr, 0.044 * s, 0.033 * s, hf);
        float arm = smin(ua, fa, 0.02 * s);
        float along = ua < fa ? hu * 0.5 : 0.5 + hf * 0.5;
        float am = along < sleeve ? 2.0 : 0.0;
        if (sleeve > 0.9 && along > 0.97) am = 0.0;
        // hand: palm + fingers (mitten) + thumb
        vec3 hd = normalize(tp - wr);
        vec3 pn = J(c, 24 + side);
        float hsz = s * 0.95;
        float palm = sdCone2(p, wr + hd * 0.01 * hsz, wr + hd * 0.06 * hsz, 0.026 * hsz, 0.03 * hsz);
        float fing = sdCone2(p, wr + hd * 0.06 * hsz, tp, 0.026 * hsz, 0.016 * hsz);
        vec3 tside = normalize(cross(pn, hd)) * (side == 0 ? 1.0 : -1.0);
        float thumb = sdCone2(p, wr + hd * 0.025 * hsz + tside * 0.02 * hsz, wr + hd * 0.07 * hsz + tside * 0.035 * hsz + pn * 0.012 * hsz, 0.011 * hsz, 0.009 * hsz);
        // flatten the hand along its palm normal
        float hflat = abs(dot(p - wr, pn)) - 0.018 * hsz;
        float hand = smin(palm, fing, 0.02 * hsz);
        hand = smax(hand, hflat, 0.01);
        hand = smin(hand, thumb, 0.008);
        if (arm < res.x) res = vec2(smin(arm, res.x, 0.035 * s), matBase + am);
        else res.x = smin(res.x, arm, 0.035 * s);
        if (hand < res.x) res = vec2(smin(hand, res.x, 0.012), matBase + 0.0);
        else res.x = smin(res.x, hand, 0.012);
    }

    // --- legs
    float legsMat = (type == 1.0) ? 0.0 : ((type == 3.0) ? 0.0 : 3.0);
    for (int side = 0; side < 2; side++) {
        vec3 hp = side == 0 ? lhp : rhp;
        vec3 kn = side == 0 ? lkn : rkn;
        vec3 an = side == 0 ? lan : ran;
        vec3 to = side == 0 ? lto : rto;
        float hth;
        float th = sdCone2h(p, hp, kn, 0.075 * s, 0.05 * s, hth);
        float hsh;
        float sh = sdCone2h(p, kn, an, 0.05 * s, 0.034 * s, hsh);
        float leg = smin(th, sh, 0.03 * s);
        float lm = legsMat;
        if (type == 3.0 && th < sh && hth < 0.55) lm = 3.0;          // shorts
        if (type == 1.0 && sh < th && hsh > 0.72) lm = 8.0;           // socks
        if (type == 3.0 && sh < th && hsh > 0.8) lm = 8.0;
        // shoe
        vec3 fdir = to - an;
        fdir.y = 0.0;
        vec3 heel = vec3(an.x, to.y + 0.004, an.z) - fdir * 0.22;
        float shoe = sdCone2(p, heel, to, 0.037 * s, 0.031 * s);
        shoe = smin(shoe, sdSphere(p - an, 0.036 * s), 0.03 * s);
        shoe = smax(shoe, (to.y - 0.034 * s) - p.y, 0.008);
        if (leg < res.x) res = vec2(leg, matBase + lm);
        if (shoe < res.x) res = vec2(shoe, matBase + 4.0);
    }

    // --- skirts and dresses
    if (type == 0.0 || type == 2.0) {
        // fabric hangs from the waist over each thigh, then falls from the knee to the hem
        float groundY = min(lan.y, ran.y) - 0.045;
        float hemY = groundY + (type == 0.0 ? 0.08 : 0.14);
        float skirt = sdEllipsoid(tl - vec3(0.0, -0.02, 0.0), vec3(0.172, 0.15, 0.128));
        float legs = 1e9;
        for (int side = 0; side < 2; side++) {
            vec3 hp = side == 0 ? lhp : rhp;
            vec3 kn = side == 0 ? lkn : rkn;
            vec3 kd = vec3(kn.x, min(kn.y, hemY), kn.z);
            float th = sdCone2(p, hp + up * 0.06, kn, 0.135, 0.118);
            float lo = sdCone2(p, kn, kd, 0.118, 0.165);
            legs = smin(legs, smin(th, lo, 0.07), 0.16);
        }
        // fabric spanning the knees and falling to the hem, so it reads as a skirt
        vec3 kab = rkn - lkn;
        float kh = clamp(dot(p.xz - lkn.xz, kab.xz) / max(dot(kab.xz, kab.xz), 1e-4), 0.0, 1.0);
        vec3 kc = lkn + kab * kh;
        float drop = clamp((kc.y - p.y) / max(kc.y - hemY, 0.01), 0.0, 1.0);
        float curtain = max(length(p.xz - kc.xz) - (0.11 + 0.06 * drop), max(p.y - kc.y, hemY - p.y));
        curtain = min(curtain, sdCone2(p, lkn, rkn, 0.11, 0.11));
        legs = smin(legs, curtain, 0.08);
        skirt = smin(skirt, legs, 0.12);
        float ang = atan(tl.z, tl.x);
        float low = smoothstep(pel.y - 0.25, hemY, p.y);
        skirt += (0.007 * sin(ang * 9.0 + 0.6 * sin(uTime * 2.0)) + 0.003 * sin(ang * 19.0)) * low;
        skirt = smax(skirt, hemY - p.y, 0.01);
        if (skirt < res.x) res = vec2(skirt, matBase + 3.0);
        float hemTop = sdCone2(p, bel, pel - up * 0.07, 0.15 * s, 0.19);
        hemTop = max(hemTop, dot(p - (pel - up * 0.07), -up));
        if (hemTop < res.x) res = vec2(hemTop, matBase + 2.0);
    } else if (type == 1.0) {
        float hem = sdCone2(p, che - up * 0.05, pel - up * 0.15, 0.1, 0.165);
        hem += 0.005 * sin(atan(tl.z, tl.x) * 9.0 + 0.5 * sin(uTime * 2.0));
        hem = max(hem, dot(p - (pel - up * 0.15), -up));
        float lap = smin(sdCone2(p, lhp, mix(lhp, lkn, 0.42), 0.078, 0.07), sdCone2(p, rhp, mix(rhp, rkn, 0.42), 0.078, 0.07), 0.05);
        float dress = smin(hem, lap, 0.06);
        dress = smin(dress, res.x + 0.004, 0.02);
        if (dress < res.x) res = vec2(dress, matBase + 2.0);
    }

    // --- scarf
    if (style.y > 0.5) {
        vec3 sc = nek + up * 0.005 * s;
        vec3 sq = p - sc;
        vec3 sl = vec3(dot(sq, rt), dot(sq, up), dot(sq, fw));
        float ring = length(vec2(length(sl.xz * vec2(1.0, 1.1)) - 0.074 * s, sl.y * 0.85)) - 0.03 * s;
        ring += 0.0025 * sin(atan(sl.z, sl.x) * 14.0 + sl.y * 80.0);
        float sway = 0.02 * sin(uTime * 1.8) * uWind;
        vec3 t0 = sc + fw * 0.085 * s + rt * 0.045 * s - up * 0.02 * s;
        vec3 t1 = t0 - up * 0.27 * s + fw * (0.045 + sway) * s + rt * 0.015;
        float tail = sdCone2(p, t0, t1, 0.03 * s, 0.026 * s);
        tail = smax(tail, abs(dot(p - t0, fw) - 0.01 * s) - 0.013 * s, 0.006);
        float scarf = min(ring, tail);
        if (scarf < res.x) res = vec2(scarf, matBase + 5.0);
    }

    // --- head
    vec3 hq = p - hed;
    float hs = child > 0.5 ? 1.12 : 1.1;
    vec3 hl = vec3(dot(hq, hR), dot(hq, hU), dot(hq, hF)) / hs;
    if (length(hl) < 0.2) {
        vec2 hres = sdHead(hl, c, type);
        hres.x *= hs;
        if (hres.x < res.x) res = vec2(smin(hres.x, res.x, 0.01), hres.y);
        else res.x = smin(res.x, hres.x, 0.01);
    } else {
        res.x = min(res.x, (length(hl) - 0.15) * hs);
    }

    // --- cane
    vec4 aux = uCAux[c];
    if (aux.w > 0.5) {
        vec3 top = rwr + rt * 0.0 + up * 0.03;
        float cane = sdCapsule(p, aux.xyz, top, 0.011);
        vec3 hk = top + fw * 0.05;
        cane = min(cane, sdCapsule(p, top, hk + up * 0.03, 0.011));
        if (cane < res.x) res = vec2(cane, MAT_CANE);
    }
    return res;
}

// ===========================================================================
// props
// ===========================================================================
vec2 sdBench(vec3 p)
{
    if (uBench.w > 50.0) return vec2(1e9, 0.0);
    vec3 q = p - uBench.xyz;
    q.xz = rot(-uBench.w) * q.xz;
    if (sdBox(q - vec3(0.0, 0.45, 0.0), vec3(0.9, 0.6, 0.45)) > 0.2) return vec2(sdBox(q - vec3(0.0, 0.45, 0.0), vec3(0.9, 0.6, 0.45)), MAT_WOOD);
    float d = 1e9;
    // seat planks
    for (int i = 0; i < 3; i++) {
        float z = -0.14 + 0.14 * float(i);
        d = min(d, sdRBox(q - vec3(0.0, 0.45, z), vec3(0.72, 0.022, 0.06), 0.012));
    }
    // back planks
    for (int i = 0; i < 2; i++) {
        float y = 0.66 + 0.15 * float(i);
        vec3 b = q - vec3(0.0, y, -0.25 - 0.03 * float(i));
        b.yz = rot(0.18) * b.yz;
        d = min(d, sdRBox(b, vec3(0.72, 0.05, 0.018), 0.01));
    }
    // frames
    vec3 fq = vec3(abs(q.x) - 0.6, q.y, q.z);
    d = min(d, sdRBox(fq - vec3(0.0, 0.22, 0.15), vec3(0.03, 0.22, 0.03), 0.01));
    d = min(d, sdRBox(fq - vec3(0.0, 0.42, -0.2), vec3(0.03, 0.42, 0.03), 0.01));
    d = min(d, sdRBox(fq - vec3(0.0, 0.41, -0.02), vec3(0.03, 0.025, 0.22), 0.01));
    d = min(d, sdRBox(fq - vec3(0.0, 0.62, 0.02), vec3(0.03, 0.022, 0.18), 0.01));
    d = min(d, sdRBox(fq - vec3(0.0, 0.53, 0.17), vec3(0.025, 0.09, 0.022), 0.01));
    return vec2(d, MAT_WOOD);
}

vec2 sdCan(vec3 p)
{
    if (uCanTS.y < 0.5) return vec2(1e9, 0.0);
    vec3 q = p - uCan.xyz;
    if (length(q) > 0.35) return vec2(length(q) - 0.3, MAT_CAN);
    q.xz = rot(-uCan.w) * q.xz;
    q.yz = rot(uCanTS.x) * q.yz;
    float body = sdCylY(q - vec3(0.0, 0.08, 0.0), 0.075, 0.075) - 0.006;
    float spout = sdCone2(q, vec3(0.0, 0.07, 0.06), vec3(0.0, 0.2, 0.2), 0.02, 0.01);
    float rose = sdCylY((q - vec3(0.0, 0.205, 0.21)), 0.018, 0.004);
    vec3 hq = q - vec3(0.0, 0.16, -0.02);
    float handle = length(vec2(length(hq.yz) - 0.07, hq.x)) - 0.009;
    handle = max(handle, -hq.y + 0.0);
    float d = min(min(body, spout), min(handle, rose));
    d = smax(d, -sdCylY(q - vec3(0.0, 0.16, 0.0), 0.06, 0.01), 0.004);
    return vec2(d, MAT_CAN);
}

vec2 sdAcorn(vec3 p, vec4 a)
{
    if (a.w <= 0.0) return vec2(1e9, 0.0);
    vec3 q = (p - a.xyz) / a.w;
    if (length(q) > 0.05) return vec2((length(q) - 0.03) * a.w, MAT_ACORN);
    float body = sdEllipsoid(q - vec3(0.0, -0.004, 0.0), vec3(0.0105, 0.0145, 0.0105));
    float cap = sdEllipsoid(q - vec3(0.0, 0.006, 0.0), vec3(0.0125, 0.008, 0.0125));
    cap = max(cap, -(q.y - 0.004));
    float stem = sdCapsule(q, vec3(0.0, 0.012, 0.0), vec3(0.001, 0.017, 0.0), 0.0015);
    float d = min(body, min(cap, stem));
    float m = (cap < body || stem < body) ? MAT_ACORN + 0.5 : MAT_ACORN;
    return vec2(d * a.w, m);
}

vec2 sdTrowel(vec3 p)
{
    if (uTrowel.w > 50.0) return vec2(1e9, 0.0);
    vec3 q = p - uTrowel.xyz;
    if (length(q) > 0.3) return vec2(length(q) - 0.25, MAT_IRON);
    q.xz = rot(-uTrowel.w) * q.xz;
    float handle = sdCapsule(q, vec3(0.0, 0.0, -0.06), vec3(0.0, 0.0, -0.16), 0.012);
    float blade = sdEllipsoid(q - vec3(0.0, -0.005, 0.03), vec3(0.035, 0.006, 0.06));
    float neck = sdCapsule(q, vec3(0.0, 0.0, -0.06), vec3(0.0, 0.0, -0.02), 0.005);
    float d = min(blade, neck);
    if (handle < d) return vec2(handle, MAT_WOOD + 0.5);
    return vec2(d, MAT_IRON);
}

vec2 sdSwing(vec3 p)
{
    if (uSwingShow <= 0.0) return vec2(1e9, 0.0);
    vec3 a = uSwing.xyz;
    float len = uSwingShow;   // rope length
    vec3 q = p - a;
    if (length(q - vec3(0.0, -len * 0.5, 0.0)) > len * 0.7) return vec2(length(q - vec3(0.0, -len * 0.5, 0.0)) - len * 0.6, MAT_ROPE);
    q.yz = rot(uSwing.w) * q.yz;
    vec3 qx = vec3(abs(q.x), q.yz);
    float rope = sdCapsule(qx, vec3(0.22, 0.0, 0.0), vec3(0.22, -len, 0.0), 0.01);
    float seat = sdRBox(q - vec3(0.0, -len - 0.02, 0.0), vec3(0.28, 0.02, 0.1), 0.01);
    if (seat < rope) return vec2(seat, MAT_WOOD);
    return vec2(rope, MAT_ROPE);
}

vec2 sdRocks(vec3 p)
{
    vec2 res = vec2(1e9, MAT_ROCK);
    for (int i = 0; i < 6; i++) {
        vec4 rk = uRock[i];
        vec3 q = p - rk.xyz;
        float bd = length(q) - rk.w * 1.5;
        if (bd > res.x) { res.x = min(res.x, bd + 0.1); continue; }
        float d = sdEllipsoid(q, rk.w * vec3(1.3, 0.75, 1.0));
        if (d < 0.3) d += 0.12 * rk.w * (fbm3(q * 3.0 / rk.w + float(i) * 3.7) - 0.5);
        res.x = min(res.x, d);
    }
    return res;
}

vec2 sdPlanted(vec3 p)
{
    vec2 res = vec2(1e9, MAT_DTREE);
    for (int i = 0; i < uPlantedCount; i++) {
        vec4 t = uPlanted[i];
        vec3 q = p - t.xyz;
        float sz = t.w;
        if (length(q - vec3(0.0, 1.6 * sz, 0.0)) - 1.8 * sz > res.x) continue;
        float trunk = sdCone2(q, vec3(0.0), vec3(0.0, 1.3 * sz, 0.0), 0.08 * sz, 0.05 * sz);
        float crown = sdEllipsoid(q - vec3(0.0, 1.75 * sz, 0.0), vec3(0.85, 0.95, 0.85) * sz);
        crown = smin(crown, sdSphere(q - vec3(0.35, 1.45, 0.2) * sz, 0.55 * sz), 0.3 * sz);
        crown = smin(crown, sdSphere(q - vec3(-0.3, 1.5, -0.25) * sz, 0.5 * sz), 0.3 * sz);
        if (crown < 0.3 * sz) crown += 0.25 * sz * (0.5 - noise3(q * 2.2 / sz + float(i) * 7.0));
        if (trunk < res.x) res = vec2(trunk, MAT_DTRUNK);
        if (crown < res.x) res = vec2(crown, MAT_DTREE);
    }
    return res;
}

vec2 sdFence(vec3 p)
{
    if (uFence.w < 0.5) return vec2(1e9, 0.0);
    vec3 q = p - uFence.xyz;
    if (length(q) > 0.9) return vec2(length(q) - 0.8, MAT_FENCE);
    float d = 1e9;
    for (int i = 0; i < 4; i++) {
        float a = float(i) * 1.5708 + 0.4;
        vec3 sp = vec3(cos(a) * 0.32, 0.0, sin(a) * 0.32);
        d = min(d, sdCapsule(q, sp - vec3(0.0, 0.1, 0.0), sp + vec3(0.0, 0.55, 0.0), 0.012));
    }
    float ring = sdTorus(q - vec3(0.0, 0.42, 0.0), vec2(0.32, 0.004));
    return vec2(min(d, ring), MAT_FENCE);
}

vec2 sdScarfOnBench(vec3 p)
{
    if (uScarfBench.w < 0.5) return vec2(1e9, 0.0);
    vec3 q = p - uScarfBench.xyz;
    if (length(q) > 0.6) return vec2(length(q) - 0.5, MAT_SCARFOBJ);
    q.xz = rot(-uBench.w) * q.xz;
    // folded over the backrest, hanging down
    float a = sdCone2(q, vec3(-0.15, 0.0, 0.0), vec3(0.1, 0.02, 0.0), 0.03, 0.03);
    float b = sdCone2(q, vec3(0.1, 0.02, 0.0), vec3(0.16, -0.25, 0.05), 0.03, 0.028);
    float c = sdCone2(q, vec3(-0.15, 0.0, 0.0), vec3(-0.2, -0.2, -0.06), 0.03, 0.028);
    float d = min(a, min(b, c));
    d = smax(d, abs(q.z) - 0.016, 0.01);
    d += 0.002 * sin(q.x * 160.0) * sin(q.y * 160.0);
    return vec2(d, MAT_SCARFOBJ);
}

// ===========================================================================
// the world
// ===========================================================================
float terrainSDF(vec3 p, float lod)
{
    float h = terrainH(p.xz, lod);
    return (p.y - h) * 0.7;
}

bool domeCanopy() { return uCanopyHullR.x > 0.0; }

float canopyBoundDist(vec3 p)
{
    if (uCanopyExt.x > 0.0) {
        // nothing of the crown hangs below its box: a cheap lower bound near the ground
        float below = (uCanopyBound.y - uCanopyExt.y) - p.y - 0.4;
        return max(sdEllipsoid(p - uCanopyBound.xyz, uCanopyExt.xyz * 1.1 + 1.0), below);
    }
    return length(p - uCanopyBound.xyz) - uCanopyBound.w;
}

vec2 mapObjects(vec3 p, float dmax, bool detail, bool canopy)
{
    vec2 res = vec2(dmax, 0.0);
    // tree wood
    if (uSegCount > 0 && length(p - uWoodBound.xyz) - uWoodBound.w < res.x) {
        float d = sdWood(p, res.x);
        if (d < res.x) res = vec2(d, MAT_BARK);
    }
    // canopy
    if (canopy && uBlobCount > 0 && uLeafAmt > 0.01 && canopyBoundDist(p) < res.x) {
        float d = sdCanopy(p, detail);
        if (d < res.x) res = vec2(d, MAT_LEAF);
    }
    // sprouts
    if (uSprout > 0.0) { vec2 s = sdSprout(p, uTreePos, uSprout); if (s.x < res.x) res = s; }
    if (uSprout2.w > 0.0) { vec2 s = sdSprout(p, uSprout2.xyz, uSprout2.w); if (s.x < res.x) res = s; }
    // people
    for (int c = 0; c < uCharCount; c++) {
        vec4 b = uCBound[c];
        float bd = length(p - b.xyz) - b.w;
        if (bd < res.x) {
            vec2 r = sdCharacter(p, c);
            if (r.x < res.x) res = r;
        }
    }
    vec2 r;
    r = sdBench(p); if (r.x < res.x) res = r;
    r = sdCan(p); if (r.x < res.x) res = r;
    r = sdAcorn(p, uAcorn); if (r.x < res.x) res = r;
    r = sdTrowel(p); if (r.x < res.x) res = r;
    r = sdSwing(p); if (r.x < res.x) res = r;
    r = sdFence(p); if (r.x < res.x) res = r;
    r = sdScarfOnBench(p); if (r.x < res.x) res = r;
    if (uCane.w < 50.0) {
        vec3 q = p - uCane.xyz;
        vec3 dir = vec3(sin(uCane.w) * 0.25, 0.97, cos(uCane.w) * 0.25);
        float cane = sdCapsule(q, vec3(0.0), dir * 0.85, 0.011);
        vec3 tip = dir * 0.85;
        cane = min(cane, sdCapsule(q, tip, tip + vec3(cos(uCane.w), 0.0, -sin(uCane.w)) * 0.07 + vec3(0.0, 0.025, 0.0), 0.011));
        if (cane < res.x) res = vec2(cane, MAT_CANE);
    }
    r = sdRocks(p); if (r.x < res.x) res = r;
    if (uPlantedCount > 0) { r = sdPlanted(p); if (r.x < res.x) res = r; }
    return res;
}

vec2 map(vec3 p, float lod)
{
    float dt = terrainSDF(p, lod);
    // soil mounds where acorns are planted
    if (uMound.w > 0.0) {
        float m = sdEllipsoid(p - uMound.xyz, vec3(uMound.w, uMound.w * 0.28, uMound.w));
        dt = smin(dt, m, 0.08);
    }
    if (uMound2.w > 0.0) {
        float m = sdEllipsoid(p - uMound2.xyz, vec3(uMound2.w, uMound2.w * 0.28, uMound2.w));
        dt = smin(dt, m, 0.08);
    }
    vec2 res = vec2(dt, MAT_TERRAIN);
    if (dot(p.xz, p.xz) > 8100.0) return res;   // every object lives within 90 m of the hill
    vec2 o = mapObjects(p, res.x, lod < 1.0, true);
    if (o.x < res.x) res = o;
    return res;
}

// a big crown casts its shadow through canopyTransmittance() instead
float mapShadow(vec3 p)
{
    float d = (p.y - terrainMidFast(p.xz)) * 0.7;
    if (dot(p.xz, p.xz) > 8100.0) return d;
    vec2 o = mapObjects(p, d, false, !domeCanopy());
    return min(d, o.x);
}

// light filtering through the oak's crown, treated as a cloud of leaves:
// soft, dappled, and far cheaper than tracing every leaf cluster
float canopyTransmittance(vec3 ro, vec3 rd)
{
    if (!domeCanopy() || uLeafAmt <= 0.01) return 1.0;
    vec3 R = uCanopyExt.xyz * 0.98;
    vec3 c = uCanopyBound.xyz + vec3(0.0, 0.1 * R.y, 0.0);
    vec3 o = (ro - c) / R, d = rd / R;
    float a = dot(d, d), b = dot(o, d), cc = dot(o, o) - 1.0;
    float disc = b * b - a * cc;
    if (disc <= 0.0) return 1.0;
    float sq = sqrt(disc);
    float t0 = max((-b - sq) / a, 0.0), t1 = (-b + sq) / a;
    if (t1 <= 0.0) return 1.0;
    float sc = clamp(uCanopyHullR.w, 0.12, 1.0);
    const int N = 8;
    float dt = (t1 - t0) / float(N);
    float tau = 0.0;
    for (int i = 0; i < N; i++) {
        vec3 p = ro + rd * (t0 + (float(i) + 0.5) * dt);
        vec3 q = (p - c) / R;
        float shape = clamp(1.0 - dot(q, q), 0.0, 1.0);
        vec3 nq = p / sc;
        float n = 0.6 * noise3(nq * 0.55 + 3.0) + 0.4 * noise3(nq * 1.6 + 9.0);
        float dens = smoothstep(0.28, 0.58, n * (0.75 + 0.6 * shape));
        tau += dens * dt;
    }
    return exp(-tau * 3.4 * uLeafAmt);
}

vec3 calcNormal(vec3 p, float t, float lod)
{
    float e = 0.0004 * max(t, 1.0);
    vec2 k = vec2(1.0, -1.0);
    return normalize(k.xyy * map(p + k.xyy * e, lod).x + k.yyx * map(p + k.yyx * e, lod).x +
                     k.yxy * map(p + k.yxy * e, lod).x + k.xxx * map(p + k.xxx * e, lod).x);
}

vec3 terrainNormal(vec3 p, float t, float lod)
{
    float e = 0.02 + 0.002 * t;
    float hx = terrainHA(p.xz + vec2(e, 0.0), lod) - terrainHA(p.xz - vec2(e, 0.0), lod);
    float hz = terrainHA(p.xz + vec2(0.0, e), lod) - terrainHA(p.xz - vec2(0.0, e), lod);
    return normalize(vec3(-hx, 2.0 * e, -hz));
}

float softShadow(vec3 ro, vec3 rd, float tmax, float k)
{
    // classic penumbra estimate: robust on the blended (inexact) fields of the
    // characters; a jittered start turns any remaining banding into fine noise
    float res = 1.0;
    float t = 0.01 + 0.02 * hash12(gl_FragCoord.xy);
    for (int i = 0; i < 72; i++) {
        float h = mapShadow(ro + rd * t);
        res = min(res, k * h / t);
        t += clamp(h, 0.012, 0.4 + t * 0.12);
        if (res < 0.003 || t > tmax) break;
    }
    res = clamp(res, 0.0, 1.0);
    return res * res * (3.0 - 2.0 * res);
}

float calcAO(vec3 p, vec3 n, float scale)
{
    float occ = 0.0, sca = 1.0;
    for (int i = 0; i < 5; i++) {
        float h = 0.02 + scale * float(i) / 4.0;
        float d = map(p + h * n, 1.0).x;
        occ += (h - d) * sca;
        sca *= 0.8;
    }
    return clamp(1.0 - 2.2 * occ / (scale * 2.0 + 0.1), 0.0, 1.0);
}

// ===========================================================================
// sky
// ===========================================================================
float ridge(float a, float f, float seed)
{
    float s = 0.0, amp = 0.5;
    for (int i = 0; i < 5; i++) { s += amp * abs(noise1(a * f + seed) * 2.0 - 1.0); f *= 2.1; amp *= 0.5; }
    return s;
}

vec3 skyGradient(vec3 rd)
{
    float y = rd.y;
    float sd = max(dot(rd, uSunDir), 0.0);
    float hz = 1.0 - clamp(y, 0.0, 1.0);
    vec3 col = mix(uSkyZen, uSkyHor, pow(hz, 5.0));
    if (y < 0.0) col = mix(uSkyHor, uFogCol, clamp(-y * 5.0, 0.0, 1.0));
    float sunUp = smoothstep(-0.12, 0.05, uSunDir.y);
    col += uSunCol * (0.035 * pow(sd, 6.0) + 0.2 * pow(sd, 48.0)) * (1.0 - 0.8 * uOvercast) * sunUp;
    float low = 1.0 - clamp(uSunDir.y * 2.5, 0.0, 1.0);
    col += uSunCol * 0.22 * low * pow(sd, 3.0) * pow(hz, 8.0) * (1.0 - 0.7 * uOvercast) * sunUp;
    return col;
}

float cloudDensity(vec2 uv, float cov)
{
    return smoothstep(cov, cov + 0.24, fbm2(uv * 1.1));
}

vec4 cloudLayer(vec3 rd)
{
    if (rd.y < 0.0) return vec4(0.0);
    float tc = 1.0 / (rd.y + 0.05);
    vec2 uv = rd.xz * tc * 0.55 + vec2(uTime * 0.004, uTime * 0.0015);
    float cov = mix(0.64, 0.24, clamp(uCloud, 0.0, 1.0));
    float dens = cloudDensity(uv, cov);
    float sd = max(dot(rd, uSunDir), 0.0);
    float low = 1.0 - smoothstep(0.0, 0.35, uSunDir.y);
    vec3 amb = mix(uSkyZen, uSkyHor, 0.55);
    vec4 res = vec4(0.0);
    if (dens > 0.001) {
        // light marched a few steps towards the sun through the layer
        vec2 sdir = normalize(uSunDir.xz + vec2(1e-4)) * 0.045;
        float sh = 0.0;
        for (int k = 1; k <= 3; k++) sh += cloudDensity(uv + sdir * float(k), cov);
        float lit = exp(-sh * 1.1);
        vec3 sunLit = uSunCol * (1.0 + 1.6 * pow(sd, 8.0));
        vec3 base = amb * 0.62 + uSunCol * 0.04;
        vec3 col = mix(base, sunLit * 0.75 + amb * 0.35, lit);
        // warm undersides when the sun is low
        col = mix(col, uSunCol * 0.8 + amb * 0.2, low * 0.45 * (1.0 - lit) * smoothstep(-0.05, 0.1, uSunDir.y + 0.1));
        // silver lining around the sun
        col += uSunCol * pow(sd, 12.0) * (1.0 - dens) * 2.5;
        col = mix(col, vec3(dot(col, vec3(0.33))) * 0.85, uOvercast * 0.6);
        float fade = smoothstep(0.0, 0.16, rd.y);
        res = vec4(col, dens * fade * (0.88 + 0.12 * uCloud));
    }
    // high cirrus streaks that catch the colours of dawn and dusk
    vec2 cq = rd.xz * (1.0 / (rd.y + 0.12)) * 0.35 + vec2(uTime * 0.002, 0.0);
    cq = mat2(0.9, 0.44, -0.44, 0.9) * cq;
    float ci = noise2(cq * vec2(0.6, 5.0)) * noise2(cq * vec2(1.3, 9.0) + 4.0);
    ci = smoothstep(0.25, 0.65, ci) * smoothstep(0.02, 0.25, rd.y) * (0.35 + 0.65 * low) * (1.0 - uOvercast);
    vec3 ccol = mix(amb * 1.1, uSunCol * 0.9 + amb * 0.2, 0.4 + 0.6 * low) + uSunCol * pow(sd, 6.0);
    ci *= 0.55 * (1.0 - res.a);
    res.rgb = (res.rgb * res.a + ccol * ci) / max(res.a + ci, 1e-4);
    res.a = res.a + ci;
    return res;
}

vec3 stars(vec3 rd)
{
    if (uNight <= 0.01 || rd.y < -0.02) return vec3(0.0);
    vec3 col = vec3(0.0);
    for (int l = 0; l < 2; l++) {
        float sc = l == 0 ? 220.0 : 420.0;
        vec3 q = rd * sc;
        vec3 id = floor(q);
        float h = hash13(id + float(l) * 17.0);
        if (h > 0.985) {
            vec3 c = id + 0.5 + 0.35 * (vec3(hash13(id + 1.1), hash13(id + 2.2), hash13(id + 3.3)) - 0.5);
            float d = length(q - c);
            float tw = 0.6 + 0.4 * sin(uTime * (2.0 + 3.0 * h) + h * 100.0);
            vec3 sc3 = mix(vec3(1.0, 0.85, 0.7), vec3(0.75, 0.85, 1.0), hash13(id + 9.0));
            col += sc3 * smoothstep(0.35, 0.0, d) * (h - 0.985) * 120.0 * tw;
        }
    }
    // milky way
    vec3 mw = normalize(vec3(0.3, 0.6, -0.75));
    float band = exp(-pow(dot(rd, mw) * 4.0, 2.0));
    col += vec3(0.35, 0.38, 0.5) * band * fbm2(rd.xz * 6.0 / (rd.y + 0.3)) * 0.35;
    return col * uNight * smoothstep(-0.02, 0.15, rd.y);
}

vec3 mountains(vec3 rd, vec3 col)
{
    float a = atan(rd.z, rd.x);
    // far range
    float hf = 0.028 + 0.06 * ridge(a, 2.2, 3.0) * (0.6 + 0.4 * sin(a * 2.0 + 0.5));
    vec3 farCol = mix(uSkyHor, uFogCol * 0.9 + vec3(0.02, 0.04, 0.08), 0.55);
    farCol = mix(farCol, uFogCol, 0.35);
    float snow = smoothstep(hf - 0.03, hf - 0.012, rd.y) * smoothstep(0.065, 0.085, hf);
    vec3 farLit = mix(farCol, mix(vec3(1.0), uSunCol, 0.4) * 0.9 + uAmb * 0.5, 0.45 * snow * (1.0 - uNight));
    if (rd.y < hf) col = mix(col, farLit, smoothstep(hf, hf - 0.002, rd.y));
    // nearer range
    float hn = 0.018 + 0.035 * ridge(a, 3.7, 11.0);
    vec3 nearCol = mix(uFogCol, vec3(0.16, 0.22, 0.26) * (uAmb * 2.0 + uSunCol * 0.2), 0.35);
    if (rd.y < hn) col = mix(col, nearCol, smoothstep(hn, hn - 0.002, rd.y));
    return col;
}

vec3 skyColor(vec3 rd, bool withSun)
{
    vec3 col = skyGradient(rd);
    col += stars(rd);
    // moon
    if (uNight > 0.01) {
        float md = dot(rd, uMoonDir);
        col += vec3(0.9, 0.92, 1.0) * smoothstep(0.99955, 0.9997, md) * 2.5 * uNight;
        col += vec3(0.3, 0.35, 0.5) * pow(max(md, 0.0), 200.0) * 0.4 * uNight;
    }
    if (withSun) {
        float sd = dot(rd, uSunDir);
        float disk = smoothstep(0.99955, 0.99975, sd);
        col += uSunCol * disk * 18.0 * (1.0 - uOvercast * 0.95) * smoothstep(-0.05, 0.02, rd.y);
    }
    vec4 cl = cloudLayer(rd);
    col = mix(col, cl.rgb, cl.a);
    col = mountains(rd, col);
    return col;
}

// height fog with sun scattering
vec3 applyFog(vec3 col, vec3 ro, vec3 rd, float t)
{
    float b = 0.06;
    float fogAmt = uFogDen * exp(-max(ro.y + 2.0, 0.0) * b) * (1.0 - exp(-t * max(rd.y, -0.5) * b - 1e-5)) / (max(rd.y, -0.5) * b + 1e-5);
    if (abs(rd.y) < 0.002) fogAmt = uFogDen * exp(-max(ro.y + 2.0, 0.0) * b) * t;
    fogAmt = 1.0 - exp(-max(fogAmt, 0.0));
    float sd = max(dot(rd, uSunDir), 0.0);
    vec3 fc = uFogCol + uSunCol * 0.2 * pow(sd, 8.0) * (1.0 - uOvercast);
    return mix(col, fc, clamp(fogAmt, 0.0, 1.0));
}

// ===========================================================================
// shading
// ===========================================================================
vec3 charColor(int c, int part) { return uCCol[c * 5 + part]; }

vec3 shadeEye(vec3 p, vec3 n, vec3 rd, int c, vec3 ldir)
{
    vec3 hed = J(c, 4), hR = J(c, 21), hU = J(c, 22), hF = J(c, 23);
    float type = uCStyle[c].x;
    float child = (type == 1.0 || type == 3.0) ? 1.0 : 0.0;
    float hs = child > 0.5 ? 1.12 : 1.1;
    vec3 hq = p - hed;
    vec3 hl = vec3(dot(hq, hR), dot(hq, hU), dot(hq, hF)) / hs;
    float sx = sign(hl.x);
    vec3 ec = vec3(EYE_C.x * sx, EYE_C.y, EYE_C.z - 0.002 * child);
    vec3 e = normalize(hl - ec);
    vec3 gw = normalize(uCGaze[c].xyz);
    vec3 gl = normalize(vec3(dot(gw, hR), dot(gw, hU), dot(gw, hF)));
    float ca = dot(e, gl);
    float irisR = 0.6 + 0.05 * child;
    vec3 sclera = vec3(0.92, 0.89, 0.86);
    vec3 irisCol = charColor(c, 4);
    vec3 col = sclera;
    float ang = acos(clamp(ca, -1.0, 1.0));
    float ir = smoothstep(irisR, irisR - 0.05, ang);
    float rays = 0.75 + 0.25 * noise2(vec2(atan(e.y, e.x) * 12.0, ang * 10.0));
    vec3 ic = irisCol * rays * mix(0.6, 1.2, smoothstep(irisR, 0.15, ang));
    col = mix(col, ic, ir);
    col = mix(col, vec3(0.08, 0.06, 0.05), smoothstep(irisR - 0.06, irisR, ang) * ir); // limbal ring
    col = mix(col, vec3(0.01), smoothstep(0.24, 0.2, ang));                            // pupil
    // soft shadow from the lid
    col *= 0.55 + 0.45 * smoothstep(0.75, 0.2, e.y);
    return col;
}

vec3 groundAlbedo(vec3 p, vec3 n, float t, out float grassAmt)
{
    vec2 xz = p.xz;
    float n1 = noise2(xz * 0.15);
    float n2 = noise2(xz * 1.3 + 3.0);
    float n3 = noise2(xz * 7.0);
    vec3 dry = mix(vec3(0.58, 0.5, 0.33), vec3(0.7, 0.62, 0.42), n1);
    dry = mix(dry, vec3(0.5, 0.46, 0.32), n2 * 0.5);
    vec3 green = mix(vec3(0.2, 0.31, 0.11), vec3(0.32, 0.41, 0.17), n1);
    green = mix(green, vec3(0.42, 0.45, 0.24), n2 * 0.35);
    vec3 col = mix(green, dry, uDry);
    col *= 0.85 + 0.3 * n3;
    grassAmt = 1.0;
    // distant patchwork of fields, hedgerows and woods
    float far = smoothstep(38.0, 80.0, length(xz));
    if (far > 0.0) {
        vec2 fq = mat2(0.8, 0.6, -0.6, 0.8) * xz / 26.0;
        vec2 cell = floor(fq);
        vec2 fr = fract(fq);
        float hsh = hash12(cell);
        vec3 field = hsh < 0.22 ? vec3(0.66, 0.58, 0.34) : (hsh < 0.5 ? vec3(0.36, 0.46, 0.2) : (hsh < 0.68 ? vec3(0.5, 0.42, 0.3) : vec3(0.3, 0.42, 0.17)));
        field = mix(field, vec3(0.62, 0.56, 0.36), uDry * 0.4);
        field *= 0.92 + 0.08 * sin((hsh > 0.5 ? fr.x : fr.y) * 70.0);
        float edge = min(min(fr.x, 1.0 - fr.x), min(fr.y, 1.0 - fr.y));
        float hedge = smoothstep(0.03, 0.005, edge);
        col = mix(col, field * (0.9 + 0.2 * n2), far * 0.85);
        col = mix(col, vec3(0.13, 0.2, 0.08), hedge * far * 0.9);
        float wood = smoothstep(0.6, 0.68, noise2(xz * 0.011 + 5.0));
        col = mix(col, vec3(0.1, 0.17, 0.07) * (0.6 + 0.8 * noise2(xz * 0.6)), wood * far);
    }
    // dirt patches on the barren hill
    float bare = smoothstep(0.58, 0.72, noise2(xz * 0.35 + 11.0)) * uDry * 0.7;
    col = mix(col, vec3(0.45, 0.36, 0.26) * (0.85 + 0.3 * n3), bare);
    // footpath
    float pd = pathDist(xz);
    float pw = 0.55 + 0.15 * noise2(xz * 0.8);
    float path = smoothstep(pw, pw - 0.35, pd);
    col = mix(col, vec3(0.55, 0.46, 0.35) * (0.9 + 0.2 * n3), path * 0.85);
    grassAmt *= 1.0 - path * 0.8;
    // shade under the oak keeps the grass greener
    float rt = length(xz - uTreePos.xz);
    float canopyR = uCanopyBound.w * 0.8;
    float under = (uBlobCount > 0) ? smoothstep(canopyR, canopyR * 0.4, rt) * uLeafAmt * smoothstep(0.3, 0.6, uGrowth) : 0.0;
    col = mix(col, vec3(0.16, 0.26, 0.08) * (0.8 + 0.4 * n2), under * 0.6);
    // bare earth ring around the young tree
    float ring = smoothstep(0.55, 0.3, rt) * (1.0 - smoothstep(0.4, 0.7, uGrowth));
    col = mix(col, vec3(0.3, 0.22, 0.15), ring * step(0.001, uSprout + uGrowth));
    // wildflowers when the land is lush
    if (uLush > 0.01 && t < 60.0) {
        vec2 g = xz * 6.0;
        vec2 id = floor(g);
        float h = hash12(id);
        vec2 fp = fract(g) - 0.5 - 0.3 * (vec2(hash12(id + 3.1), hash12(id + 7.7)) - 0.5);
        float fl = smoothstep(0.12, 0.05, length(fp)) * step(1.0 - 0.22 * uLush * smoothstep(0.35, 0.65, noise2(xz * 0.2)), h);
        vec3 fc = h > 0.995 ? vec3(0.95, 0.85, 0.2) : (h > 0.99 ? vec3(0.65, 0.4, 0.85) : (h > 0.985 ? vec3(0.95, 0.35, 0.3) : vec3(0.95, 0.95, 0.9)));
        col = mix(col, fc, fl * (1.0 - path) * smoothstep(60.0, 20.0, t));
    }
    // snow
    float sn = uSnow * smoothstep(0.3, 0.8, n.y) * (0.85 + 0.15 * n2);
    col = mix(col, vec3(0.84, 0.87, 0.93), sn);
    grassAmt *= 1.0 - sn;
    return col;
}

vec3 lighting(vec3 p, vec3 n, vec3 rd, vec3 alb, float sha, float ao, float spec, float rough, float sss, vec3 sssCol)
{
    vec3 l = uSunDir;
    float ndl = dot(n, l);
    float dif = clamp(ndl, 0.0, 1.0);
    float wrap = clamp((ndl + sss * 0.5) / (1.0 + sss * 0.5), 0.0, 1.0);
    vec3 lin = vec3(0.0);
    vec3 key = uSunCol * dif * sha;
    // subsurface: a warm wrap around the terminator and into soft shadows
    float extra = max(wrap - dif, 0.0);
    key += uSunCol * sssCol * extra * (0.35 + 0.65 * sha) * sss;
    key += uSunCol * sssCol * dif * (1.0 - sha) * 0.12 * sss;
    lin += key;
    float skyAmt = 0.55 + 0.45 * n.y;
    lin += uAmb * skyAmt * ao * 1.1;
    // warm bounce from the ground
    vec3 bounceCol = mix(vec3(0.45, 0.38, 0.22), vec3(0.22, 0.32, 0.12), 1.0 - uDry) * (uSunCol * 0.18 + uAmb * 0.15);
    lin += bounceCol * clamp(0.5 - 0.5 * n.y, 0.0, 1.0) * ao;
    // moonlight
    lin += vec3(0.05, 0.07, 0.12) * uNight * clamp(dot(n, uMoonDir) * 0.5 + 0.5, 0.0, 1.0) * ao;
    vec3 col = alb * lin;
    // specular
    vec3 h = normalize(l - rd);
    float shin = mix(200.0, 8.0, rough);
    float fre = pow(clamp(1.0 + dot(n, rd), 0.0, 1.0), 5.0);
    float sp = pow(clamp(dot(n, h), 0.0, 1.0), shin) * (shin + 8.0) / 32.0;
    col += uSunCol * sp * spec * sha * dif * (0.04 + 0.96 * fre * 0.3 + 0.2);
    // sky rim
    col += uAmb * fre * spec * 0.6 * ao;
    return col;
}

vec3 shade(vec3 ro, vec3 rd, float t, float mat, out float outDepth)
{
    vec3 p = ro + rd * t;
    float lod = t > 40.0 ? 1.0 : 0.0;
    vec3 n;
    bool isTerrain = mat == MAT_TERRAIN;
    if (isTerrain) {
        n = terrainNormal(p, t, lod);
        // mounds
        if (uMound.w > 0.0 || uMound2.w > 0.0) {
            float dm = min(uMound.w > 0.0 ? length(p.xz - uMound.xz) - uMound.w * 1.2 : 1e9, uMound2.w > 0.0 ? length(p.xz - uMound2.xz) - uMound2.w * 1.2 : 1e9);
            if (dm < 0.0) n = calcNormal(p, t, lod);
        }
    } else {
        n = calcNormal(p, t, lod);
    }

    vec3 alb = vec3(0.5);
    float spec = 0.1, rough = 0.8, sss = 0.0, aoScale = 0.35;
    vec3 sssCol = vec3(0.0);
    vec3 emis = vec3(0.0);
    float grassAmt = 0.0;
    float transl = 0.0;

    if (isTerrain) {
        alb = groundAlbedo(p, n, t, grassAmt);
        float soil = 0.0;
        if (uMound.w > 0.0) soil = max(soil, smoothstep(uMound.w * 1.3, uMound.w * 0.7, length(p.xz - uMound.xz)));
        if (uMound2.w > 0.0) soil = max(soil, smoothstep(uMound2.w * 1.3, uMound2.w * 0.7, length(p.xz - uMound2.xz)));
        alb = mix(alb, vec3(0.24, 0.17, 0.11) * (0.8 + 0.4 * noise2(p.xz * 40.0)) * (1.0 - 0.35 * uWet), soil);
        grassAmt *= 1.0 - soil;
        // grass micro-normal
        if (t < 30.0 && grassAmt > 0.0) {
            vec2 g = p.xz * 9.0;
            vec3 gn = vec3(noise2(g) - 0.5, 0.0, noise2(g + 17.0) - 0.5);
            n = normalize(n + gn * 0.35 * grassAmt * smoothstep(30.0, 5.0, t));
        }
        spec = 0.05 + 0.4 * uSnow;
        rough = mix(0.9, 0.4, uSnow);
        aoScale = 1.2;
        transl = grassAmt;
    } else if (mat == MAT_BARK) {
        float rt = length(p.xz - uTreePos.xz);
        vec3 q = p * vec3(9.0, 1.8, 9.0);
        float b = noise3(q) * 0.6 + noise3(q * 2.7) * 0.4;
        float ridges = smoothstep(0.35, 0.65, b);
        alb = mix(vec3(0.19, 0.15, 0.12), vec3(0.42, 0.38, 0.33), ridges);
        alb = mix(alb, vec3(0.32, 0.36, 0.2), smoothstep(0.55, 0.8, noise3(p * 2.3)) * 0.4 * (1.0 - uDry)); // moss
        float young = 1.0 - smoothstep(0.15, 0.4, uGrowth);
        alb = mix(alb, vec3(0.42, 0.32, 0.22), young * 0.7);
        alb = mix(alb, vec3(0.92, 0.94, 0.98), uSnow * smoothstep(0.5, 0.85, n.y));
        spec = 0.05; rough = 0.95; aoScale = 0.5;
    } else if (mat == MAT_LEAF) {
        float v = noise3(p * 0.6);
        float cl = noise3(p * 8.0);
        vec3 cell = floor(p * 24.0);
        float lf = hash13(cell);
        alb = mix(uLeafCol, uLeafCol2, smoothstep(0.3, 0.7, v));
        alb *= (0.55 + 0.65 * cl) * (0.82 + 0.36 * lf);
        float jit = 0.5 * smoothstep(45.0, 8.0, t);
        n = normalize(n + jit * (vec3(hash13(cell + 1.3), hash13(cell + 2.7), hash13(cell + 5.1)) - 0.5));
        alb = mix(alb, vec3(0.9, 0.93, 0.97), uSnow * 0.5 * smoothstep(0.4, 0.9, n.y));
        spec = 0.3 + 0.5 * step(0.75, lf); rough = 0.45; sss = 0.6; sssCol = alb * 1.6; aoScale = 2.2;
        transl = 1.0;
    } else if (mat == MAT_SPROUT) {
        float young = smoothstep(0.7, 0.2, max(uSprout, uSprout2.w));
        alb = mix(vec3(0.36, 0.56, 0.16), vec3(0.55, 0.32, 0.16), young * 0.6);
        spec = 0.3; rough = 0.4; sss = 0.8; sssCol = vec3(0.4, 0.8, 0.1); aoScale = 0.05;
        transl = 1.0;
    } else if (mat == MAT_ROCK) {
        float r = fbm3(p * 2.0);
        alb = mix(vec3(0.36, 0.34, 0.31), vec3(0.55, 0.52, 0.47), r);
        alb = mix(alb, vec3(0.3, 0.38, 0.16), smoothstep(0.55, 0.75, n.y + 0.2 * r) * (1.0 - uDry) * 0.7);
        alb = mix(alb, vec3(0.92, 0.94, 0.98), uSnow * smoothstep(0.5, 0.8, n.y));
        spec = 0.1; rough = 0.8; aoScale = 0.4;
    } else if (mat == MAT_WOOD || mat == MAT_WOOD + 0.5) {
        vec3 wq = p;
        if (uBench.w < 50.0) { wq -= uBench.xyz; wq.xz = rot(-uBench.w) * wq.xz; }
        float grain = noise3(vec3(wq.x * 1.5, wq.y * 30.0, wq.z * 30.0));
        alb = mix(vec3(0.42, 0.27, 0.15), vec3(0.6, 0.42, 0.26), grain);
        alb = mix(alb, vec3(0.45, 0.43, 0.4), uBenchAge * 0.6);      // weathered grey
        alb = mix(alb, vec3(0.25, 0.32, 0.12), uBenchAge * smoothstep(0.6, 0.8, noise3(p * 6.0)) * 0.5);
        alb = mix(alb, vec3(0.92, 0.94, 0.98), uSnow * smoothstep(0.7, 0.9, n.y));
        spec = 0.15; rough = 0.6; aoScale = 0.15;
    } else if (mat == MAT_CANE) {
        alb = vec3(0.3, 0.18, 0.09); spec = 0.5; rough = 0.3; aoScale = 0.1;
    } else if (mat == MAT_ACORN || mat == MAT_ACORN + 0.5) {
        if (mat > MAT_ACORN + 0.25) {
            alb = vec3(0.35, 0.27, 0.16) * (0.7 + 0.5 * noise3(p * 900.0));
            rough = 0.9; spec = 0.05;
        } else {
            alb = vec3(0.55, 0.33, 0.12); spec = 1.0; rough = 0.25;
        }
        aoScale = 0.01;
    } else if (mat == MAT_CAN) {
        alb = vec3(0.22, 0.45, 0.48); spec = 1.0; rough = 0.25; aoScale = 0.08;
    } else if (mat == MAT_IRON) {
        alb = vec3(0.45, 0.45, 0.47); spec = 1.5; rough = 0.3; aoScale = 0.05;
    } else if (mat == MAT_ROPE) {
        alb = vec3(0.55, 0.45, 0.3) * (0.8 + 0.3 * sin((p.y + p.x) * 300.0)); spec = 0.05; aoScale = 0.1;
    } else if (mat == MAT_SCARFOBJ) {
        alb = vec3(0.62, 0.06, 0.06) * (0.8 + 0.25 * noise3(p * 200.0)); sss = 0.3; sssCol = vec3(0.6, 0.1, 0.05); spec = 0.1; rough = 0.9; aoScale = 0.08;
    } else if (mat == MAT_DTREE) {
        float v = noise3(p * 1.5);
        alb = mix(uLeafCol, uLeafCol2, v) * (0.8 + 0.4 * noise3(p * 5.0));
        spec = 0.15; rough = 0.6; sss = 0.5; sssCol = alb; aoScale = 0.8;
        transl = 1.0;
    } else if (mat == MAT_DTRUNK || mat == MAT_FENCE) {
        alb = vec3(0.3, 0.22, 0.15); spec = 0.05; aoScale = 0.2;
    } else if (mat >= MAT_CHAR) {
        float mc = mat - MAT_CHAR;
        int c = int(mc / 20.0 + 0.001);
        int part = int(mc - float(c) * 20.0 + 0.5);
        float type = uCStyle[c].x;
        aoScale = 0.08;
        if (part == 0) {
            // skin
            alb = charColor(c, 0);
            vec3 hed = J(c, 4), hR = J(c, 21), hU = J(c, 22), hF = J(c, 23);
            float child = (type == 1.0 || type == 3.0) ? 1.0 : 0.0;
            float hs = child > 0.5 ? 1.12 : 1.1;
            vec3 hq = p - hed;
            vec3 hl = vec3(dot(hq, hR), dot(hq, hU), dot(hq, hF)) / hs;
            if (length(hl) < 0.16) {
                vec4 face = uCFace[c];
                vec3 hx = vec3(abs(hl.x), hl.yz);
                // blush
                float blush = smoothstep(0.03, 0.0, length(hx - vec3(0.04, -0.018, 0.062)));
                alb = mix(alb, alb * vec3(1.12, 0.78, 0.76), blush * (0.45 + 0.25 * child));
                // lips and mouth
                float my = -0.043 + 0.006 * child;
                float smile = face.y;
                float curve = my + smile * 0.006 * (hl.x * hl.x) / (0.018 * 0.018) - smile * 0.0015;
                float onFront = smoothstep(0.05, 0.065, hl.z);
                float lipBand = smoothstep(0.0105, 0.006, abs(hl.y - curve)) * smoothstep(0.018, 0.012, abs(hl.x));
                alb = mix(alb, alb * vec3(0.9, 0.68, 0.66), lipBand * onFront * (type == 0.0 ? 0.45 : 0.6));
                float line = smoothstep(0.0014, 0.0004, abs(hl.y - curve)) * smoothstep(0.018, 0.012, abs(hl.x));
                alb = mix(alb, vec3(0.2, 0.06, 0.06), line * onFront * 0.7);
                // smile creases / laugh lines for grandmother
                if (type == 0.0) {
                    vec2 eo = hx.xy - EYE_C.xy;
                    float crow = smoothstep(0.35, 0.0, abs(fract(atan(eo.y, eo.x) * 2.2 + 0.5) - 0.5)) * smoothstep(0.03, 0.024, length(eo)) * smoothstep(0.018, 0.022, length(eo)) * step(EYE_C.x + 0.012, hx.x);
                    alb *= 1.0 - crow * 0.22;
                    float naso = smoothstep(0.0025, 0.0, abs(length(hx.xy - vec2(0.0, -0.008)) - 0.031 - 0.002 * smile)) * smoothstep(-0.052, -0.022, hl.y) * smoothstep(-0.002, -0.018, hl.y) * smoothstep(0.012, 0.022, hx.x);
                    alb *= 1.0 - naso * 0.2;
                }
                // lashes along the upper lid, a softer line under the eye
                {
                    float er = eyeRadius(child);
                    vec3 e = hx - (EYE_C + vec3(0.0, 0.0, -0.002 * child));
                    float yTop = mix(0.6 * er, -1.05 * er, clamp(face.x, 0.0, 1.0)) - 0.1 * er * float(type == 0.0);
                    float edge = yTop - 0.25 * e.x / er * er * 0.3;
                    float onShell = smoothstep(er + 0.004, er + 0.0015, length(e)) * step(-0.3 * er, e.z);
                    float w = 0.0011 + 0.0009 * smoothstep(-0.2 * er, er, e.x) * float(type != 3.0);
                    float lash = smoothstep(w, w * 0.3, abs(e.y - edge - 0.0006)) * onShell;
                    alb = mix(alb, vec3(0.07, 0.05, 0.04), lash * 0.9);
                    float yLow = -0.78 * er;
                    float under = smoothstep(0.0012, 0.0, abs(e.y - yLow)) * onShell * 0.35;
                    alb = mix(alb, alb * vec3(0.7, 0.55, 0.5), under);
                }
                // tears of joy
                if (uCGaze[c].w > 0.0) {
                    float tr = smoothstep(0.004, 0.0, length(hx.xy - vec2(EYE_C.x - 0.003, EYE_C.y - 0.018)));
                    spec += tr * 3.0 * uCGaze[c].w;
                }
            }
            alb *= 0.94 + 0.08 * noise3(p * 90.0);
            sss = 0.6; sssCol = vec3(1.0, 0.7, 0.6); spec = 0.22; rough = 0.55;
        } else if (part == 1) {
            alb = charColor(c, 1);
            vec3 hU = J(c, 22);
            float strands = noise3(p * vec3(400.0, 60.0, 400.0));
            alb *= 0.75 + 0.5 * strands;
            spec = 0.35; rough = 0.5; sss = 0.2; sssCol = alb;
        } else if (part == 2) {
            alb = charColor(c, 2);
            if (type == 0.0 || type == 2.0) {
                // knit texture
                float knit = noise3(p * vec3(320.0, 140.0, 320.0)) * 0.6 + noise3(p * 900.0) * 0.4;
                alb *= 0.86 + 0.24 * knit;
            } else {
                alb *= 0.92 + 0.12 * noise3(p * 150.0);
            }
            spec = 0.08; rough = 0.95; sss = 0.25; sssCol = alb;
        } else if (part == 3) {
            alb = charColor(c, 3) * (0.9 + 0.15 * noise3(p * 120.0));
            spec = 0.06; rough = 0.95; sss = 0.15; sssCol = alb;
        } else if (part == 4) {
            alb = type == 1.0 ? vec3(0.65, 0.12, 0.1) : (type == 3.0 ? vec3(0.85, 0.85, 0.82) : vec3(0.25, 0.15, 0.09));
            spec = 0.6; rough = 0.35;
        } else if (part == 5) {
            vec3 q = p * 260.0;
            float knit = 0.5 + 0.5 * sin(q.x * 0.7 + q.y) * sin(q.z * 0.7 - q.y);
            alb = vec3(0.66, 0.07, 0.07) * (0.8 + 0.3 * knit);
            sss = 0.35; sssCol = vec3(0.7, 0.1, 0.05); spec = 0.08; rough = 0.95;
        } else if (part == 6) {
            alb = shadeEye(p, n, rd, c, uSunDir);
            spec = 3.0; rough = 0.05;
        } else if (part == 7) {
            alb = vec3(0.7, 0.6, 0.35); spec = 2.0; rough = 0.2;
        } else if (part == 8) {
            alb = vec3(0.92, 0.9, 0.86); spec = 0.05; rough = 0.9;
        } else if (part == 10) {
            alb = vec3(0.95, 0.75, 0.15); spec = 0.8; rough = 0.3;
        }
    }

    // all albedos above are authored in sRGB
    alb = pow(max(alb, vec3(0.0)), vec3(2.2));
    if (sss > 0.0) sssCol = pow(max(sssCol, vec3(0.0)), vec3(2.2));

    float sha = 1.0;
    float ndl = dot(n, uSunDir);
    float sunVis = smoothstep(-0.08, 0.02, uSunDir.y);
    if ((ndl > -0.25 || sss > 0.0 || transl > 0.0) && sunVis > 0.0) {
        float k = mix(7.0, 2.0, uOvercast);
        float tmax = isTerrain ? 120.0 : 60.0;
        sha = softShadow(p + n * (isTerrain ? 0.06 : 0.01) * max(1.0, t * 0.05), uSunDir, tmax, k);
        if (sha > 0.001) sha *= canopyTransmittance(p, uSunDir);
    }
    sha *= sunVis;
    sha = mix(sha, 0.6 + 0.4 * sha, uOvercast);
    float ao = t > 140.0 ? 1.0 : calcAO(p, n, aoScale);
    if (mat != MAT_LEAF && domeCanopy() && p.y < uCanopyBound.y) {
        // the canopy blocks part of the skylight underneath
        vec2 rq = (p.xz - uCanopyBound.xz) / uCanopyExt.xz;
        float cov = smoothstep(1.0, 0.35, length(rq)) * uLeafAmt;
        ao *= 1.0 - 0.3 * cov;
    }

    vec3 col = lighting(p, n, rd, alb, sha, ao, spec, rough, sss, sssCol);

    // backlit translucency of grass and leaves
    float back = pow(clamp(dot(rd, uSunDir), 0.0, 1.0), 3.0);
    col += uSunCol * alb * transl * back * sha * (isTerrain ? 0.55 : 0.9) * (1.0 - uSnow);
    if (isTerrain && t < 60.0) {
        // glitter of snow
        float gl = step(0.995, hash12(floor(p.xz * 60.0))) * uSnow;
        col += uSunCol * gl * sha * 1.5;
    }
    // rim light separates people from the background, strongest against the sun
    if (mat >= MAT_CHAR) {
        float fr = pow(1.0 - clamp(dot(n, -rd), 0.0, 1.0), 3.0);
        float backness = clamp(dot(rd, uSunDir) * 0.5 + 0.5, 0.0, 1.0);
        col += (uSunCol * 0.35 * backness * backness * sha + uAmb * 0.25) * fr * alb * 2.0;
    }
    // eye catchlight
    if (mat >= MAT_CHAR) {
        float mc = mat - MAT_CHAR;
        int c = int(mc / 20.0 + 0.001);
        int part = int(mc - float(c) * 20.0 + 0.5);
        if (part == 6) {
            vec3 kl = normalize(-rd + uCamUp * 0.5 + uCamRight * 0.35);
            vec3 r = reflect(rd, n);
            col += vec3(1.0, 0.97, 0.92) * smoothstep(0.985, 0.995, dot(r, kl)) * 1.4;
            col += skyColor(r, false) * 0.08;
        }
    }
    col += emis;
    outDepth = t;
    return col;
}

// water: the river in the valley
vec3 shadeWater(vec3 ro, vec3 rd, float t)
{
    vec3 p = ro + rd * t;
    vec2 w = p.xz * 0.4 + vec2(uTime * 0.25, uTime * 0.1);
    vec3 n = normalize(vec3((noise2(w) - 0.5) * 0.12, 1.0, (noise2(w + 9.0) - 0.5) * 0.12));
    vec3 r = reflect(rd, n);
    float fre = 0.02 + 0.98 * pow(1.0 - clamp(dot(-rd, n), 0.0, 1.0), 5.0);
    vec3 refl = skyColor(r, true);
    vec3 deep = vec3(0.04, 0.09, 0.1) * (uAmb + uSunCol * 0.2);
    vec3 col = mix(deep, refl, fre);
    if (uSnow > 0.6) col = mix(col, vec3(0.75, 0.8, 0.88) * (uAmb + uSunCol * 0.5), 0.6);
    return col;
}

void main()
{
    vec2 fc = gl_FragCoord.xy + uOffset;
    vec2 uv = (fc - 0.5 * uRes) / (0.5 * uRes.y);
    vec3 ro = uCamPos;
    vec3 rd = normalize(uCamFwd + uv.x * uTanHalf * uCamRight + uv.y * uTanHalf * uCamUp);

    // march
    float tmax = 900.0;
    float t = 0.05;
    float mat = -1.0;
    float waterT = rd.y < 0.0 ? (WATER_Y - ro.y) / rd.y : 1e9;
    float tlim = min(tmax, waterT);
    for (int i = 0; i < 320; i++) {
        vec3 p = ro + rd * t;
        float lod = t > 40.0 ? 1.0 : 0.0;
        vec2 h = map(p, lod);
        float eps = 0.00025 * t + 0.0001;
        if (h.x < eps) { mat = h.y; break; }
        t += h.x;
        if (t > tlim || (p.y > 60.0 && rd.y > 0.0)) { t = tlim + 1.0; break; }
    }

    vec3 col;
    float depth;
    if (mat > 0.0 && t < tlim) {
        col = shade(ro, rd, t, mat, depth);
        col = applyFog(col, ro, rd, t);
    } else if (waterT < tmax) {
        t = waterT;
        col = shadeWater(ro, rd, t);
        col = applyFog(col, ro, rd, t);
        depth = t;
    } else {
        col = skyColor(rd, true);
        t = 5000.0;
        depth = 5000.0;
    }

    float viewZ = t * dot(rd, uCamFwd);
    float n = uClip.x, f = uClip.y;
    float zc = min(viewZ, f * 0.999);
    float ndc = (f + n) / (f - n) - (2.0 * f * n) / ((f - n) * zc);
    gl_FragDepth = clamp(ndc * 0.5 + 0.5, 0.0, 1.0);
    fragColor = vec4(max(col, vec3(0.0)), min(viewZ, 5000.0));
}
