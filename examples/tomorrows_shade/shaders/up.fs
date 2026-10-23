#version 330
// Tent upsample, added on top of the next finer bloom level.
out vec4 fragColor;
uniform sampler2D uSrc;
uniform vec2 uRes;
uniform vec2 uSrcRes;

void main()
{
    vec2 uv = gl_FragCoord.xy / uRes;
    vec2 t = 1.0 / uSrcRes;
    vec3 c = texture(uSrc, uv).rgb * 4.0;
    c += (texture(uSrc, uv + vec2(t.x, 0.0)).rgb + texture(uSrc, uv - vec2(t.x, 0.0)).rgb +
          texture(uSrc, uv + vec2(0.0, t.y)).rgb + texture(uSrc, uv - vec2(0.0, t.y)).rgb) * 2.0;
    c += texture(uSrc, uv + t).rgb + texture(uSrc, uv - t).rgb +
         texture(uSrc, uv + vec2(t.x, -t.y)).rgb + texture(uSrc, uv + vec2(-t.x, t.y)).rgb;
    fragColor = vec4(c / 16.0, 1.0);
}
