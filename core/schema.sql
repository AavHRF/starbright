CREATE TABLE IF NOT EXISTS starborn (
    st_id BIGSERIAL PRIMARY KEY,
    discord_id BIGINT NOT NULL UNIQUE,
    stl_nation VARCHAR(40) DEFAULT '',
    hzn_nation VARCHAR(40) DEFAULT ''
);

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
        REFERENCES starborn (st_id)
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

ALTER TABLE meta_triggers ADD COLUMN IF NOT EXISTS source_channel_id BIGINT;
ALTER TABLE meta_triggers ADD COLUMN IF NOT EXISTS contains TEXT;
-- guild_id is backfilled from channel_id on startup for rows created before the column existed.
ALTER TABLE meta_jobs ADD COLUMN IF NOT EXISTS guild_id BIGINT;
ALTER TABLE meta_triggers ADD COLUMN IF NOT EXISTS guild_id BIGINT;

-- Retrofit the UNIQUE constraint for tables created before discord_id was declared UNIQUE above.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'starborn_discord_id_key'
    ) THEN
        ALTER TABLE starborn ADD CONSTRAINT starborn_discord_id_key UNIQUE (discord_id);
    END IF;
END $$;
