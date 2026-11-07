# How AI Entered the Fine Print: Tracing AI Policy Language Through Web Archives

**Tyler Garriott, Philip Marino, David Long, Finn Sparks**

*Revised proposal. It responds to feedback that the original five-provider design was too small for the expected scale of tens of thousands of samples.*

## Summary of Revisions

| | Original proposal | Revised proposal |
|---|---|---|
| Research question | How have the usage policies of major AI providers changed over time? | How has AI-related language entered and changed in online legal documents since 2022, both in AI providers' own rules and in the terms other websites set about AI? |
| Corpus | Usage policies of 5 AI providers | **Core:** usage policy, ToS and privacy policy of ~15–25 AI providers. **Broad:** ToS and privacy policy of the top ~1,500 websites (Tranco list) |
| Unit of analysis | Document version | Clause-level change between consecutive document versions |
| Expected scale | Hundreds of snapshots | ~100K candidate snapshots → ~10K–30K distinct versions → tens of thousands of clause-level changes |
| Classification | LLM labels every change | Keyword/embedding filter for AI relevance, then the LLM labels the AI-relevant changes, validated against hand labels |

The pipeline is the same one we proposed: enumerate snapshots, clean, deduplicate, diff and classify. What changes is the corpus we run it on.

## Objective

We will investigate the question: **"How has AI-related language entered and changed in online legal documents since 2022?"** We will study two related groups of documents:

1. **AI providers' rules for their users.** These are the acceptable use policies, terms of service and privacy policies of the companies that build and serve AI models. This is the focus of our original proposal, now covering more providers and document types.
2. **Other websites' rules about AI.** These are clauses added to the ToS and privacy policies of widely used websites. Examples include granting the site the right to train AI on user content, banning scraping or use of site content for model training, governing AI-generated content, and disclosing AI features.

We count both substantive changes (what is permitted) and wording-only edits as changes. We will label each AI-relevant change as a **new restriction**, a **loosened rule**, a **clarification**, or a **wording change**, and record its **AI topic**: training on user data, scraping or crawling, generated content, prohibited uses, or AI feature disclosure.

We aim to answer:

- **Adoption:** What share of top websites have AI-specific language in their ToS or privacy policy, and how has that share changed month by month since 2022? Which sectors adopted it first?
- **Direction:** Have AI providers' policies mostly tightened or loosened? Do other sites mostly add restrictions on AI (anti-scraping) or permissions for themselves (rights to train on user content)?
- **Timing:** Do changes cluster in particular periods, and do those periods line up with real-world events?
- **Length and vocabulary:** Are these documents getting longer, and is AI language making up a growing share of them?

Our end products are:
- a cleaned, versioned corpus;
- a detailed change timeline for the AI providers;
- an adoption curve and topic breakdown for AI clauses across the web.

## Motivation

AI policies are living documents with no complete, centralized record. Providers often overwrite their pages when they update them, and changelogs are rare or partial. Since 2022, AI has also changed the legal documents of companies that do not build models. Some sites have claimed the right to train AI on what their users post. Others have tried to forbid AI companies from training on their content. Several of these edits caused public controversy, including Zoom's terms update in August 2023 and Adobe's terms update in June 2024. In other cases, the edits came alongside data-licensing deals, such as Reddit's and Stack Overflow's in 2024.

Expanding the corpus lets us test our original hypothesis more convincingly. We expect AI-related terms to have become more common and more restrictive over time, with stronger vocabulary and longer documents, and with bursts of change after major events. We will report evidence both for and against these hypotheses.

This is a digital archaeology problem because the record is incomplete. Wayback captures are irregular, policy URLs move, pages redirect, and many sites render their content with JavaScript. Reconstructing a reliable history from these partial observations, and measuring how much of the record we can recover, is central to the project.

### Related Work

- **Amos et al., "Privacy Policies over Time: Curation and Analysis of a Million-Document Dataset" (WWW 2021).** This study built a longitudinal privacy policy corpus of about 1M snapshots across roughly 130K websites from the Wayback Machine. It shows that our approach scales, and its URL-discovery methods guide ours. It predates generative AI and does not study AI clauses.
- **Longpre et al., "Consent in Crisis: The Rapid Decline of the AI Data Commons" (2024).** This study measured how the websites behind AI training datasets restricted AI crawling through robots.txt and ToS. We differ in three ways:
  - we include the AI providers' own policies;
  - we study clauses that face users, such as training rights over user content, not only crawler restrictions;
  - we classify the direction of each change and build a timeline.
- **Le Pochat et al., "Tranco: A Research-Oriented Top Sites Ranking Hardened Against Manipulation" (NDSS 2019).** This is the source of our website sample.

## Data

All documents come from the Internet Archive's Wayback Machine. We use the CDX API to list captures and the raw `id_` playback mode to fetch pages without the archive's injected markup. The time range is January 2022 to the present.

### Core layer: AI providers

About 15–25 providers, including OpenAI, Anthropic, Google, Meta, xAI, Microsoft, Mistral, Cohere, Stability AI, Midjourney, Perplexity, Character.AI, ElevenLabs, Runway and DeepSeek. For each provider we collect the usage or acceptable use policy, the terms of service and the privacy policy. We will curate these URLs by hand, including historical URLs. For example, OpenAI's usage policy moved from `beta.openai.com/docs/usage-policies` to `openai.com/policies/usage-policies`.

### Broad layer: top websites

We take the top ~1,500 registrable domains from a pinned Tranco list (list `V354N`, created 2026-11-01). For each domain we find the ToS and privacy policy URLs automatically:

