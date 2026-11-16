-- 100 clientes concurrentes piden la última unidad de SKU-X en la tienda 1 (vendible = 1)
SELECT inventario.solicitar_reserva(gen_random_uuid(), 1::smallint, '[{"sku":"SKU-X","cantidad":1}]'::jsonb,
                                    'K1-' || gen_random_uuid(), 'PRUEBA_CONCURRENCIA', gen_random_uuid());
