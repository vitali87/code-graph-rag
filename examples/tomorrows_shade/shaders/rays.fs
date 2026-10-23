#version 330
// Crepuscular rays: radial blur of the visible sky towards the sun.
out vec4 fragColor;
uniform sampler2D uSrc;
uniform vec2 uRes;
uniform vec2 uSun;
uniform float uStrength;
uniform vec3 uGrade;

float hash(vec2 p) { vec3 p3 = fract(vec3(p.xyx) * 0.1031); p3 += dot(p3, p3.yzx + 33.33); return fract((p3.x + p3.y) * p3.z); }

void main()
{
    if (uStrength <= 0.0) { fragColor = vec4(0.0, 0.0, 0.0, 1.0); return; }
    vec2 uv = gl_FragCoord.xy / uRes;
    float aspect = uRes.x / uRes.y;
    vec2 d = uSun - uv;
    const int N = 72;
    vec2 stepv = d / float(N) * 0.98;
    float jitter = hash(gl_FragCoord.xy);
    vec2 p = uv + stepv * jitter;
    float decay = 1.0, acc = 0.0, wsum = 0.0;
    for (int i = 0; i < N; i++) {
        vec4 s = textureLod(uSrc, p, 0.0);
        float sky = smoothstep(4000.0, 4900.0, s.a);
        float b = sky * min(dot(s.rgb, vec3(0.3, 0.5, 0.2)), 6.0);
        acc += b * decay;
        wsum += decay;
        decay *= 0.975;
        p += stepv;
    }
    acc /= wsum;
    float fall = exp(-length(d * vec2(aspect, 1.0)) * 1.6);
    fragColor = vec4(uGrade * acc * uStrength * fall * 0.6, 1.0);
}
