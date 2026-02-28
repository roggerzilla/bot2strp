from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse
import stripe
import os
import database  
from dotenv import load_dotenv
from telegram import Bot 
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI()
load_dotenv()

# Configuración Multibot
BOT_CONFIGS = {
    "monkeyvideos": {
        "stripe_secret": os.environ.get("STRIPE_SECRET_KEY_MONKEY", os.environ.get("STRIPE_SECRET_KEY")),
        "webhook_secret": os.environ.get("STRIPE_WEBHOOK_SECRET_MONKEY", os.environ.get("STRIPE_WEBHOOK_SECRET")),
        "bot_token": os.environ.get("BOT_TOKEN_MONKEY", os.environ.get("BOT_TOKEN")),
        "users_table": "users2" # Tabla del bot antiguo
    },
    "videosSound69bot": {
        "stripe_secret": os.environ.get("STRIPE_SECRET_KEY_SOUND"),
        "webhook_secret": os.environ.get("STRIPE_WEBHOOK_SECRET_SOUND"),
        "bot_token": os.environ.get("BOT_TOKEN_SOUND"),
        "users_table": "users_sound" # Tabla del nuevo bot
    }
}

POINT_PACKAGES = {
    "p200": {"label": "500 points", "amount": 399, "points": 500, "priority_boost": 1},
    "p500": {"label": "2000 points", "amount": 999, "points": 2000, "priority_boost": 1},
    "p1000": {"label": "5000 points", "amount": 1999, "points": 5000, "priority_boost": 1}
}

@app.post("/crear-sesion")
async def crear_sesion(request: Request):
    data = await request.json()
    user_id = str(data.get("telegram_user_id"))
    paquete_id = data.get("paquete_id")
    priority_boost = data.get("priority_boost")
    project = data.get("project")

    if not project or project not in BOT_CONFIGS:
        return JSONResponse(status_code=400, content={"error": "Proyecto no válido o ausente."})

    if not user_id or paquete_id not in POINT_PACKAGES:
        return JSONResponse(status_code=400, content={"error": "Datos inválidos: user_id o package_id incorrecto."})
    
    paquete = POINT_PACKAGES[paquete_id]
    config = BOT_CONFIGS[project]
    
    if not config["stripe_secret"]:
        return JSONResponse(status_code=500, content={"error": "Clave de Stripe no configurada para este proyecto."})

    try:
        session = stripe.checkout.Session.create(
            api_key=config["stripe_secret"],
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
            success_url=f"https://t.me/{project}",
            cancel_url=f"https://t.me/{project}",
            metadata={
                "telegram_user_id": user_id,
                "package_id": paquete_id,
                "points_awarded": paquete["points"],
                "priority_boost": priority_boost,
                "project": project
            }
        )
        return {"url": session.url}
    except Exception as e:
        logging.error(f"Error al crear la sesión: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/webhook/stripe/{project}")
async def stripe_webhook(project: str, request: Request, stripe_signature: str = Header(None, alias="Stripe-Signature")):
    if project not in BOT_CONFIGS:
        return JSONResponse(status_code=400, content={"error": "Proyecto desconocido en webhook."})

    config = BOT_CONFIGS[project]
    webhook_secret = config["webhook_secret"]
    
    if not webhook_secret:
        return JSONResponse(status_code=500, content={"error": "Webhook secret no configurado."})

    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, webhook_secret)
    except stripe.error.SignatureVerificationError as e:
        raise HTTPException(status_code=400, detail="Firma inválida")
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Payload inválido")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        metadata = session.get("metadata", {})
        
        event_project = metadata.get("project")
        if event_project != project:
            return JSONResponse(status_code=200, content={"status": "ignored", "reason": "project_mismatch"})

        user_id_str = metadata.get("telegram_user_id")
        package_id = metadata.get("package_id")
        points_awarded = metadata.get("points_awarded")
        priority_boost = metadata.get("priority_boost", 2)

        try:
            user_id = int(user_id_str)
            points_awarded = int(points_awarded)
            priority_boost = int(priority_boost)
        except (ValueError, TypeError):
            return JSONResponse(status_code=400, content={"status": "error", "message": "Datos de user o puntos inválidos"})

        if user_id is not None and package_id in POINT_PACKAGES:
            try:
                # Update using the specific table for this project
                table_name = config["users_table"]
                database.update_user_points(user_id, points_awarded, table_name)
                database.update_user_priority(user_id, priority_boost, table_name)

                # Send Telegram notification
                bot_token = config["bot_token"]
                if bot_token:
                    bot = Bot(token=bot_token)
                    await bot.send_message(
                        chat_id=user_id,
                        text=f"🎉 **¡Recarga exitosa!** <b>{points_awarded}</b> puntos han sido añadidos a tu cuenta. Tu prioridad en la cola es ahora <b>{priority_boost}</b> (0=Más alta).",
                        parse_mode="HTML"
                    )
            except Exception as e:
                logging.error(f"Error procesando webhook: {e}", exc_info=True)

    return JSONResponse(status_code=200, content={"status": "ok"})
