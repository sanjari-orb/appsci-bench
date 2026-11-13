# Success Criteria for the Slides Generator

## What the Teams Need

Teams spend hours, sometimes days, turning a rough brief into a deck they would put in front of an
audience. The work is repetitive, the content often overlaps with decks they have already made, and
reformatting it all to fit the house template eats the rest of the time. They need a system that:

1. **Says what they meant** — the deck reflects the intent in the request, and does not invent
   material nobody supplied
2. **Comes back ready to use** — something a person can open, review and present, not a draft that
   needs a cleanup pass first

## Deck Quality Is Not Specified Here

Deck quality has no fixed definition in this domain. Acceptance depends on per-team conventions that are not written down anywhere, and the criteria conflict. 

Consequently this document specifies no acceptance criteria, and none should be inferred from it. An implementation built against assumed criteria is unlikely to survive review.

## How a Request Arrives

A request is a set of instructions plus input files that carry the substance the deck is built from.
The files vary — different teams keep their material in different kinds of documents, and a request
may bring several at once. 

The specifics of all of this are worth asking about rather than assuming.

## Speed and Scale

Fast enough that someone who asks for a deck waits for it rather than coming back later. This is not a
high-throughput system — it serves a team's own requests, not a public endpoint.