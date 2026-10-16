-- 0001_init.sql — core schema for the perk monitor.
-- All timestamps are stored as UTC ISO-8601 with a trailing 'Z' (second precision)
-- so that SQLite's datetime() functions and plain string comparison both work.

CREATE TABLE sources (
    id              INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL,
    kind            TEXT    NOT NULL CHECK (kind IN ('rss', 'web', 'search')),
    url             TEXT    NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    product_hint    TEXT,
    notes           TEXT,
    etag            TEXT,
    last_modified   TEXT,
    last_success_at TEXT,
    last_error      TEXT,
    last_error_at   TEXT,
    created_at      TEXT    NOT NULL
);
CREATE UNIQUE INDEX sources_url_idx ON sources(url);

CREATE TABLE articles (
    id              INTEGER PRIMARY KEY,
    source_id       INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    url             TEXT    NOT NULL,
    url_norm        TEXT,
    title           TEXT,
    author          TEXT,
    published_at    TEXT,
    fetched_at      TEXT    NOT NULL,
    content         TEXT,
    summary         TEXT,
    content_hash    TEXT    NOT NULL,
    prev_article_id INTEGER REFERENCES articles(id) ON DELETE SET NULL,
    domain          TEXT,
    extract_error   TEXT,
    analyze_state   TEXT    NOT NULL DEFAULT 'pending'
                              CHECK (analyze_state IN ('pending', 'skipped', 'done', 'failed')),
    analyzed_at     TEXT,
    analyze_error   TEXT,
    prefilter_hit   TEXT
);
-- Identity is (location, content). A re-fetch of an unchanged page is a duplicate
-- and is dropped; a *changed* page at the same location becomes a new revision row
-- linked through prev_article_id, so rule updates are never lost.
CREATE UNIQUE INDEX articles_url_hash_idx ON articles(url_norm, content_hash);
CREATE INDEX articles_content_hash_idx    ON articles(content_hash);
CREATE INDEX articles_analyze_state_idx   ON articles(analyze_state);
CREATE INDEX articles_url_norm_idx        ON articles(url_norm);

-- Every distinct location an article has been seen at. Kept separate from
-- articles so that merging duplicate copies never loses an entry point.
CREATE TABLE article_aliases (
    url_norm      TEXT    NOT NULL,
    article_id    INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    first_seen_at TEXT    NOT NULL,
    PRIMARY KEY (url_norm, article_id)
);

CREATE TABLE perks (
    id                            INTEGER PRIMARY KEY,
    canonical_key                 TEXT    NOT NULL UNIQUE,
    product                       TEXT    NOT NULL,
    product_norm                  TEXT    NOT NULL,
    benefit_type                  TEXT    CHECK (benefit_type IS NULL OR benefit_type IN
                                  ('free_membership', 'free_credits', 'redemption_code',
                                   'student_discount', 'free_trial', 'discount',
                                   'giveaway', 'other')),
    entitlement                   TEXT,
    quota                         TEXT,
    starts_at                     TEXT,
    ends_at                       TEXT,
    claim_url                     TEXT,
    rules_url                     TEXT,
    region                        TEXT,
    eligibility                   TEXT,
    new_users_only                INTEGER,
    student_verification_required INTEGER,
    payment_method_required       INTEGER,
    invite_required               INTEGER,
    confidence                    REAL,
    status                        TEXT    NOT NULL DEFAULT 'pending'
                                            CHECK (status IN ('pending', 'confirmed',
                                                              'expired', 'invalid', 'dubious')),
    verification_id               INTEGER REFERENCES verification_runs(id) ON DELETE SET NULL,
    last_verified_at              TEXT,
    first_seen_at                 TEXT    NOT NULL,
    updated_at                    TEXT    NOT NULL,
    needs_review                  INTEGER NOT NULL DEFAULT 0,
    review_reason                 TEXT,
    user_claimed                  INTEGER NOT NULL DEFAULT 0,
    user_note                     TEXT
);
CREATE INDEX perks_product_norm_idx ON perks(product_norm);
CREATE INDEX perks_status_idx       ON perks(status);
CREATE INDEX perks_ends_idx         ON perks(ends_at);

-- Verbatim quotes the model claims support each field, plus the mechanical
-- verdict on whether the quote really occurs in the source text.
CREATE TABLE perk_evidence (
    id         INTEGER PRIMARY KEY,
    perk_id    INTEGER NOT NULL REFERENCES perks(id) ON DELETE CASCADE,
    article_id INTEGER REFERENCES articles(id) ON DELETE SET NULL,
    field      TEXT,
    quote      TEXT    NOT NULL,
    verified   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL
);
CREATE INDEX perk_evidence_perk_idx ON perk_evidence(perk_id);
CREATE INDEX perk_evidence_quote_idx ON perk_evidence(quote);

