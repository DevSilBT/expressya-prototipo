-- Datos de prueba de C3: dos productos más para poder formar pedidos de cinco líneas (AT-08) en cualquier tienda.
INSERT INTO catalogo.producto (sku, descripcion, precio) VALUES
  ('SKU-A','Producto de prueba A', 30.00), ('SKU-B','Producto de prueba B', 12.00)
ON CONFLICT DO NOTHING;
SELECT inventario.cargar_existencia(t::smallint, s, 10)
  FROM generate_series(1, 5) AS t CROSS JOIN (VALUES ('SKU-A'), ('SKU-B')) AS v(s)
 WHERE NOT EXISTS (SELECT 1 FROM inventario.existencia e WHERE e.tienda_id = t AND e.sku = v.s);
