# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Scientific Literature Extraction System** in the specification phase. It defines how an AI curator should parse scientific PDFs (focused on protein biochemistry and biophysics for drug target discovery) and output structured JSON "fingerprints" for a metadata-enriched database.

There is no implementation code yet — the project consists of:
- `curation_prompt.md` — System prompt defining the AI curator's role, constraints, and extraction rules
- `extraction_schema.json` — The target JSON schema for all extracted paper data

## Architecture

The two files work together as a specification pair:

1. **`curation_prompt.md`** is fed as a system prompt to an AI model along with a scientific PDF. It enforces:
   - Strict factual provenance (every claim must have a `source_span` like "Page 4, Para 2")
   - Null values for anything not explicitly stated — no inference
   - Capture of negative results and failed hypotheses
   - Entity extraction (chemicals, proteins, equations)
   - For protein-protein interactions: domain mapping, amino acid residues, and affinity measures (Kd, Ki)

2. **`extraction_schema.json`** defines the output structure:
   - `paper_metadata`: title, DOI, study type (enum), 100–150 word situational context hook
   - `methodology`: methods, protein organism origin (NCBI taxonomy ID integer), controls, instruments
   - `key_findings[]`: claim, evidence value, affinities in Molar (float), Ki, residues, confidence (0.0–1.0), statistical significance, source span
   - `contradictions_and_negative_results[]`: finding, conflicts_with_prior_work boolean, reasoning
   - `entities`: chemicals, proteins, equations arrays

## Key Domain Constraints

- `affinities_kd_Molar` and `inhibitory_constant_Ki` must be floats in **Molar units** (not nM or µM)
- `protein_origin_organism` uses **NCBI taxonomy integer IDs** (e.g., 9606 for human)
- `confidence_score` is 0.0–1.0 float
- Output must be **strict JSON only** — no prose, no preamble
- `study_type` is a closed enum: `meta-analysis`, `randomized_controlled_trial`, `case_study`, `experimental`
