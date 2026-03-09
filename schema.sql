-- ============================================================
-- RECALL SaaS — Production-Ready Multi-Tenant Schema
-- Healthcare-Agnostic Recall Management System
-- ============================================================

-- ============================================================
-- EXTENSIONS (with version tracking)
-- ============================================================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS "pgcrypto" WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS "citext" WITH SCHEMA public;        -- Case-insensitive text
CREATE EXTENSION IF NOT EXISTS "btree_gin" WITH SCHEMA public;     -- Composite index optimization
CREATE EXTENSION IF NOT EXISTS "pg_stat_statements" WITH SCHEMA public; -- Query performance monitoring

-- ============================================================
-- SCHEMA VERSION CONTROL
-- ============================================================
CREATE TABLE IF NOT EXISTS schema_version (
    id              INTEGER PRIMARY KEY,
    version         TEXT NOT NULL,
    description     TEXT NOT NULL,
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by      TEXT NOT NULL DEFAULT current_user,
    checksum        TEXT NOT NULL
);

INSERT INTO schema_version (id, version, description, checksum) 
VALUES (1, '1.0.0', 'Initial production schema', md5('initial-schema-v1'))
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- ENUM TYPES (Better than text fields)
-- ============================================================
CREATE TYPE service_type_enum AS ENUM (
    'dental', 'gp', 'physio', 'optometry', 'chiro', 'vet', 
    'mental_health', 'dermatology', 'podiatry', 'audiology', 'custom'
);

CREATE TYPE tenant_plan_enum AS ENUM ('starter', 'growth', 'enterprise', 'custom');

CREATE TYPE recall_status_enum AS ENUM (
    'pending', 'in_progress', 'booked', 'completed', 
    'opted_out', 'failed', 'snoozed', 'cancelled'
);

CREATE TYPE sms_direction_enum AS ENUM ('outbound', 'inbound');
CREATE TYPE sms_status_enum AS ENUM ('queued', 'sent', 'delivered', 'failed', 'undelivered');
CREATE TYPE booking_status_enum AS ENUM ('confirmed', 'cancelled', 'completed', 'no_show');

-- ============================================================
-- TENANTS (Healthcare Practices / Clinics)
-- ============================================================
CREATE TABLE tenants (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT NOT NULL,
    slug                CITEXT UNIQUE NOT NULL,
    service_type        service_type_enum NOT NULL,
    phone_number        TEXT,
    
    -- Authentication & Security
    api_key             TEXT UNIQUE,
    api_key_last_chars  TEXT GENERATED ALWAYS AS (RIGHT(api_key, 4)) STORED,
    api_key_rotated_at  TIMESTAMPTZ,
    webhook_secret      TEXT,
    
    -- Twilio Configuration
    twilio_config       JSONB NOT NULL DEFAULT '{}' CHECK (
        jsonb_typeof(twilio_config) = 'object' AND
        (twilio_config ? 'sid' OR twilio_config ? 'token' OR twilio_config ? 'from_number')
    ),
    
    -- Localization
    timezone            TEXT NOT NULL DEFAULT 'Australia/Sydney',
    country_code        TEXT NOT NULL DEFAULT 'AU',
    locale              TEXT NOT NULL DEFAULT 'en-AU',
    business_hours      JSONB NOT NULL DEFAULT '{"monday": {"start": "09:00", "end": "17:00"}, "tuesday": {"start": "09:00", "end": "17:00"}, "wednesday": {"start": "09:00", "end": "17:00"}, "thursday": {"start": "09:00", "end": "17:00"}, "friday": {"start": "09:00", "end": "17:00"}, "saturday": {"start": null, "end": null}, "sunday": {"start": null, "end": null}}',
    
    -- Subscription & Billing
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    trial_ends_at       TIMESTAMPTZ,
    plan                tenant_plan_enum NOT NULL DEFAULT 'starter',
    subscription_id     TEXT,
    billing_email       TEXT,
    
    -- Limits & Quotas
    limits              JSONB NOT NULL DEFAULT '{
        "max_patients": 1000,
        "max_recalls_per_month": 500,
        "max_sms_per_month": 1000,
        "max_users": 5
    }',
    
    -- Usage (updated via triggers)
    current_usage       JSONB NOT NULL DEFAULT '{
        "patient_count": 0,
        "recalls_this_month": 0,
        "sms_this_month": 0,
        "bookings_this_month": 0
    }',
    
    -- Settings
    settings            JSONB NOT NULL DEFAULT '{
        "auto_opt_in": false,
        "require_consent": true,
        "default_recall_interval_days": 180,
        "reminder_days_before": [7, 1],
        "booking_window_days": 90,
        "allow_sms_booking": true,
        "allow_opt_out": true,
        "allow_snooze": true
    }',
    
    -- Metadata
    metadata            JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Constraints
    CONSTRAINT valid_phone CHECK (phone_number ~ '^\+[1-9]\d{1,14}$'),
    CONSTRAINT valid_api_key CHECK (api_key ~ '^[A-Za-z0-9_-]{32,64}$'),
    CONSTRAINT valid_business_hours CHECK (jsonb_typeof(business_hours) = 'object'),
    CONSTRAINT valid_limits CHECK (jsonb_typeof(limits) = 'object'),
    CONSTRAINT valid_settings CHECK (jsonb_typeof(settings) = 'object')
);

