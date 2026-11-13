COPY (
WITH j AS (
  SELECT i.invoice_number, i.duplicate_of,
         i.invoiced_qty q, i.unit_price p, i.amount amt,
         pl.po_line_id pli, pl.quantity poq, pl.unit_price pop,
         c.contract_id,
         coalesce((SELECT sum((f->>'amount')::numeric)
                     FROM jsonb_array_elements(coalesce(i.extra_fees,'[]'::jsonb)) f),0) feesum,
         (SELECT count(*) FROM jsonb_array_elements(coalesce(i.extra_fees,'[]'::jsonb)) f
           WHERE lower(trim(f->>'label')) NOT IN
             (SELECT lower(trim(a)) FROM jsonb_array_elements_text(coalesce(c.allowed_fees,'[]'::jsonb)) a)
         ) badfees
  FROM invoices i
  LEFT JOIN po_lines pl ON pl.po_line_id = i.po_line_id
  LEFT JOIN contracts c ON c.supplier_id = i.supplier_id AND c.item_id = i.item_id
)
SELECT invoice_number,
  CASE
    WHEN duplicate_of IS NOT NULL                       THEN 'DUPLICATE'
    WHEN pli IS NOT NULL AND (p-pop)*q > 500            THEN 'PRICE_DELTA'
    WHEN pli IS NOT NULL AND q <> poq                   THEN 'QTY_MISMATCH'
    WHEN abs(amt - (q*p+feesum)) > 500                  THEN 'TOTAL_MATH'
    WHEN contract_id IS NULL                            THEN 'OFF_CONTRACT_ITEM'
    WHEN badfees > 0                                    THEN 'OFF_CONTRACT_FEE'
    ELSE 'CLEAN'
  END AS expected
FROM j ORDER BY invoice_number
) TO STDOUT WITH CSV HEADER
