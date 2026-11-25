-- Datos de prueba del caso del profesor: «Producto Zeta» con inventario lógico de 3 unidades en la Dark Store 5 (CP 53400).
-- Es un dato de demostración (no es un precio ni un producto real de ExpressYa). Con CARGAR_DATOS_DEMO=0 no se carga.
INSERT INTO catalogo.producto (sku, descripcion, precio) VALUES ('SKU-ZETA', 'Producto Zeta', 20.00) ON CONFLICT DO NOTHING;
SELECT inventario.cargar_existencia(5::smallint, 'SKU-ZETA', 3, 'SISTEMA', 'Escenario Zeta: inventario lógico de 3 unidades')
 WHERE NOT EXISTS (SELECT 1 FROM inventario.existencia WHERE tienda_id = 5 AND sku = 'SKU-ZETA');
