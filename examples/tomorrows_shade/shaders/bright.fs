#version 330
// Bloom bright pass: 4x4 box downsample with a soft threshold.
out vec4 fragColor;
uniform sampler2D uSrc;
uniform vec2 uRes;
uniform vec2 uSrcRes;

void main()
{
    vec2 uv = gl_FragCoord.xy / uRes;
    vec2 t = 1.0 / uSrcRes;
    vec3 c = texture(uSrc, uv + vec2(-t.x, -t.y)).rgb + texture(uSrc, uv + vec2(t.x, -t.y)).rgb +
             texture(uSrc, uv + vec2(-t.x, t.y)).rgb + texture(uSrc, uv + vec2(t.x, t.y)).rgb;
    c *= 0.25;
    c = min(c, vec3(60.0));
    float l = max(c.r, max(c.g, c.b));
    float knee = 0.6;
    float soft = clamp(l - 0.9 + knee, 0.0, 2.0 * knee);
    soft = soft * soft / (4.0 * knee + 1e-4);
    float contrib = max(soft, l - 0.9) / max(l, 1e-4);
    fragColor = vec4(c * contrib + c * 0.015, 1.0);
}
