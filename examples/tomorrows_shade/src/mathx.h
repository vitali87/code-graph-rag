#ifndef MATHX_H
#define MATHX_H

#include "raylib.h"
#include "raymath.h"

#define PI_F 3.14159265358979f
#define TAU_F 6.28318530717959f

float clampf(float x, float a, float b);
float saturate(float x);
float mixf(float a, float b, float t);
float smooth(float e0, float e1, float x);
float ease(float t);                 /* smoothstep on [0,1] */
float easeInOut(float t);            /* quintic smootherstep */
float easeOut(float t);
float easeIn(float t);
float window(float t, float a, float b, float fadeIn, float fadeOut);
float hash1(float n);
float noise1(float x);
float fbm1(float x);

Vector3 v3(float x, float y, float z);
Vector3 v3lerp(Vector3 a, Vector3 b, float t);
Vector3 v3scale(Vector3 a, float s);
Vector3 v3add(Vector3 a, Vector3 b);
Vector3 v3sub(Vector3 a, Vector3 b);
Vector3 v3norm(Vector3 a);
Vector3 v3cross(Vector3 a, Vector3 b);
float v3dot(Vector3 a, Vector3 b);
float v3len(Vector3 a);
Vector3 v3rotY(Vector3 a, float ang);
Vector3 v3mix3(Vector3 a, Vector3 b, Vector3 c, float t); /* a->b->c over t in [0,2] */
Vector3 catmull(Vector3 p0, Vector3 p1, Vector3 p2, Vector3 p3, float t);
Vector3 rgb(int hex);

const unsigned char *NoiseTable(void);
float noise2(float x, float y);

/* Terrain height, mirrored exactly in the scene shader (terrainMid). */
float terrainHeight(float x, float z);
Vector3 onGround(float x, float z, float lift);

#endif
