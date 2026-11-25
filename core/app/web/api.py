"""Interfaz web de demostración servida por el mismo core (misma URL, sin costo adicional)."""
import pathlib

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

router = APIRouter(include_in_schema=False)
_CARPETA = pathlib.Path(__file__).parent
_HTML = _CARPETA / "index.html"
_LOGO = _CARPETA / "logo.png"          # logo de ExpressYA · Equipo Dinamita (logo/image1.png del paquete)


@router.get("/")
def raiz():
    return RedirectResponse("/app")


@router.get("/app", response_class=HTMLResponse)
def app_web():
    return HTMLResponse(_HTML.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


@router.get("/app/logo.png")
def logo():
    return FileResponse(_LOGO, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
