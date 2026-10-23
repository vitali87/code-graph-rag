#version 330
// Gather depth of field. Alpha of the source holds view-space depth.
out vec4 fragColor;
uniform sampler2D uSrc;
uniform vec2 uRes;
uniform float uFocus;
uniform float uAperture;

// uAperture is the blur radius in pixels for an object far behind the focus plane
float coc(float z) { return uAperture * clamp(abs(z - uFocus) / max(z, 0.01), 0.0, 1.0); }

void main()
{
    vec2 uv = gl_FragCoord.xy / uRes;
    vec2 px = 1.0 / uRes;
    vec4 c0 = texture(uSrc, uv);
    float r0 = coc(c0.a);
    float maxR = uAperture;
    vec3 acc = c0.rgb;
    float wsum = 1.0;
    const int N = 48;
    for (int i = 1; i < N; i++) {
        float fi = float(i);
        float r = sqrt(fi / float(N)) * maxR;
        float a = fi * 2.39996;
        vec2 off = vec2(cos(a), sin(a)) * r;
        vec4 s = texture(uSrc, uv + off * px);
        float rs = coc(s.a);
        float reach = s.a < c0.a ? rs : min(rs, r0);
        float w = smoothstep(r - 1.5, r + 0.5, reach);
        acc += s.rgb * w;
        wsum += w;
    }
    fragColor = vec4(acc / wsum, c0.a);
}
