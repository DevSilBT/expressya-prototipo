"""Punto de entrada del worker en modo job (una pasada y termina). Uso: python correr_worker.py
Existe para que la plataforma no tenga que pasar argumentos que empiezan con guion (python -m ...)."""
import logging

from app.worker import una_vez

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    una_vez()
