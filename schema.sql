-- ============================================================
-- RECALL SaaS — EMAIL-ONLY PRODUCTION SCHEMA
-- Healthcare-agnostic automated EMAIL recall engine
-- Stack: FastAPI + Supabase + Resend
-- SCHEMA v2.0.0 EMAIL MIGRATION ✅
-- ============================================================

CREATE SCHEMA IF NOT EXISTS extensions;

CREATE EXTENSION IF NOT EXISTS "uuid-ossp"          WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS "pgcrypto"           WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS "citext"             WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS "btree_gin"          WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS "pg_stat_statements" WITH SCHEMA extensions;

-- ============================================================
-- SCHEMA VERSION CONTROL
-- ============================================================
CREATE TABLE IF NOT EXISTS public.schema_version (
    id          INTEGER PRIMARY KEY,
    version     TEXT        NOT NULL,
    description TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by  TEXT        NOT NULL DEFAULT current_user,
    checksum    TEXT        NOT NULL
);

INSERT INTO public.schema_version (id, version, description, checksum)
VALUES (2, '2.0.0', 'Email-only migration - Resend integration', md5('email-migration-v2'))
ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version, description = EXCLUDED.description, checksum = EXCLUDED.checksum;

-- ============================================================
-- VALIDATION FUNCTION
-- ============================================================
CREATE OR REPLACE FUNCTION public.validate_message_sequence(sequence JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, public
AS $$
DECLARE
    item       JSONB;
    delay_days INTEGER;
BEGIN
    IF jsonb_typeof(sequence) != 'array' THEN
        RETURN FALSE;
    END IF;

    FOR item IN SELECT * FROM jsonb_array_elements(sequence)
    LOOP
        IF NOT (item ? 'delay_days' AND item ? 'subject_template' AND item ? 'body_template') THEN
            RETURN FALSE;
        END IF;

        IF jsonb_typeof(item->'delay_days') NOT IN ('number', 'string') OR
           jsonb_typeof(item->'subject_template') != 'string' OR
           jsonb_typeof(item->'body_template') != 'string' THEN
            RETURN FALSE;
        END IF;

        BEGIN
            delay_days := (item->>'delay_days')::INTEGER;
        EXCEPTION WHEN others THEN
            RETURN FALSE;
        END;

        IF delay_days < 0 OR delay_days > 365 THEN
            RETURN FALSE;
        END IF;
    END LOOP;

    RETURN TRUE;
END;
$$;

-- ============================================================
-- ENUM TYPES
-- ============================================================
DO $$ BEGIN CREATE TYPE public.service_type_enum AS ENUM (
    'dental','gp','physio','optometry','chiro','vet',
    'mental_health','dermatology','podiatry','audiology','custom'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TYPE public.tenant_plan_enum AS ENUM (
    'starter','growth','enterprise','custom'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TYPE public.recall_status_enum AS ENUM (
    'pending','in_progress','booked','completed',
    'opted_out','failed','snoozed','cancelled'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TYPE public.email_status_enum AS ENUM (
    'queued','sent','delivered','bounced','complained','failed','opened','clicked'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TYPE public.booking_status_enum AS ENUM (
    'confirmed','cancelled','completed','no_show'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ============================================================
-- TENANTS
-- ============================================================
CREATE TABLE IF NOT EXISTS public.tenants (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT NOT NULL,
    slug                CITEXT UNIQUE NOT NULL,
    service_type        public.service_type_enum NOT NULL,
    phone_number        TEXT,

    api_key             TEXT UNIQUE,
    api_key_last_chars  TEXT GENERATED ALWAYS AS (RIGHT(api_key, 4)) STORED,
    api_key_rotated_at  TIMESTAMPTZ,
    webhook_secret      TEXT,

    resend_api_key      TEXT,
    email_config        JSONB NOT NULL DEFAULT '{"from_name": "RECALL", "from_email": "noreply@recall.health"}' CHECK (
        jsonb_typeof(email_config) = 'object' AND
        email_config ? 'from_name' AND email_config ? 'from_email'
    ),

    timezone            TEXT NOT NULL DEFAULT 'Australia/Sydney',
    country_code        TEXT NOT NULL DEFAULT 'AU',
    locale              TEXT NOT NULL DEFAULT 'en-AU',
    business_hours      JSONB NOT NULL DEFAULT '{
        "monday":    {"start":"09:00","end":"17:00"},
        "tuesday":   {"start":"09:00","end":"17:00"},
        "wednesday": {"start":"09:00","end":"17:00"},
        "thursday":  {"start":"09:00","end":"17:00"},
        "friday":    {"start":"09:00","end":"17:00"},
        "saturday":  {"start":null,"end":null},
        "sunday":    {"start":null,"end":null}
    }',

    active              BOOLEAN NOT NULL DEFAULT TRUE,
    trial_ends_at       TIMESTAMPTZ,
    plan                public.tenant_plan_enum NOT NULL DEFAULT 'starter',
    subscription_id     TEXT,
    billing_email       TEXT,

    limits              JSONB NOT NULL DEFAULT '{
        "max_patients": 1000,
        "max_recalls_per_month": 500,
        "max_emails_per_month": 1000,
        "max_users": 5
    }',

    current_usage       JSONB NOT NULL DEFAULT '{
        "patient_count": 0,
        "recalls_this_month": 0,
        "emails_this_month": 0,
        "bookings_this_month": 0
    }',

    settings            JSONB NOT NULL DEFAULT '{
        "auto_opt_in": false,
        "require_consent": true,
        "default_recall_interval_days": 180,
        "reminder_days_before": [7,1],
        "booking_window_days": 90,
        "allow_email_booking": true,
        "allow_opt_out": true,
        "allow_snooze": true
    }',

    metadata            JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT valid_phone     CHECK (phone_number IS NULL OR phone_number ~ '^\+[1-9][0-9]{1,14}$'),
    CONSTRAINT valid_api_key   CHECK (api_key IS NULL OR api_key ~ '^[A-Za-z0-9_-]{32,64}$'),
    CONSTRAINT valid_biz_hours CHECK (jsonb_typeof(business_hours) = 'object'),
    CONSTRAINT valid_limits    CHECK (jsonb_typeof(limits) = 'object'),
    CONSTRAINT valid_settings  CHECK (jsonb_typeof(settings) = 'object')
);

COMMENT ON TABLE  public.tenants IS 'Multi-tenant healthcare practices/clinics';
COMMENT ON COLUMN public.tenants.email_config IS 'JSON: from_name, from_email, reply_to';
COMMENT ON COLUMN public.tenants.resend_api_key IS 'Tenant-specific Resend API key (encrypted at rest)';

-- ============================================================
-- RECALL TEMPLATES
-- ============================================================
CREATE TABLE IF NOT EXISTS public.recall_templates (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID REFERENCES public.tenants(id) ON DELETE CASCADE,
    service_type         public.service_type_enum,
    name                 TEXT NOT NULL,
    description          TEXT,
    recall_interval_days INTEGER NOT NULL CHECK (recall_interval_days > 0),
    priority             INTEGER NOT NULL DEFAULT 1 CHECK (priority BETWEEN 1 AND 5),

    message_sequence     JSONB NOT NULL CHECK (
        jsonb_typeof(message_sequence) = 'array'
        AND jsonb_array_length(message_sequence) > 0
        AND public.validate_message_sequence(message_sequence)
    ),

    applicable_days      TEXT[]  NOT NULL DEFAULT ARRAY['monday','tuesday','wednesday','thursday','friday'],
    max_reminders        INTEGER NOT NULL DEFAULT 3 CHECK (max_reminders > 0),
    snooze_days          INTEGER NOT NULL DEFAULT 7  CHECK (snooze_days > 0),

    conditions           JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(conditions) = 'object'),
    active               BOOLEAN NOT NULL DEFAULT TRUE,
    is_global            BOOLEAN GENERATED ALWAYS AS (tenant_id IS NULL) STORED,

    metadata             JSONB NOT NULL DEFAULT '{}',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_recall_templates_tenant_name
    ON public.recall_templates (COALESCE(tenant_id::text, '00000000-0000-0000-0000-000000000000'), name);

COMMENT ON TABLE public.recall_templates IS 'Recall email templates; message_sequence must include subject_template and body_template';

-- ============================================================
-- PATIENTS
-- ============================================================
CREATE TABLE IF NOT EXISTS public.patients (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    external_id         TEXT,

    first_name          TEXT NOT NULL,
    last_name           TEXT NOT NULL,
    preferred_name      TEXT,
    phone               TEXT,
    email               CITEXT NOT NULL,
    date_of_birth       DATE,
    gender              TEXT,

    communication_preferences JSONB NOT NULL DEFAULT '{
        "email": true,
        "sms": false,
        "whatsapp": false,
        "preferred_time": "anytime",
        "do_not_disturb": false
    }' CHECK (jsonb_typeof(communication_preferences) = 'object'),

    opted_out           BOOLEAN NOT NULL DEFAULT FALSE,
    opted_out_at        TIMESTAMPTZ,
    opted_out_reason    TEXT,
    opted_in_at         TIMESTAMPTZ,
    consent_version     TEXT,
    consent_given_at    TIMESTAMPTZ,

    last_appointment_at TIMESTAMPTZ,
    last_contact_at     TIMESTAMPTZ,
    last_response_at    TIMESTAMPTZ,

    metadata            JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(metadata) = 'object'),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by          UUID,
    updated_by          UUID,

    CONSTRAINT valid_phone   CHECK (phone IS NULL OR phone ~ '^\+[1-9][0-9]{1,14}$'),
    CONSTRAINT valid_email   CHECK (email ~ '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'),
    CONSTRAINT consent_check CHECK (
        (opted_out = TRUE  AND opted_out_at IS NOT NULL) OR
        (opted_out = FALSE)
    ),
    UNIQUE(tenant_id, external_id),
    UNIQUE(tenant_id, email)
);

