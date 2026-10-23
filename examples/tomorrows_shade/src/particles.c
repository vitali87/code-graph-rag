#include "particles.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "rlgl.h"

/* Every particle is a pure function of time and its index, so the film can be
 * scrubbed or rendered out of order and still look identical. */

static Texture2D gDot, gLeaf, gSeed, gPetal;
static Shader gShader;
static int gLocGain;

static float h1(float n) { float s = sinf(n * 127.1f + 11.3f) * 43758.5453f; return s - floorf(s); }

static Texture2D MakeTex(int kind)
{
    const int N = 64;
    Color *px = malloc(sizeof(Color) * N * N);
    for (int y = 0; y < N; y++) {
        for (int x = 0; x < N; x++) {
            float u = (x + 0.5f) / N * 2.0f - 1.0f, v = (y + 0.5f) / N * 2.0f - 1.0f;
            float a = 0.0f, l = 1.0f;
            if (kind == 0) {
                float r = sqrtf(u * u + v * v);
                a = expf(-r * r * 5.0f) * (1.0f - smooth(0.85f, 1.0f, r));
            } else if (kind == 1) {
                /* oak leaf silhouette with a midrib */
                float t = (v + 1.0f) * 0.5f;
                float w = 0.48f * powf(fmaxf(sinf(PI_F * t), 0.0f), 0.7f) * (0.78f + 0.22f * cosf(t * 28.0f));
                a = smooth(w + 0.03f, w - 0.03f, fabsf(u)) * smooth(0.0f, 0.06f, t) * smooth(1.0f, 0.95f, t);
                l = 0.85f + 0.15f * (1.0f - fabsf(u) / fmaxf(w, 0.01f));
                if (fabsf(u) < 0.025f) l *= 0.75f;
            } else if (kind == 2) {
                /* dandelion seed: a fluffy radial pappus over a short stalk */
                float r = sqrtf(u * u + (v + 0.35f) * (v + 0.35f));
                float ang = atan2f(v + 0.35f, u);
                float rays = powf(fabsf(cosf(ang * 9.0f)), 18.0f);
                if (v < -0.35f) a = 0.0f;
                else a = fmaxf(rays * smooth(0.65f, 0.3f, r) * 0.9f, smooth(0.62f, 0.45f, r) * 0.25f);
                if (fabsf(u) < 0.03f && v < -0.3f && v > -0.9f) a = fmaxf(a, 0.8f);
            } else {
                /* petal */
                float r = sqrtf(u * u * 2.2f + v * v);
                a = smooth(0.95f, 0.7f, r);
                l = 0.9f + 0.1f * v;
            }
            unsigned char c = (unsigned char)(255 * clampf(l, 0, 1));
            px[y * N + x] = (Color){ c, c, c, (unsigned char)(255 * clampf(a, 0, 1)) };
        }
    }
    Image img = { px, N, N, 1, PIXELFORMAT_UNCOMPRESSED_R8G8B8A8 };
    Texture2D t = LoadTextureFromImage(img);
    GenTextureMipmaps(&t);
    SetTextureFilter(t, TEXTURE_FILTER_TRILINEAR);
    free(px);
    return t;
}

static const char *kParticleFS =
    "#version 330\n"
    "in vec2 fragTexCoord;\n"
    "in vec4 fragColor;\n"
    "uniform sampler2D texture0;\n"
    "uniform vec4 colDiffuse;\n"
    "uniform float uGain;\n"
    "out vec4 finalColor;\n"
    "void main() {\n"
    "    vec4 t = texture(texture0, fragTexCoord) * colDiffuse * fragColor;\n"
    "    finalColor = vec4(t.rgb * uGain, t.a);\n"
    "}\n";

void ParticlesInit(void)
{
    gDot = MakeTex(0);
    gLeaf = MakeTex(1);
    gSeed = MakeTex(2);
    gPetal = MakeTex(3);
    gShader = LoadShaderFromMemory(NULL, kParticleFS);
    gLocGain = GetShaderLocation(gShader, "uGain");
}

