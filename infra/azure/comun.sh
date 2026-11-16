# Valores comunes de los scripts de Azure. Se pueden cambiar con variables de entorno antes de ejecutar.
RG="${RG:-expressya-prueba}"                 # grupo de recursos: borrarlo elimina todo el ambiente
LOC="${LOC:-eastus2}"                        # región; si la suscripción de estudiante la bloquea, prueba otra
ENV_NAME="${ENV_NAME:-expressya-env}"         # ambiente de Container Apps
TAG="${TAG:-latest}"                         # etiqueta de las imágenes en ghcr.io

imagen() { echo "ghcr.io/${GHCR_OWNER}/expressya-$1:${TAG}"; }

asegurar_extension() {
  az extension add --name containerapp --upgrade --yes --only-show-errors >/dev/null
}

# Ejecuta el job "migrar" y espera a que termine (falla si la migración falla).
migrar() {
  local exec estado
  exec=$(az containerapp job start -n migrar -g "$RG" --query name -o tsv)
  echo "migración en curso ($exec)..."
  for _ in $(seq 1 60); do
    estado=$(az containerapp job execution show -n migrar -g "$RG" --job-execution-name "$exec" \
               --query properties.status -o tsv 2>/dev/null || true)
    case "$estado" in
      Succeeded) echo "migración terminada"; return 0 ;;
      Failed|Stopped|Degraded) echo "LA MIGRACIÓN FALLÓ ($estado). Revisa los logs del job 'migrar' en el portal." >&2; return 1 ;;
    esac
    sleep 10
  done
  echo "La migración no terminó en 10 minutos" >&2; return 1
}
