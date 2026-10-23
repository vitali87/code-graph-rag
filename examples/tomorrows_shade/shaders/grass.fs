#version 330
// Grass blades take their light from the raymarched ground right beneath
// them, so shadows, ambient occlusion and fog match the terrain exactly.
in vec2 vUV;
in vec4 vCol;
in vec4 vRootClip;
in vec3 vPos;
in vec3 vNrm;
in float vViewZ;

uniform sampler2D texture0;   // lit ground image (alpha = view depth)
uniform vec3 uSunDir;
uniform vec3 uSunCol;
uniform vec3 uAmb;
uniform vec3 uCamPos;
uniform vec3 uFogCol;
uniform float uSnow;
uniform float uDry;

out vec4 fragColor;

void main()
{
    vec2 suv = vRootClip.xy / vRootClip.w * 0.5 + 0.5;
    vec4 g = texture(texture0, clamp(suv, vec2(0.001), vec2(0.999)));
    vec3 ground = g.rgb;
    // root hidden behind something else (a shoe, a skirt): fall back to a neutral grass tone
    if (abs(g.a - vRootClip.w) > max(0.1, 0.05 * vRootClip.w)) {
        vec3 alb = mix(vec3(0.03, 0.08, 0.012), vec3(0.2, 0.13, 0.04), uDry) * (vCol.r / 0.63);
        ground = alb * (uAmb * 0.8 + uSunCol * 0.3);
    }
    vec3 tint = vCol.rgb / 0.63;
    float v = vUV.y;
    vec3 col = ground * tint * mix(0.45, 1.45, v);
    // light shining through the blade when looking towards the sun
    vec3 vd = normalize(vPos - uCamPos);
    float back = pow(max(dot(vd, uSunDir), 0.0), 4.0);
    float lit = clamp(dot(ground, vec3(0.33)) / max(dot(uSunCol * 0.2 + uAmb, vec3(0.33)), 1e-3), 0.0, 1.5);
    col += ground * vec3(1.1, 1.25, 0.6) * back * v * v * 2.2 * lit;
    // a faint sheen along the blade edge
    col *= 0.9 + 0.2 * (1.0 - abs(vUV.x));
    col = mix(col, ground * 1.1 + vec3(0.02), uSnow * (1.0 - v) * 0.5);
    fragColor = vec4(max(col, vec3(0.0)), vViewZ);
}
