"""Backend de pagos NOWPayments — un solo servicio para todos los bots.

Sustituye a stripe_server.py. El bot llama a /crear-pago y recibe una URL de
invoice; NOWPayments manda el IPN a /webhook/nowpayments y aquí se abonan los
puntos y se notifica por Telegram. El bot nunca ve el webhook: el VPS se apaga
a mano y perderíamos pagos.

Arranque en Render:  uvicorn nowpayments_server:app --host 0.0.0.0 --port $PORT
"""
import hashlib
import hmac
import json
import logging
import os

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from telegram import Bot

import nowpayments_db as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
load_dotenv()

app = FastAPI()

NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY")
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://bot2strp.onrender.com")
NOWPAYMENTS_API = "https://api.nowpayments.io/v1"

# Un servicio para todos los proyectos. Los tres bots comparten cuenta de
# NOWPayments (una API key, un IPN secret), así que separar servicios sólo
# duplicaría el mismo secreto.
BOT_CONFIGS = {
    "monkeyvideos": {
        "bot_token": os.environ.get("BOT_TOKEN_MONKEY", os.environ.get("BOT_TOKEN")),
        "tg_username": "monkeyvideosbot",
    },
    # Pendientes de migrar. Hoy videos_2_videos e img_2_img declaran AMBOS el
    # identificador "videos2videos": hay que separarlos antes de activarlos.
    # "videos2videos": {...},
    # "img2img": {...},
}

PURCHASE_OFFERS = {
    # Oferta de entrada, sólo primera compra. $1.50 y no $1.00: con tarifa
    # flotante el mínimo más alto de las monedas activas es DOGE a $1.14
    # (BTC $1.06). A $1.50 entra con todas; a $1.00 fallarían esas dos.
    # Mantiene el ratio original de 159 pts/$.
    "p_1_50":  {"label": "Special: 239 Points ($1.50 USD)",  "amount": 1.50,  "points": 239,   "priority_days": 1,  "first_buy_only": True},
    "p_3_99":  {"label": "400 Points ($3.99 USD)",           "amount": 3.99,  "points": 400,   "priority_days": 7},
    "p_9_99":  {"label": "2000 Points ($9.99 USD)",          "amount": 9.99,  "points": 2000,  "priority_days": 15},
    "p_19_99": {"label": "5000 Points ($19.99 USD)",         "amount": 19.99, "points": 5000,  "priority_days": 30},
    "p_50_00": {"label": "14000 Points ($50.00 USD)",        "amount": 50.00, "points": 14000, "priority_days": 60},
    # Las suscripciones sub_9_99 / sub_19_99 / sub_50_00 se eliminaron: una
    # invoice de crypto es un pago único, no hay tarjeta que cobrar cada mes.
}

FINAL_FAILED = {"failed", "expired", "refunded"}


def verify_ipn_signature(raw_body: bytes, signature: str) -> bool:
    """HMAC-SHA512 del JSON con las claves ordenadas, comparado con x-nowpayments-sig."""
    if not signature or not NOWPAYMENTS_IPN_SECRET:
        return False
    try:
        parsed = json.loads(raw_body)
    except json.JSONDecodeError:
        return False
    # sort_keys ordena recursivamente y separators evita los espacios que Python
    # mete por defecto; así reproducimos el json_encode(ksort(...)) de NOWPayments.
    normalized = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    expected = hmac.new(
        NOWPAYMENTS_IPN_SECRET.strip().encode(),
        normalized.encode(),
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def notify(bot_token: str, chat_id: int, text: str) -> None:
    if not bot_token:
        return
    try:
        await Bot(token=bot_token).send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"No se pudo notificar a {chat_id}: {e}")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/crear-pago")
