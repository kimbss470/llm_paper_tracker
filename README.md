# LLM Paper Tracker

Static HTML tracker for academic papers around LLM efficiency topics such as MoE, KV cache compression, quantization, and long-context optimization.

## Features

- Daily crawling from:
  - arXiv API
  - OpenAlex API
- At-a-glance page with:
  - Title
  - Authors
  - Affiliation
  - Conference/Journal
  - Year
  - Auto-categorized topic
- Per-paper detail page with sections following `summarize_structure.md`:
  - Summary
  - Motivation
  - Key Idea
  - Experimental Results
  - Data analysis
  - Discussion (e.g., future work)
  - Significance of this study
  - Useful references to consider
- Daily auto-update + deployment with GitHub Pages

## Run locally

1. Create environment and install dependencies:

   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt

2. Build site:

   python paper_tracker.py

3. Open generated page:

   site/index.html

## Deployment

GitHub Actions workflow is in `.github/workflows/daily-paper-tracker.yml`.

- Schedule: `0 0 * * *` (UTC), which is **09:00 KST** every day.
- Trigger manually with `workflow_dispatch`.
- Deploy target: GitHub Pages from generated `site/` artifact.

## Notes

- arXiv metadata does not consistently include affiliations. The pipeline enriches affiliations from OpenAlex matches when possible.
- Categories are assigned using keyword-based rules and can be edited in `paper_tracker.py`.
