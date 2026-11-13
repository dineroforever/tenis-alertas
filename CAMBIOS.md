# Registro de cambios de los bots

Regla: antes de cambiar algo, leer este archivo y el historial del repo.
Cambios técnicos (fallos, registros, fiabilidad): se hacen y se avisa a Roberto después.
Cambios de estrategia u operación (filtros, edge, reglas de bloqueo, tamaño, frenos, horarios, modo real/paper): solo con el "sí" explícito de Roberto.
Cada cambio se anota aquí: fecha · qué · por qué · quién aprobó.

## Noviembre 2026

- **12-nov** · Aviso por Telegram cuando la verificación de ubicación de Kalshi está por vencer o vencida (`kalshi_real.py`) · una compra falló por tenerla vencida · técnico.
- **12-nov** · La siguiente ronda se encadena también tras cancelar una a mano (`alertas.yml`) · antes el bot quedaba parado · técnico.
- **12-nov** · El token de Apify se limpia de espacios/saltos/comillas y se diagnostica al arrancar (`tenis_alertas.py`) · incidente de seguridad de Apify, el token nuevo daba 401 · técnico.
- **12-nov** · Clima (paper): regla simple (favorito 40–60¢, spread ≤2¢, sin filtro de volumen ni de edge) y solo un resumen diario por Telegram; workflow de clima reactivado (estaba desactivado desde el 12-oct) · backtest ago–oct: la regla simple ≈ +4% en 798 ops, la anterior −6,7% · aprobado por Roberto.
- **12-nov** · NBA: horario automático apagado en paper y backtest · sin edge (backtest −3,6%, paper −9,6%) · aprobado por Roberto.
- **12-nov** · Sin señal de ejemplo en corridas manuales (`alertas.yml`, PRUEBA_SENAL=0) · confundía · técnico.
- **11-nov** · Pausa nocturna: de 9 pm a 12 am y de 2 a 5 am (antes 11 pm–6 am) · la franja 12–2 am (Asia) daba 8 señales en 11 días y la de 9 pm–12 am solo 1 · aprobado por Roberto.
- **11-nov** · Paper: partidos con menos de $300k de volumen pasan a sombra, igual que el real · aprobado por Roberto.
- **11-nov** · Resumen diario: Fase 3 sin partidos de <$300k y evaluación automática a las 50 ops · aprobado por Roberto.
- **9/10-nov** · Rondas largas (hasta 5 h 45 min) encadenadas con repository_dispatch; pausa nocturna dentro del bot · GitHub se saltaba rondas programadas · aprobado por Roberto (otro chat).
- **8-nov** · Prueba 2 de 25 operaciones reales con las mismas reglas; módulo de variables extra (`extras.py`) en paper · aprobado por Roberto (otro chat).
- **4-nov** · Freno real: 5 pérdidas seguidas (antes 8 acumuladas) · aprobado por Roberto.
- **3-nov** · Tope diario de Apify $1,50 entre semana (hasta $1,75 fines de semana si hace falta) · aprobado por Roberto.

## Octubre 2026

- **31-oct** · Paper: precio de Kalshi pedido al momento de entrar y salir (antes, el del inicio del ciclo) · la auditoría mostró salidas a precios que no existían · técnico.
- **30-oct** · NBA: avisos de Telegram apagados · aprobado por Roberto.
- **30-oct** · Real: saldo del exchange 3 (tenis) · Kalshi separa el saldo por exchange · técnico.
- **29-oct** · Real: órdenes por el endpoint V2 de Kalshi · el V1 devolvía 410 · técnico.
- **28-oct** · Real: reintentos ante 429 de Kalshi · técnico.
- **27-oct** · Real activado (prueba 1 de 25 ops) · aprobado por Roberto.
