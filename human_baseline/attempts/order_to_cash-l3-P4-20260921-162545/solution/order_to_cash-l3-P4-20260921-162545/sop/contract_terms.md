# Contract Terms Reference

This document describes how supplier contracts work and where to find contract information.

## How Contracts Work

Each supplier has a contract that governs what they can invoice us for:
- **Allowed items**: Specific item IDs the supplier is authorized to provide
- **Agreed pricing**: Negotiated unit prices for each item
- **Permitted fees**: Additional charges we've agreed to. Which ones differ per supplier — a fee
  one supplier may bill is not a fee another may bill, so there is no fee that is acceptable
  everywhere. Read the supplier's own list; do not generalise from another contract.

## Where to Find Contract Data

Contract details are in the `contracts` table in the OLTP database:
- `supplier_id`: Which supplier the contract applies to
- `item_id`: Which item this contract line covers
- `agreed_unit_price`: The negotiated price per unit
- `allowed_fees`: JSON array of the fee labels this supplier is permitted to bill. A fee whose
  label is absent from that array is not authorized, whatever the amount.