/* linear HDR colour -> 8-bit tint + gain */
static Color Tint(Vector3 lin, float alpha, float *gain)
{
    float m = fmaxf(lin.x, fmaxf(lin.y, fmaxf(lin.z, 1e-4f)));
    *gain = m;
    return (Color){ (unsigned char)(255 * lin.x / m), (unsigned char)(255 * lin.y / m), (unsigned char)(255 * lin.z / m), (unsigned char)(255 * clampf(alpha, 0, 1)) };
}

static void SetGain(float g)
{
    rlDrawRenderBatchActive();
    SetShaderValue(gShader, gLocGain, &g, SHADER_UNIFORM_FLOAT);
}

static Vector3 Lin(Vector3 srgb) { return v3(powf(srgb.x, 2.2f), powf(srgb.y, 2.2f), powf(srgb.z, 2.2f)); }

static Vector3 Light(const World *w, float sunK)
{
    return v3add(v3scale(w->light.amb, 1.2f), v3scale(w->light.sunCol, sunK * smooth(-0.05f, 0.1f, w->light.sunDir.y)));
}

/* wrap a world position into a box that follows the camera */
static float wrap(float x, float c, float size) { float d = x - c + size * 0.5f; d -= floorf(d / size) * size; return c - size * 0.5f + d; }

/* textured quad with arbitrary orientation */
static void Quad(Texture2D tex, Vector3 c, Vector3 ax, Vector3 ay, Color col)
{
    rlSetTexture(tex.id);
    rlBegin(RL_QUADS);
    rlColor4ub(col.r, col.g, col.b, col.a);
    rlNormal3f(0, 1, 0);
    rlTexCoord2f(0, 0); rlVertex3f(c.x - ax.x - ay.x, c.y - ax.y - ay.y, c.z - ax.z - ay.z);
    rlTexCoord2f(0, 1); rlVertex3f(c.x - ax.x + ay.x, c.y - ax.y + ay.y, c.z - ax.z + ay.z);
    rlTexCoord2f(1, 1); rlVertex3f(c.x + ax.x + ay.x, c.y + ax.y + ay.y, c.z + ax.z + ay.z);
    rlTexCoord2f(1, 0); rlVertex3f(c.x + ax.x - ay.x, c.y + ax.y - ay.y, c.z + ax.z - ay.z);
    rlEnd();
    rlSetTexture(0);
}

static void Billboard(Camera3D cam, Texture2D tex, Vector3 p, float size, Color col)
{
    Vector3 f = v3norm(v3sub(cam.target, cam.position));
    Vector3 r = v3norm(v3cross(f, cam.up));
    Vector3 u = v3cross(r, f);
    Quad(tex, p, v3scale(r, size * 0.5f), v3scale(u, size * 0.5f), col);
}

