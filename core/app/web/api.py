"""Interfaz web de demostración servida por el mismo core (misma URL, sin costo adicional)."""
import pathlib

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(include_in_schema=False)
_HTML = pathlib.Path(__file__).with_name("index.html")


@router.get("/")
def raiz():
    return RedirectResponse("/app")


@router.get("/app", response_class=HTMLResponse)
def app_web():
    return HTMLResponse(_HTML.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})
