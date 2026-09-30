-- stl_members was once named starborn. Rename it in place so its rows, and stb_activity's foreign key,
-- carry over. A database that already started under the new name also has an empty stl_members, which
-- is dropped to make way; if both tables hold rows, they must be merged by hand.
DO $$
BEGIN
    IF to_regclass('public.starborn') IS NOT NULL THEN
        IF to_regclass('public.stl_members') IS NOT NULL THEN
            IF EXISTS (SELECT 1 FROM stl_members) THEN
                RAISE EXCEPTION 'Tables starborn and stl_members both hold rows; merge them into stl_members by hand';
            END IF;
            DROP TABLE stl_members;
        END IF;
        ALTER TABLE starborn RENAME TO stl_members;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS stl_members (
    st_id BIGSERIAL PRIMARY KEY,
    discord_id BIGINT NOT NULL UNIQUE,
    stl_nation VARCHAR(40) DEFAULT '',
    hzn_nation VARCHAR(40) DEFAULT '',
    status VARCHAR(10) DEFAULT 'VOYAGER'
);

ALTER TABLE stl_members ADD COLUMN IF NOT EXISTS status VARCHAR(10) DEFAULT 'VOYAGER';

CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS stb_activity (
    userid BIGINT PRIMARY KEY,
    wa_status BOOLEAN DEFAULT FALSE,
    last_ns_login TIMESTAMP WITHOUT TIME ZONE,
    last_discord_message_sent TIMESTAMP WITHOUT TIME ZONE,
    last_rmb_message_sent TIMESTAMP WITHOUT TIME ZONE,
    CONSTRAINT fk_userid
        FOREIGN KEY (userid)
        REFERENCES stl_members (st_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meta_aliases (
    name TEXT PRIMARY KEY,
    chain TEXT NOT NULL,
    min_tier INTEGER,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS meta_jobs (
    id BIGSERIAL PRIMARY KEY,
    chain TEXT NOT NULL,
    guild_id BIGINT,
    channel_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    next_run TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    interval_seconds INTEGER,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS meta_triggers (
    id BIGSERIAL PRIMARY KEY,
    event TEXT NOT NULL,
    chain TEXT NOT NULL,
    guild_id BIGINT,
    channel_id BIGINT NOT NULL,
    created_by BIGINT NOT NULL,
    source_channel_id BIGINT,
    contains TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS invite_roles (
    invite_code TEXT NOT NULL,
    role_id BIGINT NOT NULL,
    PRIMARY KEY (invite_code, role_id)
);

-- Every happening received from the NationStates SSE feed, kept for a configurable window (see SseFeed).
-- id is NationStates' own happening ID. text keeps the @@nation@@ / %%region%% markup.
CREATE TABLE IF NOT EXISTS ns_events (
    id BIGINT PRIMARY KEY,
    time TIMESTAMP WITH TIME ZONE NOT NULL,
    text TEXT NOT NULL,
    buckets TEXT[] NOT NULL,
    rmb_message TEXT
);

CREATE INDEX IF NOT EXISTS ns_events_time_idx ON ns_events (time);
CREATE INDEX IF NOT EXISTS ns_events_buckets_idx ON ns_events USING GIN (buckets);

-- Regions whose arrivals /hail tracks. last_listed_id is the newest hail_arrivals.id already shown by /hail.
CREATE TABLE IF NOT EXISTS hail_regions (
    region TEXT PRIMARY KEY,
    last_listed_id BIGINT NOT NULL DEFAULT 0,
    added_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

-- Each nation is recorded once per region; a nation that leaves and returns is not recorded again.
CREATE TABLE IF NOT EXISTS hail_arrivals (
    id BIGSERIAL PRIMARY KEY,
    region TEXT NOT NULL REFERENCES hail_regions (region) ON DELETE CASCADE,
    nation TEXT NOT NULL,
    arrived_at TIMESTAMP WITH TIME ZONE NOT NULL,
    event_id BIGINT,
    UNIQUE (region, nation)
);

-- Legacy from refits. If stuff breaks uncomment and try to run this.
-- ALTER TABLE meta_triggers ADD COLUMN IF NOT EXISTS source_channel_id BIGINT;
-- ALTER TABLE meta_triggers ADD COLUMN IF NOT EXISTS contains TEXT;
-- -- guild_id is backfilled from channel_id on startup for rows created before the column existed.
-- ALTER TABLE meta_jobs ADD COLUMN IF NOT EXISTS guild_id BIGINT;
-- ALTER TABLE meta_triggers ADD COLUMN IF NOT EXISTS guild_id BIGINT;
--
-- -- Retrofit the UNIQUE constraint for tables created before discord_id was declared UNIQUE above.
-- -- A table renamed from starborn keeps its constraint under the old name.
-- DO $$
-- BEGIN
--     IF NOT EXISTS (
--         SELECT 1 FROM pg_constraint
--         WHERE conname IN ('starborn_discord_id_key', 'stl_members_discord_id_key')
--     ) THEN
--         ALTER TABLE stl_members ADD CONSTRAINT stl_members_discord_id_key UNIQUE (discord_id);
--     END IF;
-- END $$;
