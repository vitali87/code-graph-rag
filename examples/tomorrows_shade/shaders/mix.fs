#version 330
// Cross-dissolve between two rendered shots.
out vec4 fragColor;
uniform sampler2D uSrc;
uniform sampler2D uSrc2;
uniform float uMix;
uniform vec2 uRes;

void main()
{
    vec2 uv = gl_FragCoord.xy / uRes;
    vec4 a = texture(uSrc, uv);
    vec4 b = texture(uSrc2, uv);
    float m = uMix * uMix * (3.0 - 2.0 * uMix);
    fragColor = vec4(mix(a.rgb, b.rgb, m), m < 0.5 ? a.a : b.a);
}