COMMENT ON TABLE tenants IS 'Multi-tenant healthcare practices/clinics';
COMMENT ON COLUMN tenants.twilio_config IS 'JSON containing sid, token, from_number, messaging_service_sid';
COMMENT ON COLUMN tenants.limits IS 'Per-tenant usage limits';
COMMENT ON COLUMN tenants.current_usage IS 'Current usage counters updated via triggers';

-- ============================================================
-- RECALL TEMPLATES
-- ============================================================
CREATE TABLE recall_templates (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID REFERENCES tenants(id) ON DELETE CASCADE,
    service_type        service_type_enum,
    name                TEXT NOT NULL,
    description         TEXT,
    recall_interval_days INTEGER NOT NULL CHECK (recall_interval_days > 0),
    priority            INTEGER NOT NULL DEFAULT 1 CHECK (priority BETWEEN 1 AND 5),
    
    -- Message Sequence
    message_sequence    JSONB NOT NULL DEFAULT '[]' CHECK (
        jsonb_typeof(message_sequence) = 'array' AND
        jsonb_array_length(message_sequence) > 0
    ),
    
    -- Scheduling Rules
    applicable_days     TEXT[] NOT NULL DEFAULT ARRAY['monday','tuesday','wednesday','thursday','friday'],
    max_reminders       INTEGER NOT NULL DEFAULT 3 CHECK (max_reminders > 0),
    snooze_days         INTEGER NOT NULL DEFAULT 7 CHECK (snooze_days > 0),
    
    -- Conditions
    conditions          JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(conditions) = 'object'),
    
    -- Status
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    is_global           BOOLEAN GENERATED ALWAYS AS (tenant_id IS NULL) STORED,
    
    -- Metadata
    metadata            JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Constraints
    CONSTRAINT valid_message_sequence CHECK (validate_message_sequence(message_sequence)),
    CONSTRAINT unique_template_name UNIQUE NULLS NOT DISTINCT (tenant_id, name)
);

COMMENT ON TABLE recall_templates IS 'Recall templates with message sequences';
COMMENT ON COLUMN recall_templates.message_sequence IS 'Array of {delay_days, message_template, required_fields}';
COMMENT ON COLUMN recall_templates.conditions IS 'JSON conditions for template applicability (age, gender, last_visit, etc.)';

