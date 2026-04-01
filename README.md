# BTC Active Range Bot — Guía de despliegue en Render.com

## Qué hace este bot
- Se conecta a Binance cada **5 minutos** (24/7 desde la nube)
- Calcula la señal Active Range para **15M, 1H, 4H y 1D** con la lógica exacta del indicador HTML
- Si detecta un **cambio de señal** en cualquier timeframe, te envía un mensaje a Telegram
- Funciona aunque el móvil esté apagado, bloqueado o sin conexión

---

## Paso 1 — Sube el código a GitHub

1. Ve a https://github.com y crea cuenta (gratis)
2. Haz clic en **New repository** → nombre: `btc-bot` → Public → Create
3. Sube los 3 archivos: `main.py`, `requirements.txt`, `render.yaml`
   - Haz clic en **Add file → Upload files**
   - Arrastra los 3 archivos → **Commit changes**

---

## Paso 2 — Crea el servicio en Render.com

1. Ve a https://render.com y crea cuenta (gratis)
2. Haz clic en **New +** → **Background Worker**
3. Conecta tu cuenta de GitHub cuando te lo pida
4. Selecciona el repositorio `btc-bot`
5. Render detectará el `render.yaml` automáticamente
6. Haz clic en **Create Background Worker**

---

## Paso 3 — Añade tu Token y Chat ID

1. En el dashboard de Render, ve a tu servicio → **Environment**
2. Añade estas dos variables:

   | Key         | Value                        |
   |-------------|------------------------------|
   | TG_TOKEN    | 123456789:AAFxxxxxxxxxxxxxxx |
   | TG_CHAT_ID  | 5279904355                   |

3. Haz clic en **Save Changes** → el servidor se reinicia automáticamente

---

## Paso 4 — Verifica que funciona

- Ve a **Logs** en Render y verás mensajes como:
  ```
  BTC Active Range Bot arrancando...
  Descargando velas Binance...
    15M: 500 velas descargadas
    1H:  500 velas descargadas
  Calculando señales...
    15M: ESPERA  (score +1  RSI:52.3)
  ```
- En Telegram recibirás un mensaje de confirmación:
  `🚀 ACTIVE RANGE BOT — Servidor iniciado`

---

## Cómo obtener tu Chat ID de Telegram

1. Abre Telegram y busca el bot `@userinfobot`
2. Escríbele `/start`
3. Te responderá con tu Chat ID (número)

---

## Tier gratuito de Render

- El plan **Free** de Render es suficiente para este bot
- Incluye 750 horas/mes de cómputo (el mes tiene ~730 horas → cubre todo)
- Sin tarjeta de crédito requerida

---

## Estructura del mensaje que recibirás

```
📡 ACTIVE RANGE — Cambio de señal
Actualizado: 1H, 4H
━━━━━━━━━━━━━━━━━━━━
🟢 15M: COMPRA · RSI:42 · Rango ACTIVO
🟢 1H:  COMPRA · RSI:38 · Rango ACTIVO
🟡 4H:  ESPERA · RSI:51
⬜ 1D:  SIN RANGO
━━━━━━━━━━━━━━━━━━━━
💰 Precio: $94.250,00
📐 Rango: $91.200 → $96.800
🛑 Stop Loss: $92.850 (-1.48%)
🎯 Take Profit: $97.460 (+3.41%)
━━━━━━━━━━━━━━━━━━━━
🕐 15/04 14:00h España
```
