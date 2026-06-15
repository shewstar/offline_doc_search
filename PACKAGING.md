# Offline packaging

How to turn the source tree into a self-contained, network-free bundle that runs
on a target machine with no Python install and no internet access.

The build is a **PyInstaller onedir** bundle (a folder, not a single .exe): it
starts faster, every bundled file is visible for audit, and the writable `data/`
and optional `bin/` folders sit plainly beside the executable. This matches the
plan's "single Python runtime, portable, reviewable" posture — there is no Node
or Rust runtime to ship.

## Build (on an online machine)

You need Python 3.11+ with the runtime and build dependencies installed:

```sh
python -m venv .venv
.venv\Scripts\activate                 # Windows; source .venv/bin/activate on POSIX
pip install -r requirements.txt -r requirements-build.txt
```

Then build:

```powershell
pwsh packaging/build.ps1               # or: pwsh packaging/build.ps1 -Clean
```

or invoke PyInstaller directly:

```sh
pyinstaller --noconfirm packaging/offline-doc-search.spec
```

Output: **`dist/offline-doc-search/`** — a self-contained folder. Launch
`offline-doc-search.exe` (Windows) and the app starts the localhost server and
opens your browser at the UI.

## Bundle layout

```text
dist/offline-doc-search/
  offline-doc-search.exe     # launcher: starts 127.0.0.1 server, opens browser
  _internal/                 # PyInstaller runtime + Python stdlib + deps
    web/                     # bundled UI + vendored PDF.js (read-only)
  data/                      # created on first run, BESIDE the exe (writable):
    app.db                   #   SQLite index (WAL files alongside)
    ocr-cache/               #   OCR'd PDFs, keyed by content hash
  bin/                       # OPTIONAL — drop OCR binaries here (see below)
  models/                    # OPTIONAL — drop a GGUF model here for Ask mode (see below)
```

Read-only resources (`web/`) are unpacked inside the bundle and located via
`sys._MEIPASS`; writable state (`data/`) is resolved beside the executable. Both
are handled in [`app/paths.py`](app/paths.py), so the same code runs identically
from source (`python run_app.py`) and frozen.

To deploy, copy the whole `dist/offline-doc-search/` folder to the target
machine. Nothing else is required for **native-text PDFs** — no Python, no
network. OCR for scanned PDFs needs the extra binaries below.

## OCR toolchain (optional, for scanned PDFs)

OCR is intentionally **not** baked into the Python bundle — it depends on large
native programs (Tesseract + Ghostscript) that are better installed and audited
separately. The app degrades gracefully: without them, scanned PDFs are flagged
`required` and left un-OCR'd; native-text PDFs are unaffected.

Two offline-friendly ways to provide the toolchain, in order of preference for a
restricted environment:

### Option A — drop-in `bin/` folder (no system install)

Create a `bin/` folder **beside the exe** and place the binaries there:

```text
dist/offline-doc-search/
  bin/
    tesseract.exe
    gswin64c.exe            # Ghostscript
    ... plus their DLLs / tessdata as required by those distributions
```

On launch the app prepends `bin/` to `PATH`
([`app/launcher.py`](app/launcher.py)), so `ocrmypdf`/`tesseract` are discovered
without touching the system. This keeps everything inside one approved folder —
the plan's "portable mode."

> `ocrmypdf` itself is a Python program. If you want OCR in the frozen build,
> the simplest path is to install the Tesseract + Ghostscript native binaries in
> `bin/`, and ship `ocrmypdf` as a small console script there too (e.g. built
> with PyInstaller or copied from a venv's `Scripts/`). The app only calls these
> as external processes — see [`app/ocr.py`](app/ocr.py).

### Option B — standard system install

Install Tesseract (4.1.1+) and Ghostscript on the target machine via your
approved offline installer, ensuring both are on the system `PATH`. The app will
detect them automatically.

### Language packs

Tesseract needs a `*.traineddata` file per language (`eng.traineddata`, etc.) in
its `tessdata` directory. Bundle the languages your corpus needs; English ships
with most Tesseract distributions. Select languages at index time with
`--ocr-lang` (CLI) or the OCR language field (web UI), e.g. `eng` or `eng+deu`.

## Local LLM for Ask mode (optional)

Natural-language **Ask** mode is off by default. Keyword search works without any
model. To enable Ask, install the optional Python dependency and drop a GGUF
instruct model beside the executable:

```sh
pip install -r requirements-llm.txt
```

```text
dist/offline-doc-search/
  models/
    qwen2.5-1.5b-instruct-q4_k_m.gguf    # example; any single *.gguf works
```

The app picks the first `*.gguf` in `models/` (sorted by name). Model discovery
is handled in [`app/paths.py`](app/paths.py); inference in [`app/ask.py`](app/ask.py).

### Recommended models (16 GB RAM laptops)

| Model | Quantization | File size | RAM at inference |
|---|---|---|---|
| Qwen2.5-1.5B-Instruct | Q4_K_M | ~1 GB | ~2 GB |
| Llama-3.2-3B-Instruct | Q4_K_M | ~2 GB | ~3–4 GB |

Use **instruct/chat** variants only. Extraction or vision (VL) models often ignore
the chat format and produce unusable answers; the app will fall back to excerpt
quotes from retrieved pages, but a proper instruct model (e.g. Qwen2.5-1.5B-Instruct)
gives much better synthesized answers. The model is lazy-loaded on the first Ask
request and unloaded after 5 minutes of idle time to free RAM for indexing/OCR.

Ask mode still uses FTS5 for retrieval — the LLM only expands the question into
search terms and summarizes retrieved page excerpts with `[filename p.N]`
citations. Answers are generated locally on `127.0.0.1`; nothing leaves the machine.

> `llama-cpp-python` is intentionally **not** bundled in the default PyInstaller
> build. Operators who need Ask install it in their build venv before packaging,
> or run from source with `requirements-llm.txt`. The frozen app degrades
> gracefully when the dependency or model is absent.

## Air-gapped dependency install

If even the build machine is offline, build a wheelhouse on a connected machine:

```sh
pip download -r requirements.txt -r requirements-build.txt -d wheelhouse
```

Optional Ask mode adds `requirements-llm.txt` to the download list if needed.

Transfer `wheelhouse/`, then on the air-gapped build machine:

```sh
pip install --no-index --find-links wheelhouse -r requirements.txt -r requirements-build.txt
```

## Security notes (unchanged by packaging)

- The server binds `127.0.0.1` only — no external listener, no telemetry, no
  auto-update.
- All index data stays in the `data/` folder beside the executable; deleting
  that folder fully resets the app.
- No UPX compression is used, so every bundled binary stays byte-for-byte
  inspectable.
