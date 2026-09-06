


SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;


COMMENT ON SCHEMA "public" IS 'standard public schema';



CREATE EXTENSION IF NOT EXISTS "pg_stat_statements" WITH SCHEMA "extensions";






CREATE EXTENSION IF NOT EXISTS "pgcrypto" WITH SCHEMA "extensions";






CREATE EXTENSION IF NOT EXISTS "supabase_vault" WITH SCHEMA "vault";






CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA "extensions";





SET default_tablespace = '';

SET default_table_access_method = "heap";


CREATE TABLE IF NOT EXISTS "public"."ai_logs" (
    "id" integer NOT NULL,
    "timestamp" timestamp without time zone,
    "provider" character varying,
    "model" character varying,
    "prompt" character varying,
    "response" character varying,
    "call_type" character varying,
    "latency_ms" double precision,
    "tokens_used" integer,
    "cost_usd" double precision,
    "related_market" character varying,
    "success" boolean,
    "error" character varying
);


ALTER TABLE "public"."ai_logs" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."ai_logs_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."ai_logs_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."ai_logs_id_seq" OWNED BY "public"."ai_logs"."id";



CREATE TABLE IF NOT EXISTS "public"."bot_state" (
    "id" integer NOT NULL,
    "bankroll" double precision,
    "total_trades" integer,
    "winning_trades" integer,
    "total_pnl" double precision,
    "last_run" timestamp without time zone,
    "is_running" boolean,
    "live_start_balance" double precision,
    "live_session_start" timestamp without time zone
);


ALTER TABLE "public"."bot_state" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."bot_state_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."bot_state_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."bot_state_id_seq" OWNED BY "public"."bot_state"."id";



CREATE TABLE IF NOT EXISTS "public"."scan_logs" (
    "id" integer NOT NULL,
    "run_id" character varying,
    "started_at" timestamp without time zone,
    "completed_at" timestamp without time zone,
    "categories_scanned" json,
    "platforms_scanned" json,
    "markets_found" integer,
    "signals_generated" integer,
    "trades_executed" integer,
    "ai_calls_made" integer,
    "ai_cost_usd" double precision,
    "success" boolean,
    "error" character varying
);


ALTER TABLE "public"."scan_logs" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."scan_logs_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."scan_logs_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."scan_logs_id_seq" OWNED BY "public"."scan_logs"."id";



CREATE TABLE IF NOT EXISTS "public"."signals" (
    "id" integer NOT NULL,
    "market_ticker" character varying,
    "platform" character varying,
    "market_type" character varying,
    "timestamp" timestamp without time zone,
    "direction" character varying,
    "model_probability" double precision,
    "market_price" double precision,
    "edge" double precision,
    "confidence" double precision,
    "kelly_fraction" double precision,
    "suggested_size" double precision,
    "sources" json,
    "reasoning" character varying,
    "executed" boolean,
    "actual_outcome" character varying,
    "outcome_correct" boolean,
    "settlement_value" double precision,
    "settled_at" timestamp without time zone
);


ALTER TABLE "public"."signals" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."signals_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."signals_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."signals_id_seq" OWNED BY "public"."signals"."id";



CREATE TABLE IF NOT EXISTS "public"."trades" (
    "id" integer NOT NULL,
    "signal_id" integer,
    "market_ticker" character varying,
    "platform" character varying,
    "event_slug" character varying,
    "market_type" character varying,
    "direction" character varying,
    "entry_price" double precision,
    "size" double precision,
    "timestamp" timestamp without time zone,
    "settled" boolean,
    "settlement_time" timestamp without time zone,
    "settlement_value" double precision,
    "result" character varying,
    "pnl" double precision,
    "model_probability" double precision,
    "market_price_at_entry" double precision,
    "edge_at_entry" double precision,
    "execution_type" "text",
    "order_id" "text",
    "limit_price" real,
    "order_status" "text",
    "order_placed_at" timestamp without time zone,
    "confidence" real
);


ALTER TABLE "public"."trades" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."trades_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."trades_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."trades_id_seq" OWNED BY "public"."trades"."id";