-- ============================================================
-- PATIENTS / CLIENTS
-- ============================================================
CREATE TABLE patients (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    external_id         TEXT,
    
    -- Personal Info
    first_name          TEXT NOT NULL,
    last_name           TEXT NOT NULL,
    preferred_name      TEXT,
    phone               TEXT NOT NULL,
    email               CITEXT,
    date_of_birth       DATE,
    gender              TEXT,
    
    -- Communication Preferences
    communication_preferences JSONB NOT NULL DEFAULT '{
        "sms": true,
        "email": false,
        "whatsapp": false,
        "preferred_time": "anytime",
        "do_not_disturb": false
    }' CHECK (jsonb_typeof(communication_preferences) = 'object'),
    
    -- Consent Tracking
    opted_out           BOOLEAN NOT NULL DEFAULT FALSE,
    opted_out_at        TIMESTAMPTZ,
    opted_out_reason    TEXT,
    opted_in_at         TIMESTAMPTZ,
    consent_version     TEXT,
    consent_given_at    TIMESTAMPTZ,
    
    -- Last Activity
    last_appointment_at TIMESTAMPTZ,
    last_contact_at     TIMESTAMPTZ,
    last_response_at    TIMESTAMPTZ,
    
    -- Metadata (flexible fields)
    metadata            JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(metadata) = 'object'),
    
    -- System
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by          UUID,
    updated_by          UUID,
    
    -- Constraints
    CONSTRAINT valid_phone CHECK (phone ~ '^\+[1-9]\d{1,14}$'),
    CONSTRAINT valid_email CHECK (email IS NULL OR email ~ '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'),
    CONSTRAINT consent_check CHECK (
        (opted_out = TRUE AND opted_out_at IS NOT NULL) OR
        (opted_out = FALSE)
    ),
    UNIQUE(tenant_id, external_id),
    UNIQUE(tenant_id, phone)
);

CREATE INDEX idx_patients_tenant_lookup ON patients(tenant_id, phone, opted_out);
CREATE INDEX idx_patients_dob ON patients(tenant_id, date_of_birth) WHERE date_of_birth IS NOT NULL;
CREATE INDEX idx_patients_last_appointment ON patients(tenant_id, last_appointment_at DESC);
CREATE INDEX idx_patients_metadata_gin ON patients USING GIN (metadata);

COMMENT ON TABLE patients IS 'Patient/client information per tenant';
COMMENT ON COLUMN patients.communication_preferences IS 'JSON containing communication preferences and DND settings';
COMMENT ON COLUMN patients.metadata IS 'Flexible fields for specialty-specific data (pet_name, provider_preferences, etc.)';

-- ============================================================
-- RECALL RECORDS (Core Engine)
-- ============================================================
CREATE TABLE recalls (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    patient_id          UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    template_id         UUID REFERENCES recall_templates(id),
    
    -- Recall Details
    recall_type         TEXT NOT NULL,
    last_appointment    DATE,
    due_date            DATE NOT NULL,
    priority            INTEGER NOT NULL DEFAULT 1 CHECK (priority BETWEEN 1 AND 5),
    
    -- Status
    status              recall_status_enum NOT NULL DEFAULT 'pending',
    booked_at           TIMESTAMPTZ,
    snoozed_until       DATE,
    cancelled_at        TIMESTAMPTZ,
    cancelled_reason    TEXT,
    
    -- Sequence Tracking
    sequence_step       INTEGER NOT NULL DEFAULT 0,
    next_send_at        TIMESTAMPTZ,
    last_sent_at        TIMESTAMPTZ,
    messages_sent       INTEGER NOT NULL DEFAULT 0 CHECK (messages_sent >= 0),
    
    -- Response Tracking
    last_response_at    TIMESTAMPTZ,
    last_response_type  TEXT,
    
    -- Booking
    booking_link        TEXT,
    booking_link_expires_at TIMESTAMPTZ,
    booking_id          UUID,
    
    -- Analytics
    first_response_time INTERVAL GENERATED ALWAYS AS (last_response_at - created_at) STORED,
    
    -- Notes
    notes               TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}',
    
    -- System
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Constraints
    CONSTRAINT valid_due_date CHECK (due_date >= created_at::DATE),
    CONSTRAINT valid_snooze CHECK (snoozed_until IS NULL OR snoozed_until > CURRENT_DATE),
    CONSTRAINT valid_next_send CHECK (
        (status IN ('pending', 'in_progress') AND next_send_at IS NOT NULL) OR
        (status NOT IN ('pending', 'in_progress') AND next_send_at IS NULL)
    )
);

