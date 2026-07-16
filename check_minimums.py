"""Consulta los mínimos reales de NOWPayments para tu cuenta.

Herramienta local de diagnóstico — no la usa el servidor, no se despliega.

Los mínimos dependen del par (moneda que paga el cliente -> tu wallet de cobro)
y de la comisión de red del momento, así que hay que preguntárselos a la API en
vez de asumirlos. Vuelve a correrlo si las fees se disparan.

Uso (cmd.exe):
    set NOWPAYMENTS_API_KEY=tu_clave
    python check_minimums.py usdttrc20      # <- todas las monedas -> tu COBRO
    python check_minimums.py --mono         # <- cada moneda -> sí misma

El modo --mono es el diagnóstico: sin conversión ni swap, el mínimo sólo puede
venir de la comisión de red. Si aun así todos los pares dan la misma cifra, hay
un suelo global y no es cuestión de elegir mejor moneda.
"""
import os
import sys

import httpx

API = "https://api.nowpayments.io/v1"
KEY = os.environ.get("NOWPAYMENTS_API_KEY")

# Las monedas que quieres ofrecer a tus usuarios.
COINS = ["btc", "eth", "usdttrc20", "usdterc20", "usdcmatic", "ltc", "trx", "bnbbsc", "sol", "doge"]


def fetch_min(client, frm, to, fixed=False):
    """OJO: sin is_fixed_rate la API devuelve el mínimo FLOTANTE, que es mucho
    más bajo que el real si tus facturas usan tarifa fija. Hay que preguntar por
    el modo que de verdad estés usando."""
    params = {
        "currency_from": frm,
        "currency_to": to,
        "fiat_equivalent": "usd",
    }
    if fixed:
        params["is_fixed_rate"] = "true"
    r = client.get(f"{API}/min-amount", params=params)
    if r.status_code != 200:
        return None, f"ERROR {r.status_code}: {r.text[:40]}"
    return r.json(), None


def main():
    if not KEY:
        sys.exit("Falta NOWPAYMENTS_API_KEY en el entorno.")

    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if not arg:
        sys.exit("Falta argumento. Ej: python check_minimums.py usdttrc20  |  --mono")

    mono = arg == "--mono"
    if mono:
        print("\nMínimos MONO-MONEDA (cada moneda cobrada en sí misma)\n")
    else:
        print(f"\nMínimos para cobrar en {arg.upper()}\n")
    print(f"{'paga con':<24} {'FLOTANTE':>12} {'FIJA':>12}")
    print("-" * 50)

    worst_float = 0.0
    with httpx.Client(timeout=20, headers={"x-api-key": KEY}) as client:
        for coin in COINS:
            to = coin if mono else arg
            label = f"{coin} -> {to}" if mono else coin
            cells = []
            for fixed in (False, True):
                try:
                    d, err = fetch_min(client, coin, to, fixed=fixed)
                    if err:
                        cells.append("error")
                        continue
                    usd = d.get("fiat_equivalent")
                    if isinstance(usd, (int, float)):
                        cells.append(f"${usd:.2f}")
                        if not fixed:
                            worst_float = max(worst_float, usd)
                    else:
                        cells.append("?")
                except Exception:
                    cells.append("fallo")
            print(f"{label:<24} {cells[0]:>12} {cells[1]:>12}")

    print("-" * 50)
    print("\nFLOTANTE = el cambio se calcula al pagar (lo que tú querías).")
    print("FIJA     = el cambio se congela 20 min, cuesta 1% extra y pide mínimos más altos.")
    if worst_float:
        print(f"\nCon tarifa flotante, el paquete más barato debe pasar de ${worst_float:.2f}.\n")


if __name__ == "__main__":
    main()
