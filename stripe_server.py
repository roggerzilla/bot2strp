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

PURCHASE_OFFERS = { 
    # Special Offers
    "p_1_00": {"label": "Special: 159 Points ($1.00 USD)", "amount": 100, "points": 159, "priority_days": 1, "type": "one_time"},
    # One-time
    "p_3_99": {"label": "400 Points ($3.99 USD)", "amount": 399, "points": 400, "priority_days": 7, "type": "one_time"},
    "p_9_99": {"label": "2000 Points ($9.99 USD)", "amount": 999, "points": 2000, "priority_days": 15, "type": "one_time"},
    "p_19_99": {"label": "5000 Points ($19.99 USD)", "amount": 1999, "points": 5000, "priority_days": 30, "type": "one_time"},
    "p_50_00": {"label": "14000 Points ($50.00 USD)", "amount": 5000, "points": 14000, "priority_days": 60, "type": "one_time"},
    # Subscriptions
    "sub_9_99": {"label": "2300 Points - Sub Monthly ($9.99 USD)", "amount": 999, "points": 2300, "priority_days": 15, "type": "subscription", "price_id": "price_1TLvOMAuyjJEMmnKsXgiPdPW"},
    "sub_19_99": {"label": "5750 Points - Sub Monthly ($19.99 USD)", "amount": 1999, "points": 5750, "priority_days": 30, "type": "subscription", "price_id": "price_1TLvNtAuyjJEMmnK2WiUoX2d"},
    "sub_50_00": {"label": "16100 Points - Sub Monthly ($50.00 USD)", "amount": 5000, "points": 16100, "priority_days": 60, "type": "subscription", "price_id": "price_1TLvOfAuyjJEMmnK0UvCiBEa"},
}

@app.post("/crear-sesion")
async def crear_sesion(request: Request):
    data = await request.json()
    user_id = str(data.get("telegram_user_id"))
    paquete_id = data.get("paquete_id")
    priority_days = data.get("priority_days", 0)
    is_subscription = data.get("is_subscription", False)
    project = data.get("project")

    if not project or project not in BOT_CONFIGS:
        return JSONResponse(status_code=400, content={"error": "Proyecto no válido o ausente."})

    if not user_id or paquete_id not in PURCHASE_OFFERS:
        return JSONResponse(status_code=400, content={"error": "Datos inválidos: user_id o package_id incorrecto."})
    
    paquete = PURCHASE_OFFERS[paquete_id]
    config = BOT_CONFIGS[project]
    
    if not config["stripe_secret"]:
        return JSONResponse(status_code=500, content={"error": "Clave de Stripe no configurada para este proyecto."})

    try:
        line_item = {"quantity": 1}
        
        # Si configuras un 'price_id' de Stripe en el paquete, lo usará y permitirá restringir cupones.
        if "price_id" in paquete:
            line_item["price"] = paquete["price_id"]
        else:
            # Modalidad al vuelo (legacy)
            price_data = {
                "currency": "usd",
                "unit_amount": paquete["amount"],
                "product_data": {
                    "name": paquete["label"]
                }
            }
            if is_subscription:
                price_data["recurring"] = {"interval": "month"}
            line_item["price_data"] = price_data

        # Parámetros base de la sesión
        session_params = {
            "api_key": config["stripe_secret"],
            "line_items": [line_item],
            "mode": "subscription" if is_subscription else "payment",
            "allow_promotion_codes": True,
            "success_url": f"https://t.me/{project}",
            "cancel_url": f"https://t.me/{project}",
            "metadata": {
                "telegram_user_id": user_id,
                "package_id": paquete_id,
                "points_awarded": paquete["points"],
                "priority_days": priority_days,
                "is_subscription": str(is_subscription),
                "project": project
            }
        }

        # SCA / European Compliance: Guardar tarjeta para cobros futuros (off_session)
        if not is_subscription:
            session_params["payment_intent_data"] = {"setup_future_usage": "off_session"}

        session = stripe.checkout.Session.create(**session_params)
        return {"url": session.url}
    except Exception as e:
        logging.error(f"Error al crear la sesión: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/webhook/stripe")