-- Optimized indexes for recall processing
CREATE INDEX idx_recalls_pending ON recalls(tenant_id, next_send_at) 
    WHERE status IN ('pending', 'in_progress') AND next_send_at <= NOW();
CREATE INDEX idx_recalls_due_date ON recalls(tenant_id, due_date) WHERE status NOT IN ('booked', 'completed', 'cancelled');
CREATE INDEX idx_recalls_patient_status ON recalls(patient_id, status);
CREATE INDEX idx_recalls_metadata_gin ON recalls USING GIN (metadata);
CREATE INDEX idx_recalls_booking ON recalls(booking_id) WHERE booking_id IS NOT NULL;

COMMENT ON TABLE recalls IS 'Core recall tracking engine';
COMMENT ON COLUMN recalls.booking_link_expires_at IS 'Expiration timestamp for booking link security';

-- ============================================================
-- SMS MESSAGES LOG
-- ============================================================
CREATE TABLE sms_messages (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    patient_id          UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    recall_id           UUID REFERENCES recalls(id),
    
    -- Message Details
    message_sid         TEXT,
    direction           sms_direction_enum NOT NULL DEFAULT 'outbound',
    from_number         TEXT NOT NULL,
    to_number           TEXT NOT NULL,
    body                TEXT NOT NULL,
    
    -- Status Tracking
    status              sms_status_enum NOT NULL DEFAULT 'queued',
    status_history      JSONB[] NOT NULL DEFAULT ARRAY[]::JSONB[],
    error_code          TEXT,
    error_message       TEXT,
    
    -- Delivery Tracking
    sent_at             TIMESTAMPTZ,
    delivered_at        TIMESTAMPTZ,
    failed_at           TIMESTAMPTZ,
    
    -- Cost Tracking
    cost                NUMERIC(10, 6) CHECK (cost >= 0),
    cost_currency       TEXT DEFAULT 'USD',
    segments            INTEGER CHECK (segments > 0),
    
    -- Metadata
    sequence_step       INTEGER,
    template_used       TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}',
    
    -- System
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Constraints
    CONSTRAINT valid_numbers CHECK (from_number ~ '^\+[1-9]\d{1,14}$' AND to_number ~ '^\+[1-9]\d{1,14}$'),
    CONSTRAINT valid_timeline CHECK (
        (sent_at IS NOT NULL AND status != 'queued') OR
        (sent_at IS NULL AND status = 'queued')
    )
);

-- Partition by creation date for better performance
CREATE INDEX idx_sms_messages_tenant_date ON sms_messages(tenant_id, created_at DESC);
CREATE INDEX idx_sms_messages_recall ON sms_messages(recall_id);
CREATE INDEX idx_sms_messages_sid ON sms_messages(message_sid) WHERE message_sid IS NOT NULL;
CREATE INDEX idx_sms_messages_status ON sms_messages(status, created_at) WHERE status NOT IN ('delivered', 'failed');

COMMENT ON TABLE sms_messages IS 'SMS message log with full delivery tracking';
COMMENT ON COLUMN sms_messages.status_history IS 'Array of status change events with timestamps';

