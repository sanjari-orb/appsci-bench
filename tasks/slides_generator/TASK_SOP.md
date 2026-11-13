# Slides Generator — Task SOP (ground truth)

Comprehensive definition of the task. **Not shown to the agent** — it is the source from which the user-personas, environment, and oracle are authored. Personas reveal only audience-scoped subsets.

## Task Description

Automatically generate structured slide decks (JSON format + .pptx output) from a user prompt and any attached grounding documents. The task requires the agent to:
- Parse and understand the intent of the request (informative, instructive, or persuasive) from the prompt and grounding documents
- Synthesize or faithfully represent the content depending on fidelity requirements
- Structure the content into a logical slide sequence
- Adhere to the provided presentation template.pptx and lay content out to fit it
- Output both structured JSON (for further processing) and a rendered PowerPoint file

JSON format: 
```json
{
  "presentation": {
    "title": "Q2 Performance Report",
    "theme": "modern_dark",
    "aspectRatio": "16:9",
    "metadata": {
      "author": "Data Analytics Team",
      "version": "1.0",
      "date": "2026-06-15"
    },
    "slides": [
      {
        "slideNumber": 1,
        "layout": "Blank Basic Layout / Photo full",
        "elements": [
          {
            "type": "text",
            "name": "TitlePlaceholder",
            "content": "Quarterly Results Q2",
            "style": {
              "fontSize": 44,
              "bold": true,
              "color": "#FFFFFF"
            }
          },
          {
            "type": "text",
            "name": "SubtitlePlaceholder",
            "content": "An analysis of user growth and retention metrics",
            "style": {
              "fontSize": 20,
              "color": "#A0A0A0"
            }
          }
        ]
      },
      {
        "slideNumber": 2,
        "layout": "Header_1 block",
        "elements": [
          {
            "type": "text",
            "name": "Header",
            "content": "Key Metrics Summary"
          },
          {
            "type": "image",
            "name": "FeatureChart",
            "sourceUrl": "https://example.com",
            "altText": "Bar chart showcasing a 15 percent increase in macro sales"
          },
          {
            "type": "bullet_list",
            "name": "KeyPointsList",
            "items": [
              "Net revenue increased by 14.5% quarter-over-quarter.",
              "User churn rate successfully reduced to 2.1%.",
              "Expansion initiated into European markets ahead of schedule."
            ]
          }
        ],
        "notes": "Remind the team that European market performance heavily exceeded our baseline projections."
      }
    ]
  }
}

```
## Problem

Teams produce requests of varying content type — a terse prompt, prose notes, prior decks, dense reports — and need to turn them into structured decks with different objectives (informative, instructive or persuasive). Manually doing this is heavily time-consuming and an automation step can help with keeping up with the load.

## KPIs (customer success)
- The agent produces a slide deck of the expected length.
- The latency on generation is bounded by the limit criteria. 
- No hallucination, the presentation only contains data present in the prompt and grounding documents and the representation is reliable.
- The trade-off between coverage and summarization is kept according to the input and the requested number of slides. 
- The resulting presentation adheres to the template.pptx given by the user. 

## Sections by audience

- **L1 — Applied-science manager / tech lead:** co-designs the approach — LLM prompting vs. structured extraction vs. hybrid, output schema trade-offs, slide-count heuristics, input preprocessing (prompt + grounding documents). Surfaces the evaluation difficulty (automatic metrics are limited) and the fidelity-vs-synthesis tension. Will not hand the full solution — expects the agent to design the architecture. *(Corresponds to `l1_manager_collaborative` / `l1_manager_terse` personas)*
- **L2 — Non-AI technical partner (platform / software engineer):** owns the integration contract: JSON output schema, asynchronous response, timeouts, request limit. Treats all AI-method questions (prompt design, model selection, evaluation strategy) as out of scope and defers them to the agent. *(Corresponds to `l2_technical_partner_collaborative` / `l2_technical_partner_terse` personas)*
- **L3 — Product manager:** frames business pain (manual deck creation is slow), describes what a good deck means (covers stated outcomes, coherent flow, appropriate tone), explains the cross-org audience range, and confirms typical slide counts only when the agent asks with a concrete request example (recurring QBRs ~8 slides; pitches 8–12; research briefings 6–8; sparse status updates 3–5). Does NOT speak to JSON schema, model choice, metric thresholds, or exact formatting — defers all of those to the agent. *(Corresponds to `l3_pm_collaborative` / `l3_pm_terse` personas)*