static void DrawLeaves(const World *w, Camera3D cam, float amount, int petals)
{
    const TreeState *t = &w->tree;
    Vector3 c;
    float R, top;
    if (t->blobCount > 0 && !petals) {
        c = v3(t->canopyBound.x, t->canopyBound.y, t->canopyBound.z);
        R = t->canopyBound.w * 0.9f;
        top = c.y + R * 0.5f;
    } else {
        c = cam.target;
        R = 6.0f;
        top = c.y + 4.0f;
    }
    float ground = terrainHeight(c.x, c.z) - 0.5f;
    float height = top - ground;
    int n = (int)(amount * (petals ? 120 : fminf(60.0f + R * 30.0f, 260.0f)));
    Vector3 col = petals ? Lin(v3(0.98f, 0.8f, 0.85f)) : v3lerp(Lin(w->leafCol), Lin(w->leafCol2), 0.5f);
    float gain;
    Vector3 L = Light(w, 0.45f);
    for (int i = 0; i < n; i++) {
        float fi = (float)i;
        float speed = 0.45f + 0.35f * h1(fi * 1.3f);
        float ph = h1(fi * 7.1f);
        float cyc = w->anim * speed / height + ph;
        float k = cyc - floorf(cyc);
        float y = top - k * height;
        float ang = h1(fi * 3.3f) * TAU_F, rad = sqrtf(h1(fi * 5.7f)) * R;
        float drift = (w->anim * 0.4f + ph * 10.0f) * w->wind;
        Vector3 p = v3(c.x + cosf(ang) * rad + sinf(w->anim * 1.3f + fi) * 0.4f + drift * 0.6f * fmodf(k * 3.0f, 3.0f),
                       y, c.z + sinf(ang) * rad + cosf(w->anim * 1.1f + fi) * 0.4f);
        float g = terrainHeight(p.x, p.z);
        if (p.y < g + 0.02f) continue;
        Vector3 tint = v3lerp(col, Lin(petals ? v3(1, 0.9f, 0.92f) : w->leafCol2), h1(fi * 9.1f));
        Color cc = Tint(v3(tint.x * L.x, tint.y * L.y, tint.z * L.z), 0.95f, &gain);
        SetGain(gain);
        float spin = w->anim * (2.0f + 3.0f * h1(fi * 4.4f)) + fi;
        float tilt = w->anim * (1.5f + 2.0f * h1(fi * 6.6f)) + fi * 0.7f;
        float sz = petals ? 0.012f : (0.025f + 0.015f * h1(fi * 8.8f));
        Vector3 ax = v3(cosf(spin) * sz, sinf(tilt) * sz * 0.6f, sinf(spin) * sz);
        Vector3 ay = v3(-sinf(spin) * cosf(tilt) * sz * 1.4f, cosf(tilt) * sz * 1.4f * 0.7f, cosf(spin) * cosf(tilt) * sz * 1.4f);
        Quad(petals ? gPetal : gLeaf, p, ax, ay, cc);
    }
}

static void DrawSnow(const World *w, Camera3D cam, float amount)
{
    int n = (int)(amount * 1800);
    const float S = 26.0f, H = 14.0f;
    Vector3 c = cam.position;
    float gain;
    Vector3 L = Light(w, 0.25f);
    Color cc = Tint(v3(0.9f * L.x, 0.92f * L.y, 0.97f * L.z), 0.85f, &gain);
    SetGain(gain);
    for (int i = 0; i < n; i++) {
        float fi = (float)i;
        float speed = 0.7f + 0.5f * h1(fi * 2.1f);
        float y = wrap(h1(fi * 3.7f) * H - w->anim * speed, c.y, H);
        float x = wrap(h1(fi * 5.3f) * S + sinf(w->anim * 0.9f + fi) * 0.3f + w->anim * 0.4f * w->wind, c.x, S);
        float z = wrap(h1(fi * 7.9f) * S + cosf(w->anim * 0.7f + fi) * 0.3f, c.z, S);
        if (y < terrainHeight(x, z)) continue;
        Billboard(cam, gDot, v3(x, y, z), 0.035f + 0.03f * h1(fi * 1.7f), cc);
    }
}

static void DrawDust(const World *w, Camera3D cam, float amount)
{
    int n = (int)(amount * 260);
    const float S = 7.0f;
    Vector3 c = v3lerp(cam.position, cam.target, 0.5f);
    float gain;
    Vector3 L = v3scale(w->light.sunCol, 0.35f * smooth(0.0f, 0.15f, w->light.sunDir.y));
    for (int i = 0; i < n; i++) {
        float fi = (float)i;
        float x = wrap(h1(fi * 1.9f) * S + sinf(w->anim * 0.21f + fi) * 0.5f + w->anim * 0.05f, c.x, S);
        float y = wrap(h1(fi * 2.9f) * S * 0.6f + sinf(w->anim * 0.17f + fi * 1.3f) * 0.4f, c.y, S * 0.6f);
        float z = wrap(h1(fi * 4.9f) * S + cosf(w->anim * 0.19f + fi) * 0.5f, c.z, S);
        float tw = 0.5f + 0.5f * sinf(w->anim * 2.0f + fi * 3.0f);
        Color cc = Tint(v3scale(L, 0.4f + 0.6f * tw), 0.6f, &gain);
        SetGain(gain);
        Billboard(cam, gDot, v3(x, y, z), 0.012f, cc);
    }
}