ALTER TABLE ONLY "public"."ai_logs" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."ai_logs_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."bot_state" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."bot_state_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."scan_logs" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."scan_logs_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."signals" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."signals_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."trades" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."trades_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."ai_logs"
    ADD CONSTRAINT "ai_logs_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."bot_state"
    ADD CONSTRAINT "bot_state_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."scan_logs"
    ADD CONSTRAINT "scan_logs_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."signals"
    ADD CONSTRAINT "signals_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."trades"
    ADD CONSTRAINT "trades_pkey" PRIMARY KEY ("id");



CREATE INDEX "ix_ai_logs_call_type" ON "public"."ai_logs" USING "btree" ("call_type");



CREATE INDEX "ix_ai_logs_id" ON "public"."ai_logs" USING "btree" ("id");



CREATE INDEX "ix_ai_logs_provider" ON "public"."ai_logs" USING "btree" ("provider");



CREATE INDEX "ix_ai_logs_timestamp" ON "public"."ai_logs" USING "btree" ("timestamp");



CREATE INDEX "ix_scan_logs_id" ON "public"."scan_logs" USING "btree" ("id");



CREATE UNIQUE INDEX "ix_scan_logs_run_id" ON "public"."scan_logs" USING "btree" ("run_id");



CREATE INDEX "ix_signals_id" ON "public"."signals" USING "btree" ("id");



CREATE INDEX "ix_signals_market_ticker" ON "public"."signals" USING "btree" ("market_ticker");



CREATE INDEX "ix_signals_market_type" ON "public"."signals" USING "btree" ("market_type");



CREATE INDEX "ix_signals_timestamp" ON "public"."signals" USING "btree" ("timestamp");



CREATE INDEX "ix_trades_id" ON "public"."trades" USING "btree" ("id");



CREATE INDEX "ix_trades_market_ticker" ON "public"."trades" USING "btree" ("market_ticker");



CREATE INDEX "ix_trades_market_type" ON "public"."trades" USING "btree" ("market_type");



CREATE INDEX "ix_trades_signal_id" ON "public"."trades" USING "btree" ("signal_id");





ALTER PUBLICATION "supabase_realtime" OWNER TO "postgres";


GRANT USAGE ON SCHEMA "public" TO "postgres";
GRANT USAGE ON SCHEMA "public" TO "anon";
GRANT USAGE ON SCHEMA "public" TO "authenticated";
GRANT USAGE ON SCHEMA "public" TO "service_role";





































































































































































GRANT ALL ON TABLE "public"."ai_logs" TO "anon";
GRANT ALL ON TABLE "public"."ai_logs" TO "authenticated";
GRANT ALL ON TABLE "public"."ai_logs" TO "service_role";



GRANT ALL ON SEQUENCE "public"."ai_logs_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."ai_logs_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."ai_logs_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."bot_state" TO "anon";
GRANT ALL ON TABLE "public"."bot_state" TO "authenticated";
GRANT ALL ON TABLE "public"."bot_state" TO "service_role";



GRANT ALL ON SEQUENCE "public"."bot_state_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."bot_state_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."bot_state_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."scan_logs" TO "anon";
GRANT ALL ON TABLE "public"."scan_logs" TO "authenticated";
GRANT ALL ON TABLE "public"."scan_logs" TO "service_role";



GRANT ALL ON SEQUENCE "public"."scan_logs_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."scan_logs_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."scan_logs_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."signals" TO "anon";
GRANT ALL ON TABLE "public"."signals" TO "authenticated";
GRANT ALL ON TABLE "public"."signals" TO "service_role";



GRANT ALL ON SEQUENCE "public"."signals_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."signals_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."signals_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."trades" TO "anon";
GRANT ALL ON TABLE "public"."trades" TO "authenticated";
GRANT ALL ON TABLE "public"."trades" TO "service_role";



GRANT ALL ON SEQUENCE "public"."trades_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."trades_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."trades_id_seq" TO "service_role";









ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "service_role";






ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "service_role";






ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "service_role";