CREATE TABLE perk_sources (
    perk_id    INTEGER NOT NULL REFERENCES perks(id)    ON DELETE CASCADE,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL DEFAULT 'mention' CHECK (role IN ('mention', 'primary', 'update', 'repost')),
    added_at   TEXT    NOT NULL,
    PRIMARY KEY (perk_id, article_id)
);

CREATE TABLE perk_versions (
    id         INTEGER PRIMARY KEY,
    perk_id    INTEGER NOT NULL REFERENCES perks(id) ON DELETE CASCADE,
    snapshot   TEXT    NOT NULL,
    reason     TEXT    NOT NULL CHECK (reason IN ('created', 'rule_update', 'manual', 'status', 'merged')),
    article_id INTEGER REFERENCES articles(id) ON DELETE SET NULL,
    changed_fields TEXT,
    created_at TEXT    NOT NULL
);
CREATE INDEX perk_versions_perk_idx ON perk_versions(perk_id);

CREATE TABLE perk_status_history (
    id          INTEGER PRIMARY KEY,
    perk_id     INTEGER NOT NULL REFERENCES perks(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT    NOT NULL,
    reason      TEXT    NOT NULL,
    changed_by  TEXT    NOT NULL CHECK (changed_by IN ('system', 'llm', 'verification', 'human', 'scheduler')),
    created_at  TEXT    NOT NULL
);
CREATE INDEX perk_history_perk_idx ON perk_status_history(perk_id);

CREATE TABLE verification_runs (
    id                   INTEGER PRIMARY KEY,
    perk_id              INTEGER NOT NULL REFERENCES perks(id) ON DELETE CASCADE,
    url                  TEXT    NOT NULL,
    registrable_domain   TEXT,
    official_domain_match INTEGER NOT NULL DEFAULT 0,
    http_status          INTEGER,
    page_accessible      INTEGER NOT NULL DEFAULT 0,
    verdict              TEXT    NOT NULL CHECK (verdict IN
                           ('available', 'not_found', 'expired', 'login_required',
                            'paywall', 'captcha', 'error', 'unknown')),
    detail               TEXT,
    checked_at           TEXT    NOT NULL
);
CREATE INDEX verification_perk_idx ON verification_runs(perk_id);

-- Notification dedupe: a perk only re-notifies when the fingerprint of its
-- status / deadline / entitlement actually changes. UNIQUE + INSERT OR IGNORE.
CREATE TABLE notifications (
    id         INTEGER PRIMARY KEY,
    perk_id    INTEGER REFERENCES perks(id) ON DELETE CASCADE,
    channel    TEXT    NOT NULL,
    kind       TEXT    NOT NULL,
    dedupe_key TEXT    NOT NULL UNIQUE,
    report_day TEXT,
    payload    TEXT,
    sent_at    TEXT    NOT NULL
);

CREATE TABLE reports (
    id            INTEGER PRIMARY KEY,
    day           TEXT    NOT NULL UNIQUE,
    generated_at  TEXT    NOT NULL,
    markdown_path TEXT,
    stats         TEXT,
    sent_at       TEXT,
    superseded_at TEXT,
    send_error    TEXT
);

CREATE TABLE runs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT    NOT NULL,
    started_at  TEXT    NOT NULL,
    finished_at TEXT,
    counts      TEXT,
    error       TEXT
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Hard, database-level enforcement of the project rule:
--   a perk may never reach 'confirmed' on the strength of an article or an LLM
--   judgement alone. It requires a fresh, official-domain, 'available' check.
-- The freshness window is read from meta['verification_max_age_days'] so the
-- application and the database cannot drift apart.
CREATE TRIGGER perks_no_confirm_without_verification
BEFORE UPDATE OF status ON perks
FOR EACH ROW WHEN NEW.status = 'confirmed'
BEGIN
    SELECT RAISE(ABORT, 'CONFIRM_GUARD: 已确认 需要新鲜的官方域名核验记录')
    WHERE NOT EXISTS (
        SELECT 1 FROM verification_runs v
        WHERE v.id = NEW.verification_id
          AND v.perk_id = NEW.id
          AND v.official_domain_match = 1
          AND v.verdict = 'available'
          AND v.checked_at >= datetime(
                'now',
                '-' || COALESCE(
                    (SELECT CAST(value AS INTEGER) FROM meta WHERE key = 'verification_max_age_days'),
                    7
                ) || ' days'
              )
    );
END;

CREATE TRIGGER perks_no_insert_confirmed
BEFORE INSERT ON perks
FOR EACH ROW WHEN NEW.status = 'confirmed'
BEGIN
    SELECT RAISE(ABORT, 'CONFIRM_GUARD: 新福利不得直接标记为已确认');
END;
