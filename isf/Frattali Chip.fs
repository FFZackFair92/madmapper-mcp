/*{
  "DESCRIPTION": "Frattali bianchi e neri a circuito (chip, piste, pin) con onde di luce che corrono sulle piste. Fatto per lo sfondo del palco.",
  "CREDIT": "MadMapper MCP",
  "ISFVSN": "2",
  "CATEGORIES": ["Generator"],
  "INPUTS": [
    {"NAME": "speed",    "TYPE": "float", "DEFAULT": 1.0,  "MIN": 0.0, "MAX": 4.0},
    {"NAME": "scale",    "TYPE": "float", "DEFAULT": 4.0,  "MIN": 1.0, "MAX": 16.0},
    {"NAME": "depth",    "TYPE": "float", "DEFAULT": 4.0,  "MIN": 1.0, "MAX": 5.0},
    {"NAME": "waveFreq", "TYPE": "float", "DEFAULT": 2.0,  "MIN": 0.25, "MAX": 8.0},
    {"NAME": "glow",     "TYPE": "float", "DEFAULT": 0.12, "MIN": 0.0, "MAX": 1.0},
    {"NAME": "seed",     "TYPE": "float", "DEFAULT": 0.0,  "MIN": 0.0, "MAX": 100.0},
    {"NAME": "invert",   "TYPE": "bool",  "DEFAULT": false}
  ]
}*/

// Frattale: ogni cella si divide in 4 finche' l'hash non la "ferma" (quadtree
// casuale), come i blocchi di un circuito stampato visti da vicino. Ogni blocco
// fermato diventa un chip: bordo, pin, pista (dritta, a croce o a 45 gradi) e
// a volte un core pieno. La luce non e' fissa: due onde (una diagonale, una ad
// anelli dal centro) corrono sopra, cosi' le piste si accendono a ondate.

float hash(vec2 p) {
    p = fract(p * vec2(123.34, 456.21) + seed * 0.137);
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}

void main() {
    vec2 uv = isf_FragNormCoord;
    vec2 p = vec2(uv.x * RENDERSIZE.x / RENDERSIZE.y, uv.y) * scale;
    float t = TIME * speed;

    vec2 cell = floor(p);
    vec2 f = fract(p);
    float h = hash(cell);
    for (int i = 1; i < 5; i++) {
        if (float(i) >= depth || h > 0.55) break;   // cella intera: un chip
        p *= 2.0;
        cell = floor(p);
        f = fract(p);
        h = hash(cell + float(i) * 17.0);
    }

    vec2 d = min(f, 1.0 - f);
    float bordo = 1.0 - smoothstep(0.035, 0.06, min(d.x, d.y));

    // pin lungo i lati, solo sui chip "grandi"
    float pinX = step(0.5, fract(f.x * 6.0)) * (1.0 - step(0.12, d.y));
    float pinY = step(0.5, fract(f.y * 6.0)) * (1.0 - step(0.12, d.x));
    float pin = max(pinX, pinY) * step(0.62, h);

    // pista: a croce, oppure diagonale in un verso o nell'altro
    vec2 g = f - 0.5;
    float dist = min(abs(g.x), abs(g.y));
    if (h < 0.15) dist = abs(g.x + g.y) * 0.7071;
    else if (h < 0.3) dist = abs(g.x - g.y) * 0.7071;
    float pista = 1.0 - smoothstep(0.02, 0.045, dist);

    // core pieno al centro di alcuni chip
    float core = step(0.8, h) * step(0.28, min(d.x, d.y));

    float ink = max(max(bordo, pin * 0.85), max(pista, core * 0.9));

    // onde di luce
    vec2 q = uv - 0.5;
    float onda = 0.5 + 0.5 * sin((uv.x + uv.y) * waveFreq * 6.2832 - t * 3.0 + h * 1.5);
    float anello = 0.5 + 0.5 * sin(length(q) * waveFreq * 12.566 - t * 4.0);
    float luce = pow(max(onda, anello * 0.85), 3.0);

    float c = ink * (glow + (1.0 - glow) * luce);
    if (invert) c = 1.0 - c;
    gl_FragColor = vec4(vec3(c), 1.0);
}