1. **Homepage links.** Fetch the archived homepage from mid-2022 and from the latest capture, and take the footer links whose text or URL looks like terms or privacy. Using two homepages captures URL moves over time.
2. **Common paths.** If the homepage has no usable links, which is common for pages built with JavaScript, query the CDX index for common paths such as `/terms`, `/legal/terms` and `/privacy-policy`.
3. **Monthly snapshots.** For every policy URL found, list successful (HTTP 200) captures since 2022, keeping at most one per month, and record each capture's content digest.

Many Tranco domains are infrastructure, such as CDNs and API hosts, and have no homepage. Others are never captured or block archiving. We will report how many domains we could recover at each step as a finding in its own right, not hide it.

### Expected scale

| Stage | Estimate |
|---|---|
| Domains processed | ~1,500 |
| Policy URLs found (ToS + privacy) | ~1,500–2,500 |
| Monthly snapshots listed (≤ ~58 months per URL) | ~60K–140K |
| Distinct versions after text-level deduplication | ~10K–30K |
| Clause-level changes between consecutive versions | tens of thousands |
| AI-relevant clause changes (labeled by the LLM) | low thousands |

The core layer adds roughly 75 documents with denser histories. Our existing five-provider index already holds 3,445 captures before deduplication.

### Data challenges

- **Gaps.** A change is dated to the first snapshot that shows it, so the true date lies somewhere between the previous capture and that one. Where a policy states its own "last updated" date, we extract it and use it to check our dating.
- **Redirects and URL moves.** We resolve homepage links through archived redirects and find URLs from two time points. Redirect-only captures are recorded for the core providers.
- **Noise that isn't policy text.** Cookie banners, navigation, dynamic tokens and layout changes alter the page's digest without changing the policy. We therefore deduplicate on cleaned text, not on raw digests.
- **Error and placeholder pages.** We discard captures whose extracted text is too short or matches login walls, consent walls or error templates, and we log how often this happens.

## Approach

The work has five stages, all written in Python.

1. **Retrieval.**
   - Core: CDX enumeration over hand-curated URLs (`extract_policies.py`).
   - Broad: automatic URL discovery and monthly snapshot enumeration over the Tranco list (`enumerate_tranco.py`).
   - Then fetch the pages through polite, rate-limited requests that can resume after an interruption.
2. **Cleaning and deduplication.** Remove boilerplate (scripts, navigation, headers and footers, cookie banners) and normalize whitespace and Unicode. Merge consecutive snapshots with identical cleaned text, which gives each document an ordered list of versions.
3. **Comparison.** Split each version into clauses using headings and paragraphs. Align clauses between consecutive versions and diff them to get added, removed and modified clauses.
4. **Labeling.**
   - Filter clause changes for AI relevance with a keyword list (AI, artificial intelligence, machine learning, model, train, generative, LLM, scrape, crawl, bot), backed by embedding similarity.
   - Label every AI-relevant change with an LLM: change type and AI topic.
   - All four team members hand-label a stratified sample of about 300 changes. Two members double-label a subset to measure inter-annotator agreement (Cohen's κ), and we report the LLM's accuracy and per-class F1 against the adjudicated labels.
   - We also hand-check a sample of changes the filter rejected, to estimate how many AI-relevant changes it misses.
5. **Analysis.**
   - Monthly adoption curves of AI language across top sites, broken down by sector and by AI topic.
   - The balance of tightening and loosening per provider and over time.
   - Tests for clustering of changes in time.
   - Alignment with events: ChatGPT's launch (November 2022), the Zoom (August 2023) and Adobe (June 2024) controversies, the Reddit and Stack Overflow licensing deals (2024), and the EU AI Act entering into force (August 2024).
   - Trends in document length.

## Team Responsibilities

- **Tyler Garriott, retrieval.** Builds CDX enumeration for both layers, Tranco URL discovery, redirect and URL-move handling, and the rate-limited page fetcher.
- **Philip Marino, cleaning and processing.** Builds boilerplate removal, the filter for error and placeholder pages, text-level deduplication, clause segmentation and corpus organization.
- **David Long, comparison and classification.** Builds clause alignment and diffing, the AI-relevance filter, the LLM classification prompts and pipeline, and the evaluation against hand labels.
- **Finn Sparks, analysis and visualization.** Builds the adoption curves, provider timelines, tightening and loosening trends, length analysis and event alignment.

All members hand-label the sample, with two members double-labeling a subset to measure agreement. Everyone contributes equally to the final report and presentation.

## Timeline and Milestones

| Dates | Milestone |
|---|---|
| By Nov 10 (progress check) | Core-layer pipeline running end to end with preliminary classifications. Broad-layer URL discovery and snapshot enumeration finished, with coverage statistics. |
| Nov 11–17 | Fetch broad-layer snapshots. Clean, deduplicate and segment into clauses. Calibrate the AI-relevance filter. Hand-label the sample. |
| Nov 18–24 | Run the LLM classification at full scale and evaluate it. Run the analysis and build the visualizations. Draft the final report. |
| Nov 25–Dec 1 | Event alignment, report revisions, code documentation, slides and rehearsal. Present on December 1. |

## Expected Outcome

1. **A versioned corpus** of AI-provider policies and top-site ToS and privacy policies from 2022 to the present, with clause-level diffs, released with our code so others can extend it.
2. **A provider timeline** showing when each AI provider's policies changed, what changed, and whether each change tightened or loosened the rules.
3. **An adoption curve** showing how AI language spread through the fine print of top websites, broken down by topic and sector.
4. **An evaluation** of LLM-based change classification against human labels, including inter-annotator agreement.
5. **A coverage report** quantifying what the archive could and could not recover. This is a finding about the Wayback Machine as a source for policy research.

Because snapshots have gaps, change dates are "first observed" dates with known error bounds, not exact publication dates. Where documents state their own update dates, we use them to tighten that bound.