CREATE INDEX IF NOT EXISTS idx_patients_tenant_lookup    ON public.patients(tenant_id, email, opted_out);
CREATE INDEX IF NOT EXISTS idx_patients_dob              ON public.patients(tenant_id, date_of_birth) WHERE date_of_birth IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_patients_last_appointment ON public.patients(tenant_id, last_appointment_at DESC);
CREATE INDEX IF NOT EXISTS idx_patients_metadata_gin     ON public.patients USING GIN (metadata);

-- ============================================================
-- RECALLS
-- ============================================================
CREATE TABLE IF NOT EXISTS public.recalls (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    patient_id   UUID NOT NULL REFERENCES public.patients(id) ON DELETE CASCADE,
    template_id  UUID REFERENCES public.recall_templates(id),

    recall_type      TEXT NOT NULL,
    last_appointment DATE,
    due_date         DATE NOT NULL,
    priority         INTEGER NOT NULL DEFAULT 1 CHECK (priority BETWEEN 1 AND 5),

    status              public.recall_status_enum NOT NULL DEFAULT 'pending',
    booked_at           TIMESTAMPTZ,
    snoozed_until       DATE,
    cancelled_at        TIMESTAMPTZ,
    cancelled_reason    TEXT,

    sequence_step       INTEGER NOT NULL DEFAULT 0,
    next_send_at        TIMESTAMPTZ,
    last_sent_at        TIMESTAMPTZ,
    messages_sent       INTEGER NOT NULL DEFAULT 0 CHECK (messages_sent >= 0),

    last_response_at    TIMESTAMPTZ,
    last_response_type  TEXT,

    booking_link            TEXT,
    booking_link_expires_at TIMESTAMPTZ,
    booking_id              UUID,

    first_response_time INTERVAL GENERATED ALWAYS AS (last_response_at - created_at) STORED,

    notes       TEXT,
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT valid_due_date  CHECK (due_date >= created_at::DATE),
    CONSTRAINT valid_snooze    CHECK (snoozed_until IS NULL OR snoozed_until >= created_at::DATE),
    CONSTRAINT valid_next_send CHECK (
        (status IN ('pending', 'in_progress') AND next_send_at IS NOT NULL) OR
        (status NOT IN ('pending', 'in_progress') AND next_send_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_recalls_pending        ON public.recalls(tenant_id, next_send_at) WHERE status IN ('pending', 'in_progress');
CREATE INDEX IF NOT EXISTS idx_recalls_due_date       ON public.recalls(tenant_id, due_date)     WHERE status NOT IN ('booked', 'completed', 'cancelled');
CREATE INDEX IF NOT EXISTS idx_recalls_patient_status ON public.recalls(patient_id, status);
CREATE INDEX IF NOT EXISTS idx_recalls_metadata_gin   ON public.recalls USING GIN (metadata);
CREATE INDEX IF NOT EXISTS idx_recalls_booking        ON public.recalls(booking_id) WHERE booking_id IS NOT NULL;

-- ============================================================
-- EMAIL MESSAGES LOG
-- ============================================================
CREATE TABLE IF NOT EXISTS public.email_messages (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    patient_id UUID NOT NULL REFERENCES public.patients(id) ON DELETE CASCADE,
    recall_id  UUID REFERENCES public.recalls(id),

    message_id TEXT,
    from_email TEXT NOT NULL,
    to_email   TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body_text  TEXT,
    body_html  TEXT,

    status         public.email_status_enum NOT NULL DEFAULT 'queued',
    status_history JSONB[] NOT NULL DEFAULT ARRAY[]::JSONB[],
    error_code     TEXT,
    error_message  TEXT,

    sent_at      TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    bounced_at   TIMESTAMPTZ,
    opened_at    TIMESTAMPTZ,
    clicked_at   TIMESTAMPTZ,
    failed_at    TIMESTAMPTZ,

    sequence_step INTEGER,
    template_used TEXT,
    metadata      JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT valid_emails CHECK (
        from_email ~ '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$' AND
        to_email   ~ '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'
    ),
    CONSTRAINT valid_timeline CHECK (
        (sent_at IS NOT NULL AND status != 'queued') OR
        (sent_at IS NULL     AND status = 'queued')
    )
);

CREATE INDEX IF NOT EXISTS idx_email_messages_tenant_date ON public.email_messages(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_messages_recall      ON public.email_messages(recall_id);
CREATE INDEX IF NOT EXISTS idx_email_messages_message_id  ON public.email_messages(message_id) WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_email_messages_status      ON public.email_messages(status, created_at) WHERE status NOT IN ('delivered','failed');

-- ============================================================
-- INBOUND RESPONSES
-- ============================================================
CREATE TABLE IF NOT EXISTS public.inbound_responses (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID REFERENCES public.tenants(id),
    patient_id UUID REFERENCES public.patients(id),
    recall_id  UUID REFERENCES public.recalls(id),

    message_id    TEXT NOT NULL UNIQUE,
    from_email    TEXT NOT NULL,
    to_email      TEXT NOT NULL,
    subject       TEXT,
    body_text     TEXT,
    body_html     TEXT,
    thread_id     TEXT,
    in_reply_to   TEXT,

    intent            TEXT,
    intent_confidence NUMERIC(3,2) CHECK (intent_confidence BETWEEN 0 AND 1),
    entities          JSONB NOT NULL DEFAULT '{}',

    processed        BOOLEAN NOT NULL DEFAULT FALSE,
    processed_at     TIMESTAMPTZ,
    processing_error TEXT,
    action_taken     TEXT,

    response_sent       BOOLEAN NOT NULL DEFAULT FALSE,
    response_message_id UUID REFERENCES public.email_messages(id),

    metadata    JSONB NOT NULL DEFAULT '{}',
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_inbound_responses_unprocessed ON public.inbound_responses(tenant_id, received_at) WHERE processed = FALSE;
CREATE INDEX IF NOT EXISTS idx_inbound_responses_patient      ON public.inbound_responses(patient_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbound_responses_email        ON public.inbound_responses(from_email, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbound_responses_intent       ON public.inbound_responses(intent) WHERE intent IS NOT NULL;

-- ============================================================
-- BOOKINGS
-- ============================================================
CREATE TABLE IF NOT EXISTS public.bookings (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    patient_id UUID NOT NULL REFERENCES public.patients(id),
    recall_id  UUID REFERENCES public.recalls(id) UNIQUE,

    external_booking_id TEXT,
    appointment_time    TIMESTAMPTZ NOT NULL,
    duration_minutes    INTEGER CHECK (duration_minutes > 0),
    provider_id         TEXT,
    provider_name       TEXT,
    location            TEXT,

    status           public.booking_status_enum NOT NULL DEFAULT 'confirmed',
    cancelled_at     TIMESTAMPTZ,
    cancelled_reason TEXT,

    source         TEXT NOT NULL DEFAULT 'recall_email',
    source_details JSONB NOT NULL DEFAULT '{}',

    revenue_amount   NUMERIC(10,2) CHECK (revenue_amount >= 0),
    revenue_currency TEXT DEFAULT 'AUD',

    metadata   JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT future_booking CHECK (appointment_time > created_at)
);

CREATE INDEX IF NOT EXISTS idx_bookings_tenant_date ON public.bookings(tenant_id, appointment_time DESC);
CREATE INDEX IF NOT EXISTS idx_bookings_patient     ON public.bookings(patient_id, appointment_time DESC);
CREATE INDEX IF NOT EXISTS idx_bookings_status      ON public.bookings(status, appointment_time) WHERE status = 'confirmed';
CREATE INDEX IF NOT EXISTS idx_bookings_external    ON public.bookings(external_booking_id) WHERE external_booking_id IS NOT NULL;

-- ============================================================
-- WEBHOOK EVENTS
-- ============================================================
CREATE TABLE IF NOT EXISTS public.webhook_events (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES public.tenants(id),

    source     TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_id   TEXT,

    headers         JSONB NOT NULL DEFAULT '{}',
    payload         JSONB NOT NULL,
    signature       TEXT,
    signature_valid BOOLEAN,

    processed    BOOLEAN NOT NULL DEFAULT FALSE,
    processed_at TIMESTAMPTZ,
    retry_count  INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    last_error   TEXT,

    response_status INTEGER,
    response_body   TEXT,

    metadata    JSONB NOT NULL DEFAULT '{}',
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_webhook_events_unprocessed ON public.webhook_events(tenant_id, received_at) WHERE processed = FALSE;
CREATE INDEX IF NOT EXISTS idx_webhook_events_source      ON public.webhook_events(source, event_type, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_webhook_events_event_id    ON public.webhook_events(event_id) WHERE event_id IS NOT NULL;

-- ============================================================
-- AUDIT LOG
-- ============================================================
CREATE TABLE IF NOT EXISTS public.audit_log (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES public.tenants(id),

    user_id    UUID,
    user_email TEXT,
    user_role  TEXT,

    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   UUID,
    changes     JSONB,

    ip_address INET,
    user_agent TEXT,
    request_id TEXT,

    metadata   JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_time ON public.audit_log(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_entity      ON public.audit_log(entity_type, entity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_user        ON public.audit_log(user_id, created_at DESC) WHERE user_id IS NOT NULL;

-- ============================================================
-- ANALYTICS SNAPSHOTS
-- ============================================================
CREATE TABLE IF NOT EXISTS public.analytics_daily (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    date      DATE NOT NULL,

    recalls_pending   INTEGER NOT NULL DEFAULT 0,
    recalls_sent      INTEGER NOT NULL DEFAULT 0,
    recalls_booked    INTEGER NOT NULL DEFAULT 0,
    recalls_completed INTEGER NOT NULL DEFAULT 0,
    recalls_opted_out INTEGER NOT NULL DEFAULT 0,

    emails_sent       INTEGER NOT NULL DEFAULT 0,
    emails_delivered  INTEGER NOT NULL DEFAULT 0,
    emails_bounced    INTEGER NOT NULL DEFAULT 0,
    emails_opened     INTEGER NOT NULL DEFAULT 0,
    emails_clicked    INTEGER NOT NULL DEFAULT 0,
    emails_failed     INTEGER NOT NULL DEFAULT 0,

    inbound_replies   INTEGER NOT NULL DEFAULT 0,
    unique_responders INTEGER NOT NULL DEFAULT 0,
    opt_outs          INTEGER NOT NULL DEFAULT 0,
    opt_ins           INTEGER NOT NULL DEFAULT 0,

    bookings_created   INTEGER NOT NULL DEFAULT 0,
    bookings_completed INTEGER NOT NULL DEFAULT 0,
    bookings_cancelled INTEGER NOT NULL DEFAULT 0,
    estimated_revenue  NUMERIC(10,2) DEFAULT 0,
    actual_revenue     NUMERIC(10,2) DEFAULT 0,

    avg_response_time INTERVAL,
    conversion_rate   NUMERIC(5,2) GENERATED ALWAYS AS (
        CASE WHEN recalls_sent > 0
        THEN (recalls_booked::NUMERIC / recalls_sent * 100)
        ELSE 0 END
    ) STORED,

    metadata   JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(tenant_id, date)
);

CREATE INDEX IF NOT EXISTS idx_analytics_tenant_date ON public.analytics_daily(tenant_id, date DESC);

-- ============================================================
-- FUNCTIONS
-- ============================================================

CREATE OR REPLACE FUNCTION public.update_tenant_usage()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    month_start TIMESTAMPTZ;
    t_id        UUID;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id AND OLD.tenant_id IS NOT NULL THEN
            month_start := date_trunc('month', now());
            UPDATE public.tenants SET current_usage = jsonb_build_object(
                'patient_count',       (SELECT COUNT(*) FROM public.patients        WHERE tenant_id = OLD.tenant_id),
                'recalls_this_month',  (SELECT COUNT(*) FROM public.recalls         WHERE tenant_id = OLD.tenant_id AND created_at >= month_start),
                'emails_this_month',   (SELECT COUNT(*) FROM public.email_messages  WHERE tenant_id = OLD.tenant_id AND created_at >= month_start),
                'bookings_this_month', (SELECT COUNT(*) FROM public.bookings        WHERE tenant_id = OLD.tenant_id AND created_at >= month_start)
            ) WHERE id = OLD.tenant_id;
        END IF;
        t_id := NEW.tenant_id;
    ELSE
        t_id := COALESCE(NEW.tenant_id, OLD.tenant_id);
    END IF;

    IF t_id IS NULL THEN RETURN COALESCE(NEW, OLD); END IF;

    month_start := date_trunc('month', now());
    UPDATE public.tenants
    SET current_usage = jsonb_build_object(
        'patient_count',       (SELECT COUNT(*) FROM public.patients        WHERE tenant_id = t_id),
        'recalls_this_month',  (SELECT COUNT(*) FROM public.recalls         WHERE tenant_id = t_id AND created_at >= month_start),
        'emails_this_month',   (SELECT COUNT(*) FROM public.email_messages  WHERE tenant_id = t_id AND created_at >= month_start),
        'bookings_this_month', (SELECT COUNT(*) FROM public.bookings        WHERE tenant_id = t_id AND created_at >= month_start)
    )
    WHERE id = t_id;

    RETURN COALESCE(NEW, OLD);
END;
$$;

DROP TRIGGER IF EXISTS trg_update_tenant_usage_patients     ON public.patients;
DROP TRIGGER IF EXISTS trg_update_tenant_usage_patients_upd ON public.patients;
CREATE TRIGGER trg_update_tenant_usage_patients
    AFTER INSERT OR DELETE ON public.patients
    FOR EACH ROW EXECUTE FUNCTION public.update_tenant_usage();
CREATE TRIGGER trg_update_tenant_usage_patients_upd
    AFTER UPDATE OF tenant_id ON public.patients
    FOR EACH ROW EXECUTE FUNCTION public.update_tenant_usage();

DROP TRIGGER IF EXISTS trg_update_tenant_usage_recalls      ON public.recalls;
DROP TRIGGER IF EXISTS trg_update_tenant_usage_recalls_upd  ON public.recalls;
CREATE TRIGGER trg_update_tenant_usage_recalls
    AFTER INSERT OR DELETE ON public.recalls
    FOR EACH ROW EXECUTE FUNCTION public.update_tenant_usage();
CREATE TRIGGER trg_update_tenant_usage_recalls_upd
    AFTER UPDATE OF tenant_id ON public.recalls
    FOR EACH ROW EXECUTE FUNCTION public.update_tenant_usage();

DROP TRIGGER IF EXISTS trg_update_tenant_usage_emails       ON public.email_messages;
DROP TRIGGER IF EXISTS trg_update_tenant_usage_emails_upd   ON public.email_messages;
CREATE TRIGGER trg_update_tenant_usage_emails
    AFTER INSERT OR DELETE ON public.email_messages
    FOR EACH ROW EXECUTE FUNCTION public.update_tenant_usage();
CREATE TRIGGER trg_update_tenant_usage_emails_upd
    AFTER UPDATE OF tenant_id ON public.email_messages
    FOR EACH ROW EXECUTE FUNCTION public.update_tenant_usage();

DROP TRIGGER IF EXISTS trg_update_tenant_usage_bookings     ON public.bookings;
DROP TRIGGER IF EXISTS trg_update_tenant_usage_bookings_upd ON public.bookings;
CREATE TRIGGER trg_update_tenant_usage_bookings
    AFTER INSERT OR DELETE ON public.bookings
    FOR EACH ROW EXECUTE FUNCTION public.update_tenant_usage();
CREATE TRIGGER trg_update_tenant_usage_bookings_upd
    AFTER UPDATE OF tenant_id ON public.bookings
    FOR EACH ROW EXECUTE FUNCTION public.update_tenant_usage();

CREATE OR REPLACE FUNCTION public.check_tenant_limits(p_tenant_id UUID, p_resource TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    tenant_record public.tenants%ROWTYPE;
    limit_value   INTEGER;
    usage_value   INTEGER;
BEGIN
    SELECT * INTO tenant_record FROM public.tenants WHERE id = p_tenant_id;
    IF NOT FOUND THEN RETURN FALSE; END IF;

    limit_value := (tenant_record.limits->>p_resource)::INTEGER;
    IF limit_value IS NULL THEN RETURN FALSE; END IF;

    usage_value := (tenant_record.current_usage->>p_resource)::INTEGER;
    RETURN COALESCE(usage_value, 0) < limit_value;
END;
$$;

CREATE OR REPLACE FUNCTION public.get_recall_stats(
    p_tenant_id UUID,
    p_days      INTEGER DEFAULT 30
)
RETURNS TABLE (
    status     TEXT,
    count      BIGINT,
    percentage NUMERIC
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    IF current_setting('jwt.claims.role', true) IS DISTINCT FROM 'service_role'
       AND current_setting('app.current_tenant', true)::UUID IS DISTINCT FROM p_tenant_id
    THEN
        RAISE EXCEPTION 'permission denied: cannot read stats for another tenant';
    END IF;

    RETURN QUERY
    WITH total AS (
        SELECT COUNT(*)::NUMERIC AS total_count
        FROM public.recalls
        WHERE tenant_id = p_tenant_id
          AND created_at >= NOW() - (p_days || ' days')::INTERVAL
    )
    SELECT
        r.status::TEXT,
        COUNT(*)  AS count,
        ROUND((COUNT(*)::NUMERIC / NULLIF(total.total_count, 0)) * 100, 1) AS percentage
    FROM public.recalls r, total
    WHERE r.tenant_id = p_tenant_id
      AND r.created_at >= NOW() - (p_days || ' days')::INTERVAL
    GROUP BY r.status, total.total_count
    ORDER BY count DESC;
END;
$$;

CREATE OR REPLACE FUNCTION public.update_updated_at_column()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_tenants_updated   ON public.tenants;
DROP TRIGGER IF EXISTS trg_patients_updated  ON public.patients;
DROP TRIGGER IF EXISTS trg_recalls_updated   ON public.recalls;
DROP TRIGGER IF EXISTS trg_bookings_updated  ON public.bookings;
DROP TRIGGER IF EXISTS trg_analytics_updated ON public.analytics_daily;

CREATE TRIGGER trg_tenants_updated   BEFORE UPDATE ON public.tenants         FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();
CREATE TRIGGER trg_patients_updated  BEFORE UPDATE ON public.patients        FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();
CREATE TRIGGER trg_recalls_updated   BEFORE UPDATE ON public.recalls         FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();
CREATE TRIGGER trg_bookings_updated  BEFORE UPDATE ON public.bookings        FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();
CREATE TRIGGER trg_analytics_updated BEFORE UPDATE ON public.analytics_daily FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

CREATE OR REPLACE FUNCTION public.cleanup_old_data(retain_days INTEGER DEFAULT 90)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    deleted_count INTEGER := 0;
    tmp           INTEGER;
BEGIN
    DELETE FROM public.webhook_events
    WHERE received_at < NOW() - (retain_days || ' days')::INTERVAL
      AND processed = TRUE;
    GET DIAGNOSTICS tmp = ROW_COUNT;
    deleted_count := deleted_count + tmp;

    DELETE FROM public.audit_log
    WHERE created_at < NOW() - (retain_days || ' days')::INTERVAL;
    GET DIAGNOSTICS tmp = ROW_COUNT;
    deleted_count := deleted_count + tmp;

    RETURN deleted_count;
END;
$$;

CREATE OR REPLACE FUNCTION public.patients_search_vector(
    first_name     TEXT,
    last_name      TEXT,
    preferred_name TEXT
)
RETURNS tsvector
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, public
AS $$
BEGIN
    RETURN pg_catalog.to_tsvector(
        'english',
        coalesce(first_name, '')     || ' ' ||
        coalesce(last_name, '')      || ' ' ||
        coalesce(preferred_name, '')
    );
END;
$$;

-- ============================================================
-- ADDITIONAL INDEXES
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_patients_name_search ON public.patients USING GIN (public.patients_search_vector(first_name, last_name, preferred_name));
CREATE INDEX IF NOT EXISTS idx_patients_communication ON public.patients USING GIN (communication_preferences);
CREATE INDEX IF NOT EXISTS idx_recalls_metadata_path  ON public.recalls  USING GIN (metadata jsonb_path_ops);

-- ============================================================
-- ROW LEVEL SECURITY
-- ============================================================
ALTER TABLE public.tenants           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.patients          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.recalls           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.email_messages    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.inbound_responses ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.bookings          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.analytics_daily   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_log         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.recall_templates  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.schema_version    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.webhook_events    ENABLE ROW LEVEL SECURITY;

-- ============================================================
-- RLS POLICIES
-- ============================================================

DROP POLICY IF EXISTS tenants_select ON public.tenants;
DROP POLICY IF EXISTS tenants_insert ON public.tenants;
DROP POLICY IF EXISTS tenants_update ON public.tenants;
DROP POLICY IF EXISTS tenants_delete ON public.tenants;
DROP POLICY IF EXISTS tenants_service_role ON public.tenants;

CREATE POLICY tenants_select ON public.tenants FOR SELECT TO authenticated
    USING (id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY tenants_insert ON public.tenants FOR INSERT TO authenticated
    WITH CHECK (id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY tenants_update ON public.tenants FOR UPDATE TO authenticated
    USING (id = current_setting('app.current_tenant', true)::UUID)
    WITH CHECK (id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY tenants_delete ON public.tenants FOR DELETE TO authenticated
    USING (id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY tenants_service_role ON public.tenants FOR ALL TO authenticated
    USING (current_setting('jwt.claims.role', true) = 'service_role');

DROP POLICY IF EXISTS patients_select ON public.patients;
DROP POLICY IF EXISTS patients_insert ON public.patients;
DROP POLICY IF EXISTS patients_update ON public.patients;
DROP POLICY IF EXISTS patients_delete ON public.patients;
DROP POLICY IF EXISTS patients_service_role ON public.patients;

CREATE POLICY patients_select ON public.patients FOR SELECT TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY patients_insert ON public.patients FOR INSERT TO authenticated
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY patients_update ON public.patients FOR UPDATE TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY patients_delete ON public.patients FOR DELETE TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY patients_service_role ON public.patients FOR ALL TO authenticated
    USING (current_setting('jwt.claims.role', true) = 'service_role');

DROP POLICY IF EXISTS recalls_select ON public.recalls;
DROP POLICY IF EXISTS recalls_insert ON public.recalls;
DROP POLICY IF EXISTS recalls_update ON public.recalls;
DROP POLICY IF EXISTS recalls_delete ON public.recalls;
DROP POLICY IF EXISTS recalls_service_role ON public.recalls;

CREATE POLICY recalls_select ON public.recalls FOR SELECT TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY recalls_insert ON public.recalls FOR INSERT TO authenticated
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY recalls_update ON public.recalls FOR UPDATE TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY recalls_delete ON public.recalls FOR DELETE TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY recalls_service_role ON public.recalls FOR ALL TO authenticated
    USING (current_setting('jwt.claims.role', true) = 'service_role');

DROP POLICY IF EXISTS recall_templates_select ON public.recall_templates;
DROP POLICY IF EXISTS recall_templates_insert ON public.recall_templates;
DROP POLICY IF EXISTS recall_templates_update ON public.recall_templates;
DROP POLICY IF EXISTS recall_templates_delete ON public.recall_templates;
DROP POLICY IF EXISTS recall_templates_service_role ON public.recall_templates;

CREATE POLICY recall_templates_select ON public.recall_templates FOR SELECT TO authenticated
    USING (tenant_id IS NULL OR tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY recall_templates_insert ON public.recall_templates FOR INSERT TO authenticated
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY recall_templates_update ON public.recall_templates FOR UPDATE TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY recall_templates_delete ON public.recall_templates FOR DELETE TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY recall_templates_service_role ON public.recall_templates FOR ALL TO authenticated
    USING (current_setting('jwt.claims.role', true) = 'service_role');

DROP POLICY IF EXISTS email_messages_select ON public.email_messages;
DROP POLICY IF EXISTS email_messages_insert ON public.email_messages;
DROP POLICY IF EXISTS email_messages_update ON public.email_messages;
DROP POLICY IF EXISTS email_messages_delete ON public.email_messages;
DROP POLICY IF EXISTS email_messages_service_role ON public.email_messages;

CREATE POLICY email_messages_select ON public.email_messages FOR SELECT TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY email_messages_insert ON public.email_messages FOR INSERT TO authenticated
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY email_messages_update ON public.email_messages FOR UPDATE TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY email_messages_delete ON public.email_messages FOR DELETE TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY email_messages_service_role ON public.email_messages FOR ALL TO authenticated
    USING (current_setting('jwt.claims.role', true) = 'service_role');

DROP POLICY IF EXISTS inbound_responses_select ON public.inbound_responses;
DROP POLICY IF EXISTS inbound_responses_insert ON public.inbound_responses;
DROP POLICY IF EXISTS inbound_responses_update ON public.inbound_responses;
DROP POLICY IF EXISTS inbound_responses_delete ON public.inbound_responses;
DROP POLICY IF EXISTS inbound_responses_service_role ON public.inbound_responses;

CREATE POLICY inbound_responses_select ON public.inbound_responses FOR SELECT TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY inbound_responses_insert ON public.inbound_responses FOR INSERT TO authenticated
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY inbound_responses_update ON public.inbound_responses FOR UPDATE TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY inbound_responses_delete ON public.inbound_responses FOR DELETE TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY inbound_responses_service_role ON public.inbound_responses FOR ALL TO authenticated
    USING (current_setting('jwt.claims.role', true) = 'service_role');

DROP POLICY IF EXISTS bookings_select ON public.bookings;
DROP POLICY IF EXISTS bookings_insert ON public.bookings;
DROP POLICY IF EXISTS bookings_update ON public.bookings;
DROP POLICY IF EXISTS bookings_delete ON public.bookings;
DROP POLICY IF EXISTS bookings_service_role ON public.bookings;

CREATE POLICY bookings_select ON public.bookings FOR SELECT TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY bookings_insert ON public.bookings FOR INSERT TO authenticated
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY bookings_update ON public.bookings FOR UPDATE TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY bookings_delete ON public.bookings FOR DELETE TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY bookings_service_role ON public.bookings FOR ALL TO authenticated
    USING (current_setting('jwt.claims.role', true) = 'service_role');

DROP POLICY IF EXISTS webhook_events_select ON public.webhook_events;
DROP POLICY IF EXISTS webhook_events_insert ON public.webhook_events;
DROP POLICY IF EXISTS webhook_events_update ON public.webhook_events;
DROP POLICY IF EXISTS webhook_events_delete ON public.webhook_events;
DROP POLICY IF EXISTS webhook_events_service_role ON public.webhook_events;

CREATE POLICY webhook_events_select ON public.webhook_events FOR SELECT TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY webhook_events_insert ON public.webhook_events FOR INSERT TO authenticated
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY webhook_events_update ON public.webhook_events FOR UPDATE TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY webhook_events_delete ON public.webhook_events FOR DELETE TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY webhook_events_service_role ON public.webhook_events FOR ALL TO authenticated
    USING (current_setting('jwt.claims.role', true) = 'service_role');

DROP POLICY IF EXISTS audit_log_select ON public.audit_log;
DROP POLICY IF EXISTS audit_log_service_role ON public.audit_log;

CREATE POLICY audit_log_select ON public.audit_log FOR SELECT TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY audit_log_service_role ON public.audit_log FOR ALL TO authenticated
    USING (current_setting('jwt.claims.role', true) = 'service_role');

DROP POLICY IF EXISTS analytics_daily_select ON public.analytics_daily;
DROP POLICY IF EXISTS analytics_daily_insert ON public.analytics_daily;
DROP POLICY IF EXISTS analytics_daily_update ON public.analytics_daily;
DROP POLICY IF EXISTS analytics_daily_delete ON public.analytics_daily;
DROP POLICY IF EXISTS analytics_daily_service_role ON public.analytics_daily;

CREATE POLICY analytics_daily_select ON public.analytics_daily FOR SELECT TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY analytics_daily_insert ON public.analytics_daily FOR INSERT TO authenticated
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY analytics_daily_update ON public.analytics_daily FOR UPDATE TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY analytics_daily_delete ON public.analytics_daily FOR DELETE TO authenticated
    USING (tenant_id = current_setting('app.current_tenant', true)::UUID);
CREATE POLICY analytics_daily_service_role ON public.analytics_daily FOR ALL TO authenticated
    USING (current_setting('jwt.claims.role', true) = 'service_role');

DROP POLICY IF EXISTS schema_version_readonly ON public.schema_version;
DROP POLICY IF EXISTS schema_version_service_role ON public.schema_version;

CREATE POLICY schema_version_readonly ON public.schema_version FOR SELECT TO authenticated
    USING (TRUE);
CREATE POLICY schema_version_service_role ON public.schema_version FOR ALL TO authenticated
    USING (current_setting('jwt.claims.role', true) = 'service_role');

-- ============================================================
-- REVOKE / GRANT
-- ============================================================
REVOKE EXECUTE ON FUNCTION public.check_tenant_limits(UUID, TEXT) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.get_recall_stats(UUID, INTEGER)  FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.cleanup_old_data(INTEGER)        FROM PUBLIC;

GRANT EXECUTE ON FUNCTION public.check_tenant_limits(UUID, TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_recall_stats(UUID, INTEGER)  TO authenticated;

COMMENT ON SCHEMA public IS 'RECALL SaaS — Email-Only Healthcare Recall Management System';
COMMENT ON SCHEMA extensions IS 'Isolated PostgreSQL extensions';
COMMENT ON DATABASE postgres IS 'Multi-tenant email recall management database';
