[English](../README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [Français](README.fr.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Tiếng Việt](README.vi.md) · [中文 (简体)](README.zh-Hans.md) · [中文（繁體）](README.zh-Hant.md) · [Deutsch](README.de.md) · [Русский](README.ru.md)

[![LazyingArt banner](https://github.com/lachlanchen/lachlanchen/raw/main/figs/banner.png)](https://github.com/lachlanchen/lachlanchen/blob/main/figs/banner.png)

# LocalVideoGen

*Lokale MiniMax-H3-Videogenerierung in maximaler Qualität für eine Dual-RTX-4090-Workstation – mit nativem Bild, Ton und Referenzen sowie sorgfältiger Ressourcenverantwortung.*

[![Website](https://img.shields.io/badge/Website-lazying.art-0EA5E9?style=flat-square)](https://lazying.art)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](../LICENSE)
[![ComfyUI pinned](https://img.shields.io/badge/ComfyUI-pinned-5C35D5?style=flat-square)](../config/runtime-versions.txt)
[![Workflows](https://img.shields.io/badge/H3_workflows-17-16A34A?style=flat-square)](../workflows/)
[![GitHub Sponsors](https://img.shields.io/badge/Sponsor-lachlanchen-EA4AAA?style=flat-square&logo=githubsponsors)](https://github.com/sponsors/lachlanchen)

LocalVideoGen ist eine reproduzierbare Betriebsschicht um eine extern installierte, fest versionierte ComfyUI-Umgebung und das offizielle ausgerichtete MiniMax-H3-Modellpaket. Enthalten sind die nur über Loopback erreichbare Webapp H3 Studio, prüfsummengesteuerter Modellbezug, T2V/I2V/R2V-Workflows, gemeinsame native Video- und Audioerzeugung, dauerhafte Auftragsprotokolle sowie konservative Lebenszyklusregeln für zwei RTX 4090 mit je 24 GiB und 128 GiB Host-RAM.

Für lange visuelle Referenzen gilt das Preset **Long reference · 24 GiB safe**: `quality_int8_offload`, `match` und 704×1248 im Hoch- oder 1248×704 im Querformat. Eine Zulassungsprüfung kontrolliert Abmessungen und Referenzlast und stoppt einen unsicheren Auftrag vor der GPU-Übermittlung.

| Donate | PayPal | Stripe |
| --- | --- | --- |
| [![Donate](https://img.shields.io/badge/Donate-LazyingArt-0EA5E9?style=for-the-badge&logo=kofi&logoColor=white)](https://chat.lazying.art/donate) | [![PayPal](https://img.shields.io/badge/PayPal-RongzhouChen-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/RongzhouChen) | [![Stripe](https://img.shields.io/badge/Stripe-Donate-635BFF?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/aFadR8gIaflgfQV6T4fw400) |

## H3 Studio im Einsatz

Das helle Design zeigt Referenzeinrichtung, Qualitätsregler und Renderstatus übersichtlich in einem lokalen Arbeitsbereich.

![Helles H3-Studio-Design mit lokalen MiniMax-H3-Referenz- und Renderreglern](../docs/images/h3-studio-light.png)

## Eine Videoserie erstellen

In H3 Studio lässt sich direkt zwischen **Single Clip** und **Series** wechseln. Der Serienmodus bietet **LALACHAN Series**, die qualitätsorientierte Vorlage **World Travel** und **My Movie** als neutrale Vorlage für beliebige Besetzungen und Bildstile.

![H3-Studio-Regieansicht für World Travel im hellen Design](../docs/images/h3-studio-world-travel-light.png)

- Gemeinsame Figuren-, Welt-, Stimm- und Bewegungsreferenzen werden einmal hochgeladen; anschließend lassen sich 2–12 Shot-Karten mit eigenem Prompt, eigener Dauer und eigenem Seed anordnen.
- **World Travel** fixiert die sieben kanonischen Figuren- und Requisitenbilder auf P1–P7, weist jedem Shot eine eigene Zielorttafel auf P8 zu und reserviert P9 für das exakte Schlussbild des zuvor akzeptierten Shots. Frühere Episoden dürfen nur Identität oder Stimme vorgeben; sie dürfen weder das neue Land noch Handlung, Blocking, Farbpalette oder Bildkomposition steuern.
- Eine gemeinsame Zulassungssperre hält alle H3-Renderings strikt sequenziell. Bei aktivierter Kontinuität liefert jeder validierte, nicht letzte Shot sein exaktes Schlussbild und den eingestellten 2–4-Sekunden-Nachlauf (standardmäßig 3 Sekunden) an den nächsten Shot.
- Nach dem aktuellen Shot pausieren, nach einem Neustart fortsetzen, einen Shot samt Folgeshots wiederholen oder Nachbearbeitung und Endmontage erneut ausführen – ohne GPU-Zeit für bereits gültige MP4-Dateien zu verbrauchen.
- Jeder Render-Versuch bleibt erhalten. Der fertige Film wird erst nach Prüfung der erwarteten Bildzahl, gemeldeter durchschnittlicher 24 fps, der Stereo-Audio-Ausrichtung innerhalb der AAC-Toleranz, vollständigem Decode und SHA-256 verlustfrei per Stream-Copy zusammengesetzt; ein Prüfmanifest bleibt daneben erhalten.
- Andere lokale Projekte und Codex-Sitzungen können den ausschließlich auf der Python stdlib basierenden, abhängigkeitsfreien Series-Client verwenden. Er lädt größenbegrenzte Referenzen hoch, prüft die Serverfähigkeiten, schreibt vor der Generierung atomar einen Beleg mit dauerhafter ID, übersteht pausierte Prüfungen und unterbrochenes Polling und verifiziert Größe sowie SHA-256 eines Artefakts, bevor ein Download installiert wird.

Der Ablauf übernimmt die klare Storyboard-Idee von Xiaoyunque, doch Generierung und Projektzustand bleiben per Loopback vollständig auf dieser Workstation. Xiaoyunque oder ein kostenpflichtiger Cloud-Generator wird nicht aufgerufen.

Der [Leitfaden zum Serien-Workflow](../docs/series-workflow.md) beschreibt Kontinuität und Wiederherstellung, der [projektübergreifende Series-API-Leitfaden](../docs/local-series-api.md) den stdlib-CLI/-Client und den vollständigen HTTP-Vertrag und die [Übersicht zu Optionen für flüssige Langvideos](../docs/smooth-long-video-options.md) die vertrauenswürdige native H3-Basis, experimentelle Kontinuitätsprojekte, optionale Interpolation und Qualitätsprüfungen.

## Leistungsumfang

- Höchste BF16-Treue gilt für kurze visuelle Videoreferenzen oder reines Bild-R2V; lange Videoreferenzen verwenden standardmäßig den gemessenen, für 24 GiB sicheren INT8/Offload-Pfad.
- Auf der gemeinsam genutzten Workstation führt GPU 0 standardmäßig alle H3-Stufen aus; GPU 1 bleibt für LocalLLM frei. Nur ein explizites `H3_AUX_DEVICE=gpu:1` verlagert Qwen- und Referenz-VAE-Arbeit auf GPU 1.
- Lokales T2V, I2V und R2V mit mehreren Referenzen, einschließlich Max-Identity-Profil und nativ synchronisiertem Audio.
- Profile für Qualität, Ein-GPU-Rückfall und niedrig aufgelöste INT8-Turbo-Vorschau.
- Lokales H3 liefert bei 24 fps bis zu 768 Pixel an der kurzen Kante. MiniMax' separate 2K-Regenerierung ist nur per API verfügbar und wird nicht als lokale Funktion dargestellt.

## Architektur

```mermaid
flowchart LR
    B[Browser] -->|Loopback :8190| S[H3 Studio]
    S --> V[Upload- und Graphvalidierung]
    V --> J[(Private Auftragsdatenbank)]
    V -->|Loopback :8188| C[Fest versioniertes ComfyUI]
    C --> G0[GPU 0: standardmäßig alle H3-Stufen]
    G1[GPU 1: standardmäßig für LocalLLM reserviert]
    C -. optionales H3_AUX_DEVICE .-> G1
    C <--> R[Host-RAM: DynamicVRAM + asynchrones Offloading]
    M[SHA-256-geprüftes Modellpaket] --> C
```

Die Webapp startet niemals einen zweiten ComfyUI-Prozess. Dienste binden nur an `127.0.0.1`; Uploads werden in begrenzte Medien normalisiert, im Browser sichtbare Kennungen sind undurchsichtig und Ausgabepfade stehen auf einer Positivliste.

## Aktueller Inhalt

| Pfad | Zweck |
| --- | --- |
| [`webapp/`](../webapp/) | Responsives H3 Studio, lokale API, Upload-Normalisierung, Auftragsdatenbank und Tests |
| [`workflows/dual_gpu/`](../workflows/dual_gpu/) | BF16/INT8-T2V-, I2V- und R2V-Workflows mit Stufenverteilung |
| [`workflows/quality/`](../workflows/quality/) | 25-Schritt-Qualitätsworkflows für eine GPU |
| [`workflows/preview/`](../workflows/preview/) | Kleine INT8-Turbo-Vorschauworkflows |
| [`scripts/`](../scripts/) | Werkzeuge für Download, Prüfung, Lebenszyklus, Ressourcen, Smoke-Test und Workflows |
| [`config/model-manifest.sha256`](../config/model-manifest.sha256) | Exakte Positivliste aus neun Modelldateien und SHA-256-Prüfsummen |
| [`config/runtime-versions.txt`](../config/runtime-versions.txt) | Festgelegte Versionen und Commits der externen Laufzeit |

Die großen Verzeichnisse `ComfyUI/` und `workflow_templates/`, Modellgewichte, Ausgaben, Uploads, Datenbanken und Laufzeitbelege sind lokale Installationen oder privater/generierter Zustand und werden bewusst nicht committet.

## Schnellstart

Dieses Repository ist die öffentliche Orchestrierungsschicht, kein universeller Installer. Installieren Sie vor diesen Befehlen externe Arbeitskopien von `ComfyUI/` und `workflow_templates/` auf den exakten Commits in [`config/runtime-versions.txt`](../config/runtime-versions.txt), erstellen Sie `.venv` mit dem dort erfassten Python/PyTorch/CUDA-Stack und installieren Sie die Upstream-ComfyUI-Abhängigkeiten. Diese Upstream-Bäume sind absichtlich nicht eingebettet.

Die vollständige offizielle, ausgerichtete Modellwarteschlange umfasst **147.804.799.439 Bytes (137,65 GiB)**. Downloads sind fortsetzbar; planen Sie zusätzlich zu den Modelldaten mindestens 32 GiB freien Speicher ein.

```bash
git clone https://github.com/lachlanchen/LocalVideoGen.git
cd LocalVideoGen

# Nach Installation der festgelegten externen Laufzeit:
./scripts/download_models.sh
./scripts/verify_models.sh
./scripts/status.sh

./scripts/start_comfyui.sh
./scripts/start_webapp.sh
```

Öffnen Sie <http://127.0.0.1:8190>. ComfyUI bleibt unter <http://127.0.0.1:8188> privat.

Stoppen Sie bei inaktivem Rendering nur die verifizierten Prozesse dieses Projekts:

```bash
./scripts/stop_webapp.sh
./scripts/stop_comfyui.sh
```

## Sicherheits- und Ressourcenregeln

- Der Start erfordert mindestens 48 GiB verfügbaren RAM, 20.000 MiB freien VRAM auf jeder angeforderten GPU und höchstens 75 % Swap-Auslastung.
- Unvollständige Downloads und geänderte Größen-/mtime-Fingerabdrücke blockieren den Start; alle neun Dateien werden strukturell und per SHA-256 geprüft.
- Vor Lebenszyklusaktionen werden PID, Boot-Identität, Kommandozeile, Listener-Eigentümer, Dienstmarkierung und Warteschlangenstatus verifiziert.
- GPU-Bereinigung ist standardmäßig ein Dry-Run. Explizite Bereinigung nutzt exakte pidfds und schützt Prozessbäume unter `LocalLLM` und `AgenticApp`; fremde Prozesse werden nie automatisch beendet.
- Swap ist eine Notreserve und ersetzt nicht die RAM-Startschwelle. Für dieses Projekt sind nur eine Render-Engine und ein leichtgewichtiges Studio vorgesehen.

## Validierung

Statische Workflows und Webapp-Tests lassen sich ohne Renderauftrag ausführen:

```bash
./scripts/prepare_workflows.py
./scripts/validate_workflows.py
.venv/bin/python -m unittest discover -s webapp/tests -v
./scripts/verify_models.sh
```

Mit verifizierten Modellen und freier GPU 0 kann der kleine native Audio-Video-Smoke-Graph ausgeführt werden:

```bash
H3_CUDA_DEVICES=0 ./scripts/start_comfyui.sh
./scripts/smoke_test.sh
H3_SMOKE_MODEL=minimax_h3_fl2va_pruned_bf16.safetensors ./scripts/smoke_test.sh
./scripts/stop_comfyui.sh
```

Die Ausgabe mit fünf Frames prüft Qwen-Conditioning, gemeinsames Video-/Audio-Sampling, beide VAEs, MP4-Muxing und Stream-Dekodierbarkeit; sie ist kein visueller Qualitätsbenchmark.

## Lizenz und Modellgebiet

Der ursprüngliche LocalVideoGen-Code und die Dokumentation stehen unter der [MIT License](../LICENSE). Diese Lizenz **lizenziert ComfyUI, workflow templates, MiniMax-H3-Gewichte, Qwen-Komponenten, FFmpeg, andere Upstream-Abhängigkeiten oder generierte Inhalte nicht neu**. Deren eigene Bedingungen gelten fort.

Ein jailbroken oder abliterated Conditioner ist nicht enthalten: Das Manifest akzeptiert ausschließlich die ausgerichtete Comfy-Org-Datei `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` mit der hinterlegten Prüfsumme. Die MiniMax H3 Community License schließt EU, Vereinigtes Königreich, Republik Korea und USA aus ihrem Anwendungsgebiet aus und enthält Weitergabepflichten. Prüfen Sie vor Download oder Nutzung die [Upstream-Modellkarte](https://huggingface.co/MiniMaxAI/MiniMax-H3), die [Lizenz](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE), das anwendbare Recht und alle Abhängigkeitslizenzen. Dieses Projekt bietet keine Rechtsberatung.

## Zitierung

Wenn Sie LocalVideoGen in der Forschung verwenden, zitieren Sie dieses Repository. GitHub liest [CITATION.cff](../CITATION.cff) und zeigt auf der Repository-Seite den Bereich **Cite this repository** an.

```bibtex
@software{chen_localvideogen_2026,
  author = {Chen, Lachlan},
  title = {LocalVideoGen: Resource-Aware Local MiniMax H3 Video Generation},
  year = {2026},
  version = {0.1.0},
  url = {https://github.com/lachlanchen/LocalVideoGen}
}
```

## Status und Umfang

Version **0.1.0** ist eine workstationbezogene Forschungsversion, validiert unter Linux mit zwei RTX 4090 und 128 GiB RAM. Sie priorisiert Reproduzierbarkeit, Modellintegrität, visuelle Qualität und das sichere Nebeneinander mit anderen Langzeitprojekten vor breiter Hardwareunterstützung oder Ein-Klick-Einrichtung. Ergebnisse bleiben generativ und sollten vor Veröffentlichung geprüft werden.

Projekt: [github.com/lachlanchen/LocalVideoGen](https://github.com/lachlanchen/LocalVideoGen) · Homepage: [lazying.art](https://lazying.art)
