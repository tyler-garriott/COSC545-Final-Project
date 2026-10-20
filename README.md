# COSC545-Final-Project

## Project Title
Tracing AI Provider Usage Policy Changes Through Web Archives

## Objective and Approach
The question we want to investigate is "How have the usage policies of major AI providers changed over time?" We plan to approach this by recovering historical versions from the Wayback Machine, cleaning and deduplicating them, diffing consecutive versions, and classifying changes (e.g., new restrictions, loosened rules, wording-only edits), using an LLM validated against a hand-labeled sample. Our primary end product will be a timeline showing what changed and when, with changes aligned to real-world events if time permits.

## Expected Data Source
We will analyze the usage policies of four providers: OpenAI, Anthropic, Google (Generative AI), and Meta (Meta AI). We plan on using the Wayback Machine CDX API to enumerate snapshots before fetching the archived pages. The time range we would like to observe is from 2022 (or each policy’s first appearance) to the present. Because snapshots are irregular samples rather than a complete change history, we’ll treat gaps as missing observations and track URL moves and redirects to follow each policy across its versions.
