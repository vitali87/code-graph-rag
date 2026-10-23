#include "mathx.h"

#include <math.h>

float clampf(float x, float a, float b) { return x < a ? a : (x > b ? b : x); }
float saturate(float x) { return clampf(x, 0.0f, 1.0f); }
float mixf(float a, float b, float t) { return a + (b - a) * t; }

float smooth(float e0, float e1, float x)
{
    float t = saturate((x - e0) / (e1 - e0));
    return t * t * (3.0f - 2.0f * t);
}

float ease(float t) { t = saturate(t); return t * t * (3.0f - 2.0f * t); }
float easeInOut(float t) { t = saturate(t); return t * t * t * (t * (t * 6.0f - 15.0f) + 10.0f); }
float easeOut(float t) { t = saturate(t); return 1.0f - (1.0f - t) * (1.0f - t); }
float easeIn(float t) { t = saturate(t); return t * t; }

/* 0 before a, ramps to 1 over fadeIn, holds, ramps back to 0 over fadeOut ending at b. */
float window(float t, float a, float b, float fadeIn, float fadeOut)
{
    return smooth(a, a + fadeIn, t) * (1.0f - smooth(b - fadeOut, b, t));
}

float hash1(float n)
{
    float s = sinf(n * 127.1f) * 43758.5453f;
    return s - floorf(s);
}

float noise1(float x)
{
    float i = floorf(x), f = x - i;
    float u = f * f * (3.0f - 2.0f * f);
    return mixf(hash1(i), hash1(i + 1.0f), u) * 2.0f - 1.0f;
}

float fbm1(float x)
{
    return 0.5f * noise1(x) + 0.25f * noise1(x * 2.03f + 1.7f) + 0.125f * noise1(x * 4.01f + 3.1f);
}

