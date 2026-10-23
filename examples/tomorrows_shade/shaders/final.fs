#version 330
// Final composite: supersample resolve, bloom, rays, tone mapping, grading,
// vignette, chromatic fringe and film grain.
out vec4 fragColor;
uniform sampler2D uSrc;
uniform sampler2D uSrc2;
uniform sampler2D uSrc3;
uniform vec2 uRes;
uniform vec2 uSrcRes;
uniform float uScale;
uniform float uExposure;
uniform vec3 uGrade;
uniform float uSat;
uniform float uContrast;
uniform float uVignette;
uniform float uWarmth;
uniform float uFade;
uniform float uWhite;
uniform float uTime;
uniform float uBloomStr;

vec3 aces(vec3 x)
{
    const float a = 2.51, b = 0.03, c = 2.43, d = 0.59, e = 0.14;
    return clamp((x * (a * x + b)) / (x * (c * x + d) + e), 0.0, 1.0);
}

float hash(vec2 p) { vec3 p3 = fract(vec3(p.xyx) * 0.1031); p3 += dot(p3, p3.yzx + 33.33); return fract((p3.x + p3.y) * p3.z); }

void main()
{
    vec2 uv = gl_FragCoord.xy / uRes;
    vec2 tx = 1.0 / uSrcRes;
    float o = 0.25 * uScale;
    vec2 cd = uv - 0.5;
    vec2 ca = cd * 0.0035 * dot(cd, cd) * 4.0;
    vec3 col = vec3(0.0);
    for (int i = 0; i < 4; i++) {
        vec2 off = vec2((i & 1) == 0 ? -o : o, (i & 2) == 0 ? -o : o) * tx;
        col.r += texture(uSrc, uv + off - ca).r;
        col.g += texture(uSrc, uv + off).g;
        col.b += texture(uSrc, uv + off + ca).b;
    }
    col *= 0.25;
    vec3 bloom = texture(uSrc2, uv).rgb / 5.0;
    vec3 rays = texture(uSrc3, uv).rgb;
    col += bloom * uBloomStr + rays;
    col *= uExposure * uGrade;
    col *= vec3(1.0 + 0.08 * uWarmth, 1.0 + 0.01 * uWarmth, 1.0 - 0.1 * uWarmth);
    col = aces(col);
    col = pow(col, vec3(1.0 / 2.2));
    float l = dot(col, vec3(0.299, 0.587, 0.114));
    col = mix(vec3(l), col, uSat);
    // gentle S-curve around mid grey
    col = clamp(col, 0.0, 1.0);
    vec3 s = col * col * (3.0 - 2.0 * col);
    col = mix(col, s, uContrast);
    // vignette
    vec2 q = cd * vec2(1.0, 0.85);
    col *= 1.0 - uVignette * smoothstep(0.15, 0.75, length(q) * 1.15);
    // grain, stronger in the shadows
    float g = hash(gl_FragCoord.xy * 1.37 + vec2(uTime * 13.1, uTime * 7.3)) + hash(gl_FragCoord.xy * 0.71 + uTime * 3.7) - 1.0;
    col += g * 0.028 * (1.0 - 0.6 * l);
    col = mix(col, vec3(1.0, 0.98, 0.95), uWhite);
    col *= 1.0 - uFade;
    col += (hash(gl_FragCoord.xy + 0.5) - 0.5) / 255.0;
    fragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
