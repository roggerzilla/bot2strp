-- ============================================================================
-- Migración NOWPayments — ejecutar en el SQL Editor de Supabase
-- Requiere que migrations.sql (clone_id + PK compuesta + tabla clones) ya esté
-- aplicado, cosa que es el caso en producción.
-- ============================================================================

-- 1. Registro de pagos -------------------------------------------------------
--    La llave de idempotencia es order_id (la generamos nosotros al crear la
--    invoice). payment_id NO sirve como llave: no existe hasta que el usuario
--    elige moneda, y una invoice puede generar varios pagos.
CREATE TABLE IF NOT EXISTS payments_nowpayments (
    id              BIGSERIAL PRIMARY KEY,
    order_id        TEXT UNIQUE NOT NULL,
    payment_id      TEXT UNIQUE,            -- nullable: lo rellena el IPN
    invoice_id      TEXT,
    project         TEXT NOT NULL,
    user_id         BIGINT NOT NULL,
    clone_id        UUID NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
    package_id      TEXT NOT NULL,
    amount_usd      NUMERIC NOT NULL,
    points_awarded  BIGINT NOT NULL,
    priority_days   INT NOT NULL DEFAULT 0,
    crypto_currency TEXT,
    actually_paid   NUMERIC,
    status          TEXT NOT NULL DEFAULT 'waiting',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_np_user  ON payments_nowpayments(user_id, clone_id);
CREATE INDEX IF NOT EXISTS idx_np_status ON payments_nowpayments(status);

-- 2. Confirmación idempotente ------------------------------------------------
--    Todo en UNA transacción: el guard, el abono, la prioridad y la comisión.
--    Devuelve awarded=TRUE sólo si esta llamada fue la que abonó, para que el
--    caller sepa si debe notificar por Telegram.
CREATE OR REPLACE FUNCTION nowpayments_confirm(
    p_order_id      TEXT,
    p_payment_id    TEXT,
    p_pay_currency  TEXT,
    p_actually_paid NUMERIC
)
RETURNS TABLE (
    awarded       BOOLEAN,
    r_user_id     BIGINT,
    r_clone_id    UUID,
    r_points      BIGINT,
    r_owner_id    BIGINT,
    r_commission  BIGINT,
    r_bot_token   TEXT,
    r_bot_username TEXT
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_row         payments_nowpayments%ROWTYPE;
    v_owner_id    BIGINT := NULL;
    v_owner_clone UUID   := NULL;
    v_commission  BIGINT := 0;
    v_bot_token   TEXT   := NULL;
    v_bot_user    TEXT   := NULL;
    v_rows        INT;
BEGIN
    -- Guard: un único UPDATE condicional. Postgres toma lock de fila, así que
    -- de N webhooks concurrentes para el mismo order_id exactamente uno ve
    -- FOUND=true. Los demás salen sin abonar.
    UPDATE payments_nowpayments
       SET status          = 'finished',
           payment_id      = COALESCE(payment_id, p_payment_id),
           crypto_currency = p_pay_currency,
           actually_paid   = p_actually_paid,
           confirmed_at    = NOW()
     WHERE order_id = p_order_id
       AND status <> 'finished'
    RETURNING * INTO v_row;

    IF NOT FOUND THEN
        RETURN QUERY SELECT FALSE, NULL::BIGINT, NULL::UUID, NULL::BIGINT,
                            NULL::BIGINT, NULL::BIGINT, NULL::TEXT, NULL::TEXT;
        RETURN;
    END IF;

    -- Abono atómico: points = points + X, nunca read-modify-write.
    -- priority_expiry se extiende si aún es futura, si no cuenta desde ahora.
    UPDATE users2
       SET points          = points + v_row.points_awarded,
           has_purchased   = TRUE,
           priority_expiry = CASE
               WHEN v_row.priority_days > 0
               THEN GREATEST(COALESCE(priority_expiry, NOW()), NOW())
                    + (v_row.priority_days || ' days')::INTERVAL
               ELSE priority_expiry
           END
     WHERE user_id = v_row.user_id
       AND clone_id = v_row.clone_id;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows = 0 THEN
        -- El usuario no existe en este ámbito. Abortamos TODO (incluido el
        -- status='finished') para no tragarnos el pago en silencio: la fila
        -- queda pendiente, NOWPayments reintenta y queda rastro para arreglarlo.
        RAISE EXCEPTION 'usuario % no existe en clone % (order %)',
            v_row.user_id, v_row.clone_id, p_order_id;
    END IF;

    -- Comisión 10% al dueño del clon, en SU ámbito (clones.owner_clone_id).
    IF v_row.clone_id <> '00000000-0000-0000-0000-000000000000'::UUID THEN
        SELECT c.owner_id, c.owner_clone_id, c.bot_token, c.bot_username
          INTO v_owner_id, v_owner_clone, v_bot_token, v_bot_user
          FROM clones c WHERE c.id = v_row.clone_id;

        IF v_owner_id IS NOT NULL THEN
            v_commission := FLOOR(v_row.points_awarded * 0.10);
            IF v_commission > 0 THEN
                UPDATE users2 SET points = points + v_commission
                 WHERE user_id = v_owner_id AND clone_id = v_owner_clone;
            END IF;
        END IF;
    END IF;

    RETURN QUERY SELECT TRUE, v_row.user_id, v_row.clone_id, v_row.points_awarded,
                        v_owner_id, v_commission, v_bot_token, v_bot_user;
END;
$$;

-- 3. Estados no finales ------------------------------------------------------
CREATE OR REPLACE FUNCTION nowpayments_touch_status(
    p_order_id   TEXT,
    p_payment_id TEXT,
    p_status     TEXT
) RETURNS VOID
LANGUAGE sql
AS $$
    UPDATE payments_nowpayments
       SET status     = p_status,
           payment_id = COALESCE(payment_id, p_payment_id)
     WHERE order_id = p_order_id
       AND status <> 'finished';   -- nunca degradar una compra ya abonada
$$;


-- ============================================================================
-- 4. OPCIONAL — leer antes de ejecutar. Ver notas en el chat.
--
-- La cuenta de Stripe está cerrada, así que el evento customer.subscription.deleted
-- (lo único que ponía is_subscriber = FALSE) NO puede volver a dispararse nunca.
-- is_priority_user() devuelve True si is_subscriber, sin mirar fecha alguna:
-- todo suscriptor histórico tiene Prioridad 1 GRATIS Y PARA SIEMPRE.
--
-- Esto lo cierra, dando 30 días de prioridad como gesto de buena fe:
--
-- UPDATE users2
--    SET priority_expiry = GREATEST(COALESCE(priority_expiry, NOW()), NOW())
--                          + INTERVAL '30 days',
--        is_subscriber   = FALSE
--  WHERE is_subscriber IS TRUE;
--
-- Cuenta a cuánta gente afecta antes de decidir:
-- SELECT COUNT(*) FROM users2 WHERE is_subscriber IS TRUE;
-- ============================================================================
