#version 330
// Grass blades: wind sway in the vertex shader.
in vec3 vertexPosition;
in vec2 vertexTexCoord;
in vec3 vertexNormal;
in vec4 vertexColor;
in vec4 vertexTangent;

uniform mat4 mvp;
uniform float uTime;
uniform float uWind;

out vec2 vUV;
out vec4 vCol;
out vec4 vRootClip;
out vec3 vPos;
out vec3 vNrm;
out float vViewZ;

void main()
{
    vec3 p = vertexPosition;
    vec3 root = vertexTangent.xyz;
    float ph = vertexTangent.w;
    float h = max(p.y - root.y, 0.0);
    // rolling gusts travel across the meadow, plus each blade's own flutter
    float gust = 0.5 + 0.5 * sin(dot(root.xz, vec2(0.35, 0.18)) - uTime * 1.6);
    gust = gust * gust;
    float flutter = sin(uTime * 3.1 + ph) * 0.3 + sin(uTime * 5.3 + ph * 1.7) * 0.12;
    vec2 wdir = normalize(vec2(1.0, 0.4));
    float amt = (gust * 1.2 + flutter) * uWind;
    float k = h * h * 2.2;
    p.xz += wdir * amt * k * 0.55;
    p.y -= abs(amt) * k * 0.18;
    vUV = vertexTexCoord;
    vCol = vertexColor;
    vRootClip = mvp * vec4(root, 1.0);
    vPos = p;
    vNrm = vertexNormal;
    gl_Position = mvp * vec4(p, 1.0);
    vViewZ = gl_Position.w;
}