Vector3 v3(float x, float y, float z) { return (Vector3){ x, y, z }; }
Vector3 v3lerp(Vector3 a, Vector3 b, float t) { return v3(mixf(a.x, b.x, t), mixf(a.y, b.y, t), mixf(a.z, b.z, t)); }
Vector3 v3scale(Vector3 a, float s) { return v3(a.x * s, a.y * s, a.z * s); }
Vector3 v3add(Vector3 a, Vector3 b) { return v3(a.x + b.x, a.y + b.y, a.z + b.z); }
Vector3 v3sub(Vector3 a, Vector3 b) { return v3(a.x - b.x, a.y - b.y, a.z - b.z); }
float v3dot(Vector3 a, Vector3 b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
float v3len(Vector3 a) { return sqrtf(v3dot(a, a)); }

Vector3 v3norm(Vector3 a)
{
    float l = v3len(a);
    return l > 1e-8f ? v3scale(a, 1.0f / l) : v3(0, 1, 0);
}

Vector3 v3cross(Vector3 a, Vector3 b)
{
    return v3(a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x);
}

Vector3 v3rotY(Vector3 a, float ang)
{
    float c = cosf(ang), s = sinf(ang);
    return v3(c * a.x + s * a.z, a.y, -s * a.x + c * a.z);
}

Vector3 v3mix3(Vector3 a, Vector3 b, Vector3 c, float t)
{
    return t < 1.0f ? v3lerp(a, b, t) : v3lerp(b, c, t - 1.0f);
}

Vector3 catmull(Vector3 p0, Vector3 p1, Vector3 p2, Vector3 p3, float t)
{
    float t2 = t * t, t3 = t2 * t;
    Vector3 r;
    r.x = 0.5f * ((2 * p1.x) + (-p0.x + p2.x) * t + (2 * p0.x - 5 * p1.x + 4 * p2.x - p3.x) * t2 + (-p0.x + 3 * p1.x - 3 * p2.x + p3.x) * t3);
    r.y = 0.5f * ((2 * p1.y) + (-p0.y + p2.y) * t + (2 * p0.y - 5 * p1.y + 4 * p2.y - p3.y) * t2 + (-p0.y + 3 * p1.y - 3 * p2.y + p3.y) * t3);
    r.z = 0.5f * ((2 * p1.z) + (-p0.z + p2.z) * t + (2 * p0.z - 5 * p1.z + 4 * p2.z - p3.z) * t2 + (-p0.z + 3 * p1.z - 3 * p2.z + p3.z) * t3);
    return r;
}

Vector3 rgb(int hex)
{
    return v3(((hex >> 16) & 255) / 255.0f, ((hex >> 8) & 255) / 255.0f, (hex & 255) / 255.0f);
}

static float gauss2(float x, float z, float cx, float cz, float s)
{
    float dx = x - cx, dz = z - cz;
    return expf(-(dx * dx + dz * dz) / (2.0f * s * s));
}

/* 256x256 random bytes shared with the GPU noise texture (red channel) */
const unsigned char *NoiseTable(void)
{
    static unsigned char tab[256 * 256];
    static int ready = 0;
    if (!ready) {
        unsigned s = 12345u;
        for (int i = 0; i < 256 * 256; i++) { s = s * 1664525u + 1013904223u; tab[i] = (unsigned char)(s >> 24); }
        ready = 1;
    }
    return tab;
}

/* identical to noise2() in scene.fs: smoothed bilinear lookup into the table */
float noise2(float x, float y)
{
    const unsigned char *t = NoiseTable();
    float px = floorf(x), py = floorf(y);
    float fx = x - px, fy = y - py;
    fx = fx * fx * (3.0f - 2.0f * fx);
    fy = fy * fy * (3.0f - 2.0f * fy);
    int ix = (int)px & 255, iy = (int)py & 255;
    int ix1 = (ix + 1) & 255, iy1 = (iy + 1) & 255;
    float a = t[iy * 256 + ix], b = t[iy * 256 + ix1], c = t[iy1 * 256 + ix], d = t[iy1 * 256 + ix1];
    return mixf(mixf(a, b, fx), mixf(c, d, fx), fy) / 255.0f;
}

static float terrainMacro(float x, float z)
{
    float r = sqrtf(x * x + z * z);
    float h = 7.0f * gauss2(x, z, 0.0f, 0.0f, 15.0f);
    h += 4.5f * gauss2(x, z, -46.0f, -28.0f, 14.0f);
    h += 6.0f * gauss2(x, z, 52.0f, -44.0f, 18.0f);
    h += 3.0f * gauss2(x, z, 34.0f, 62.0f, 16.0f);
    h += 3.5f * gauss2(x, z, -40.0f, 40.0f, 15.0f);
    float u = 1.2f * sinf(0.031f * x + 0.7f) * cosf(0.027f * z - 0.4f) + 0.7f * sinf(0.053f * x - 0.061f * z + 1.3f);
    h += u * smooth(12.0f, 40.0f, r);
    float zc = -88.0f + 14.0f * sinf(0.018f * x + 0.5f);
    float dz = z - zc;
    h -= 4.0f * expf(-dz * dz / (2.0f * 24.0f * 24.0f)) + 3.0f * expf(-dz * dz / (2.0f * 6.0f * 6.0f));
    float ang = atan2f(z, x);
    h += 14.0f * smooth(150.0f, 460.0f, r) * (0.65f + 0.35f * sinf(ang * 3.0f + 1.0f) * sinf(ang * 5.0f - 0.5f));
    return h;
}

float terrainHeight(float x, float z)
{
    float r = sqrtf(x * x + z * z);
    float qx = x * 0.07f, qz = z * 0.07f;
    float n = 0.5f * noise2(qx, qz);
    float tx = 1.6f * qx - 1.2f * qz, tz = 1.2f * qx + 1.6f * qz;
    n += 0.25f * noise2(tx, tz);
    qx = 1.6f * tx - 1.2f * tz;
    qz = 1.2f * tx + 1.6f * tz;
    n += 0.125f * noise2(qx, qz);
    return terrainMacro(x, z) + 0.4f * (n / 0.875f - 0.5f) * smooth(1.5f, 7.0f, r);
}

Vector3 onGround(float x, float z, float lift)
{
    return v3(x, terrainHeight(x, z) + lift, z);
}
