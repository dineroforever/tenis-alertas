# Alertas de tenis en vivo → Telegram

El bot vigila los partidos de tenis en vivo que tienen mercado en Kalshi y **te escribe por Telegram solo cuando un partido pasa el filtro completo**. Si nada pasa, no recibes nada.

## Qué revisa (cada 2 minutos)

1. **Set activo parejo:** a 1 game o menos de diferencia, y en el momento entre games (nunca con un punto en juego). No entra en tiebreak.
2. **Liquidez en Kalshi:** volumen alto, spread de 3¢ o menos, precio entre 20¢ y 80¢, y al menos 3 veces tus contratos en el ask.
3. **Modelo:** la calculadora (cuota previa al partido + saque de hoy + marcador exacto) se combina con la casa blanda en vivo: 60% calculadora y 40% casa.
4. **Confirmación:**
   - la calculadora y la casa no difieren en más de 10 pp;
   - el modelo da al menos +5 pp de edge contra el ask de Kalshi;
   - la casa da al menos +3 pp.
5. **Monto:** ¼ de Kelly, con un tope de 2% del bankroll.

**Salida por valor justo:** después de cada alerta de compra, el bot sigue ese partido. Cuando Kalshi paga más de lo que vale la posición (bid − comisión ≥ prob. del modelo + 0.5 pp), te manda **💰 VENDER**. Si el edge vuelve a aparecer, avisa la reentrada, con un máximo de 3 por partido.

Cada compra queda en `senales_tenis.csv` y cada venta o liquidación en `senales_salidas.csv`. Así se lleva la estadística real.

## Instalación (10 minutos)

1. Crea un repositorio **público** en GitHub, por ejemplo `tenis-alertas`, y sube estos archivos. En los repositorios públicos los minutos de Actions son gratis e ilimitados; en uno privado se acabarían en unos 3 días. Las llaves no quedan a la vista porque van como Secrets.
2. En el repositorio, entra a **Settings → Secrets and variables → Actions → New repository secret** y crea estos 3:
   - `APIFY_TOKEN`: tu token de Apify (en Apify: Settings → API & Integrations).
   - `TELEGRAM_BOT_TOKEN`: el token de tu bot de clima.
   - `TELEGRAM_CHAT_ID`: tu chat ID, el mismo que usa el bot de clima.
3. Opcional: en la pestaña **Variables** crea `BANKROLL` con tu bankroll en dólares. Si no lo pones, usa 500.
4. Entra a **Actions → Alertas tenis Kalshi → Run workflow** para la primera corrida. Desde ahí arranca solo cada 30 minutos y cada corrida vigila durante 28 minutos.

## Ajustes

Todos se cambian en `tenis_alertas.py`, arriba del todo:

- `EDGE_MODELO_MIN` (0.05): el edge mínimo que pide al modelo.
- `MIN_VOL_CONTRATOS` (300000): el volumen mínimo en contratos.
- `POLL_SECONDS` (120): cada cuántos segundos revisa. Va en el archivo del workflow.

## Límites conocidos

- **No hay cuota sharp** (ni Pinnacle ni Betfair), así que las alertas son de confianza MEDIA. Antes de comprar, confirma el marcador en Kalshi.
- GitHub a veces retrasa unos minutos el arranque programado.
- Si el repositorio pasa 60 días sin cambios, GitHub apaga el cron. Las señales registradas cuentan como cambios.
- Apify cobra cada consulta. Con una revisión cada 2 minutos son aproximadamente $10-15 al mes. Si subes `POLL_SECONDS` a 300, el costo baja a menos de la mitad.
- El bot no compra nada: solo avisa.

*Son estimaciones de probabilidad, no garantías. Solo informativo.*
