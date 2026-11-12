/*{
    "CREDIT": "MadMapper MCP",
    "DESCRIPTION": "Frattali bianchi e neri a circuito (chip, piste, pin) con onde di luce che corrono sulle piste, a tempo col BPM.",
    "VSN": "1.0",
    "TAGS": "pattern",
    "INPUTS": [
        { "LABEL": "Speed", "NAME": "speed", "TYPE": "float", "MIN": 0.0, "MAX": 4.0, "DEFAULT": 1.0 },
        { "LABEL": "Reverse", "NAME": "reverse", "TYPE": "bool", "DEFAULT": 0, "FLAGS": "button" },
        { "LABEL": "Scale", "NAME": "scale", "TYPE": "float", "MIN": 1.0, "MAX": 16.0, "DEFAULT": 4.0 },
        { "LABEL": "Depth", "NAME": "depth", "TYPE": "float", "MIN": 1.0, "MAX": 5.0, "DEFAULT": 4.0 },
        { "LABEL": "Wave Freq", "NAME": "waveFreq", "TYPE": "float", "MIN": 0.25, "MAX": 8.0, "DEFAULT": 2.0 },
        { "LABEL": "Glow", "NAME": "glow", "TYPE": "float", "MIN": 0.0, "MAX": 1.0, "DEFAULT": 0.12 },
        { "LABEL": "Aspect", "NAME": "aspect", "TYPE": "float", "MIN": 0.25, "MAX": 4.0, "DEFAULT": 1.7778 },
        { "LABEL": "Seed", "NAME": "seed", "TYPE": "float", "MIN": 0.0, "MAX": 100.0, "DEFAULT": 0.0 },
        { "LABEL": "Invert", "NAME": "invert", "TYPE": "bool", "DEFAULT": 0, "FLAGS": "button" }
    ],
    "GENERATORS": [
        { "NAME": "animation_time", "TYPE": "time_base", "PARAMS": {"speed": "speed", "reverse": "reverse", "link_speed_to_global_bpm": true} }
    ]
}*/

// Frattale: ogni cella si divide in 4 finche' l'hash non la "ferma" (quadtree
// casuale), come i blocchi di un circuito stampato. Ogni blocco fermato e' un
// chip: bordo, pin, pista (dritta, a croce o a 45 gradi) e a volte un core
// pieno. Sopra corrono due onde di luce (diagonale + anelli dal centro), col
// tempo agganciato al BPM globale di MadMapper.

float hashFC(vec2 p)
{
    p = fract(p * vec2(123.34, 456.21) + seed * 0.137);
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}

vec4 materialColorForPixel(vec2 texCoord)
{
    vec2 uv = texCoord;
    vec2 p = vec2(uv.x * aspect, uv.y) * scale;
    float t = animation_time;

    vec2 cell = floor(p);
    vec2 f = fract(p);
    float h = hashFC(cell);
    for (int i = 1; i < 5; i++)
    {
        if (float(i) >= depth || h > 0.55) break;
        p *= 2.0;
        cell = floor(p);
        f = fract(p);
        h = hashFC(cell + float(i) * 17.0);
    }

    vec2 d = min(f, 1.0 - f);
    float bordo = 1.0 - smoothstep(0.035, 0.06, min(d.x, d.y));

    float pinX = step(0.5, fract(f.x * 6.0)) * (1.0 - step(0.12, d.y));
    float pinY = step(0.5, fract(f.y * 6.0)) * (1.0 - step(0.12, d.x));
    float pin = max(pinX, pinY) * step(0.62, h);

    vec2 g = f - 0.5;
    float dist = min(abs(g.x), abs(g.y));
    if (h < 0.15) dist = abs(g.x + g.y) * 0.7071;
    else if (h < 0.3) dist = abs(g.x - g.y) * 0.7071;
    float pista = 1.0 - smoothstep(0.02, 0.045, dist);

    float core = step(0.8, h) * step(0.28, min(d.x, d.y));

    float ink = max(max(bordo, pin * 0.85), max(pista, core * 0.9));

    vec2 q = uv - 0.5;
    float onda = 0.5 + 0.5 * sin((uv.x + uv.y) * waveFreq * 6.2832 - t * 3.0 + h * 1.5);
    float anello = 0.5 + 0.5 * sin(length(q) * waveFreq * 12.566 - t * 4.0);
    float luce = pow(max(onda, anello * 0.85), 3.0);

    float c = ink * (glow + (1.0 - glow) * luce);
    if (invert) c = 1.0 - c;
    return vec4(vec3(c), 1.0);
}