static void DrawSeeds(const World *w, Camera3D cam, float amount)
{
    int n = (int)(amount * 90);
    const float S = 14.0f;
    Vector3 c = v3lerp(cam.position, cam.target, 0.6f);
    float gain;
    Vector3 L = Light(w, 0.8f);
    for (int i = 0; i < n; i++) {
        float fi = (float)i;
        float x = wrap(h1(fi * 1.1f) * S + w->anim * (0.35f + 0.2f * h1(fi)) + sinf(w->anim * 0.5f + fi) * 0.6f, c.x, S);
        float y = wrap(h1(fi * 2.3f) * S * 0.5f + w->anim * 0.08f + sinf(w->anim * 0.7f + fi) * 0.3f, c.y + 1.0f, S * 0.5f);
        float z = wrap(h1(fi * 3.1f) * S + cosf(w->anim * 0.4f + fi) * 0.6f, c.z, S);
        if (y < terrainHeight(x, z) + 0.2f) continue;
        Vector3 p = v3(x, y, z);
        float back = powf(fmaxf(v3dot(v3norm(v3sub(p, cam.position)), w->light.sunDir), 0.0f), 4.0f);
        Color cc = Tint(v3scale(L, 0.6f + 2.5f * back), 0.9f, &gain);
        SetGain(gain);
        Billboard(cam, gSeed, p, 0.05f, cc);
    }
}

static void DrawFireflies(const World *w, Camera3D cam, float amount)
{
    int n = (int)(amount * 70);
    Vector3 c = v3(1.0f, terrainHeight(1, 2), 2.0f);
    float gain;
    for (int i = 0; i < n; i++) {
        float fi = (float)i;
        float ang = h1(fi * 1.7f) * TAU_F + w->anim * 0.05f * (h1(fi) - 0.5f);
        float rad = 1.0f + 7.0f * sqrtf(h1(fi * 2.7f));
        float x = c.x + cosf(ang) * rad + sinf(w->anim * 0.4f + fi) * 0.6f;
        float z = c.z + sinf(ang) * rad + cosf(w->anim * 0.33f + fi * 2.0f) * 0.6f;
        float y = terrainHeight(x, z) + 0.3f + 1.2f * h1(fi * 3.9f) + 0.25f * sinf(w->anim * 0.8f + fi);
        float pulse = powf(0.5f + 0.5f * sinf(w->anim * (1.5f + h1(fi * 5.1f)) + fi * 4.0f), 3.0f);
        Color cc = Tint(v3(4.0f * pulse, 3.6f * pulse, 0.8f * pulse), 1.0f, &gain);
        SetGain(gain);
        Billboard(cam, gDot, v3(x, y, z), 0.06f, cc);
    }
}

/* spout of the watering can, mirroring the transform in scene.fs */
static Vector3 CanToWorld(const World *w, Vector3 l)
{
    float t = w->canTilt;
    /* undo q.yz = rot(t) * q.yz */
    float c = cosf(t), s = sinf(t);
    float y = c * l.y - s * l.z, z = s * l.y + c * l.z;
    Vector3 q = v3(l.x, y, z);
    /* undo q.xz = rot(-yaw) * q.xz */
    float yaw = w->can.w;
    float cy = cosf(yaw), sy = sinf(yaw);
    float x2 = cy * q.x + sy * q.z, z2 = -sy * q.x + cy * q.z;
    return v3(w->can.x + x2, w->can.y + q.y, w->can.z + z2);
}

