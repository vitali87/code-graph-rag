#include "grass.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "rlgl.h"

#define MAX_BLADES 26000
#define VPB 15 /* vertices per blade: 5 triangles */
#define CELL 0.045f
#define RMAX 13.0f

static Mesh gMesh;
static Shader gShader;
static int gLocTime, gLocWind, gLocGround, gLocRes, gLocSun, gLocSunCol, gLocAmb, gLocCam, gLocSnow, gLocFog, gLocDry;
static float *gPos, *gUV, *gNrm, *gTan;
static unsigned char *gCol;

static unsigned hashU(int x, int z)
{
    unsigned h = (unsigned)x * 374761393u + (unsigned)z * 668265263u;
    h = (h ^ (h >> 13)) * 1274126177u;
    return h ^ (h >> 16);
}
static float hashF(unsigned h, int k) { return (float)((h >> (k * 5)) & 1023) / 1023.0f; }

void GrassInit(const char *baseDir)
{
    char vs[1024], fs[1024];
    snprintf(vs, sizeof(vs), "%sshaders/grass.vs", baseDir);
    snprintf(fs, sizeof(fs), "%sshaders/grass.fs", baseDir);
    gShader = LoadShader(vs, fs);
    gLocTime = GetShaderLocation(gShader, "uTime");
    gLocWind = GetShaderLocation(gShader, "uWind");
    gLocGround = GetShaderLocation(gShader, "uGround");
    gLocRes = GetShaderLocation(gShader, "uRes");
    gLocSun = GetShaderLocation(gShader, "uSunDir");
    gLocSunCol = GetShaderLocation(gShader, "uSunCol");
    gLocAmb = GetShaderLocation(gShader, "uAmb");
    gLocCam = GetShaderLocation(gShader, "uCamPos");
    gLocSnow = GetShaderLocation(gShader, "uSnow");
    gLocFog = GetShaderLocation(gShader, "uFogCol");
    gLocDry = GetShaderLocation(gShader, "uDry");

    memset(&gMesh, 0, sizeof(gMesh));
    int nv = MAX_BLADES * VPB;
    gMesh.vertexCount = nv;
    gMesh.triangleCount = nv / 3;
    gPos = gMesh.vertices = RL_CALLOC(nv * 3, sizeof(float));
    gUV = gMesh.texcoords = RL_CALLOC(nv * 2, sizeof(float));
    gNrm = gMesh.normals = RL_CALLOC(nv * 3, sizeof(float));
    gCol = gMesh.colors = RL_CALLOC(nv * 4, sizeof(unsigned char));
    gTan = gMesh.tangents = RL_CALLOC(nv * 4, sizeof(float));
    UploadMesh(&gMesh, true);
}

static float pathDist(float x, float z)
{
    float d = 1e9f;
    for (int i = 0; i < PATH_PTS - 1; i++) {
        float ax = gPath[i].x, az = gPath[i].z, bx = gPath[i + 1].x, bz = gPath[i + 1].z;
        float px = x - ax, pz = z - az, ux = bx - ax, uz = bz - az;
        float h = clampf((px * ux + pz * uz) / (ux * ux + uz * uz), 0.0f, 1.0f);
        float dx = px - ux * h, dz = pz - uz * h;
        float dd = dx * dx + dz * dz;
        if (dd < d) d = dd;
    }
    return sqrtf(d);
}

