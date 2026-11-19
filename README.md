# MadMapper MCP

Server MCP che pilota MadMapper via **OSC** (scrittura) e **OSCQuery** (lettura
dell'albero completo dei parametri). Stesso stampo del Resolume MCP.

## 1. Attiva OSC Query in MadMapper

MadMapper → **Preferences → OSC / MIDI** → attiva **OSC Query**. Porta di default
**8010** (se è diversa, imposta `MADMAPPER_QUERY_PORT`). La porta OSC in ingresso
il server la chiede da solo a MadMapper (`HOST_INFO`); per forzarla
`MADMAPPER_OSC_PORT`.

Verifica: <http://127.0.0.1:8010> nel browser deve mostrare un JSON.

## 2. Registra il server

In `%APPDATA%\Claude\claude_desktop_config.json`, dentro `mcpServers`:

```json
"madmapper": {
  "command": "uv",
  "args": ["run", "--script", "C:\\percorso\\madmapper-mcp\\madmapper_mcp.py"],
  "env": { "MADMAPPER_HOST": "127.0.0.1", "MADMAPPER_QUERY_PORT": "8010" }
}
```

Riavvia Claude. Per MadMapper su un altro PC cambia `MADMAPPER_HOST`.

## 3. Prima volta con MadMapper vero

```powershell
python check_mcp.py
```

Salva l'albero in `albero_madmapper.json` e prova una scrittura con rilettura.

Verificato su MadMapper 5.0.7: gli angoli di una Quad sono
`/surfaces/<nome>/output/handles/0..3/x,y`, in pixel della **tela di uscita**
(default 1920x1080, origine in alto a sinistra, y in giù), in ordine basso-sx,
basso-dx, alto-dx, alto-sx. La tela è stirata a tutto schermo sul proiettore.
I booleani sono interi (`TYPE i`), i media stanno in `/medias`, le cue in
`/cues/Bank-N/cues/by_cell/col_X/row_Y`.

## Tool

**Lettura** — `get_state` · `find_parameters(query)`

**Scrittura** — `set_parameter(address, value)` (universale, rilegge il valore;
`*` per tutte le superfici, senza valore = trigger) · `osc_send` (grezzo)

**Live** — `master(level, video, dmx, laser)` · `blackout(on)` · `freeze(engine, output)`
· `set_bpm` · `tap` · `output_enable` · `surface_set(surface, param, value)`
· `assign_media(media, surfaces)` · `select_media` · `cue(col, row)` · `cue_scene(col)` · `cue_go(next|previous|N)`
· `output_view()` — cattura di quello che esce sul proiettore

**Automapping** — `calibrate()` → `automap()`; correzione a mano con `automap_points()`

**Scene e job** — `scene_save/recall/list` · `job_start(record|play|auto, file)` · `job_stop` · `job_status`

**Effetti a battute** — `fx_strobe` · `fx_chase` · `fx_mix(seed)` · `fx_media` · `fx_modulate`
· `fx_status` · `fx_stop_all`

**Dissolvenze e look** (aggiunti a novembre 2026, confrontando con
[jaxondoss/map-mapper-agent](https://github.com/jaxondoss/map-mapper-agent))
- `fade(targets, to, seconds, from_value, curve)` — dissolvenza a tempo di superfici,
  master o qualsiasi parametro numerico, anche più insieme; il valore di partenza si
  legge da OSCQuery. `curve`: `linear` o `smooth`, massimo 120 s
- `look_save(name, roots)` · `look_apply(name, seconds)` · `look_list()` — preset
  mirati dei parametri di un materiale/shader o di una superficie, richiamabili con
  un morph (salvati in `looks.json`)

**Media e superfici**
- `media_import(path)` — importa un file o uno shader nella lista media
- `media_speed(media, speed)` · `surface_visible(surfaces, visible, solo)`
- `show_check()` — controllo pre-show: master a 0, uscite spente, superfici
  invisibili, a opacità 0 o senza media, freeze attivo

**Superfici, luci e ascolto** (1.2, 19 novembre 2026)
- `surfaces_where(pattern, param, value)` — un parametro su tutte le superfici di un modello (`Led*`, `Quad-?`)
- `surface_info(surface)` — tutti i parametri di una superficie con tipo e range
- `surface_transform(surface, position, scale, rotation, move_by)` — sposta/scala/ruota; `move_by=[dx, dy]` sposta
  tutti gli handle insieme quando non c'è un parametro di posizione
- `fixtures(list|set, fixture, values)` — luci DMX di MadMapper per nome di parametro (`{"Dimmer": 1}`), anche a modello
- `media_transport(media, play|pause|restart|status, loop)` — trova da solo i comandi giusti del media
- `watch_parameters(query, seconds)` — ascolta i parametri: cosa si muove, min/max, quante volte
- `open_madmapper(project, wait_seconds)` — apre MadMapper (o un .mad) e aspetta OSCQuery (`MADMAPPER_EXE`)

## Progetti: aggiungere Quad e aprire un .mad

Via OSC MadMapper non crea superfici e non apre progetti. Il file `.mad` però
è un QDataStream Qt che `madfile.py` legge e riscrive identico byte per byte
(`uv run python madfile.py progetto.mad` lo verifica), quindi:

```
project_add_quads("C:/.../show.mad", ["Cassa-SX", "Cassa-DX"])
    clona la prima Quad del progetto, scrive show_mcp.mad (l'originale non si
    tocca) e lo apre
project_remove_quads("C:/.../show_mcp.mad", ["Quad-3"])
    toglie superfici (un file che e' gia' _mcp.mad viene aggiornato)
project_open("C:/.../show.mad")
    chiude MadMapper e lo riapre con quel file
output_fullscreen(True)
```

Il progetto va **salvato** prima (Ctrl+S). `project_open` fotografa lo stato
dal vivo (angoli, media, opacità) e lo rimette dopo la riapertura, così la
mappatura fatta via OSC non si perde; la copia resta in
`scenes/_prima_di_project_open.json`. Il tool ritorna subito: MadMapper ci mette
30-60 secondi, un job `riapri` aspetta e rimette tutto (`job_status()`).

Vincoli scoperti sul campo:
- MadMapper aperto con un file da riga di comando mentre è già in esecuzione
  **ignora** il file: va chiuso e riaperto.
- Lanciato da un processo figlio muore con lui; via WMI parte senza desktop e
  muore. Funziona come **attività pianificata** `MadMapperMCP` (indipendente da
  Claude), con le opzioni batteria attive, se no su un portatile a batteria
  resta "in coda".
- Ripartire subito dopo la chiusura lo manda in crash (0xC0000005): si aspetta
  che il processo sparisca, e il job lo rilancia fino a 3 volte.
- Le Quad nuove nascono sopra la prima: si posizionano con `automap` /
  `automap_points`.

## Automapping

```
calibrate()      spegne l'uscita di MadMapper, proietta scacchiera / bianco / nero,
                 scatta con la webcam, calcola l'omografia e trova le superfici
                 (le macchie chiare a 4 lati della differenza bianco-nero).
                 Riaccende l'uscita. Disegna cosa ha trovato in automap_debug.jpg
automap()        scrive gli angoli nelle superfici di MadMapper, da sinistra a destra
```

Le superfici devono **esistere già** in MadMapper, una Quad per oggetto: l'MCP
le posiziona, non le crea. Requisiti come per Resolume: Windows in **Estendi**,
proiettore alla risoluzione nativa (`python displays.py`), webcam che vede tutto.

Manopole:
- `automap(dry_run=True)` mostra le coordinate senza scrivere.
- `canvas="1280x720"` (o `MADMAPPER_CANVAS`) se la tela di uscita di MadMapper non è 1920x1080.
- `output_view()` per vedere subito sul proiettore dove sono finite.
- forme sbagliate o oggetti che si toccano → `automap_points({"Quad-1": [[x,y]x4]})`
  con i punti presi da `automap_bianco.jpg`.
- `calibrate(dots=True)` su superfici già dipinte (dischi al posto della scacchiera).

`calibra.py`, `webcam.py`, `displays.py` sono copiati dal progetto Resolume.

## Effetti

Agiscono direttamente sull'**opacità delle superfici**: niente preparazione,
niente preset da ricaricare. Girano tutti nello stesso job `auto` (un processo
staccato che continua a Claude chiuso), in battute del BPM di MadMapper riletto
ogni 2 secondi, quindi seguono il tap tempo. Allo stop le opacità tornano com'erano.

```
fx_strobe(beats=0.25, duty=0.2)            flash sui sedicesimi, tutte le superfici
fx_chase(beats=1, order="random", seed=3)  una alla volta
fx_mix(beats=2, seed=7)                    salti casuali, stessi ogni sera con lo stesso seed
fx_media("Quad-1", ["Clip A","Clip B"], 8) cambia media ogni 8 battute
fx_modulate("/surfaces/Quad-2/opacity", "sine", 4, 0.2, 1)
fx_strobe(beats=0)                         spegne solo lo strobo
```

## Show automatico

Una volta mappato, `auto_show(energia)` fa girare le visual da solo, a tempo
col BPM di MadMapper, anche a Claude chiuso:

- ogni superficie cambia media a rotazione, **sfalsate** (non cambiano tutte
  insieme), pescando dai generatori e materiali del progetto (esclusi test,
  colori pieni, testo, camere e audio) o da `medias=[...]`
- una scaletta di effetti si alterna a blocchi di battute:
  `calma` (media ogni 32, chase morbido) · `media` (ogni 16: pulito / chase /
  pulito / mix) · `alta` (ogni 8: chase veloci, strobo, mix)
- stesso `seed` = stesso show

`fx_stop_all()` lo ferma e rimette le opacità. Il BPM si segue con `tap()` o
`set_bpm()`: il pilota lo rilegge ogni 2 secondi.

Il nome da scrivere in `/surfaces/X/visual/name` è quello della **lista** di
MadMapper ("Bar Code"), non la chiave OSC ("Bar_Code"): con la chiave il valore
viene accettato ma il media non cambia (verificato).

## Collaudo senza MadMapper

```powershell
python test_offline.py          # tutto il server contro un finto MadMapper
uv run --script automap.py verifica
python fake_madmapper.py        # finto MadMapper sulla 8010, per provare da Claude
```

## Limiti

- OSC non conferma la ricezione: le scritture importanti vengono rilette da OSCQuery.
- Creare/eliminare superfici e caricare media non è previsto: si fa in MadMapper.
- Spatial Scanner nativo non è pilotabile; l'automapping usa la nostra calibrazione.

## Licenza

MIT, vedi [LICENSE](LICENSE).