async def crear_pago(request: Request):
    data = await request.json()
    project = data.get("project")
    package_id = data.get("paquete_id")
    clone_id = data.get("clone_id") or db.NIL_CLONE

    if project not in BOT_CONFIGS:
        return JSONResponse(status_code=400, content={"error": "Proyecto no válido."})
    if package_id not in PURCHASE_OFFERS:
        return JSONResponse(status_code=400, content={"error": "Paquete no válido."})
    if not NOWPAYMENTS_API_KEY:
        return JSONResponse(status_code=500, content={"error": "NOWPAYMENTS_API_KEY no configurada."})

    try:
        user_id = int(data.get("telegram_user_id"))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "user_id inválido."})

    package = PURCHASE_OFFERS[package_id]

    # El gating de primera compra se revalida aquí: el cliente no es de fiar.
    if package.get("first_buy_only") and db.has_purchased(user_id, clone_id):
        return JSONResponse(status_code=400, content={"error": "Esta oferta es sólo para tu primera compra."})

    order_id = db.new_order_id()
    if not db.create_pending_payment(order_id, project, user_id, clone_id, package_id,
                                     package["amount"], package["points"],
                                     package["priority_days"]):
        return JSONResponse(status_code=500, content={"error": "No se pudo registrar la compra."})

    tg = BOT_CONFIGS[project]["tg_username"]
    payload = {
        # price_currency usd: NOWPayments calcula el equivalente en crypto al
        # momento del pago, así la volatilidad no nos afecta.
        "price_amount": package["amount"],
        "price_currency": "usd",
        # Tarifa flotante, explícito y a propósito. La fija congela el cambio 20
        # minutos, cobra un 1% extra y exige mínimos MUCHO más altos: con ella
        # ni un paquete de $3.99 llega al mínimo, ni pagando en la misma moneda
        # que cobramos. No dependemos del ajuste del panel.
        "is_fixed_rate": False,
        # Ambos van explícitos porque los dos suben el mínimo si están activos,
        # y omitirlos deja que mande el default de la cuenta, que no controlamos.
        "is_fee_paid_by_user": False,
        "order_id": order_id,
        "order_description": package["label"],
        "ipn_callback_url": f"{PUBLIC_BASE_URL}/webhook/nowpayments",
        "success_url": f"https://t.me/{tg}",
        "cancel_url": f"https://t.me/{tg}",
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{NOWPAYMENTS_API}/invoice",
                json=payload,
                headers={"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type": "application/json"},
            )
        if resp.status_code not in (200, 201):
            logging.error(f"NOWPayments rechazó la invoice ({resp.status_code}): {resp.text}")
            db.touch_status(order_id, None, "failed")
            return JSONResponse(status_code=502, content={"error": "El procesador de pagos rechazó la solicitud."})

        invoice = resp.json()
        db.attach_invoice_id(order_id, invoice.get("id"))
        return {"url": invoice["invoice_url"], "order_id": order_id}

    except Exception as e:
        logging.error(f"Error creando invoice (order {order_id}): {e}", exc_info=True)
        db.touch_status(order_id, None, "failed")
        return JSONResponse(status_code=500, content={"error": "Error contactando al procesador de pagos."})


@app.post("/webhook/nowpayments")
async def nowpayments_webhook(request: Request, x_nowpayments_sig: str = Header(None, alias="x-nowpayments-sig")):
    raw = await request.body()

    if not verify_ipn_signature(raw, x_nowpayments_sig):
        logging.warning("IPN con firma inválida — descartado.")
        return JSONResponse(status_code=400, content={"error": "firma inválida"})

    body = json.loads(raw)
    order_id = body.get("order_id")
    payment_id = str(body.get("payment_id")) if body.get("payment_id") else None
    status = body.get("payment_status")

    if not order_id:
        return JSONResponse(status_code=200, content={"status": "ignorado", "reason": "sin order_id"})

    # Sólo 'finished' abona. waiting/confirming/confirmed/sending son tránsito.
    if status != "finished":
        if status in FINAL_FAILED or status in ("waiting", "confirming", "confirmed", "sending", "partially_paid"):
            db.touch_status(order_id, payment_id, status)
        if status == "partially_paid":
            logging.warning(f"Pago parcial en order {order_id}: pagó {body.get('actually_paid')} "
                            f"de {body.get('pay_amount')} {body.get('pay_currency')}. Requiere revisión manual.")
        return JSONResponse(status_code=200, content={"status": "ok"})

    row = db.get_payment(order_id)
    if not row:
        logging.error(f"IPN 'finished' para un order_id desconocido: {order_id}")
        return JSONResponse(status_code=200, content={"status": "ignorado", "reason": "order desconocido"})

    try:
        result = db.confirm(order_id, payment_id, body.get("pay_currency"), body.get("actually_paid"))
    except Exception as e:
        # 500 a propósito: que NOWPayments reintente. No se abonó nada.
        logging.error(f"nowpayments_confirm falló (order {order_id}): {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": "error interno"})

    if not result.get("awarded"):
        # Reenvío del mismo IPN, o el usuario pagó dos veces la misma invoice.
        # En el segundo caso hace falta reembolso manual, de ahí el log.
        logging.info(f"IPN duplicado para order {order_id} (payment {payment_id}) — sin abonar.")
        return JSONResponse(status_code=200, content={"status": "ya procesado"})

    project_cfg = BOT_CONFIGS.get(row["project"], {})
    master_token = project_cfg.get("bot_token")
    points = result["r_points"]
    user_id = result["r_user_id"]

    # Al comprador le escribe el bot con el que compró: el clon si vino de un clon.
    user_bot_token = result.get("r_bot_token") or master_token
    await notify(user_bot_token, user_id,
                 f"🎉 <b>¡Recarga exitosa!</b> Se añadieron <b>{points}</b> puntos a tu cuenta "
                 f"y se sumaron tus días de prioridad.")

    if result.get("r_owner_id") and result.get("r_commission"):
        await notify(master_token, result["r_owner_id"],
                     f"💰 <b>¡Comisión recibida!</b>\n\nUn usuario de tu clon "
                     f"@{result.get('r_bot_username') or '???'} compró <b>{points}</b> puntos.\n"
                     f"Ganaste <b>{result['r_commission']}</b> puntos de comisión (10%). 🎉")

    logging.info(f"Order {order_id} confirmado: {points} pts a user {user_id} "
                 f"(clone {result['r_clone_id']}, {body.get('pay_currency')}).")
    return JSONResponse(status_code=200, content={"status": "ok"})