void GrassDraw(const World *w, Camera3D cam, Texture2D ground, int width, int height)
{
    Matrix view = MatrixLookAt(cam.position, cam.target, cam.up);
    Matrix proj = MatrixPerspective(cam.fovy * DEG2RAD, (double)width / height, rlGetCullDistanceNear(), rlGetCullDistanceFar());
    Matrix vp = MatrixMultiply(view, proj);
    Vector3 c = cam.position;
    float camGround = terrainHeight(c.x, c.z);
    if (c.y - camGround > 9.0f) return; /* too high above the grass to matter */

    float snowCut = 1.0f - 0.75f * w->snow;
    float heightScale = (0.58f + 0.14f * w->lush) * snowCut;
    float dryness = w->dry;
    int n = 0;
    int r = (int)(RMAX / CELL);
    int cx = (int)floorf(c.x / CELL), cz = (int)floorf(c.z / CELL);
    float treeR = w->tree.segCount > 0 ? w->tree.segA[0].w + 0.25f : 0.0f;
    float rootsR = w->tree.growth > 0.3f ? treeR + 1.4f * w->tree.growth : treeR;

    for (int iz = -r; iz <= r && n < MAX_BLADES; iz++) {
        for (int ix = -r; ix <= r && n < MAX_BLADES; ix++) {
            int gx = cx + ix, gz = cz + iz;
            unsigned h = hashU(gx, gz);
            float x = (gx + hashF(h, 0)) * CELL, z = (gz + hashF(h, 1)) * CELL;
            float dx = x - c.x, dz = z - c.z;
            float d = sqrtf(dx * dx + dz * dz);
            if (d > RMAX) continue;
            /* density falls with distance; blades get wider to compensate */
            float keep = d < 2.5f ? 1.0f : 2.5f * 2.5f / (d * d) * 1.6f;
            if (hashF(h, 2) > keep) continue;
            float y = terrainHeight(x, z);
            /* frustum test on the blade middle */
            Vector3 mid = { x, y + 0.15f, z };
            Vector4 cl = { 0 };
            cl.x = vp.m0 * mid.x + vp.m4 * mid.y + vp.m8 * mid.z + vp.m12;
            cl.y = vp.m1 * mid.x + vp.m5 * mid.y + vp.m9 * mid.z + vp.m13;
            cl.w = vp.m3 * mid.x + vp.m7 * mid.y + vp.m11 * mid.z + vp.m15;
            if (cl.w < 0.05f) continue;
            if (fabsf(cl.x) > cl.w * 1.08f || fabsf(cl.y) > cl.w * 1.15f) continue;
            if (y < -2.0f) continue;
            float rt = sqrtf(x * x + z * z);
            if (rt < rootsR) continue;
            if (w->mound.w > 0.0f && hypotf(x - w->mound.x, z - w->mound.z) < w->mound.w * 1.3f) continue;
            if (w->mound2.w > 0.0f && hypotf(x - w->mound2.x, z - w->mound2.z) < w->mound2.w * 1.3f) continue;
            if ((w->tree.sprout > 0.0f || (w->tree.growth > 0.0f && w->tree.growth < 0.5f)) && rt < 0.55f) continue;
            float pd = pathDist(x, z);
            if (pd < 0.45f + 0.2f * hashF(h, 3)) continue;
            /* shorter, trampled grass around the hilltop where people walk and sit */
            float hs = heightScale * (pd < 0.9f ? 0.5f : 1.0f) * mixf(0.62f, 1.0f, smooth(5.0f, 14.0f, rt));

            float patch = noise2(x * 0.7f, z * 0.7f);
            float bh = (0.1f + 0.2f * hashF(h, 3)) * hs * (0.6f + 0.8f * patch);
            if (bh < 0.03f) continue;
            float bw = (0.007f + 0.006f * hashF(h, 4)) * (1.0f + d * 0.09f);
            float yaw = hashF(h, 5) * TAU_F;
            float lean = 0.15f + 0.5f * hashF(h, 6);
            float sx = cosf(yaw), sz = sinf(yaw);       /* blade width axis */
            float lx = -sz * lean, lz = sx * lean;     /* lean direction */
            Vector3 nrm = v3norm(v3(-sz, 0.35f, sx));
            float tint = 0.8f + 0.45f * hashF(h, 1);
            if (hashF(h, 6) > 0.82f) tint *= 0.7f;
            float hue = hashF(h, 0);
            unsigned char cr, cg, cb;
            {
                float rr = tint * (0.95f + 0.15f * hue * dryness);
                float gg = tint * (1.0f + 0.05f * (1.0f - hue));
                float bb = tint * (0.85f + 0.1f * hue);
                cr = (unsigned char)fminf(255.0f, rr * 160.0f);
                cg = (unsigned char)fminf(255.0f, gg * 160.0f);
                cb = (unsigned char)fminf(255.0f, bb * 160.0f);
            }
            float phase = hashF(h, 2) * TAU_F;
            /* rows: v = 0, 0.4, 0.75, tip */
            const float vs[4] = { 0.0f, 0.4f, 0.75f, 1.0f };
            const float ws[4] = { 1.0f, 0.85f, 0.55f, 0.0f };
            Vector3 L[4], R[4];
            for (int k = 0; k < 4; k++) {
                float v = vs[k];
                float bend = v * v;
                Vector3 cpos = { x + lx * bend * bh, y - 0.04f + v * bh * (1.0f - 0.25f * lean * bend), z + lz * bend * bh };
                float hw = bw * ws[k];
                L[k] = v3(cpos.x - sx * hw, cpos.y, cpos.z - sz * hw);
                R[k] = v3(cpos.x + sx * hw, cpos.y, cpos.z + sz * hw);
            }
            Vector3 tri[VPB];
            float tu[VPB], tv[VPB];
            int q = 0;
            for (int k = 0; k < 3; k++) {
                if (k < 2) {
                    tri[q] = L[k]; tu[q] = -1; tv[q++] = vs[k];
                    tri[q] = R[k]; tu[q] = 1; tv[q++] = vs[k];
                    tri[q] = R[k + 1]; tu[q] = 1; tv[q++] = vs[k + 1];
                    tri[q] = L[k]; tu[q] = -1; tv[q++] = vs[k];
                    tri[q] = R[k + 1]; tu[q] = 1; tv[q++] = vs[k + 1];
                    tri[q] = L[k + 1]; tu[q] = -1; tv[q++] = vs[k + 1];
                } else {
                    tri[q] = L[k]; tu[q] = -1; tv[q++] = vs[k];
                    tri[q] = R[k]; tu[q] = 1; tv[q++] = vs[k];
                    tri[q] = L[k + 1]; tu[q] = 0; tv[q++] = 1.0f;
                }
            }
            int base = n * VPB;
            for (int k = 0; k < q; k++) {
                int vi = base + k;
                gPos[vi * 3 + 0] = tri[k].x;
                gPos[vi * 3 + 1] = tri[k].y;
                gPos[vi * 3 + 2] = tri[k].z;
                gUV[vi * 2 + 0] = tu[k];
                gUV[vi * 2 + 1] = tv[k];
                gNrm[vi * 3 + 0] = nrm.x;
                gNrm[vi * 3 + 1] = nrm.y;
                gNrm[vi * 3 + 2] = nrm.z;
                gCol[vi * 4 + 0] = cr;
                gCol[vi * 4 + 1] = cg;
                gCol[vi * 4 + 2] = cb;
                gCol[vi * 4 + 3] = (unsigned char)(hashF(h, 4) * 255.0f);
                gTan[vi * 4 + 0] = x;
                gTan[vi * 4 + 1] = y;
                gTan[vi * 4 + 2] = z;
                gTan[vi * 4 + 3] = phase;
            }
            for (int k = q; k < VPB; k++) {
                int vi = base + k;
                gPos[vi * 3 + 0] = gPos[vi * 3 + 1] = gPos[vi * 3 + 2] = 0;
            }
            n++;
        }
    }
    if (n == 0) return;
    int nv = n * VPB;
    UpdateMeshBuffer(gMesh, 0, gPos, nv * 3 * sizeof(float), 0);
    UpdateMeshBuffer(gMesh, 1, gUV, nv * 2 * sizeof(float), 0);
    UpdateMeshBuffer(gMesh, 2, gNrm, nv * 3 * sizeof(float), 0);
    UpdateMeshBuffer(gMesh, 3, gCol, nv * 4, 0);
    UpdateMeshBuffer(gMesh, 4, gTan, nv * 4 * sizeof(float), 0);
    int saved = gMesh.vertexCount;
    gMesh.vertexCount = nv;

    float res[2] = { (float)width, (float)height };
    SetShaderValue(gShader, gLocTime, &w->anim, SHADER_UNIFORM_FLOAT);
    SetShaderValue(gShader, gLocWind, &w->wind, SHADER_UNIFORM_FLOAT);
    SetShaderValue(gShader, gLocRes, res, SHADER_UNIFORM_VEC2);
    SetShaderValue(gShader, gLocSun, &w->light.sunDir, SHADER_UNIFORM_VEC3);
    SetShaderValue(gShader, gLocSunCol, &w->light.sunCol, SHADER_UNIFORM_VEC3);
    SetShaderValue(gShader, gLocAmb, &w->light.amb, SHADER_UNIFORM_VEC3);
    SetShaderValue(gShader, gLocCam, &cam.position, SHADER_UNIFORM_VEC3);
    SetShaderValue(gShader, gLocSnow, &w->snow, SHADER_UNIFORM_FLOAT);
    SetShaderValue(gShader, gLocFog, &w->light.fogCol, SHADER_UNIFORM_VEC3);
    SetShaderValue(gShader, gLocDry, &w->dry, SHADER_UNIFORM_FLOAT);
    Material mat = LoadMaterialDefault();
    mat.shader = gShader;
    mat.maps[MATERIAL_MAP_DIFFUSE].texture = ground;
    rlDisableBackfaceCulling();
    rlDisableColorBlend();
    DrawMesh(gMesh, mat, MatrixIdentity());
    rlEnableColorBlend();
    rlEnableBackfaceCulling();
    gMesh.vertexCount = saved;
    RL_FREE(mat.maps);
}
