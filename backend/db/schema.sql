-- ═══════════════════════════════════════════════════════════════════════════════
-- Nipate Missing Persons Platform — Database Schema
-- PostgreSQL (Supabase)
-- ═══════════════════════════════════════════════════════════════════════════════
-- Run this in the Supabase SQL Editor (Settings > SQL Editor > New query)
-- Run sections in order: Extensions → Tables → Indexes → RLS → Triggers → Functions
-- ═══════════════════════════════════════════════════════════════════════════════

-- ── Extensions ────────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";          -- for fuzzy name search
CREATE EXTENSION IF NOT EXISTS "unaccent";         -- for diacritic-insensitive search


-- ═══════════════════════════════════════════════════════════════════════════════
-- TABLES
-- ═══════════════════════════════════════════════════════════════════════════════

-- ── profiles ──────────────────────────────────────────────────────────────────
-- Extends auth.users (1-to-1). Role is duplicated here for convenience but
-- the authoritative role lives in auth.users.app_metadata (server-controlled).
CREATE TABLE IF NOT EXISTS public.profiles (
    id                      UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    full_name               TEXT NOT NULL,
    email                   TEXT NOT NULL,
    phone                   TEXT,
    county                  TEXT,
    role                    TEXT NOT NULL DEFAULT 'registered_user'
                                CHECK (role IN ('public_viewer','registered_user','police_officer','admin')),
    id_verified             BOOLEAN NOT NULL DEFAULT FALSE,
    id_verification_status  TEXT DEFAULT 'not_submitted'
                                CHECK (id_verification_status IN ('not_submitted','pending','approved','rejected')),
    id_document_path        TEXT,                  -- private Supabase Storage path (manual upload fallback)
    id_verification_session_id TEXT,                -- Didit session_id, for the automated flow
    police_badge_number     TEXT,
    police_station          TEXT,
    preferred_language      TEXT DEFAULT 'en' CHECK (preferred_language IN ('en','sw')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── cases ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.cases (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Identification
    case_number             TEXT UNIQUE,           -- auto-generated: NP-YYYY-NNNNN
    full_name               TEXT NOT NULL,
    alias                   TEXT,
    date_of_birth           DATE NOT NULL,
    age                     INTEGER GENERATED ALWAYS AS (
                                DATE_PART('year', AGE(date_of_birth))::INTEGER
                            ) STORED,
    gender                  TEXT CHECK (gender IN ('female','male','other','unknown')),
    nationality             TEXT DEFAULT 'Kenyan',
    national_id_number      TEXT,                  -- of missing person (optional)

    -- Physical description
    physical_description    TEXT NOT NULL,
    height_cm               INTEGER,
    weight_kg               INTEGER,
    complexion              TEXT,
    hair_description        TEXT,
    distinguishing_marks    TEXT,
    clothing_description    TEXT,

    -- Last seen
    last_seen_date          DATE NOT NULL,
    last_seen_time          TIME,
    last_seen_location      TEXT NOT NULL,
    last_seen_county        TEXT NOT NULL,
    last_seen_lat           DOUBLE PRECISION,
    last_seen_lng           DOUBLE PRECISION,
    circumstances           TEXT,

    -- Category & status
    category                TEXT NOT NULL DEFAULT 'adult'
                                CHECK (category IN ('child','adult','elderly','vulnerable_adult','foreign_national')),
    status                  TEXT NOT NULL DEFAULT 'reported'
                                CHECK (status IN ('reported','under_investigation','found_safe','found_deceased','closed')),
    risk_level              TEXT NOT NULL DEFAULT 'standard'
                                CHECK (risk_level IN ('standard','high','urgent')),

    -- Reporter
    reporter_id             UUID REFERENCES public.profiles(id) ON DELETE SET NULL,
    reporter_name           TEXT,
    reporter_phone          TEXT,
    reporter_relationship   TEXT,

    -- Police assignment
    assigned_officer_id     UUID REFERENCES public.profiles(id) ON DELETE SET NULL,
    assigned_county         TEXT,

    -- Deduplication
    dedup_hash              TEXT,                  -- hash of name+dob for quick dedup check
    possible_duplicate_of   UUID REFERENCES public.cases(id) ON DELETE SET NULL,

    -- Meta
    is_verified             BOOLEAN NOT NULL DEFAULT TRUE,
    is_public               BOOLEAN NOT NULL DEFAULT TRUE,   -- cases go public on submit; tips/notes stay restricted
    alert_sent              BOOLEAN NOT NULL DEFAULT FALSE,
    alert_sent_at           TIMESTAMPTZ,
    found_at                TIMESTAMPTZ,
    found_county            TEXT,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── case_images ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.case_images (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    case_id     UUID NOT NULL REFERENCES public.cases(id) ON DELETE CASCADE,
    storage_url TEXT NOT NULL,
    is_primary  BOOLEAN DEFAULT FALSE,
    uploaded_by UUID REFERENCES public.profiles(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── tips ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.tips (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    case_id         UUID NOT NULL REFERENCES public.cases(id) ON DELETE CASCADE,
    tip_number      TEXT UNIQUE,                    -- NP-TIP-YYYYMMDD-NNN
    category        TEXT DEFAULT 'sighting'
                        CHECK (category IN ('sighting','location_info','suspect_info','other')),
    content         TEXT NOT NULL,
    is_anonymous    BOOLEAN NOT NULL DEFAULT TRUE,
    tipster_id      UUID REFERENCES public.profiles(id) ON DELETE SET NULL,
    tipster_email   TEXT,                           -- for non-registered tipsters
    tipster_phone   TEXT,
    status          TEXT NOT NULL DEFAULT 'received'
                        CHECK (status IN ('received','reviewed','under_investigation','resolved','dismissed')),
    assigned_to     UUID REFERENCES public.profiles(id) ON DELETE SET NULL,
    reviewed_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── tip_attachments ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.tip_attachments (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tip_id      UUID NOT NULL REFERENCES public.tips(id) ON DELETE CASCADE,
    storage_path TEXT NOT NULL,                    -- private bucket path
    file_type   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── investigation_notes ───────────────────────────────────────────────────────
-- Restricted to police and admin. RLS enforces this at the DB layer.
CREATE TABLE IF NOT EXISTS public.investigation_notes (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    case_id     UUID NOT NULL REFERENCES public.cases(id) ON DELETE CASCADE,
    author_id   UUID NOT NULL REFERENCES public.profiles(id) ON DELETE RESTRICT,
    content     TEXT NOT NULL,
    is_sensitive BOOLEAN DEFAULT FALSE,            -- extra flag for highly sensitive notes
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── alert_subscribers ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.alert_subscribers (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email       TEXT NOT NULL,
    counties    TEXT[] NOT NULL DEFAULT ARRAY['all'],   -- ['Nairobi','Mombasa'] or ['all']
    categories  TEXT[] NOT NULL DEFAULT ARRAY['all'],
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(email)
);


-- ── audit_log ─────────────────────────────────────────────────────────────────
-- Append-only. No UPDATE or DELETE allowed (enforced by RLS).
CREATE TABLE IF NOT EXISTS public.audit_log (
    id           BIGSERIAL PRIMARY KEY,
    user_id      UUID,
    user_email   TEXT,
    role         TEXT,
    action       TEXT NOT NULL,
    http_method  TEXT,
    path         TEXT,
    status_code  INTEGER,
    ip_address   TEXT,
    metadata     JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ═══════════════════════════════════════════════════════════════════════════════
-- INDEXES
-- ═══════════════════════════════════════════════════════════════════════════════

-- Case search (full-text + trigram for fuzzy)
CREATE INDEX IF NOT EXISTS idx_cases_full_name_trgm
    ON public.cases USING GIN (full_name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_cases_status
    ON public.cases (status) WHERE is_public = TRUE;

CREATE INDEX IF NOT EXISTS idx_cases_county
    ON public.cases (last_seen_county, status) WHERE is_public = TRUE;

CREATE INDEX IF NOT EXISTS idx_cases_risk_level
    ON public.cases (risk_level, created_at DESC) WHERE is_public = TRUE;

CREATE INDEX IF NOT EXISTS idx_cases_dedup_hash
    ON public.cases (dedup_hash);

CREATE INDEX IF NOT EXISTS idx_cases_reporter
    ON public.cases (reporter_id);

-- Tips
CREATE INDEX IF NOT EXISTS idx_tips_case
    ON public.tips (case_id, status);

CREATE INDEX IF NOT EXISTS idx_tips_assigned
    ON public.tips (assigned_to, status);

-- Audit
CREATE INDEX IF NOT EXISTS idx_audit_user
    ON public.audit_log (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_action
    ON public.audit_log (action, created_at DESC);


-- ═══════════════════════════════════════════════════════════════════════════════
-- ROW-LEVEL SECURITY POLICIES
-- ═══════════════════════════════════════════════════════════════════════════════
-- Enable RLS on all tables first
ALTER TABLE public.profiles           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cases              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_images        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tips               ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tip_attachments    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.investigation_notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.alert_subscribers  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_log          ENABLE ROW LEVEL SECURITY;

-- Helper: get current user's role from app_metadata (server-controlled, unforgeable)
CREATE OR REPLACE FUNCTION public.current_user_role()
RETURNS TEXT AS $$
    SELECT COALESCE(
        (auth.jwt() -> 'app_metadata' ->> 'role'),
        'public_viewer'
    )
$$ LANGUAGE SQL STABLE SECURITY DEFINER;

-- Helper: check if current user has at least a minimum role
CREATE OR REPLACE FUNCTION public.has_role(minimum_role TEXT)
RETURNS BOOLEAN AS $$
DECLARE
    role_tier JSONB := '{"public_viewer":0,"registered_user":1,"police_officer":2,"admin":3}'::JSONB;
    current_tier INTEGER;
    required_tier INTEGER;
BEGIN
    current_tier := (role_tier ->> public.current_user_role())::INTEGER;
    required_tier := (role_tier ->> minimum_role)::INTEGER;
    RETURN COALESCE(current_tier, 0) >= COALESCE(required_tier, 999);
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER;


-- ── profiles RLS ──────────────────────────────────────────────────────────────
DROP POLICY IF EXISTS "profiles_select_own" ON public.profiles;
CREATE POLICY "profiles_select_own" ON public.profiles
    FOR SELECT USING (
        auth.uid() = id                              -- own profile
        OR public.has_role('police_officer')         -- police/admin can see all
    );

DROP POLICY IF EXISTS "profiles_update_own" ON public.profiles;
CREATE POLICY "profiles_update_own" ON public.profiles
    FOR UPDATE USING (auth.uid() = id)
    WITH CHECK (auth.uid() = id);

DROP POLICY IF EXISTS "profiles_insert_own" ON public.profiles;
CREATE POLICY "profiles_insert_own" ON public.profiles
    FOR INSERT WITH CHECK (auth.uid() = id);

-- Admins can update any profile (for role changes, verification)
DROP POLICY IF EXISTS "profiles_admin_update" ON public.profiles;
CREATE POLICY "profiles_admin_update" ON public.profiles
    FOR UPDATE USING (public.has_role('admin'));


-- ── cases RLS ─────────────────────────────────────────────────────────────────

-- Public: see all published cases (no police verification gate)
-- Tips, notes, and reporter contact details remain restricted separately.
DROP POLICY IF EXISTS "cases_public_select" ON public.cases;
CREATE POLICY "cases_public_select" ON public.cases
    FOR SELECT USING (
        is_public = TRUE                              -- any public case is visible
        OR auth.uid() = reporter_id                   -- reporter sees own even if unpublished
        OR public.has_role('police_officer')          -- police/admin see everything
    );

-- Registered users can insert cases
DROP POLICY IF EXISTS "cases_insert_user" ON public.cases;
CREATE POLICY "cases_insert_user" ON public.cases
    FOR INSERT WITH CHECK (
        public.has_role('registered_user')
        AND auth.uid() = reporter_id
    );

-- Reporter can update own case (limited fields — officer update handled server-side)
DROP POLICY IF EXISTS "cases_update_reporter" ON public.cases;
CREATE POLICY "cases_update_reporter" ON public.cases
    FOR UPDATE USING (
        auth.uid() = reporter_id                      -- own case
        OR public.has_role('police_officer')          -- police can update any
    );

-- Only admin can delete (soft-delete preferred; hard delete audit-logged)
DROP POLICY IF EXISTS "cases_delete_admin" ON public.cases;
CREATE POLICY "cases_delete_admin" ON public.cases
    FOR DELETE USING (public.has_role('admin'));


-- ── case_images RLS ───────────────────────────────────────────────────────────
DROP POLICY IF EXISTS "case_images_select" ON public.case_images;
CREATE POLICY "case_images_select" ON public.case_images
    FOR SELECT USING (
        EXISTS (
            SELECT 1 FROM public.cases c
            WHERE c.id = case_id
            AND (c.is_public = TRUE OR auth.uid() = c.reporter_id OR public.has_role('police_officer'))
        )
    );

DROP POLICY IF EXISTS "case_images_insert" ON public.case_images;
CREATE POLICY "case_images_insert" ON public.case_images
    FOR INSERT WITH CHECK (
        public.has_role('registered_user')
        AND EXISTS (
            SELECT 1 FROM public.cases c
            WHERE c.id = case_id AND c.reporter_id = auth.uid()
        )
    );


-- ── tips RLS ──────────────────────────────────────────────────────────────────

-- Anyone can insert a tip (anonymous tips have no tipster_id)
DROP POLICY IF EXISTS "tips_insert_any" ON public.tips;
CREATE POLICY "tips_insert_any" ON public.tips
    FOR INSERT WITH CHECK (TRUE);

-- Tipster can see their own non-anonymous tips; reporter sees tips on their case; police/admin see all
DROP POLICY IF EXISTS "tips_select" ON public.tips;
CREATE POLICY "tips_select" ON public.tips
    FOR SELECT USING (
        public.has_role('police_officer')                         -- police & admin see all
        OR (is_anonymous = FALSE AND auth.uid() = tipster_id)    -- tipster sees own identified tip
        OR EXISTS (                                               -- case reporter (family) sees tips on their case
            SELECT 1 FROM public.cases c
            WHERE c.id = case_id AND c.reporter_id = auth.uid()
        )
    );

-- Only police/admin can update tip status
DROP POLICY IF EXISTS "tips_update_police" ON public.tips;
CREATE POLICY "tips_update_police" ON public.tips
    FOR UPDATE USING (public.has_role('police_officer'));


-- ── tip_attachments RLS ───────────────────────────────────────────────────────
DROP POLICY IF EXISTS "tip_attachments_select" ON public.tip_attachments;
CREATE POLICY "tip_attachments_select" ON public.tip_attachments
    FOR SELECT USING (public.has_role('police_officer'));

DROP POLICY IF EXISTS "tip_attachments_insert" ON public.tip_attachments;
CREATE POLICY "tip_attachments_insert" ON public.tip_attachments
    FOR INSERT WITH CHECK (TRUE);    -- any (anonymous) user can attach


-- ── investigation_notes RLS ───────────────────────────────────────────────────
DROP POLICY IF EXISTS "investigation_notes_select" ON public.investigation_notes;
CREATE POLICY "investigation_notes_select" ON public.investigation_notes
    FOR SELECT USING (public.has_role('police_officer'));

DROP POLICY IF EXISTS "investigation_notes_insert" ON public.investigation_notes;
CREATE POLICY "investigation_notes_insert" ON public.investigation_notes
    FOR INSERT WITH CHECK (
        public.has_role('police_officer')
        AND auth.uid() = author_id
    );

DROP POLICY IF EXISTS "investigation_notes_update_own" ON public.investigation_notes;
CREATE POLICY "investigation_notes_update_own" ON public.investigation_notes
    FOR UPDATE USING (
        auth.uid() = author_id                 -- officer edits own notes
        OR public.has_role('admin')
    );


-- ── alert_subscribers RLS ─────────────────────────────────────────────────────
DROP POLICY IF EXISTS "subscribers_insert" ON public.alert_subscribers;
CREATE POLICY "subscribers_insert" ON public.alert_subscribers
    FOR INSERT WITH CHECK (TRUE);   -- anyone can subscribe

DROP POLICY IF EXISTS "subscribers_select_own" ON public.alert_subscribers;
CREATE POLICY "subscribers_select_own" ON public.alert_subscribers
    FOR SELECT USING (public.has_role('admin'));

DROP POLICY IF EXISTS "subscribers_update_own" ON public.alert_subscribers;
CREATE POLICY "subscribers_update_own" ON public.alert_subscribers
    FOR UPDATE USING (public.has_role('admin'));


-- ── audit_log RLS — append-only ───────────────────────────────────────────────
DROP POLICY IF EXISTS "audit_log_insert" ON public.audit_log;
CREATE POLICY "audit_log_insert" ON public.audit_log
    FOR INSERT WITH CHECK (TRUE);   -- backend inserts via service role

DROP POLICY IF EXISTS "audit_log_select_admin" ON public.audit_log;
CREATE POLICY "audit_log_select_admin" ON public.audit_log
    FOR SELECT USING (public.has_role('admin'));

-- NO UPDATE or DELETE policy on audit_log — ever.


-- ═══════════════════════════════════════════════════════════════════════════════
-- TRIGGERS & FUNCTIONS
-- ═══════════════════════════════════════════════════════════════════════════════

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER profiles_updated_at
    BEFORE UPDATE ON public.profiles
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE OR REPLACE TRIGGER cases_updated_at
    BEFORE UPDATE ON public.cases
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE OR REPLACE TRIGGER tips_updated_at
    BEFORE UPDATE ON public.tips
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


-- Auto-generate case number: NP-2024-00001
CREATE OR REPLACE FUNCTION public.generate_case_number()
RETURNS TRIGGER AS $$
DECLARE
    year TEXT := TO_CHAR(NOW(), 'YYYY');
    seq  INTEGER;
BEGIN
    SELECT COUNT(*) + 1 INTO seq
    FROM public.cases
    WHERE EXTRACT(YEAR FROM created_at) = EXTRACT(YEAR FROM NOW());
    NEW.case_number := 'NP-' || year || '-' || LPAD(seq::TEXT, 5, '0');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER cases_generate_number
    BEFORE INSERT ON public.cases
    FOR EACH ROW EXECUTE FUNCTION public.generate_case_number();


-- Auto-generate tip number: NP-TIP-20240518-001
CREATE OR REPLACE FUNCTION public.generate_tip_number()
RETURNS TRIGGER AS $$
DECLARE
    date_str TEXT := TO_CHAR(NOW(), 'YYYYMMDD');
    seq      INTEGER;
BEGIN
    SELECT COUNT(*) + 1 INTO seq
    FROM public.tips
    WHERE DATE(created_at) = CURRENT_DATE;
    NEW.tip_number := 'NP-TIP-' || date_str || '-' || LPAD(seq::TEXT, 3, '0');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER tips_generate_number
    BEFORE INSERT ON public.tips
    FOR EACH ROW EXECUTE FUNCTION public.generate_tip_number();


-- Dedup hash generation on insert/update
CREATE OR REPLACE FUNCTION public.set_dedup_hash()
RETURNS TRIGGER AS $$
BEGIN
    NEW.dedup_hash := MD5(
        LOWER(REGEXP_REPLACE(NEW.full_name, '\s+', '', 'g'))
        || TO_CHAR(NEW.date_of_birth, 'YYYYMMDD')
        || LOWER(NEW.last_seen_county)
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER cases_dedup_hash
    BEFORE INSERT OR UPDATE ON public.cases
    FOR EACH ROW EXECUTE FUNCTION public.set_dedup_hash();


-- Auto-create profile on auth.users insert (via Supabase Auth hook)
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO public.profiles (id, full_name, email)
    VALUES (
        NEW.id,
        COALESCE(NEW.raw_user_meta_data ->> 'full_name', 'New User'),
        NEW.email
    )
    ON CONFLICT (id) DO NOTHING;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE OR REPLACE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();
