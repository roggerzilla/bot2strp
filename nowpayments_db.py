"""Acceso a datos para el flujo NOWPayments.

Separado de database.py a propósito: database.py es el módulo del flujo Stripe
(muerto) y no filtra por clone_id, cosa que con la PK compuesta (user_id, clone_id)
escribe en todas las filas del usuario a la vez. Nada de aquí depende de él.
"""
import logging
import os
import uuid

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Faltan SUPABASE_URL / SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

NIL_CLONE = "00000000-0000-0000-0000-000000000000"


def new_order_id() -> str:
    return uuid.uuid4().hex


def create_pending_payment(order_id: str, project: str, user_id: int, clone_id: str,
                           package_id: str, amount_usd: float, points: int,
                           priority_days: int) -> bool:
    """Registra la compra ANTES de mandar al usuario a pagar.

    Los puntos se congelan aquí. El IPN nunca decide cuántos puntos dar: los lee
    de esta fila, así un payload manipulado no puede inflar el abono.
    """
    try:
        supabase.table("payments_nowpayments").insert({
            "order_id": order_id,
            "project": project,
            "user_id": user_id,
            "clone_id": clone_id or NIL_CLONE,
            "package_id": package_id,
            "amount_usd": amount_usd,
            "points_awarded": points,
            "priority_days": priority_days,
            "status": "waiting",
        }).execute()
        return True
    except Exception as e:
        logging.error(f"create_pending_payment falló (order {order_id}): {e}", exc_info=True)
        return False


def attach_invoice_id(order_id: str, invoice_id: str) -> None:
    try:
        supabase.table("payments_nowpayments").update(
            {"invoice_id": str(invoice_id)}
        ).eq("order_id", order_id).execute()
    except Exception as e:
        logging.error(f"attach_invoice_id falló (order {order_id}): {e}")


def confirm(order_id: str, payment_id: str, pay_currency: str, actually_paid):
    """Confirma el pago de forma idempotente. Devuelve el dict de la RPC.

    awarded=True sólo en la llamada que realmente abonó; en reenvíos del mismo
    IPN devuelve awarded=False. Deja que las excepciones suban: el caller debe
    responder 500 para que NOWPayments reintente.
    """
    resp = supabase.rpc("nowpayments_confirm", {
        "p_order_id": order_id,
        "p_payment_id": payment_id,
        "p_pay_currency": pay_currency,
        "p_actually_paid": actually_paid,
    }).execute()
    return resp.data[0] if resp.data else {"awarded": False}


def confirm_v2v(order_id: str, payment_id: str, pay_currency: str, actually_paid):
    """Confirmación para el pool usersv2v (videos_2_videos + img_2_img). Sin
    clones ni comisiones. Devuelve {awarded, r_user_id, r_points}."""
    resp = supabase.rpc("nowpayments_confirm_v2v", {
        "p_order_id": order_id,
        "p_payment_id": payment_id,
        "p_pay_currency": pay_currency,
        "p_actually_paid": actually_paid,
    }).execute()
    return resp.data[0] if resp.data else {"awarded": False}


def confirm_img(order_id: str, payment_id: str, pay_currency: str, actually_paid):
    """Confirmación para el pool users4 (img_2_img). Sin clones."""
    resp = supabase.rpc("nowpayments_confirm_img", {
        "p_order_id": order_id,
        "p_payment_id": payment_id,
        "p_pay_currency": pay_currency,
        "p_actually_paid": actually_paid,
    }).execute()
    return resp.data[0] if resp.data else {"awarded": False}


def confirm_t2v2(order_id: str, payment_id: str, pay_currency: str, actually_paid):
    """Confirmación clone-aware para text-to-video, aislada en users_t2v2."""
    resp = supabase.rpc("nowpayments_confirm_t2v2", {
        "p_order_id": order_id,
        "p_payment_id": payment_id,
        "p_pay_currency": pay_currency,
        "p_actually_paid": actually_paid,
    }).execute()
    return resp.data[0] if resp.data else {"awarded": False}


def touch_status(order_id: str, payment_id: str, status: str) -> None:
    try:
        supabase.rpc("nowpayments_touch_status", {
            "p_order_id": order_id,
            "p_payment_id": payment_id,
            "p_status": status,
        }).execute()
    except Exception as e:
        logging.error(f"touch_status falló (order {order_id}): {e}")


def get_payment(order_id: str):
    try:
        resp = supabase.table("payments_nowpayments").select("*").eq(
            "order_id", order_id).execute()
        return resp.data[0] if resp.data else None
    except Exception as e:
        logging.error(f"get_payment falló (order {order_id}): {e}")
        return None


def has_purchased(user_id: int, clone_id: str) -> bool:
    try:
        resp = supabase.table("users2").select("has_purchased").eq(
            "user_id", user_id).eq("clone_id", clone_id or NIL_CLONE).execute()
        return bool(resp.data and resp.data[0].get("has_purchased"))
    except Exception as e:
        logging.error(f"has_purchased falló ({user_id}): {e}")
        return False


def has_purchased_t2v2(user_id: int, clone_id: str) -> bool:
    """Gating de primera compra exclusivamente para users_t2v2."""
    try:
        resp = supabase.table("users_t2v2").select("has_purchased").eq(
            "user_id", user_id).eq("clone_id", clone_id or NIL_CLONE).execute()
        return bool(resp.data and resp.data[0].get("has_purchased"))
    except Exception as e:
        logging.error(f"has_purchased_t2v2 falló ({user_id}): {e}")
        return False


def get_clone(clone_id: str):
    try:
        resp = supabase.table("clones").select("*").eq("id", clone_id).execute()
        return resp.data[0] if resp.data else None
    except Exception as e:
        logging.error(f"get_clone falló ({clone_id}): {e}")
        return None
