#ifndef CHARACTER_H
#define CHARACTER_H

#include "world.h"

typedef struct {
    CharType type;
    float pelvisH, spine, neck, headOff;
    float shoulderW, hipW;
    float upperArm, foreArm, handLen;
    float thigh, shin, ankleH, footLen;
} Body;

/* High level pose: where the body is, where it looks, and where the hands and
 * feet should be. Arms and legs are solved with two-bone IK. */
typedef struct {
    Vector3 root;           /* pelvis */
    float yaw;              /* facing, forward = (sin yaw, 0, cos yaw) */
    float lean, side, twist;
    Vector3 look;
    float lookW;
    float headPitch, headYaw, headRoll;
    Vector3 handL, handR;
    Vector3 handDirL, handDirR;
    Vector3 palmL, palmR;
    Vector3 elbowL, elbowR; /* pole hints (world directions) */
    Vector3 footL, footR;
    Vector3 toeL, toeR;     /* foot directions */
    Vector3 kneeL, kneeR;   /* pole hints */
} Pose;

Body BodyFor(CharType t);
Vector3 Fwd(float yaw);
Vector3 Right(float yaw);

void PoseSolve(const Body *b, const Pose *p, CharState *out);
Pose PoseLerp(const Pose *a, const Pose *b, float t);

/* building blocks */
Pose PoseStand(const Body *b, Vector3 ground, float yaw, float t);
Pose PoseWalk(const Body *b, float s, float t, float stepLen, float lateral);
Pose PoseSit(const Body *b, Vector3 benchPos, float benchYaw, float offset, float t);
Pose PoseKneel(const Body *b, Vector3 ground, float yaw, float t);
Pose PoseSquat(const Body *b, Vector3 ground, float yaw, float t);

void ArmsRelaxed(const Body *b, Pose *p, float t);
void HandReach(const Body *b, Pose *p, int right, Vector3 target, Vector3 palm, float w);

/* faces */
float Blink(float t, float seed);
float Talk(float t, float seed);

/* path helpers: position and heading on the footpath */
Vector3 PathPos(float s);       /* s = metres from the bottom of the path */
float PathYaw(float s);

#endif
