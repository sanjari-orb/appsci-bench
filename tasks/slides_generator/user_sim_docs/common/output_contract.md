# How a finished deck comes back to us

Whoever asks for a deck gets two things back from the same request, in one response: the deck as a
**file we can open and edit in PowerPoint**, and the deck's content as **structured data** our own
tooling reads.

Both matter to us, for different reasons. The file is what someone actually presents, so it has to be
a real editable `.pptx` — never an image of slides, never a flattened text dump. The structured data is
what lets us do anything else with a deck afterwards without re-parsing PowerPoint: diff it against
last quarter's, pull every figure out for a check, feed it into another tool. A response with only one
of the two is not usable.

## The structured side

One object describing the presentation as a whole, and inside it an ordered list of slides. Each slide
says which of the template's layouts it uses, and carries the pieces of content that sit on it — each
piece named, and typed by what it is: running text, a bulleted list, a table, an image. A slide can
also carry speaker notes.

At the presentation level we expect the things that describe the deck rather than any one slide: its
title, the theme it was built on, the aspect ratio, and the housekeeping fields (author, version,
date).

That is the shape, and here is an example of the format we expect:

```json
{
  "presentation": {
    "title": "Q2 Business Review",
    "theme": "default",
    "aspectRatio": "16:9",
    "metadata": { "author": "Jane Doe", "version": "1.0", "date": "2026-01-14" },
    "slides": [
      {
        "slideNumber": 1,
        "layout": "title_and_content",
        "elements": [
          { "type": "bullet_list", "name": "Headline", "items": ["Point one", "Point two"] },
          { "type": "text", "name": "Body", "content": "Running paragraph text." },
          { "type": "table", "name": "Metrics", "rows": [["Q1", "Q2"], ["120", "145"]] },
          { "type": "image", "name": "Chart", "sourceUrl": "chart.png", "altText": "Growth chart" }
        ],
        "notes": "Optional speaker notes."
      }
    ]
  }
}
```

Every slide needs `slideNumber`, `layout`, and at least one element carrying real content (`content`
for text, `items` for a list, `rows` for a table, `sourceUrl` for an image) — an element with none of
those is treated as empty. What we care about is that these names come back every time, that the
slide order in the data matches the slide order in the file, and that every slide in the deck is
present in the data.

## What counts as a failure

An empty presentation object, or one we cannot read as structured data, is a failed request — we would
rather get a clear error than a deck we cannot process. Same for a response with no `.pptx`, or a
`.pptx` that PowerPoint will not open.
