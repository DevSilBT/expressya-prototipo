-- Datos mínimos para desarrollo y pruebas (no son datos reales de ExpressYa)
INSERT INTO acceso.rol VALUES ('CLIENTE'),('PICKER'),('SUPERVISOR'),('ENCARGADO'),('RECEPCION'),('FINANZAS'),('ATENCION'),('AUDITOR'),('SERVICIO')
ON CONFLICT DO NOTHING;
INSERT INTO inventario.dark_store (tienda_id, nombre) VALUES (1,'Dark Store 1'),(2,'Dark Store 2'),(3,'Dark Store 3'),(4,'Dark Store 4'),(5,'Dark Store 5')
ON CONFLICT DO NOTHING;
INSERT INTO catalogo.producto (sku, descripcion, precio) VALUES
  ('SKU-X','Producto de prueba X', 25.00), ('SKU-Y','Producto de prueba Y', 40.00), ('SKU-Z','Producto de prueba Z', 15.50)
ON CONFLICT DO NOTHING;
INSERT INTO inventario.existencia (tienda_id, sku, vendible) VALUES
  (1,'SKU-X',1), (1,'SKU-Y',10), (1,'SKU-Z',3), (2,'SKU-X',10), (2,'SKU-Y',10), (2,'SKU-Z',10)
ON CONFLICT DO NOTHING;
INSERT INTO pedidos.zona_cobertura (tienda_id, codigo_postal, distancia_km) VALUES
  (1,'53150',1.2), (2,'53150',2.5), (2,'53100',0.8)
ON CONFLICT DO NOTHING;
