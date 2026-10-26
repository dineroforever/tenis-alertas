# Bot NBA en papel para Kalshi

Funciona igual que el bot de tenis: corre solo en GitHub Actions, **no envía órdenes reales** y manda todo al Telegram KalshiClima.

## Qué hace

1. **Cada 15 minutos** (de 11 a. m. a 12:45 a. m., hora de Miami) busca partidos NBA que empiezan en 15-100 minutos.
2. Aplica las **4 variables**:
   - **Plantilla**: si Pinnacle y DraftKings discrepan más de 1,5 pts en el hándicap o 3 en el total (señal de una baja de último minuto), ese partido no se opera.
   - **Línea sharp**: Pinnacle (o DraftKings) vía The Odds API. Es el ancla, con 65 % del peso. Sin clave `ODDS_API_KEY` usa la línea implícita en los precios de Kalshi.
   - **Rating ajustado**: puntos anotados y recibidos en los últimos 15 partidos, local +2 y back-to-back −2. Pesa 35 %.
   - **Ritmo**: total de puntos esperado (ofensiva de uno contra defensa del otro).

   Los resultados históricos salen de los mercados liquidados de Kalshi (ESPN bloquea los servidores de GitHub).
3. Compara con Kalshi en 8 mercados: ganador, hándicap, total, total por equipo, hándicap y total de la 1.ª mitad, hándicap y total del 1.er cuarto.
4. Registra en papel la mejor señal de cada mercado si el edge neto de comisión es de 4-5 puntos o más. Si el edge pasa de 15, la descarta por sospechosa.
   - Exige liquidez: diferencia de 5¢ o menos entre compra y venta.
   - Exige un precio entre 12¢ y 88¢.
5. Liquida los resultados con el resultado oficial de Kalshi y manda ✅/❌ con el P&L.
6. **Informes automáticos** a las 25, 50, 100, 150… operaciones, por mercado y por tramo de edge.

Apuesta ficticia: $10 por operación.

## Backtest (prueba con la temporada pasada)

En GitHub, entra a **Actions → "NBA backtest Kalshi" → Run workflow**.

- Usa la temporada 2025-26 con precios reales de Kalshi 60 minutos antes de cada partido. Como no hay líneas históricas gratis de las casas, la referencia es la línea implícita de Kalshi: el backtest mide si el modelo agrega valor sobre el mercado.
- Si no termina en una corrida (más de 5 horas), vuelve a darle Run y retoma donde quedó.
- El informe llega a Telegram.

## Archivos

- `nba/bot.py`: bot en papel.
- `nba/backtest.py`: prueba histórica.
- `nba/model.py`: el modelo y sus parámetros (`CFG`).
- `nba/data/`: resultados, que el bot guarda solo.
- `.github/workflows/nba-papel.yml` y `nba-backtest.yml`: las tareas automáticas.

## Secretos de GitHub

El bot usa `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` (los mismos del bot de tenis) y, opcional, `ODDS_API_KEY` (clave gratis de the-odds-api.com, 500 créditos al mes; el bot gasta unos 2 por franja horaria de partidos).
