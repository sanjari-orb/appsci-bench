-- Order-to-Cash OLTP Database Schema
-- This schema supports the invoice/PO/contract matching task.

-- Suppliers table
CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id INTEGER PRIMARY KEY,
    supplier_name VARCHAR(255) NOT NULL
);

-- Items table
CREATE TABLE IF NOT EXISTS items (
    item_id INTEGER PRIMARY KEY,
    item_number VARCHAR(50) NOT NULL,
    description VARCHAR(255)
);

-- Contracts table (defines allowed items and agreed prices per supplier)
CREATE TABLE IF NOT EXISTS contracts (
    contract_id SERIAL PRIMARY KEY,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(supplier_id),
    item_id INTEGER NOT NULL REFERENCES items(item_id),
    agreed_unit_price DECIMAL(12, 2) NOT NULL,
    allowed_fees JSONB DEFAULT '[]'::jsonb,
    effective_date DATE,
    expiry_date DATE,
    UNIQUE(supplier_id, item_id)
);

-- Purchase Orders table
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_number VARCHAR(50) PRIMARY KEY,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(supplier_id),
    created_date DATE,
    status VARCHAR(50) DEFAULT 'OPEN'
);

-- Purchase Order Lines table
CREATE TABLE IF NOT EXISTS po_lines (
    po_line_id INTEGER PRIMARY KEY,
    po_number VARCHAR(50) NOT NULL REFERENCES purchase_orders(po_number),
    item_id INTEGER NOT NULL REFERENCES items(item_id),
    item_number VARCHAR(50),
    item_uom VARCHAR(20),
    quantity DECIMAL(12, 2) NOT NULL,
    unit_price DECIMAL(12, 2) NOT NULL,
    received_qty DECIMAL(12, 2),
    pay_terms VARCHAR(50),
    currency VARCHAR(10) DEFAULT 'USD'
);

-- Invoices table
-- Note: item_id does NOT have a foreign key constraint because invoices may reference
-- items that are not in our items catalog (off-contract items - a fault type to detect).
CREATE TABLE IF NOT EXISTS invoices (
    invoice_number VARCHAR(50) PRIMARY KEY,
    po_line_id INTEGER REFERENCES po_lines(po_line_id),
    po_number VARCHAR(50) REFERENCES purchase_orders(po_number),
    supplier_id INTEGER NOT NULL REFERENCES suppliers(supplier_id),
    supplier_name VARCHAR(255),
    item_id INTEGER,  -- No FK: invoices may reference off-contract items
    item_number VARCHAR(50),
    uom VARCHAR(20),
    currency VARCHAR(10) DEFAULT 'USD',
    invoiced_qty DECIMAL(12, 2) NOT NULL,
    unit_price DECIMAL(12, 2) NOT NULL,
    amount DECIMAL(12, 2) NOT NULL,
    extra_fees JSONB DEFAULT '[]'::jsonb,
    duplicate_of VARCHAR(50),
    invoice_date DATE,
    status VARCHAR(50) DEFAULT 'PENDING'
);

-- Create indexes for common queries
CREATE INDEX IF NOT EXISTS idx_invoices_po_line ON invoices(po_line_id);
CREATE INDEX IF NOT EXISTS idx_invoices_supplier ON invoices(supplier_id);
CREATE INDEX IF NOT EXISTS idx_po_lines_po_number ON po_lines(po_number);
CREATE INDEX IF NOT EXISTS idx_contracts_supplier ON contracts(supplier_id);
