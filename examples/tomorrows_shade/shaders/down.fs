#version 330
out vec4 fragColor;
uniform sampler2D uSrc;
uniform vec2 uRes;
uniform vec2 uSrcRes;

void main()
{
    vec2 uv = gl_FragCoord.xy / uRes;
    vec2 t = 1.0 / uSrcRes;
    vec3 c = texture(uSrc, uv).rgb * 4.0;
    c += texture(uSrc, uv + vec2(-t.x, -t.y)).rgb + texture(uSrc, uv + vec2(t.x, -t.y)).rgb;
    c += texture(uSrc, uv + vec2(-t.x, t.y)).rgb + texture(uSrc, uv + vec2(t.x, t.y)).rgb;
    fragColor = vec4(c / 8.0, 1.0);
}
