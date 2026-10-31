-- Apply once to an existing SQLite database before starting the expanded catalog.
-- Fresh databases get these fields from SQLAlchemy metadata automatically.
ALTER TABLE products ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1;
ALTER TABLE product_variants ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1;
ALTER TABLE discounts ADD COLUMN minimum_purchase_amount NUMERIC(12, 2) NOT NULL DEFAULT 0;
ALTER TABLE discounts ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1;

CREATE TABLE discount_products (
    id INTEGER PRIMARY KEY,
    discount_id INTEGER NOT NULL REFERENCES discounts(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE
);
CREATE INDEX ix_discount_products_discount_id ON discount_products(discount_id);
CREATE INDEX ix_discount_products_product_id ON discount_products(product_id);
CREATE UNIQUE INDEX ix_discount_products_pair ON discount_products(discount_id, product_id);
