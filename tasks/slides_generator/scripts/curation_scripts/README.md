# Curation Scripts

Reconstructs evaluation data points from raw `.pptx` presentations.

## What it does

For each `slidesgen_NN/` folder it produces:

| Output file | Source | Processor |
|---|---|---|
| `presentation.pdf` | `presentation.pptx` | LibreOffice headless |
| `presentation.md` | `presentation.pdf` | liteparse PDF→Markdown |
| `input_brief.md` | `presentation.md` | Claude API (prose rewrite) |
| `presentation_template.pptx` | `presentation.pptx` | lorem-ipsum anonymisation + layout dedup |
| `presentation_json.json` | `presentation.pptx` | python-pptx slide-by-slide text extraction |

## Pipeline

```
presentation.pptx
    ├── [PptxToPdf]       → presentation.pdf
    │       └── [PdfToManuscript]  → presentation.md
    │               └── [ManuscriptToBrief] → input_brief.md
    ├── [PptxToTemplate]  → presentation_template.pptx
    └── [PptxToJson]      → presentation_json.json
```

## Prerequisites

**LibreOffice** (required for PPTX→PDF):

```bash
# macOS
brew install --cask libreoffice
# or download from https://www.libreoffice.org/download/

# Ubuntu/Debian
sudo apt install libreoffice
```

**Python dependencies:**

```bash
pip install -r requirements.txt
```

**Anthropic API key** (required for MD→Brief):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
# or use gateway — credentials auto-detected from ~/.config/gateway/tokens.json
```

## Usage

```bash
cd tasks/slides_generator/eval_data/curation_scripts

# Run pipeline — skips steps whose output already exists
python main.py

# Force re-run all steps
python main.py --force
```

## Individual processors

Each processor can also be run standalone:

```bash
python processors/pptx2pdf.py      path/to/presentation.pptx
python processors/pdf2manuscript.py path/to/presentation.pdf
python processors/manuscript2brief.py path/to/presentation.md
python processors/pptx2template.py  path/to/presentation.pptx
python processors/pptx2json.py      path/to/presentation.pptx
```

`pptx2json.py`'s extraction logic now lives in `tasks/slides_generator/pptx_json.py`, shared with
`eval.py`'s content judges (which score the JSON extracted from the produced deck rather than the
artifact's self-report — see that module's docstring). This file is a thin wrapper around it so the
standalone CLI usage above keeps working unchanged.
