from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse
import stripe
import os
import database  # Asegúrate de que este módulo maneja una DB en la nube (ej., Supabase)
from dotenv import load_dotenv
from telegram import Bot # Importa Bot para enviar mensajes de confirmación
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI()

# Carga las variables de entorno (útil para desarrollo local, Render las inyecta directamente)
load_dotenv() 

# Configuración de Stripe con variables de entorno
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
BOT_TOKEN = os.environ.get("BOT_TOKEN") # Asegúrate de tener este valor en Render

# Asegúrate de que las claves de Stripe están configuradas
if not stripe.api_key:
    logging.error("La variable de entorno STRIPE_SECRET_KEY no está configurada.")
    raise ValueError("Configuración de Stripe incompleta: STRIPE_SECRET_KEY no encontrada.")
if not STRIPE_WEBHOOK_SECRET:
    logging.error("La variable de entorno STRIPE_WEBHOOK_SECRET no está configurada.")
    # No es un error crítico para el inicio del servidor, pero es necesario para webhooks seguros.

# Instancia del Bot para enviar confirmaciones (si BOT_TOKEN está disponible)
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
if not bot:
    logging.warning("BOT_TOKEN no configurado en el backend de Stripe. Los mensajes de confirmación no se pueden enviar a Telegram.")


# Define tus paquetes de puntos aquí con el precio en centavos (USD)
# ⬅️ AÑADIMOS 'priority_boost' a cada paquete.
# Valores 'priority_boost' MÁS BAJOS indican MAYOR prioridad.
# Asegúrate de que esta definición de POINT_PACKAGES esté sincronizada con points_handlers.py en tu bot
POINT_PACKAGES = {
    "p200": {"label": "500 points", "amount": 399, "points": 500, "priority_boost": 1},  # Prioridad Normal
    "p500": {"label": "2000 points", "amount": 999, "points": 2000, "priority_boost": 1},  # Alta Prioridad
    "p1000": {"label": "5000 points", "amount": 1999, "points": 5000, "priority_boost": 1} # Muy Alta Prioridad
}

# --- CAMBIO 1: Define el identificador único para este proyecto ---
# Esto es crucial para el filtrado de webhooks.
PROJECT_IDENTIFIER = "video697bot" # <--- ¡IMPORTANTE! Este es el identificador para el backend de "Monkeyvideos"

@app.post("/crear-sesion")
async def crear_sesion(request: Request):
    """
    Endpoint para crear una sesión de pago de Stripe.
    Llamado desde tu bot de Telegram.
    """
    data = await request.json()
    user_id = str(data.get("telegram_user_id"))
    paquete_id = data.get("paquete_id")
    # ⬅️ Recibimos el 'priority_boost' del bot
    priority_boost = data.get("priority_boost") 

    # Validación
    if not user_id or paquete_id not in POINT_PACKAGES:
        logging.error(f"Datos inválidos en /crear-sesion: user_id={user_id}, paquete_id={paquete_id}")
        return JSONResponse(status_code=400, content={"error": "Datos inválidos: user_id o package_id incorrecto."})
    
    # Valida que priority_boost sea un entero válido si se envía
    if priority_boost is not None and not isinstance(priority_boost, int):
        logging.error(f"Tipo de dato inválido para priority_boost: {priority_boost}")
        return JSONResponse(status_code=400, content={"error": "Datos inválidos: priority_boost debe ser un entero."})

    paquete = POINT_PACKAGES[paquete_id]

    try:
        session = stripe.checkout.Session.create(
            
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": paquete["amount"],
                    "product_data": {
                        "name": paquete["label"]
                    }
                },
                "quantity": 1
            }],
            mode="payment",
            allow_promotion_codes=True,
            success_url="https://t.me/video697bot",  # URL de éxito para este bot
            cancel_url="https://t.me/video697bot",   # URL de cancelación para este bot
            metadata={
                "telegram_user_id": user_id,
                "package_id": paquete_id,
                "points_awarded": paquete["points"], # También útil para el webhook
                "priority_boost": priority_boost,    # ⬅️ Pasamos el 'priority_boost' en el metadata
                "project": PROJECT_IDENTIFIER        # <--- CAMBIO 2: AÑADIDO: Identificador del proyecto
            }
        )
        logging.info(f"Sesión de Stripe creada para el usuario {user_id}, paquete {paquete_id}. URL: {session.url}")
        return {"url": session.url}
    except Exception as e:
        logging.error(f"Error al crear la sesión de Stripe para el usuario {user_id}, paquete {paquete_id}: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": f"Error interno al crear la sesión: {str(e)}"})

