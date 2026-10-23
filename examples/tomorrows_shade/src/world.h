#ifndef WORLD_H
#define WORLD_H

#include "mathx.h"

#define MAX_SEGS 140
#define MAX_GROUPS 28
#define MAX_BLOBS 100
#define MAX_CHARS 4
#define NJ 24 /* joints per character, last 3 slots hold the head frame */
#define PATH_PTS 16
#define MAX_PLANTED 24

enum {
    J_PELVIS, J_BELLY, J_CHEST, J_NECK, J_HEAD,
    J_LSHO, J_LELB, J_LWRI, J_LTIP,
    J_RSHO, J_RELB, J_RWRI, J_RTIP,
    J_LHIP, J_LKNE, J_LANK, J_LTOE,
    J_RHIP, J_RKNE, J_RANK, J_RTOE,
    J_HEADR, J_HEADU, J_HEADF
};

typedef enum { CH_GRANDMA = 0, CH_GIRL = 1, CH_WOMAN = 2, CH_BOY = 3 } CharType;

typedef struct {
    Vector3 pos, target;
    float fov;       /* vertical, degrees */
    float roll;      /* radians */
    float focus;     /* focus distance for depth of field (0 = auto) */
    float aperture;  /* 0 = no depth of field */
    Vector3 shake;   /* handheld amplitude */
} CamState;

typedef struct {
    Vector3 sunDir, sunCol, skyZen, skyHor, fogCol, amb, moonDir;
    float fogDen, night, cloud, overcast;
} Lighting;

typedef struct {
    int active;
    CharType type;
    Vector3 j[NJ];
    float blink, smile, mouthOpen, brow;
    Vector3 gaze;   /* world space eye direction */
    float scarf;    /* 1 = wearing the red scarf */
    float cane;     /* 1 = holding a cane in right hand */
    float sit;      /* 0..1, for skirt draping */
    float tear;     /* glistening eyes */
    Vector3 palmL, palmR; /* palm normals */
    Vector3 caneTip;
} CharState;

typedef struct {
    float growth;
    int segCount;
    Vector4 segA[MAX_SEGS], segB[MAX_SEGS]; /* xyz + radius */
    int groupCount;
    Vector4 groupBound[MAX_GROUPS];         /* sphere */
    float groupRange[MAX_GROUPS * 2];       /* start, end (exclusive) */
    int blobCount;
    Vector4 blob[MAX_BLOBS];                /* foliage spheres */
    int blobGroupCount;
    Vector4 blobGroupBound[MAX_GROUPS];
    float blobGroupRange[MAX_GROUPS * 2];
    Vector4 canopyBound, woodBound;
    Vector4 hullC, hullR;                   /* canopy dome ellipsoid; hullR.w = size scale */
    Vector4 canopyExt;                      /* canopy half extents around canopyBound */
    float sprout;                           /* 0..1 sprout stage visibility */
} TreeState;

typedef struct {
    float t;              /* story time, seconds */
    float anim;           /* animation clock for wind etc (can run fast in timelapse) */
    CamState cam;
    Lighting light;
    float hour, season;
    float snow, dry, lush, wet;
    Vector3 leafCol, leafCol2;
    float leafAmt, wind;
    TreeState tree;
    int charCount;
    CharState ch[MAX_CHARS];
    Vector4 bench;        /* xyz, yaw; w > 50 = hidden */
    float benchAge;
    Vector4 cane;         /* leaning cane: xyz base, yaw; w > 50 hidden */
    Vector4 acorn;        /* xyz, scale (0 hidden) */
    Vector4 can;          /* watering can: xyz, yaw */
    float canTilt, canShow;
    Vector4 trowel;       /* xyz, yaw; w>50 hidden */
    Vector4 mound;        /* soil mound xyz, radius (0 none) */
    Vector4 mound2;
    Vector4 swing;        /* anchor xyz, angle */
    float swingShow;
    Vector4 fence;        /* small sapling fence: xyz, alpha */
    Vector4 scarfOnBench; /* xyz, w>0.5 shown */
    float forest;         /* distant planted trees, 0..1 */
    int plantedCount;
    Vector4 planted[MAX_PLANTED]; /* young trees planted on the far hills: xyz, size */
    Vector4 sprout2;      /* the new acorn's sprout at the end: xyz, growth */
    float pLeaves, pSnow, pFireflies, pSeeds, pDust, pWater, pBirds, pPetals;
    float exposure;
    Vector3 grade;        /* color balance multiplier */
    float saturation, contrast, vignette, warmth;
    float godrays, bloom;
} World;

typedef struct {
    World a, b;
    float mix;            /* 0 = only a, 1 = only b (dissolve) */
    float fade;           /* 0 = visible, 1 = black */
    float whiteFade;
    const char *caption;
    const char *speaker;
    float captionAlpha;
    const char *title;
    const char *subtitle;
    float titleAlpha;
    int titleStyle;
    float letterbox;
} Frame;

extern Vector3 gPath[PATH_PTS];
void PathInit(void);
Vector3 PathPoint(float s);     /* s in [0,1] from the valley to the hilltop */
float PathLength(void);

#endif