async def legacy_stripe_webhook(request: Request, stripe_signature: str = Header(None, alias="Stripe-Signature")):
    # Ruta de compatibilidad para el bot antiguo que no enviaba {project} en la URL
    # Redirigimos internamente a la lógica pasándole "monkeyvideos" (que usa users2)
    return await stripe_webhook("monkeyvideos", request, stripe_signature)

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
        
        event_project = metadata.get("project", "monkeyvideos") # Por defecto, si no hay project, es el bot antiguo (monkeyvideos)
        if event_project != project:
            return JSONResponse(status_code=200, content={"status": "ignored", "reason": "project_mismatch"})

        user_id_str = metadata.get("telegram_user_id")
        package_id = metadata.get("package_id")
        points_awarded = metadata.get("points_awarded")
        priority_days = metadata.get("priority_days", 0)
        is_sub_meta = metadata.get("is_subscription") == "True"

        try:
            user_id = int(user_id_str)
            points_awarded = int(points_awarded)
            priority_days = int(priority_days)
        except (ValueError, TypeError):
            return JSONResponse(status_code=400, content={"status": "error", "message": "Datos de user o puntos inválidos"})

        if user_id is not None and package_id in PURCHASE_OFFERS:
            try:
                # Update using the specific table for this project
                table_name = config["users_table"]
                database.update_user_points(user_id, points_awarded, table_name)
                
                if priority_days > 0:
                    database.add_priority_days(user_id, priority_days, table_name)
                    
                if is_sub_meta:
                    database.set_subscriber_status(user_id, True, table_name)

                # Send Telegram notification
                bot_token = config["bot_token"]
                if bot_token:
                    bot = Bot(token=bot_token)
                    await bot.send_message(
                        chat_id=user_id,
                        text=f"🎉 **¡Recarga exitosa!** <b>{points_awarded}</b> puntos han sido añadidos a tu cuenta. Se han sumado tus días de prioridad.",
                        parse_mode="HTML"
                    )
            except Exception as e:
                logging.error(f"Error procesando webhook: {e}", exc_info=True)

    elif event["type"] in ["customer.subscription.updated", "customer.subscription.created"]:
        subscription = event["data"]["object"]
        status = subscription.get("status")
        metadata = subscription.get("metadata", {})
        
        event_project = metadata.get("project", "monkeyvideos")
        if event_project != project:
            return JSONResponse(status_code=200, content={"status": "ignored"})

        user_id_str = metadata.get("telegram_user_id")
        if user_id_str and status == "incomplete":
            try:
                user_id = int(user_id_str)
                table_name = config["users_table"]
                user_data = database.get_user(user_id, table_name)
                user_lang = user_data.get("language", "en") if user_data else "en"
                
                if user_lang == "es":
                    text_msg = "⚠️ **Tu suscripción requiere atención.** El pago inicial no se pudo completar automáticamente. Por favor, revisa tu correo para autorizar el pago o intenta nuevamente."
                else:
                    text_msg = "⚠️ **Your subscription requires attention.** The initial payment could not be completed automatically. Please check your email to authorize the payment or try again."

                bot_token = config["bot_token"]
                if bot_token:
                    bot = Bot(token=bot_token)
                    await bot.send_message(
                        chat_id=user_id,
                        text=text_msg,
                        parse_mode="HTML"
                    )
            except Exception as e:
                logging.error(f"Error procesando sub incompleta: {e}")

    elif event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        metadata = invoice.get("subscription_details", {}).get("metadata", {}) or invoice.get("metadata", {})
        
        if not metadata:
            customer_id = invoice.get("customer")
            if customer_id:
                customer = stripe.Customer.retrieve(customer_id, api_key=config["stripe_secret"])
                metadata = customer.get("metadata", {})

        user_id_str = metadata.get("telegram_user_id")
        if user_id_str:
            try:
                user_id = int(user_id_str)
                table_name = config["users_table"]
                user_data = database.get_user(user_id, table_name)
                user_lang = user_data.get("language", "en") if user_data else "en"
                
                bot_token = config["bot_token"]
                hosted_invoice_url = invoice.get("hosted_invoice_url")
                
                if bot_token:
                    bot = Bot(token=bot_token)
                    if user_lang == "es":
                        msg = "❌ **El pago de tu suscripción ha fallado.**"
                        if hosted_invoice_url:
                            msg += f"\n\nPuedes intentar pagar manualmente aquí: <a href='{hosted_invoice_url}'>Pagar Factura</a>"
                        else:
                            msg += "\n\nPor favor, actualiza tu método de pago en el banco o usa otra tarjeta."
                    else:
                        msg = "❌ **Your subscription payment has failed.**"
                        if hosted_invoice_url:
                            msg += f"\n\nYou can try to pay manually here: <a href='{hosted_invoice_url}'>Pay Invoice</a>"
                        else:
                            msg += "\n\nPlease update your payment method with your bank or use a different card."
                    
                    await bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML")
            except Exception as e:
                logging.error(f"Error procesando invoice.payment_failed: {e}")

    elif event["type"] == "customer.subscription.deleted":
        session = event["data"]["object"]
        metadata = session.get("metadata", {})
        
        event_project = metadata.get("project", "monkeyvideos")
        if event_project != project:
            return JSONResponse(status_code=200, content={"status": "ignored", "reason": "project_mismatch"})

        user_id_str = metadata.get("telegram_user_id")
        if user_id_str:
            try:
                user_id = int(user_id_str)
                table_name = config["users_table"]
                database.set_subscriber_status(user_id, False, table_name)
            except Exception as e:
                logging.error(f"Error procesando cancelacion de subscripcion: {e}", exc_info=True)

    return JSONResponse(status_code=200, content={"status": "ok"})
