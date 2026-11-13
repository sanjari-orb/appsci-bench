# What a review request looks like when it reaches the system

This is the INBOUND half of the interface. The outbound half -- the shape of the reply the AP review
queue can ingest -- is a separate document, and nothing here describes it.

AP submits **one invoice at a time**, never a batch. The request is a plain-English sentence naming
three things: the invoice number, its supplier, and its purchase-order number. It carries none of the
invoice's own values -- no quantity, no unit price, no total, no fee list -- so the system has to
recover the invoice number from the sentence and look the record up itself.

The wording is fixed; only the three names change from one request to the next.

## Five examples, verbatim

```
Review invoice INV-1877 (supplier Supplier_7, PO PO-000498) against its PO line and the supplier
contract; decide whether to flag it for human review and give a short reason.
```

```
Review invoice INV-5029 (supplier Supplier_4, PO PO-000334) against its PO line and the supplier
contract; decide whether to flag it for human review and give a short reason.
```

```
Review invoice INV-1542 (supplier Supplier_5, PO PO-000224) against its PO line and the supplier
contract; decide whether to flag it for human review and give a short reason.
```

```
Review invoice INV-2953 (supplier Supplier_6, PO PO-000131) against its PO line and the supplier
contract; decide whether to flag it for human review and give a short reason.
```

```
Review invoice INV-5637 (supplier Supplier_8, PO PO-000598) against its PO line and the supplier
contract; decide whether to flag it for human review and give a short reason.
```

## Note for whoever maintains this file

These five invoices are drawn from the BUILD-phase store and appear in no scored case, so quoting them
reveals nothing held out. Keep it that way if the examples are ever refreshed: an invoice number from
the eval store would name a case the artifact is about to be scored on.

And keep the examples INPUT-ONLY. An input-output pair would demonstrate the reply's three key names,
an exact fault-type spelling, the reason style and the permitted-fee rule -- four or more elicitation
rubric items answered by one document, when each is meant to be obtained separately by asking.
