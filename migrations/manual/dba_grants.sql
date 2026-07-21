-- Manual DBA script — NOT part of the Alembic chain.
-- Run once per environment by a DBA, after `alembic upgrade head` has created
-- the tables (revision a1c0ffee0001).
--
-- Purpose (spec §3.2): keep-event-handler hydrates its automation trigger
-- index via read-only SELECT on `automations` and must never write. It gets
-- no access to `automation_runs` / `automation_revisions`.
--
-- Replace the password placeholder before running. Role names are referenced
-- by the services via env vars (DATABASE_EVENT_HANDLER_ROLE); keep them in
-- sync with automation-contracts.md §"Environment & endpoints".

-- 1. Read-only user for keep-event-handler
CREATE ROLE keep_event_handler_ro LOGIN PASSWORD '<password-from-secret-store>';
GRANT CONNECT ON DATABASE keep_automations TO keep_event_handler_ro;
GRANT SELECT ON automations TO keep_event_handler_ro;

-- 2. (Optional) dedicated writer role for keep-automation-api.
--    Skip when the API connects as the table owner (the role that ran the
--    migrations) — the owner needs no explicit grants.
-- CREATE ROLE keep_automation_api LOGIN PASSWORD '<password-from-secret-store>';
-- GRANT CONNECT ON DATABASE keep_automations TO keep_automation_api;
-- GRANT SELECT, INSERT, UPDATE, DELETE
--     ON automations, automation_runs, automation_revisions
--     TO keep_automation_api;

-- Verification (run as keep_event_handler_ro):
--   SELECT id, triggers, matching_state FROM automations;         -- OK
--   INSERT/UPDATE/DELETE on automations                           -- must fail
--   SELECT on automation_runs / automation_revisions              -- must fail