-- ============================================================
-- INBOUND RESPONSES
-- ============================================================
CREATE TABLE inbound_responses (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID REFERENCES tenants(id),
    patient_id          UUID REFERENCES patients(id),
    recall_id           UUID REFERENCES recalls(id),
    
    -- Message Details
    message_sid         TEXT NOT NULL UNIQUE,
    from_number         TEXT NOT NULL,
    to_number           TEXT NOT NULL,
    body                TEXT NOT NULL,
    
    -- NLP/Intent
    intent              TEXT,
    intent_confidence   NUMERIC(3,2) CHECK (intent_confidence BETWEEN 0 AND 1),
    entities            JSONB NOT NULL DEFAULT '{}',
    
    -- Processing
    processed           BOOLEAN NOT NULL DEFAULT FALSE,
    processed_at        TIMESTAMPTZ,
    processing_error    TEXT,
    action_taken        TEXT,
    
    -- Response
    response_sent       BOOLEAN NOT NULL DEFAULT FALSE,
    response_message_id UUID REFERENCES sms_messages(id),
    
    -- Metadata
    metadata            JSONB NOT NULL DEFAULT '{}',
    received_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_inbound_responses_unprocessed ON inbound_responses(tenant_id, received_at) 
    WHERE processed = FALSE;
CREATE INDEX idx_inbound_responses_patient ON inbound_responses(patient_id, received_at DESC);
CREATE INDEX idx_inbound_responses_phone ON inbound_responses(from_number, received_at DESC);
CREATE INDEX idx_inbound_responses_intent ON inbound_responses(intent) WHERE intent IS NOT NULL;

COMMENT ON TABLE inbound_responses IS 'Inbound SMS responses with intent analysis';

-- ============================================================
-- BOOKINGS
-- ============================================================
CREATE TABLE bookings (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    patient_id          UUID NOT NULL REFERENCES patients(id),
    recall_id           UUID REFERENCES recalls(id) UNIQUE,
    
    -- Booking Details
    external_booking_id TEXT,
    appointment_time    TIMESTAMPTZ NOT NULL,
    duration_minutes    INTEGER CHECK (duration_minutes > 0),
    provider_id         TEXT,
    provider_name       TEXT,
    location            TEXT,
    
    -- Status
    status              booking_status_enum NOT NULL DEFAULT 'confirmed',
    cancelled_at        TIMESTAMPTZ,
    cancelled_reason    TEXT,
    
    -- Source & Attribution
    source              TEXT NOT NULL DEFAULT 'recall_sms',
    source_details      JSONB NOT NULL DEFAULT '{}',
    
    -- Revenue
    revenue_amount      NUMERIC(10, 2) CHECK (revenue_amount >= 0),
    revenue_currency    TEXT DEFAULT 'AUD',
    
    -- Metadata
    metadata            JSONB NOT NULL DEFAULT '{}',
    
    -- System
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Constraints
    CONSTRAINT future_booking CHECK (appointment_time > created_at)
);

CREATE INDEX idx_bookings_tenant_date ON bookings(tenant_id, appointment_time DESC);
CREATE INDEX idx_bookings_patient ON bookings(patient_id, appointment_time DESC);
CREATE INDEX idx_bookings_status ON bookings(status, appointment_time) WHERE status = 'confirmed';
CREATE INDEX idx_bookings_external ON bookings(external_booking_id) WHERE external_booking_id IS NOT NULL;

COMMENT ON TABLE bookings IS 'Booking records from recall campaigns';

-- ============================================================
-- WEBHOOK EVENTS
-- ============================================================
CREATE TABLE webhook_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID REFERENCES tenants(id),
    
    -- Event Details
    source              TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    event_id            TEXT,
    
    -- Payload
    headers             JSONB NOT NULL DEFAULT '{}',
    payload             JSONB NOT NULL,
    signature           TEXT,
    signature_valid     BOOLEAN,
    
    -- Processing
    processed           BOOLEAN NOT NULL DEFAULT FALSE,
    processed_at        TIMESTAMPTZ,
    retry_count         INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    last_error          TEXT,
    
    -- Response
    response_status     INTEGER,
    response_body       TEXT,
    
    -- Metadata
    metadata            JSONB NOT NULL DEFAULT '{}',
    received_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_webhook_events_unprocessed ON webhook_events(tenant_id, received_at) 
    WHERE processed = FALSE;
CREATE INDEX idx_webhook_events_source ON webhook_events(source, event_type, received_at DESC);
CREATE INDEX idx_webhook_events_event_id ON webhook_events(event_id) WHERE event_id IS NOT NULL;

-- ============================================================
-- AUDIT LOG (Compliance)
-- ============================================================
CREATE TABLE audit_log (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID REFERENCES tenants(id),
    
    -- Who
    user_id             UUID,
    user_email          TEXT,
    user_role           TEXT,
    
    -- What
    action              TEXT NOT NULL,
    entity_type         TEXT NOT NULL,
    entity_id           UUID,
    changes             JSONB,
    
    -- Context
    ip_address          INET,
    user_agent          TEXT,
    request_id          TEXT,
    
    -- Metadata
    metadata            JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_log_tenant_time ON audit_log(tenant_id, created_at DESC);
CREATE INDEX idx_audit_log_entity ON audit_log(entity_type, entity_id, created_at DESC);
CREATE INDEX idx_audit_log_user ON audit_log(user_id, created_at DESC) WHERE user_id IS NOT NULL;

COMMENT ON TABLE audit_log IS 'Compliance audit trail for all sensitive operations';

-- ============================================================
-- ANALYTICS SNAPSHOTS
-- ============================================================
CREATE TABLE analytics_daily (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    date                DATE NOT NULL,
    
    -- Recall Metrics
    recalls_pending     INTEGER NOT NULL DEFAULT 0,
    recalls_sent        INTEGER NOT NULL DEFAULT 0,
    recalls_booked      INTEGER NOT NULL DEFAULT 0,
    recalls_completed   INTEGER NOT NULL DEFAULT 0,
    recalls_opted_out   INTEGER NOT NULL DEFAULT 0,
    
    -- SMS Metrics
    sms_sent            INTEGER NOT NULL DEFAULT 0,
    sms_delivered       INTEGER NOT NULL DEFAULT 0,
    sms_failed          INTEGER NOT NULL DEFAULT 0,
    sms_cost_total      NUMERIC(10, 2) DEFAULT 0,
    
    -- Response Metrics
    inbound_replies     INTEGER NOT NULL DEFAULT 0,
    unique_responders   INTEGER NOT NULL DEFAULT 0,
    opt_outs            INTEGER NOT NULL DEFAULT 0,
    opt_ins             INTEGER NOT NULL DEFAULT 0,
    
    -- Booking Metrics
    bookings_created    INTEGER NOT NULL DEFAULT 0,
    bookings_completed  INTEGER NOT NULL DEFAULT 0,
    bookings_cancelled  INTEGER NOT NULL DEFAULT 0,
    estimated_revenue   NUMERIC(10, 2) DEFAULT 0,
    actual_revenue      NUMERIC(10, 2) DEFAULT 0,
    
    -- Performance
    avg_response_time   INTERVAL,
    conversion_rate     NUMERIC(5,2) GENERATED ALWAYS AS (
        CASE WHEN recalls_sent > 0 
        THEN (recalls_booked::NUMERIC / recalls_sent * 100)
        ELSE 0 END
    ) STORED,
    
    -- Metadata
    metadata            JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    UNIQUE(tenant_id, date)
);

CREATE INDEX idx_analytics_tenant_date ON analytics_daily(tenant_id, date DESC);

-- ============================================================
-- FUNCTIONS AND TRIGGERS
-- ============================================================

-- Validation function for message sequences
CREATE OR REPLACE FUNCTION validate_message_sequence(sequence JSONB)
RETURNS BOOLEAN AS $$
DECLARE
    item JSONB;
    delay_days INTEGER;
BEGIN
    IF jsonb_typeof(sequence) != 'array' THEN
        RETURN FALSE;
    END IF;
    
    FOR item IN SELECT * FROM jsonb_array_elements(sequence)
    LOOP
        -- Check required fields
        IF NOT (item ? 'delay_days' AND item ? 'message_template') THEN
            RETURN FALSE;
        END IF;
        
        -- Validate types
        IF jsonb_typeof(item->'delay_days') != 'number' OR
           jsonb_typeof(item->'message_template') != 'string' THEN
            RETURN FALSE;
        END IF;
        
        -- Validate values
        delay_days := (item->>'delay_days')::INTEGER;
        IF delay_days < 0 OR delay_days > 365 THEN
            RETURN FALSE;
        END IF;
    END LOOP;
    
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- Update tenant usage statistics
CREATE OR REPLACE FUNCTION update_tenant_usage()
RETURNS TRIGGER AS $$
DECLARE
    month_start DATE;
BEGIN
    month_start := DATE_TRUNC('month', CURRENT_DATE);
    
    UPDATE tenants 
    SET current_usage = jsonb_build_object(
        'patient_count', (SELECT COUNT(*) FROM patients WHERE tenant_id = NEW.tenant_id),
        'recalls_this_month', (SELECT COUNT(*) FROM recalls WHERE tenant_id = NEW.tenant_id AND created_at >= month_start),
        'sms_this_month', (SELECT COUNT(*) FROM sms_messages WHERE tenant_id = NEW.tenant_id AND created_at >= month_start),
        'bookings_this_month', (SELECT COUNT(*) FROM bookings WHERE tenant_id = NEW.tenant_id AND created_at >= month_start)
    )
    WHERE id = NEW.tenant_id;
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create triggers for usage updates
CREATE TRIGGER trg_update_tenant_usage_patients
    AFTER INSERT OR DELETE ON patients
    FOR EACH ROW EXECUTE FUNCTION update_tenant_usage();

CREATE TRIGGER trg_update_tenant_usage_recalls
    AFTER INSERT OR DELETE ON recalls
    FOR EACH ROW EXECUTE FUNCTION update_tenant_usage();

-- Function to check tenant limits
CREATE OR REPLACE FUNCTION check_tenant_limits(p_tenant_id UUID, p_resource TEXT)
RETURNS BOOLEAN AS $$
DECLARE
    tenant_record tenants%ROWTYPE;
    limit_value INTEGER;
    usage_value INTEGER;
BEGIN
    SELECT * INTO tenant_record FROM tenants WHERE id = p_tenant_id;
    
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    
    limit_value := (tenant_record.limits->>p_resource)::INTEGER;
    usage_value := (tenant_record.current_usage->>p_resource)::INTEGER;
    
    RETURN usage_value < limit_value;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to get recall stats
CREATE OR REPLACE FUNCTION get_recall_stats(
    p_tenant_id UUID, 
    p_days INTEGER DEFAULT 30
)
RETURNS TABLE (
    status TEXT,
    count BIGINT,
    percentage NUMERIC
) AS $$
BEGIN
    RETURN QUERY
    WITH total AS (
        SELECT COUNT(*)::NUMERIC as total_count
        FROM recalls
        WHERE tenant_id = p_tenant_id
          AND created_at >= NOW() - (p_days || ' days')::INTERVAL
    )
    SELECT 
        recalls.status::TEXT,
        COUNT(*) as count,
        ROUND((COUNT(*)::NUMERIC / NULLIF(total.total_count, 0)) * 100, 1) as percentage
    FROM recalls, total
    WHERE tenant_id = p_tenant_id
      AND created_at >= NOW() - (p_days || ' days')::INTERVAL
    GROUP BY recalls.status, total.total_count
    ORDER BY count DESC;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER;

-- Update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Apply updated_at triggers
CREATE TRIGGER trg_tenants_updated BEFORE UPDATE ON tenants FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER trg_patients_updated BEFORE UPDATE ON patients FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER trg_recalls_updated BEFORE UPDATE ON recalls FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER trg_bookings_updated BEFORE UPDATE ON bookings FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER trg_analytics_updated BEFORE UPDATE ON analytics_daily FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================================
-- INDEXES (Performance Optimizations)
-- ============================================================

-- Composite indexes for common query patterns
CREATE INDEX idx_recalls_tenant_status_composite ON recalls(tenant_id, status, next_send_at) 
    WHERE status IN ('pending', 'in_progress');

CREATE INDEX idx_patients_tenant_opt_out ON patients(tenant_id, opted_out, last_contact_at) 
    WHERE opted_out = FALSE;

CREATE INDEX idx_sms_messages_delivery_tracking ON sms_messages(tenant_id, status, created_at)
    WHERE status IN ('queued', 'sent');

-- Full-text search indexes
CREATE INDEX idx_patients_name_search ON patients USING GIN (
    to_tsvector('english', coalesce(first_name, '') || ' ' || coalesce(last_name, '') || ' ' || coalesce(preferred_name, ''))
);

-- JSONB path indexes for common queries
CREATE INDEX idx_tenants_twilio_config ON tenants USING GIN ((twilio_config->'sid'));
CREATE INDEX idx_patients_communication ON patients USING GIN (communication_preferences);
CREATE INDEX idx_recalls_metadata_path ON recalls USING GIN (metadata jsonb_path_ops);

-- ============================================================
-- ROW LEVEL SECURITY POLICIES
-- ============================================================

-- Enable RLS on all tables
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE patients ENABLE ROW LEVEL SECURITY;
ALTER TABLE recalls ENABLE ROW LEVEL SECURITY;
ALTER TABLE sms_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE inbound_responses ENABLE ROW LEVEL SECURITY;
ALTER TABLE bookings ENABLE ROW LEVEL SECURITY;
ALTER TABLE analytics_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;

-- Tenant isolation policy
CREATE POLICY tenant_isolation_policy ON tenants
    USING (id = current_setting('app.current_tenant')::UUID);

CREATE POLICY patient_isolation_policy ON patients
    USING (tenant_id = current_setting('app.current_tenant')::UUID);

CREATE POLICY recall_isolation_policy ON recalls
    USING (tenant_id = current_setting('app.current_tenant')::UUID);

-- Service role bypass (for backend)
CREATE POLICY service_role_all_access ON tenants
    USING (current_user = 'service_role');

-- ============================================================
-- INITIAL DATA
-- ============================================================

-- Insert default templates (as shown in original schema)
-- [Keep your existing INSERT statements]

-- ============================================================
-- MAINTENANCE FUNCTIONS
-- ============================================================

-- Function to clean up old data
CREATE OR REPLACE FUNCTION cleanup_old_data(retain_days INTEGER DEFAULT 90)
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    -- Archive old webhook events
    DELETE FROM webhook_events 
    WHERE created_at < NOW() - (retain_days || ' days')::INTERVAL
    AND processed = TRUE;
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    
    -- Archive old audit log entries
    DELETE FROM audit_log 
    WHERE created_at < NOW() - (retain_days || ' days')::INTERVAL;
    
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Schedule maintenance job (requires pg_cron extension)
-- CREATE EXTENSION IF NOT EXISTS pg_cron;
-- SELECT cron.schedule('cleanup-job', '0 2 * * *', 'SELECT cleanup_old_data(90);');

-- ============================================================
-- COMMENTS AND DOCUMENTATION
-- ============================================================

COMMENT ON SCHEMA public IS 'RECALL SaaS - Healthcare Recall Management System';
COMMENT ON DATABASE postgres IS 'Multi-tenant recall management database for healthcare practices';
💡 

-- Add documentation for complex functions
COMMENT ON FUNCTION validate_message_sequence(JSONB) IS 'Validates message sequence JSON structure and values';
COMMENT ON FUNCTION check_tenant_limits(UUID, TEXT) IS 'Checks if tenant has capacity for requested resource';
COMMENT ON FUNCTION get_recall_stats(UUID, INTEGER) IS 'Returns recall statistics breakdown with percentages';
