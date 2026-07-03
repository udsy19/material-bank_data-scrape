"""material_bank — DSource AI registry-driven material harvest pipeline.

Stage 0 (registry) + Stage 1 (probe) live here. Deterministic Python only;
LLM agents enter in four slots elsewhere (see PIPELINE.md). The probe is the
verifier — nothing in the seed CSVs is trusted until probed.
"""

__version__ = "0.1.0"