@app.post("/webhook/stripe")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None, alias="Stripe-Signature")):
    """
    Endpoint que recibe webhooks de Stripe.
    Es llamado por Stripe cuando ocurren eventos como 'checkout.session.completed'.
    """
    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError as e:
        logging.error(f"Error de verificación de firma del webhook de Stripe: {e}")
        raise HTTPException(status_code=400, detail="Firma inválida")
    except ValueError as e:
        logging.error(f"Error de procesamiento de payload del webhook de Stripe: {e}")
        raise HTTPException(status_code=400, detail="Payload inválido")
    
    # --- CAMBIO 3: INICIO DE LA LÓGICA DE FILTRADO POR METADATA DENTRO DEL WEBHOOK ---
    # Si el evento es de tipo 'checkout.session.completed', verificamos el metadata 'project'.
    # Si el evento no tiene el metadata 'project' o no coincide con este backend, lo ignoramos.
    if event["type"] == "checkout.session.completed":
        session_metadata = event["data"]["object"].get("metadata", {})
        event_project = session_metadata.get("project")

        # Verifica si el identificador del proyecto en el metadata del evento
        # NO coincide con el identificador de ESTE backend.
        if event_project != PROJECT_IDENTIFIER:
            logging.info(f"Webhook recibido para el proyecto '{event_project}', pero este backend es '{PROJECT_IDENTIFIER}'. Ignorando evento.")
            # Es crucial devolver un 200 OK para que Stripe no reintente el envío.
            return JSONResponse(status_code=200, content={"status": "ignored", "reason": "project_mismatch"})
    # --- FIN DE LA LÓGICA DE FILTRADO POR METADATA ---

    # El resto del código del webhook solo se ejecuta si el filtro pasó (es decir, el evento es para este proyecto)
    # Handle checkout session completed event
    if event["type"] == "checkout.session.completed": # Esta condición se repite, pero es para claridad después del filtro.
        session = event["data"]["object"]
        metadata = session.get("metadata", {})
        user_id_str = metadata.get("telegram_user_id") # Leer como string
        package_id = metadata.get("package_id")
        points_awarded = metadata.get("points_awarded") # Puntos a otorgar
        priority_boost = metadata.get("priority_boost") # ⬅️ Recupera el 'priority_boost'

        # Convierte user_id a int de forma segura
        try:
            user_id = int(user_id_str)
        except (ValueError, TypeError):
            logging.error(f"Webhook: user_id inválido o faltante en metadata: {user_id_str}")
            return JSONResponse(status_code=400, content={"status": "error", "message": "user_id inválido en metadata"})

        # Convierte points_awarded a int de forma segura
        try:
            points_awarded = int(points_awarded)
        except (ValueError, TypeError):
            logging.error(f"Webhook: points_awarded inválido o faltante en metadata: {points_awarded}")
            points_awarded = 0 # O maneja como error si es crítico

        # Convierte priority_boost a int de forma segura
        try:
            priority_boost = int(priority_boost)
        except (ValueError, TypeError):
            logging.warning(f"Webhook: priority_boost inválido o faltante en metadata: {priority_boost}. Usando prioridad por defecto (2).")
            priority_boost = 2 # Usa prioridad por defecto si no se puede convertir

        if user_id is not None and package_id in POINT_PACKAGES:
            try:
                # Actualiza los puntos del usuario
                # Asegúrate de que tu database.py para Monkeyvideos usa la tabla correcta (ej. "users")
                database.update_user_points(user_id, points_awarded)
                logging.info(f"Usuario {user_id} recibió {points_awarded} puntos por compra en Stripe.")

                # ⬅️ Actualiza la prioridad del usuario
                # Solo actualizamos si la nueva prioridad es "mejor" (numéricamente menor)
                database.update_user_priority(user_id, priority_boost)
                logging.info(f"Prioridad del usuario {user_id} actualizada a {priority_boost} (if better).")

                # Envía mensaje de confirmación al usuario de Telegram
                if bot: # Solo intenta enviar si el bot se inicializó correctamente
                    try:
                        await bot.send_message(
                            chat_id=user_id,
                            text=f"🎉 **¡Recarga exitosa!** <b>{points_awarded}</b> puntos han sido añadidos a tu cuenta. Tu prioridad en la cola es ahora <b>{priority_boost}</b> (0=Más alta).",
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logging.error(f"Error al enviar mensaje de confirmación de Telegram para {user_id}: {e}")
                else:
                    logging.warning("Advertencia: Bot de Telegram no inicializado en el backend de Stripe (¿TOKEN faltante?). No se pudo enviar la confirmación.")
            except Exception as e:
                logging.error(f"Error al actualizar puntos/prioridad o enviar confirmación para {user_id}: {e}", exc_info=True)
        else:
            logging.warning(f"Webhook recibido pero metadata incompleta o inválida: user_id={user_id_str}, package_id={package_id}")

    # Puedes manejar otros tipos de eventos de Stripe aquí si es necesario
    # elif event["type"] == "payment_intent.succeeded":
    #     logging.info("¡Payment Intent exitoso!")

    return JSONResponse(status_code=200, content={"status": "ok"})
