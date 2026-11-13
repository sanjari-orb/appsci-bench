#!/usr/bin/env python
"""Deterministic three-state clause detection.

Two kinds of pattern, and the distinction is load-bearing:

  * CONFIRMING -- establishes the clause's meaning ("aggregate liability shall not exceed").
  * AMBIGUOUS  -- related vocabulary that establishes nothing on its own (a bare
    "LIMITATION OF LIABILITY" heading, a bare "irrevocable").

Resolution:

                      | must_have                          | must_not_have
  --------------------+------------------------------------+---------------------------------
  confirming match    | satisfied                          | violation, verified span
  ambiguous only      | violation, present=false, no span  | UNRESOLVED
  no match            | violation, present=false, no span  | no violation
  confirming, no span | n/a                                | UNRESOLVED

The asymmetry is forced by the output schema: a must_have violation carries no span, so an
ambiguous required clause is still expressible as a judgment-call violation. A must_not_have
violation requires an exact span, so an unconfirmable one is inexpressible and must be routed
out as unresolved rather than reported as genuinely-checked-and-clean.

Negation guards are pattern-specific and reference each category's own terms. There is no blanket
"not" test: "liability shall not exceed $1m" confirms a cap, while "liability shall not be capped"
confirms the opposite. A clean negation ("no liquidated damages shall be payable") is benign; a
carve-out ("except as set out in Exhibit C") downgrades a confirming match to ambiguous, because
the clause may live in text this reviewer cannot see.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from policies import MUST_HAVE, MUST_NOT_HAVE, NEEDS_REVIEW, POLICIES

MAX_SPANS_PER_FINDING = 3
MAX_SPAN_CHARS = 400
_EXCLUDE_WINDOW = 48
_SENTENCE_BREAK = ".;\n"

# Reasons recorded for unresolved cases. Codes only -- never document text.
AMBIGUOUS_ONLY = "ambiguous_only"
SPAN_UNVERIFIABLE = "span_unverifiable"
CARVE_OUT = "carve_out"


def rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


@dataclass(frozen=True)
class Pattern:
    regex: re.Pattern[str]
    # Sentence-scope: the category is explicitly negated -> the match is benign, drop it.
    benign_if: tuple[re.Pattern[str], ...] = ()
    # Sentence-scope: a carve-out keeps the clause possible -> downgrade to ambiguous.
    ambiguous_if: tuple[re.Pattern[str], ...] = ()
    # Local-window: benign idiom that happens to use the same word -> drop it.
    excludes: tuple[re.Pattern[str], ...] = ()


def P(pattern: str, **kwargs: object) -> Pattern:
    return Pattern(rx(pattern), **kwargs)  # type: ignore[arg-type]


# A carve-out that can hide a clause in text this reviewer does not have.
CARVE_OUT_GUARDS: tuple[re.Pattern[str], ...] = (
    rx(r"\bexcept\s+(?:as\s+)?(?:set\s+(?:forth|out)|provided|specified|described)"),
    rx(r"\bexcept\s+(?:in|for|as\s+to)\s+(?:the\s+)?(?:exhibit|schedule|annex|appendix|addendum|order\s+form|sow)"),
    rx(r"\b(?:save|other\s+than)\s+as\s+(?:set\s+(?:forth|out)|provided|specified)"),
    rx(r"\bunless\s+otherwise\s+(?:agreed|specified|provided|set\s+(?:forth|out))"),
    rx(r"\bsubject\s+to\s+(?:the\s+)?(?:exhibit|schedule|annex|appendix|addendum|order\s+form|sow)\b"),
)

# ---------------------------------------------------------------------------
# Pattern-specific negation guards. Each references its own category's terms: there is no blanket
# "not" test, because "liability shall not exceed" confirms a cap while "liability shall not be
# capped" confirms the opposite. For a required clause these matter twice over -- without them
# "Buyer shall have no right to audit" would satisfy the Audit Rights requirement, which is the
# false-clean class this design exists to prevent.
# ---------------------------------------------------------------------------

_NEG_CAP = (
    rx(r"liabilit\w+[^.;\n]{0,40}(?:shall|will|is|are)\s+not\s+(?:be\s+)?(?:capped|limited)"),
    rx(r"\bno\s+(?:cap|limit|limitation)s?\s+on\s+[^.;\n]{0,30}liabilit"),
    rx(r"\bno\s+limitation\s+of\s+liability\s+(?:applies|shall\s+apply)"),
    rx(r"liabilit\w+[^.;\n]{0,40}not\s+subject\s+to\s+(?:any\s+)?(?:monetary\s+)?(?:cap|limit)"),
    rx(r"(?:liability\s+cap|cap\s+on\s+liability|limitation\s+of\s+liability|limitations?\s+in\s+this\s+section)"
       r"[^.;\n]{0,40}(?:shall|will|does|do)\s+not\s+(?:apply|be\s+(?:effective|enforceable))"),
)
_NEG_AUDIT = (
    rx(r"\bno\s+(?:right|rights)\s+to\s+(?:audit|inspect|examine)"),
    rx(r"(?:shall|will)\s+have\s+no\s+(?:right|rights)\s+(?:to\s+)?(?:audit|inspect|examine|access|review)"),
    rx(r"\bno\s+audit\s+rights?"),
    rx(r"(?:shall|will|is|are)\s+not\s+(?:be\s+)?(?:entitled|permitted|required|obliged)\s+to\s+(?:audit|inspect|examine|permit)"),
    rx(r"(?:shall|will)\s+not\s+(?:audit|inspect|permit|allow|grant|provide|give|afford)"),
    rx(r"audit\s+(?:rights?|and\s+inspection\s+rights?)[^.;\n]{0,40}(?:shall|will|are|is)\s+not\b"),
    rx(r"right(?:s)?\s+(?:to|of)\s+(?:audit|inspection|inspect)[^.;\n]{0,40}(?:shall|will|is|are)\s+not\b"),
)
_NEG_INSURANCE = (
    rx(r"(?:shall|will|is|are)\s+not\s+(?:be\s+)?(?:required|obliged|obligated)\s+to\s+(?:maintain|carry|procure|obtain)"),
    rx(r"\bno\s+insurance\s+(?:is\s+)?(?:required|shall\s+be\s+(?:required|maintained))"),
    rx(r"(?:shall|will)\s+not\s+(?:maintain|carry|procure|obtain)[^.;\n]{0,30}insurance"),
    rx(r"(?:shall|will|is|are)\s+not\s+(?:be\s+)?insured\b"),
)
# "... law" that names no forum. rx() applies IGNORECASE to every pattern in this module, so
# capitalisation cannot be used to recognise a jurisdiction. Instead the slot in front of "law" is
# blocked when it holds a vacuous qualifier ("applicable law") or a body of subject-matter law
# ("export law"): neither chooses a governing forum, so neither establishes the clause.
_VACUOUS_LAW = (
    r"(?:applicable|local|mandatory|relevant|governing|any|such|other|the"
    r"|data|privacy|protection|export|tax|employment|labou?r|competition|antitrust|consumer"
    r"|securities|copyright|patent|trademark|common|case|statutory|anti-?bribery"
    r"|anti-?corruption|sanctions?|environmental|immigration|health|safety)\b"
)

# Complying with a jurisdiction's law, or performing services under it, is not choosing it as the
# law that governs the agreement.
_BENIGN_COMPLY_WITH_LAW = (
    rx(r"\bcompl(?:y|ies|ied|iance)\b"),
    rx(r"\b(?:performed|provided|delivered|rendered)\s+in\s+accordance\s+with"),
)

_NEG_GOVERNING_LAW = (
    rx(r"\bnot\s+govern(?:ed)?\s+by"),
    rx(r"\bno\s+(?:governing|choice\s+of)\s+law"),
    rx(r"governing\s+law[^.;\n]{0,40}(?:is|shall\s+be)\s+(?:not\s+specified|to\s+be\s+(?:agreed|determined)|undetermined)"),
)
_NEG_LICENSE_GRANT = (
    rx(r"\bno\s+licen[cs]e\s+(?:is|shall\s+be|are)\s+(?:hereby\s+)?granted"),
    rx(r"grants?\s+no\s+licen[cs]e"),
    rx(r"\bno\s+licen[cs]e\s+(?:or|nor)\b"),
    rx(r"(?:shall|will)\s+not\s+(?:be\s+)?(?:grant|granted|deemed\s+to\s+grant)[^.;\n]{0,30}licen[cs]e"),
)
_NEG_IP_ASSIGNMENT = (
    rx(r"\bassigns?\s+(?:nothing|none|no\s+)"),
    rx(r"retains?\s+all\s+(?:right|ownership|title)"),
    rx(r"\bno\s+(?:assignment|transfer)\s+of\s+(?:intellectual\s+property|ip\b|ownership|right)"),
    rx(r"(?:shall|will)\s+not\s+(?:be\s+)?(?:assign|assigned|transferred|vest)"),
)

# ---------------------------------------------------------------------------
# must_have categories
# ---------------------------------------------------------------------------

CONFIRMING: dict[str, tuple[Pattern, ...]] = {
    "Cap On Liability": (
        P(r"(?:aggregate|total|cumulative|maximum|entire)\s+liabilit\w+[^.;\n]{0,80}not\s+exceed",
          ambiguous_if=CARVE_OUT_GUARDS),
        P(r"liabilit\w+[^.;\n]{0,60}(?:shall|will|may)\s+not\s+exceed", ambiguous_if=CARVE_OUT_GUARDS),
        P(r"liabilit\w+\s+(?:shall|will|is|are)\s+(?:be\s+)?(?:limited|capped)\b",
          ambiguous_if=CARVE_OUT_GUARDS),
        P(r"liability\s+cap\s+(?:of|is|shall|equal)"),
        P(r"cap(?:ped)?\s+at\s+(?:the\s+)?(?:aggregate|total|amounts?|fees?|\$|[0-9])"),
        P(r"(?:in\s+no\s+event|under\s+no\s+circumstances)[^.;\n]{0,80}liabilit\w+[^.;\n]{0,40}exceed"),
        P(r"maximum\s+(?:aggregate\s+)?liability\s+(?:of|is|shall\s+be|under)"),
        P(r"(?:damages|exposure)[^.;\n]{0,60}(?:shall|will)\s+(?:not\s+exceed|be\s+limited\s+to)"),
        P(r"limitation\s+of\s+liability[^.;\n]{0,80}(?:not\s+exceed|limited\s+to|capped)"),
        # "Liability under this Section is restricted to direct damages not exceeding $100,000."
        P(r"liabilit\w+[^.;\n]{0,40}(?:shall|will|is|are)\s+(?:be\s+)?(?:limited|capped|restricted)\s+to\b",
          ambiguous_if=CARVE_OUT_GUARDS),
        # "The parties' liability is subject to a cap equal to the annual charges." The mirror
        # image ("not subject to any cap") is held off by _NEG_CAP, which every must_have
        # confirming pattern is checked against.
        P(r"liabilit\w+[^.;\n]{0,40}subject\s+to\s+(?:an?\s+)?(?:aggregate\s+|overall\s+|monetary\s+)?(?:cap|limit|maximum)\b",
          ambiguous_if=CARVE_OUT_GUARDS),
        # "Neither party shall be liable for more than the fees paid hereunder." The ceiling is on
        # the verb, not the noun, so the liabilit\w+ patterns above never see it.
        P(r"(?:neither\s+party|no\s+party|in\s+no\s+event|under\s+no\s+circumstances|not\s+be)"
          r"[^.;\n]{0,40}\bliable\s+for\s+"
          r"(?:(?:no|not)\s+more\s+than|more\s+than|(?:any\s+)?amounts?\s+in\s+excess\s+of|in\s+excess\s+of)",
          ambiguous_if=CARVE_OUT_GUARDS),
    ),
    "Audit Rights": (
        P(r"right(?:s)?\s+to\s+(?:audit|inspect)\b"),
        P(r"(?:shall|may|will)\s+(?:have\s+the\s+right\s+to\s+)?audit\b"),
        P(r"(?:may|shall\s+be\s+(?:entitled|permitted)\s+to)\s+(?:audit|inspect|examine)\b"),
        P(r"(?:audit|inspect|examine|review)[^.;\n]{0,40}(?:books|records|accounts|facilities|invoices)"),
        P(r"books\s+and\s+records[^.;\n]{0,60}(?:available|open|inspect|audit|examin)"),
        P(r"(?:permit|allow)[^.;\n]{0,40}(?:to\s+)?(?:audit|inspect|examine)"),
        P(r"right\s+of\s+(?:audit|inspection|access\s+to\s+(?:books|records))"),
        P(r"audit\s+rights?\s+(?:shall|are|include|survive|under)"),
        # "Buyer may conduct an audit of Vendor's compliance once per year." The head is required
        # to be a right-conferring one, so a vendor self-audit obligation ("Vendor shall conduct an
        # annual SOC 2 audit") does not satisfy the requirement.
        P(r"(?:may|shall\s+have\s+the\s+right\s+to|(?:is|are)\s+entitled\s+to|right\s+to)"
          r"\s+(?:\w+\s+){0,2}(?:conduct|perform|carry\s+out|initiate|commission)"
          r"\s+(?:an?\s+|any\s+)?(?:\w+\s+){0,2}audit\b"),
        # "Vendor shall grant Buyer access to its records relating to the Services." Restricted to
        # audit objects: access to documentation or software is not an audit right.
        P(r"(?:grant|provide|give|allow|afford)\w*\s+[^.;\n]{0,30}access\s+to"
          r"[^.;\n]{0,30}(?:books|records|accounts|invoices)"),
    ),
    "Insurance": (
        P(r"(?:shall|will|must)\s+(?:at\s+all\s+times\s+)?(?:maintain|carry|procure|obtain|keep\s+in\s+(?:full\s+)?force)[^.;\n]{0,60}insurance"),
        P(r"insurance[^.;\n]{0,80}(?:shall|will)\s+be\s+(?:maintained|carried|in\s+place)"),
        P(r"(?:commercial\s+general|general|professional|cyber|umbrella|employers|product)\s+liability\s+insurance"),
        P(r"workers'?\s+compensation\s+(?:insurance|coverage)"),
        P(r"certificates?\s+of\s+insurance"),
        P(r"insurance\s+(?:policy|policies|coverage)\s+(?:of|with|in\s+(?:an\s+)?amounts?|not\s+less\s+than)"),
        P(r"coverage\s+(?:of|limits?\s+of|in\s+an?\s+amount)[^.;\n]{0,40}(?:\$|not\s+less\s+than|[0-9])"),
        # "Vendor shall be insured against professional liability with limits of $2,000,000."
        P(r"(?:shall|will|must)\s+(?:\w+\s+){0,3}insured\s+(?:against|for|by|with|under)\b"),
    ),
    "Governing Law": (
        # Postpositive form: "governed by the laws of the State of New York".
        P(r"govern(?:ed|s)?\s+(?:by|in\s+accordance\s+with|under)[^.;\n]{0,60}laws?\s+of"),
        P(r"construed\s+in\s+accordance\s+with\s+the\s+laws?\s+of"),
        # Adjectival form: "governed by California law" / "governed under Delaware law". The
        # jurisdiction slot accepts up to three words and is blocked only for _VACUOUS_LAW, which
        # keeps "governed by applicable law" out (it stays an ambiguous signal, and so a violation
        # of the requirement rather than a false satisfaction of it).
        P(r"govern(?:ed|s)?\s+(?:by|in\s+accordance\s+with|under)"
          r"(?:\s+and\s+construed\s+in\s+accordance\s+with)?(?:\s+the)?"
          r"\s+(?!" + _VACUOUS_LAW + r")[\w'-]+(?:\s+[\w'-]+){0,2}\s+laws?\b"),
        # "This Agreement shall be subject to the laws of Ontario": the agreement itself must be
        # the subject, so "Vendor is subject to the laws of Ireland" is not swept in.
        P(r"(?:this\s+(?:agreement|contract|msa|sow|order)|these\s+terms)"
          r"[^.;\n]{0,40}subject\s+to\s+(?:the\s+)?laws?\s+of\s+\w",
          benign_if=_BENIGN_COMPLY_WITH_LAW),
        P(r"laws?\s+of\s+the\s+(?:state|commonwealth|province|republic)\s+of\s+[A-Z\w]",
          benign_if=_BENIGN_COMPLY_WITH_LAW),
        P(r"governing\s+law[^.;\n]{0,40}(?:shall\s+be|is)\s+[^.;\n]{0,40}law"),
        # There is deliberately no bare "<this agreement> ... governed by" pattern: it establishes
        # nothing on its own ("governed by the terms of the MSA" chooses no forum), and requiring
        # only that a law word follow somewhere in the sentence would re-admit "governed by
        # applicable law". The two forms above carry every genuine wording of this clause.
        P(r"choice\s+of\s+law[^.;\n]{0,40}(?:shall\s+be|is)\b"),
    ),
    "License Grant": (
        P(r"(?:hereby\s+)?grants?[^.;\n]{0,80}licen[cs]e"),
        P(r"licen[cs]e\s+is\s+(?:hereby\s+)?granted"),
        P(r"grants?\s+to\s+[^.;\n]{0,60}right\s+to\s+(?:use|reproduce|distribute|modify|display)"),
        P(r"(?:non-?exclusive|exclusive|worldwide|royalty-?free|limited)[^.;\n]{0,40}licen[cs]e\s+to\b"),
        P(r"licensor\s+grants\b"),
        P(r"granted\s+a\s+licen[cs]e\b"),
        # "Vendor licenses the Platform to Buyer for internal business use." A licence out to third
        # parties is background, not a grant to the counterparty, so that recipient is excluded.
        P(r"licenses\s+[^.;\n]{0,50}\bto\s+(?!any\s+third|third\s+part)",
          benign_if=_NEG_LICENSE_GRANT),
    ),
    "Ip Ownership Assignment": (
        P(r"(?:hereby\s+)?assigns?[^.;\n]{0,80}(?:intellectual\s+property|inventions?|works?|copyrights?|patents?|deliverables?)"),
        P(r"assigns?\s+and\s+transfers?\s+to\b"),
        P(r"right,?\s+title,?\s+and\s+interest[^.;\n]{0,80}(?:vest|assign|belong|transfer|shall\s+be)"),
        P(r"(?:shall|will)\s+(?:be\s+)?(?:the\s+)?(?:sole|exclusive)\s+(?:and\s+exclusive\s+)?(?:owner|property)\s+of"),
        P(r"works?\s+made\s+for\s+hire"),
        P(r"ownership\s+of[^.;\n]{0,60}(?:vests?|shall\s+vest|transfers?|is\s+assigned|shall\s+belong)"),
        P(r"(?:all\s+)?(?:intellectual\s+property|ip\s+rights?)[^.;\n]{0,60}(?:shall\s+(?:be|vest|belong)|are\s+owned\s+by)"),
    ),
}

# ---------------------------------------------------------------------------
# must_not_have categories
# ---------------------------------------------------------------------------

_NEG_UNCAPPED = (
    rx(r"\bno\s+(?:party\s+)?(?:shall\s+have\s+)?unlimited\s+liabilit"),
    rx(r"unlimited\s+liabilit\w+[^.;\n]{0,40}(?:shall|will|does)\s+not\s+(?:apply|arise)"),
    rx(r"(?:in\s+no\s+event|under\s+no\s+circumstances)[^.;\n]{0,60}unlimited"),
    rx(r"(?:not|never)\s+(?:have|be\s+subject\s+to|bear)\s+unlimited"),
)
_NEG_LIQUIDATED = (
    rx(r"\bno\s+liquidated\s+damages"),
    rx(r"liquidated\s+damages[^.;\n]{0,40}(?:shall|will|are|is)\s+not\s+(?:apply|be\s+(?:payable|due|owed|assessed|applicable))"),
    rx(r"(?:waives?|waived|waiver\s+of)[^.;\n]{0,30}liquidated\s+damages"),
    rx(r"without\s+(?:any\s+)?liquidated\s+damages"),
    rx(r"not\s+(?:as\s+)?(?:a\s+)?liquidated\s+damages?\s+(?:clause|provision)"),
)
_NEG_MFN = (
    rx(r"\bno\s+most\s+favo(?:u)?r"),
    rx(r"\bno\s+mfn\b"),
    rx(r"most\s+favo\w+[^.;\n]{0,40}(?:shall|will|does)\s+not\s+apply"),
    rx(r"without\s+(?:any\s+)?most\s+favo"),
)
_NEG_NONCOMPETE = (
    rx(r"\bno\s+non-?compet"),
    rx(r"non-?compet\w*[^.;\n]{0,40}(?:shall|will|does)\s+not\s+apply"),
    rx(r"without\s+(?:any\s+)?non-?compet"),
    rx(r"free\s+to\s+compete\b"),
    rx(r"nothing[^.;\n]{0,40}restrict\w*[^.;\n]{0,40}compet"),
)
_NEG_EXCLUSIVITY = (
    rx(r"\bno\s+exclusivity\b"),
    rx(r"exclusivity[^.;\n]{0,40}(?:shall|will|does)\s+not\s+apply"),
    rx(r"non-?exclusive\s+(?:supplier|provider|vendor|distributor|basis|arrangement|relationship)"),
    rx(r"without\s+(?:any\s+)?exclusivity"),
    rx(r"\bnot\s+(?:an?\s+)?exclusive\s+(?:supplier|provider|vendor|distributor|source)"),
)
_NEG_MINIMUM = (
    rx(r"\bno\s+minimum\s+(?:commitment|purchase|order|volume|spend|quantit)"),
    rx(r"minimum\s+(?:commitment|purchase|order|volume|spend)\w*[^.;\n]{0,40}(?:shall|will|does|is|are)\s+not\s+(?:apply|required)"),
    rx(r"without\s+(?:any\s+)?minimum\s+(?:commitment|purchase|order|volume|spend)"),
    rx(r"not\s+(?:obligated|required)\s+to\s+(?:purchase|buy|order)\s+any\s+minimum"),
)
_NEG_JOINT_IP = (
    rx(r"\bno\s+(?:joint|co-?)\s*own"),
    rx(r"joint(?:ly)?\s+own\w*[^.;\n]{0,40}(?:shall|will|is|are)\s+not"),
    rx(r"(?:shall|will)\s+not\s+be\s+(?:jointly\s+owned|co-?owned)"),
    rx(r"\bno\s+joint\s+(?:intellectual\s+property|ip\b|inventions?)"),
)
_NEG_PERPETUAL = (
    rx(r"\bno\s+(?:irrevocable|perpetual)\s+(?:licen|right)"),
    rx(r"licen[cs]e[^.;\n]{0,40}(?:shall|will|is)\s+not\s+(?:be\s+)?(?:irrevocable|perpetual)"),
    # The lookbehind is load-bearing: without it this guard matches inside "irrevocable
    # licence" and "non-revocable licence" -- both of which are the violation, not a negation.
    rx(r"(?<![A-Za-z-])(?:revocable|terminable)\s+licen[cs]e"),
    rx(r"licen[cs]e[^.;\n]{0,40}(?:may\s+be|is)\s+(?:revoked|terminated|revocable)"),
)

CONFIRMING.update({
    "Uncapped Liability": (
        P(r"unlimited\s+liabilit\w+", benign_if=_NEG_UNCAPPED),
        P(r"liabilit\w+[^.;\n]{0,40}(?:shall|will|is|are)\s+(?:be\s+)?unlimited", benign_if=_NEG_UNCAPPED),
        P(r"(?:no|without\s+any?|there\s+is\s+no)\s+(?:cap|limit|limitation)s?\s+on\s+[^.;\n]{0,30}liabilit\w+"),
        P(r"liabilit\w+[^.;\n]{0,40}(?:shall|will|is|are)\s+not\s+(?:be\s+)?(?:capped|limited|subject\s+to\s+(?:any\s+)?(?:cap|limit))"),
        P(r"uncapped\s+(?:\w+\s+){0,2}(?:liabilit\w+|damages|indemnit\w+|indemnification|exposure)"),
        P(r"without\s+(?:any\s+)?(?:monetary\s+)?(?:cap|limit)\s+(?:as\s+to\s+(?:the\s+)?amount|on\s+(?:the\s+)?amount)"),
        P(r"liabilit\w+[^.;\n]{0,30}not\s+subject\s+to\s+(?:any\s+)?(?:monetary\s+)?(?:cap|limit)"),
        # "Vendor's indemnity obligations are uncapped." The liability noun is required: "support
        # obligations are uncapped" is about scope, not money, and stays an ambiguous signal.
        P(r"(?:liabilit\w+|damages|indemnit\w+|indemnification|exposure)"
          r"[^.;\n]{0,40}(?:is|are|shall\s+be|will\s+be)\s+uncapped", benign_if=_NEG_UNCAPPED),
    ),
    "Liquidated Damages": (
        P(r"liquidated\s+damages", benign_if=_NEG_LIQUIDATED, ambiguous_if=CARVE_OUT_GUARDS),
        P(r"(?:pay|owe|forfeit)[^.;\n]{0,40}\$\s?[0-9][^.;\n]{0,40}per\s+(?:day|week|month)",
          ambiguous_if=CARVE_OUT_GUARDS),
        P(r"penalt(?:y|ies)\s+of\s+(?:\$\s?)?[0-9]", benign_if=_NEG_LIQUIDATED),
        P(r"agreed\s+damages\s+(?:of|in\s+the\s+amount)", benign_if=_NEG_LIQUIDATED),
        P(r"(?:stipulated|pre-?(?:agreed|determined))\s+damages", benign_if=_NEG_LIQUIDATED),
    ),
    "Most Favored Nation": (
        P(r"most\s+favo(?:u)?red\s+(?:nation|customer|pricing|terms|status)", benign_if=_NEG_MFN),
        P(r"\bmfn\b", benign_if=_NEG_MFN),
        P(r"(?:no|not)\s+less\s+favo(?:u)?rable\s+(?:than|terms|prices?|pricing|rates?)", benign_if=_NEG_MFN),
        P(r"(?:price|pricing|rates?|terms|discount)\w*[^.;\n]{0,60}at\s+least\s+as\s+favo(?:u)?rable", benign_if=_NEG_MFN),
        P(r"lowest\s+(?:price|rate)s?[^.;\n]{0,50}(?:offered|charged|made\s+available)\s+to\s+any", benign_if=_NEG_MFN),
        P(r"best\s+(?:price|pricing|rate|terms)s?[^.;\n]{0,40}(?:offered|given|provided|available)\s+to\s+any", benign_if=_NEG_MFN),
        P(r"match\s+(?:any\s+)?lower\s+(?:price|rate)", benign_if=_NEG_MFN),
        # "Vendor represents that no other customer receives better pricing than Buyer."
        P(r"\bno\s+other\s+(?:customer|client|purchaser|buyer|licensee)s?\s+[^.;\n]{0,40}"
          r"(?:better|lower|more\s+favo(?:u)?rable)\s+(?:price|pricing|rate|term|discount)",
          benign_if=_NEG_MFN),
        P(r"(?:shall|will)\s+not\s+(?:offer|provide|charge|grant)[^.;\n]{0,40}"
          r"(?:lower|better|more\s+favo(?:u)?rable)\s+(?:price|pricing|rate|term|discount)\w*"
          r"[^.;\n]{0,40}to\s+(?:any\s+)?other", benign_if=_NEG_MFN),
    ),
    "Non-Compete": (
        P(r"non-?compet(?:e|ition)\b", benign_if=_NEG_NONCOMPETE, ambiguous_if=CARVE_OUT_GUARDS),
        P(r"(?:shall|will|agrees?\s+to|covenants?\s+to)\s+not\s+(?:directly\s+or\s+indirectly\s+)?compet",
          benign_if=_NEG_NONCOMPETE),
        P(r"(?:shall|will)\s+not[^.;\n]{0,60}(?:compete\s+with|competing\s+(?:products?|services?|business|offerings?))",
          benign_if=_NEG_NONCOMPETE),
        P(r"restrict\w*[^.;\n]{0,40}from\s+(?:competing|engaging\s+in\s+(?:any\s+)?compet)",
          benign_if=_NEG_NONCOMPETE),
        P(r"(?:shall|will)\s+not\s+(?:develop|offer|sell|market)[^.;\n]{0,40}competing",
          benign_if=_NEG_NONCOMPETE),
    ),
    "Exclusivity": (
        P(r"(?<!non-)(?<!non )exclusive\s+(?:supplier|provider|vendor|distributor|reseller|source|partner|agent|manufacturer)",
          benign_if=_NEG_EXCLUSIVITY, ambiguous_if=CARVE_OUT_GUARDS),
        P(r"exclusivity\s+(?:period|obligation|provision|clause|term|arrangement|commitment)",
          benign_if=_NEG_EXCLUSIVITY, ambiguous_if=CARVE_OUT_GUARDS),
        P(r"(?:shall|will)\s+(?:purchase|procure|source|obtain|buy)\s+(?:all|exclusively)\b",
          benign_if=_NEG_EXCLUSIVITY),
        P(r"(?:appoints?|designates?|engages?)[^.;\n]{0,40}as\s+(?:its\s+)?(?<!non-)(?<!non )exclusive",
          benign_if=_NEG_EXCLUSIVITY),
        P(r"(?:shall|will)\s+not\s+(?:purchase|source|obtain|buy)[^.;\n]{0,40}from\s+(?:any\s+)?(?:other|third)",
          benign_if=_NEG_EXCLUSIVITY),
        P(r"exclusively\s+(?:from|through)\b", benign_if=_NEG_EXCLUSIVITY),
        P(r"(?:sole\s+and\s+exclusive|exclusive)\s+(?:right\s+to\s+(?:supply|distribute|resell)|supply\s+arrangement)",
          benign_if=_NEG_EXCLUSIVITY),
        # "Vendor is granted exclusivity for the initial 24-month period." -- no noun follows
        # "exclusivity", so the exclusivity_period pattern above never fires.
        P(r"(?:granted|grants?|receives?|retains?)\s+(?:\w+\s+){0,2}exclusivity\b",
          benign_if=_NEG_EXCLUSIVITY, ambiguous_if=CARVE_OUT_GUARDS),
    ),
    "Minimum Commitment": (
        P(r"minimum\s+(?:annual\s+|monthly\s+|quarterly\s+)?(?:purchase\s+)?(?:commitment|quantit\w+|volume|order|spend|revenue)",
          benign_if=_NEG_MINIMUM, ambiguous_if=CARVE_OUT_GUARDS),
        P(r"(?:shall|will|agrees?\s+to)\s+(?:purchase|buy|order|spend)\s+(?:at\s+least|a\s+minimum\s+of|no\s+(?:less|fewer)\s+than)",
          benign_if=_NEG_MINIMUM),
        P(r"commits?\s+to\s+(?:purchase|buy|spend|order)\b", benign_if=_NEG_MINIMUM),
        P(r"take-?or-?pay"),
        P(r"guaranteed\s+(?:minimum|annual)\s+(?:volume|spend|purchase|revenue|quantit\w+)", benign_if=_NEG_MINIMUM),
        P(r"annual\s+minimum\s+(?:of|commitment|purchase|spend)", benign_if=_NEG_MINIMUM),
    ),
    "Joint Ip Ownership": (
        P(r"joint(?:ly)?\s+own\w*", benign_if=_NEG_JOINT_IP, ambiguous_if=CARVE_OUT_GUARDS),
        P(r"own(?:ed)?\s+jointly", benign_if=_NEG_JOINT_IP),
        P(r"co-?own\w*", benign_if=_NEG_JOINT_IP),
        P(r"joint\s+(?:intellectual\s+property|ip\b|inventions?|patents?)", benign_if=_NEG_JOINT_IP),
        P(r"(?:each\s+party|both\s+parties)[^.;\n]{0,60}own[^.;\n]{0,40}(?:undivided|equal|50)", benign_if=_NEG_JOINT_IP),
        P(r"undivided\s+(?:one-?half|50\s?%|equal)\s+(?:interest|share)", benign_if=_NEG_JOINT_IP),
    ),
    "Irrevocable Or Perpetual License": (
        P(r"(?:irrevocable|perpetual)[^.;\n]{0,40}licen[cs]e", benign_if=_NEG_PERPETUAL, ambiguous_if=CARVE_OUT_GUARDS),
        P(r"licen[cs]e[^.;\n]{0,40}(?:is|shall\s+be)[^.;\n]{0,30}(?:irrevocable|perpetual)", benign_if=_NEG_PERPETUAL),
        P(r"(?:perpetual|irrevocable)(?:,?\s+\w+){0,3}\s+right\s+to\s+(?:use|reproduce|distribute|sublicen[cs]e)",
          benign_if=_NEG_PERPETUAL),
        P(r"licen[cs]e[^.;\n]{0,40}in\s+perpetuity", benign_if=_NEG_PERPETUAL),
        P(r"(?:non-?terminable|non-?revocable|cannot\s+be\s+(?:revoked|terminated))[^.;\n]{0,40}licen[cs]e", benign_if=_NEG_PERPETUAL),
    ),
})

# ---------------------------------------------------------------------------
# needs_review categories (routing only -- never a fault)
# ---------------------------------------------------------------------------

CONFIRMING.update({
    "Covenant Not To Sue": (
        P(r"covenants?\s+not\s+to\s+sue"),
        P(r"(?:agrees?|covenants?)\s+not\s+to\s+(?:sue|bring\s+(?:any\s+)?(?:claim|action|suit|proceeding)|institute\s+(?:any\s+)?(?:proceeding|action))"),
        P(r"(?:waives?|releases?)[^.;\n]{0,40}right\s+to\s+(?:sue|bring\s+(?:any\s+)?(?:claim|action))"),
        P(r"forbear\w*\s+from\s+(?:suing|bringing\s+(?:any\s+)?(?:claim|action))"),
    ),
    "Change Of Control": (
        P(r"change\s+(?:of|in)\s+control"),
        P(r"(?:merger|acquisition|consolidation)[^.;\n]{0,80}(?:terminate|assign|consent|notify)"),
        P(r"(?:direct|indirect)\s+(?:change|transfer)\s+of\s+(?:ownership|control)"),
        P(r"acquir\w+\s+by\s+(?:a\s+)?competitor"),
        P(r"(?:sale|transfer)\s+of\s+(?:all\s+or\s+substantially\s+all|a\s+majority\s+of\s+(?:the\s+)?(?:voting|equity|shares|stock))"),
        # "Either party may terminate on the acquisition of the other by a competitor." The
        # consequence can precede the event, which the merger-then-consequence pattern cannot see.
        P(r"(?:terminate|termination|assign|assignment|consent)[^.;\n]{0,60}"
          r"(?:change\s+(?:of|in)\s+control|merger|acquisition|consolidation|takeover|acquir\w+\s+by)"),
    ),
    "Volume Restriction": (
        P(r"(?:volume|quantity|usage|seat)\s+(?:restriction|limit|cap|threshold|allowance|ceiling)s?"),
        P(r"(?:shall|may)\s+not\s+(?:exceed|purchase\s+more\s+than)[^.;\n]{0,40}(?:units|licen[cs]es|seats|volume|quantit\w+|hours)"),
        P(r"maximum\s+(?:volume|quantity|number\s+of\s+(?:units|seats|licen[cs]es|users))"),
        P(r"(?:capped|limited)\s+(?:at|to)\s+[0-9][0-9,]*\s+(?:units|seats|licen[cs]es|hours|users)"),
        P(r"tiered\s+volume"),
    ),
    "Source Code Escrow": (
        P(r"source\s+code\s+escrow"),
        P(r"escrow[^.;\n]{0,60}source\s+code"),
        P(r"(?:deposit|place|lodge)[^.;\n]{0,40}source\s+code[^.;\n]{0,40}escrow"),
        P(r"release\s+(?:of\s+)?(?:the\s+)?source\s+code\s+(?:upon|if|in\s+the\s+event)"),
    ),
})

# ---------------------------------------------------------------------------
# Ambiguous signals. For must_have these produce a violation (recall-leaning), so they may be
# generous. For must_not_have they produce UNRESOLVED -> a 422 on an otherwise-clean document,
# so they are deliberately narrow and exclude common boilerplate ("including without
# limitation", "sole and exclusive remedy", "irrevocably waives", "exclusive jurisdiction").
# ---------------------------------------------------------------------------

AMBIGUOUS: dict[str, tuple[Pattern, ...]] = {
    "Cap On Liability": (
        P(r"(?m)^[ \t]*(?:[0-9.]+[ \t]*)?limitations?\s+of\s+liability[ \t]*:?[ \t]*$"),
        P(r"(?m)^[ \t]*(?:[0-9.]+[ \t]*)?liability[ \t]*:?[ \t]*$"),
        P(r"subject\s+to\s+the\s+(?:limitations|liability\s+provisions)"),
        P(r"liability\s+(?:provisions?|section|clause)"),
    ),
    "Audit Rights": (
        P(r"(?m)^[ \t]*(?:[0-9.]+[ \t]*)?audit(?:\s+rights?)?[ \t]*:?[ \t]*$"),
        P(r"audited\s+financial\s+statements"),
        P(r"\baudit(?:or|ors|ing)\b"),
        P(r"\brecords\b"),
    ),
    "Insurance": (
        P(r"(?m)^[ \t]*(?:[0-9.]+[ \t]*)?insurance[ \t]*:?[ \t]*$"),
        P(r"\binsur(?:ance|ed|er|ers)\b"),
        P(r"\bcoverage\b"),
    ),
    "Governing Law": (
        P(r"(?m)^[ \t]*(?:[0-9.]+[ \t]*)?governing\s+law[ \t]*:?[ \t]*$"),
        P(r"\bjurisdiction\b"),
        P(r"applicable\s+law"),
        P(r"\bvenue\b"),
    ),
    "License Grant": (
        P(r"(?m)^[ \t]*(?:[0-9.]+[ \t]*)?licen[cs]e[ \t]*:?[ \t]*$"),
        P(r"\blicen[cs]e[ds]?\b"),
        P(r"\blicensee?\b"),
    ),
    "Ip Ownership Assignment": (
        P(r"(?m)^[ \t]*(?:[0-9.]+[ \t]*)?intellectual\s+property[ \t]*:?[ \t]*$"),
        P(r"\bintellectual\s+property\b"),
        P(r"\bownership\b"),
        P(r"\bwork\s+product\b"),
    ),
    "Uncapped Liability": (
        P(r"\buncapped\b"),
        P(r"\bno\s+(?:monetary\s+)?(?:cap|ceiling)\b",
          excludes=(rx(r"volume|quantity|usage|seat|user|unit|headcount"),)),
    ),
    "Liquidated Damages": (
        P(r"\bpenalt(?:y|ies)\b", excludes=(rx(r"tax|statutor|regulator|criminal|interest\s+penalt"),)),
        P(r"damages\s+for\s+(?:delay|late\s+delivery)"),
    ),
    "Most Favored Nation": (
        P(r"most\s+favo(?:u)?r(?:able|ed)\b"),
        P(r"benchmark\w*\s+(?:pricing|price|rates?)"),
    ),
    "Non-Compete": (
        P(r"\bcompetitors?\b", excludes=(rx(r"competitive\s+(?:pricing|rates?|bid|market|tender)"),)),
        P(r"(?:shall|will)\s+not\s+(?:solicit|hire)[^.;\n]{0,40}(?:employees?|personnel|customers?)"),
        P(r"restrictive\s+covenants?"),
    ),
    "Exclusivity": (
        P(r"(?<!non-)(?<!non )\bexclusiv(?:e|ity|ely)\b",
          excludes=(rx(r"exclusiv\w*\s+(?:remedy|remedies|jurisdiction|venue|forum|of\s+(?:tax|vat)|licen[cs]e|right\s+to\s+use|discretion|control|use\s+of)"),
                    rx(r"(?:sole\s+and\s+exclusive|non-?exclusive)"),)),
    ),
    "Minimum Commitment": (
        P(r"\bminimum\b",
          excludes=(rx(r"minimum\s+(?:coverage|insurance|notice|wage|standards?|requirements?|age|term|balance)"),
                    rx(r"at\s+a\s+minimum"),)),
        P(r"\bforecast(?:s|ed|ing)?\b"),
    ),
    "Joint Ip Ownership": (
        P(r"joint(?:ly)?\s+(?:develop\w*|creat\w*|prepar\w*|author\w*)"),
        P(r"joint\s+(?:development|deliverables?|works?)"),
        P(r"\bjointly\b"),
    ),
    "Irrevocable Or Perpetual License": (
        P(r"\birrevocabl[ey]\b",
          excludes=(rx(r"irrevocabl\w*\s+(?:waives?|waiver|submits?|consents?|appoints?|agrees?)"),
                    rx(r"irrevocable\s+letter\s+of\s+credit"),)),
        P(r"\bperpetual(?:ly)?\b", excludes=(rx(r"perpetual\s+inventory"),)),
        P(r"\bin\s+perpetuity\b"),
    ),
    # needs_review ambiguity is not a fault and is never allowed to block a review; see resolve().
    "Covenant Not To Sue": (
        P(r"\brelease\s+of\s+claims\b"),
        P(r"\bwaiver\s+of\s+claims\b"),
    ),
    "Change Of Control": (
        P(r"\b(?:merger|acquisition|consolidation)\b"),
        P(r"\bassignment\b"),
    ),
    "Volume Restriction": (
        P(r"\bvolume\b"),
        P(r"\bthroughput\b"),
    ),
    "Source Code Escrow": (
        P(r"\bescrow\b"),
        P(r"\bsource\s+code\b"),
    ),
}


# ---------------------------------------------------------------------------
# Clean-negation guards per category. Each bundle references its own category's vocabulary, so
# this is never a blanket "not" test. Consulted by scan() in two places:
#
#   * a must_have confirming match -- "Buyer shall have no right to audit" must not satisfy the
#     Audit Rights requirement, which is the false-clean class correction 5 exists to prevent;
#   * any ambiguous match -- "No most favored nation pricing applies" is a clean negation, and
#     must not leave a must_not_have category unresolved (and so 422 an otherwise clean document).
#
# must_not_have *confirming* matches deliberately keep their own per-pattern benign_if instead:
# there, suppression means "benign", so each guard is attached only to the wording it was written
# for rather than to every pattern in the category.
# ---------------------------------------------------------------------------

CLEAN_NEGATION: dict[str, tuple[re.Pattern[str], ...]] = {
    # must_have
    "Cap On Liability": _NEG_CAP,
    "Audit Rights": _NEG_AUDIT,
    "Insurance": _NEG_INSURANCE,
    "Governing Law": _NEG_GOVERNING_LAW,
    "License Grant": _NEG_LICENSE_GRANT,
    "Ip Ownership Assignment": _NEG_IP_ASSIGNMENT,
    # must_not_have
    "Uncapped Liability": _NEG_UNCAPPED,
    "Liquidated Damages": _NEG_LIQUIDATED,
    "Most Favored Nation": _NEG_MFN,
    "Non-Compete": _NEG_NONCOMPETE,
    "Exclusivity": _NEG_EXCLUSIVITY,
    "Minimum Commitment": _NEG_MINIMUM,
    "Joint Ip Ownership": _NEG_JOINT_IP,
    "Irrevocable Or Perpetual License": _NEG_PERPETUAL,
    # needs_review categories carry no negation guards: a routing flag is not a fault, so there is
    # nothing to be wrongly satisfied or wrongly cleared.
}

# Which rule kind a category is used as, derived from the catalog so the two cannot drift.
_KIND_OF: dict[str, str] = {}
for _rules in POLICIES.values():
    for _kind in (MUST_HAVE, MUST_NOT_HAVE, NEEDS_REVIEW):
        for _category in _rules[_kind]:
            _KIND_OF[_category] = _kind


@dataclass
class Evidence:
    """What the document says about one category."""

    spans: list[str] = field(default_factory=list)
    ambiguous_terms: list[str] = field(default_factory=list)
    confirmed_but_unverifiable: int = 0
    carved_out: bool = False

    @property
    def confirmed(self) -> bool:
        return bool(self.spans)

    @property
    def has_ambiguous(self) -> bool:
        return bool(self.ambiguous_terms) or self.carved_out


def sentence_bounds(document: str, start: int, end: int) -> tuple[int, int]:
    left = start
    while left > 0 and document[left - 1] not in _SENTENCE_BREAK:
        left -= 1
    right = end
    while right < len(document) and document[right] not in _SENTENCE_BREAK:
        right += 1
    return left, right


def build_span(document: str, start: int, end: int) -> str:
    """Slice the match out to sentence bounds, capped at MAX_SPAN_CHARS.

    The result is a contiguous slice of `document` with surrounding whitespace stripped, so it
    remains an exact substring -- which the caller verifies before emitting it as evidence.
    """
    left, right = sentence_bounds(document, start, end)
    if right - left > MAX_SPAN_CHARS:
        left = max(left, start - 150)
        right = min(right, max(end + 250, left + MAX_SPAN_CHARS))
    return document[left:right].strip()


def _window(document: str, start: int, end: int) -> str:
    return document[max(0, start - _EXCLUDE_WINDOW):min(len(document), end + _EXCLUDE_WINDOW)]


def scan(document: str, category: str) -> Evidence:
    """Collect confirming spans and ambiguous signals for one category."""
    evidence = Evidence()
    seen: set[str] = set()
    negation = CLEAN_NEGATION.get(category, ())
    # A required clause must be affirmatively established, so every confirming pattern in the
    # category is checked against the category's negation guards. A prohibited clause is the other
    # way round -- suppression there means "benign" -- so those keep their per-pattern guards only.
    confirming_negation = negation if _KIND_OF.get(category) == MUST_HAVE else ()

    for pattern in CONFIRMING.get(category, ()):
        for match in pattern.regex.finditer(document):
            left, right = sentence_bounds(document, match.start(), match.end())
            sentence = document[left:right]
            if any(guard.search(sentence) for guard in pattern.benign_if + confirming_negation):
                continue  # explicitly negated: the clause is absent, not uncertain
            if any(guard.search(_window(document, match.start(), match.end())) for guard in pattern.excludes):
                continue  # benign idiom using the same words
            if any(guard.search(sentence) for guard in pattern.ambiguous_if):
                evidence.carved_out = True  # a carve-out can hide the clause elsewhere
                continue
            span = build_span(document, match.start(), match.end())
            if not span or span not in document:
                evidence.confirmed_but_unverifiable += 1
                continue
            if span in seen:
                continue
            seen.add(span)
            if len(evidence.spans) < MAX_SPANS_PER_FINDING:
                evidence.spans.append(span)

    for pattern in AMBIGUOUS.get(category, ()):
        # Every occurrence, not just the first: a negated first mention must not hide a later one.
        for match in pattern.regex.finditer(document):
            left, right = sentence_bounds(document, match.start(), match.end())
            if any(guard.search(document[left:right]) for guard in negation):
                continue  # negated here; keep looking for an occurrence that is not
            if any(guard.search(_window(document, match.start(), match.end())) for guard in pattern.excludes):
                continue
            term = match.group(0).strip()
            # Ambiguous terms are short quoted fragments of the matched vocabulary, used only in a
            # must_have rationale (where they help the reviewer) -- never emitted as evidence.
            if term and term not in evidence.ambiguous_terms:
                evidence.ambiguous_terms.append(term[:60])
            break  # one surviving signal per pattern is enough

    return evidence


def _quote_terms(terms: list[str]) -> str:
    return ", ".join('"' + t.replace('"', "'") + '"' for t in terms[:3])


def resolve(document: str, policy: str) -> dict:
    """Review `document` against `policy`.

    Returns {"violations": [...], "needs_review": [...], "unresolved": [...], "dropped": [...]}.
    `unresolved` and `dropped` carry category metadata only -- no document text.
    """
    rules = POLICIES[policy]
    violations: list[dict] = []
    review_flags: list[dict] = []
    unresolved: list[dict] = []
    dropped: list[dict] = []

    for category in rules[MUST_HAVE]:
        evidence = scan(document, category)
        if evidence.confirmed:
            continue
        if evidence.has_ambiguous:
            terms = _quote_terms(evidence.ambiguous_terms) or "a related heading"
            rationale = (
                f"{policy} requires a {category} clause. Only ambiguous language was found "
                f"({terms}); nothing in the document confirms the clause operates. Add explicit "
                f"wording or confirm on review."
            )
        else:
            rationale = (
                f"{policy} requires a {category} clause and no language establishing one was "
                f"found in the document. Add the clause before this ships."
            )
        violations.append({
            "category": category,
            "rule": MUST_HAVE,
            "present": False,
            "evidence_spans": [],
            "rationale": rationale,
        })

    for category in rules[MUST_NOT_HAVE]:
        evidence = scan(document, category)
        if evidence.confirmed:
            violations.append({
                "category": category,
                "rule": MUST_NOT_HAVE,
                "present": True,
                "evidence_spans": list(evidence.spans),
                "rationale": (
                    f"{policy} prohibits {category}; the quoted text establishes it in this "
                    f"document. Remove or renegotiate the clause."
                ),
            })
            continue
        if evidence.confirmed_but_unverifiable:
            unresolved.append({
                "category": category,
                "rule": MUST_NOT_HAVE,
                "reason": SPAN_UNVERIFIABLE,
                "signals": evidence.confirmed_but_unverifiable,
            })
            continue
        if evidence.carved_out:
            unresolved.append({
                "category": category,
                "rule": MUST_NOT_HAVE,
                "reason": CARVE_OUT,
                "signals": len(evidence.ambiguous_terms) + 1,
            })
            continue
        if evidence.ambiguous_terms:
            unresolved.append({
                "category": category,
                "rule": MUST_NOT_HAVE,
                "reason": AMBIGUOUS_ONLY,
                "signals": len(evidence.ambiguous_terms),
            })

    for category in rules[NEEDS_REVIEW]:
        evidence = scan(document, category)
        if evidence.confirmed:
            review_flags.append({"category": category, "evidence_spans": list(evidence.spans)})
        elif evidence.has_ambiguous or evidence.confirmed_but_unverifiable:
            # A needs_review entry requires a verifiable span and is never a fault, so an
            # unquotable routing signal is recorded as metadata rather than emitted or escalated
            # into an error: blocking a review on a non-fault would be wrong.
            dropped.append({
                "category": category,
                "rule": NEEDS_REVIEW,
                "reason": AMBIGUOUS_ONLY if evidence.has_ambiguous else SPAN_UNVERIFIABLE,
                "signals": len(evidence.ambiguous_terms) + evidence.confirmed_but_unverifiable,
            })

    return {
        "violations": violations,
        "needs_review": review_flags,
        "unresolved": unresolved,
        "dropped": dropped,
    }