static void DrawWater(const World *w, Camera3D cam, float amount)
{
    if (w->canShow < 0.5f) return;
    Vector3 tip = CanToWorld(w, v3(0, 0.205f, 0.215f));
    Vector3 base = CanToWorld(w, v3(0, 0.07f, 0.06f));
    Vector3 dir = v3norm(v3sub(tip, base));
    float gain;
    Vector3 L = Light(w, 0.5f);
    Color cc = Tint(v3(0.75f * L.x + 0.3f, 0.85f * L.y + 0.3f, 0.95f * L.z + 0.35f), 0.55f * amount, &gain);
    SetGain(gain);
    for (int i = 0; i < 90; i++) {
        float fi = (float)i;
        float age = fmodf(w->t * 2.2f + fi / 90.0f, 1.0f) * 0.42f;
        float spread = 0.06f;
        Vector3 v = v3add(v3scale(dir, 0.9f), v3((h1(fi) - 0.5f) * spread, (h1(fi * 2.0f) - 0.5f) * spread, (h1(fi * 3.0f) - 0.5f) * spread));
        Vector3 p = v3add(tip, v3scale(v, age));
        p.y -= 4.9f * age * age;
        if (p.y < terrainHeight(p.x, p.z)) continue;
        Billboard(cam, gDot, p, 0.012f, cc);
    }
}

static void DrawBirds(const World *w, Camera3D cam, float amount)
{
    if (w->light.night > 0.5f) return;
    Vector3 col = v3scale(w->light.amb, 0.25f);
    float gain;
    Color cc = Tint(v3add(col, v3(0.005f, 0.005f, 0.006f)), 0.85f, &gain);
    SetGain(gain);
    rlSetTexture(0);
    for (int i = 0; i < 7; i++) {
        float fi = (float)i;
        float t = w->t * 0.11f + fi * 0.031f;
        float a = t * 1.3f + 1.0f;
        Vector3 c = v3(cosf(a) * 60.0f + fi * 2.5f, 32.0f + fi * 1.2f + sinf(t * 3.0f + fi) * 2.0f, sinf(a) * 45.0f - 20.0f + fi * 1.8f);
        Vector3 f = v3norm(v3(-sinf(a), 0, cosf(a)));
        Vector3 r = v3cross(f, v3(0, 1, 0));
        float flap = sinf(w->t * 9.0f + fi * 1.7f) * 0.5f;
        float span = 0.55f;
        Vector3 tipL = v3add(c, v3add(v3scale(r, -span), v3(0, flap * span, 0)));
        Vector3 tipR = v3add(c, v3add(v3scale(r, span), v3(0, flap * span, 0)));
        Vector3 back = v3add(c, v3scale(f, -0.25f));
        rlBegin(RL_TRIANGLES);
        rlColor4ub(cc.r, cc.g, cc.b, cc.a);
        rlVertex3f(c.x, c.y, c.z); rlVertex3f(tipL.x, tipL.y, tipL.z); rlVertex3f(back.x, back.y, back.z);
        rlVertex3f(c.x, c.y, c.z); rlVertex3f(back.x, back.y, back.z); rlVertex3f(tipR.x, tipR.y, tipR.z);
        rlEnd();
    }
    (void)amount;
    (void)cam;
}

void ParticlesDraw(const World *w, Camera3D cam, int pass)
{
    rlDrawRenderBatchActive();
    rlDisableDepthMask();
    rlDisableBackfaceCulling();
    BeginShaderMode(gShader);
    if (pass == 0) {
        if (w->pLeaves > 0.0f) DrawLeaves(w, cam, w->pLeaves, 0);
        if (w->pPetals > 0.0f) DrawLeaves(w, cam, w->pPetals, 1);
        if (w->pSnow > 0.0f) DrawSnow(w, cam, w->pSnow);
        if (w->pWater > 0.01f) DrawWater(w, cam, w->pWater);
        if (w->pBirds > 0.0f) DrawBirds(w, cam, w->pBirds);
        if (w->pSeeds > 0.0f) DrawSeeds(w, cam, w->pSeeds);
    } else {
        if (w->pDust > 0.0f) DrawDust(w, cam, w->pDust);
        if (w->pFireflies > 0.0f) DrawFireflies(w, cam, w->pFireflies);
    }
    rlDrawRenderBatchActive();
    EndShaderMode();
    rlEnableBackfaceCulling();
    rlEnableDepthMask();
}
