-- Procrastinate Schema

CREATE EXTENSION IF NOT EXISTS plpgsql WITH SCHEMA pg_catalog;


CREATE TYPE procrastinate_job_status AS ENUM (
    'todo',  -- The job is queued
    'doing',  -- The job has been fetched by a worker
    'succeeded',  -- The job ended succesfully
    'failed'  -- The job ended with an error
);

CREATE TYPE procrastinate_job_event_type AS ENUM (
    'deferred',  -- Job created, in todo
    'started',  -- todo -> doing
    'deferred_for_retry',  -- doing -> todo
    'failed',  -- doing -> failed
    'succeeded',  -- doing -> succeeded
    'cancelled', -- todo -> failed or succeeded
    'scheduled' -- not an event transition, but recording when a task is scheduled for
);

CREATE TABLE procrastinate_jobs (
    id bigserial PRIMARY KEY,
    queue_name character varying(128) NOT NULL,
    task_name character varying(128) NOT NULL,
    lock text,
    queueing_lock text,
    args jsonb DEFAULT '{}' NOT NULL,
    status procrastinate_job_status DEFAULT 'todo'::procrastinate_job_status NOT NULL,
    scheduled_at timestamp with time zone NULL,
    attempts integer DEFAULT 0 NOT NULL
);

-- this prevents from having several jobs with the same queueing lock in the "todo" state
CREATE UNIQUE INDEX procrastinate_jobs_queueing_lock_idx ON procrastinate_jobs (queueing_lock) WHERE status = 'todo';

CREATE TABLE procrastinate_events (
    id BIGSERIAL PRIMARY KEY,
    job_id integer NOT NULL REFERENCES procrastinate_jobs ON DELETE CASCADE,
    type procrastinate_job_event_type,
    at timestamp with time zone DEFAULT NOW() NULL
);

CREATE FUNCTION procrastinate_fetch_job(target_queue_names character varying[]) RETURNS procrastinate_jobs
    LANGUAGE plpgsql
    AS $$
DECLARE
    found_job procrastinate_jobs;
BEGIN
    WITH
    candidates AS (
        SELECT DISTINCT ON (lock)
            id,
            lock
        FROM procrastinate_jobs
        WHERE status IN ('todo', 'doing')
        ORDER BY lock, id ASC
    ),
    candidate AS (
        SELECT jobs.*
            FROM procrastinate_jobs jobs JOIN candidates ON jobs.id = candidates.id
            WHERE
                status = 'todo'
                AND (target_queue_names IS NULL OR queue_name = ANY( target_queue_names ))
                AND (scheduled_at IS NULL OR scheduled_at <= now())
            ORDER BY jobs.id ASC LIMIT 1
            FOR UPDATE OF jobs SKIP LOCKED
    )
    UPDATE procrastinate_jobs
        SET status = 'doing'
        FROM candidate
        WHERE procrastinate_jobs.id = candidate.id
        RETURNING procrastinate_jobs.* INTO found_job;

    RETURN found_job;
END;
$$;

CREATE FUNCTION procrastinate_finish_job(job_id integer, end_status procrastinate_job_status, next_scheduled_at timestamp with time zone) RETURNS void
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE procrastinate_jobs
      SET status = end_status,
          attempts = attempts + 1,
          scheduled_at = COALESCE(next_scheduled_at, scheduled_at)
      WHERE id = job_id;
END;
$$;

CREATE FUNCTION procrastinate_notify_queue() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
	PERFORM pg_notify('procrastinate_queue#' || NEW.queue_name, NEW.task_name);
	PERFORM pg_notify('procrastinate_any_queue', NEW.task_name);
	RETURN NEW;
END;
$$;

CREATE FUNCTION procrastinate_trigger_status_events_procedure_insert() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    INSERT INTO procrastinate_events(job_id, type)
        VALUES (NEW.id, 'deferred'::procrastinate_job_event_type);
	RETURN NEW;
END;
$$;

CREATE FUNCTION procrastinate_trigger_status_events_procedure_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    WITH t AS (
        SELECT CASE
            WHEN OLD.status = 'todo'::procrastinate_job_status
                AND NEW.status = 'doing'::procrastinate_job_status
                THEN 'started'::procrastinate_job_event_type
            WHEN OLD.status = 'doing'::procrastinate_job_status
                AND NEW.status = 'todo'::procrastinate_job_status
                THEN 'deferred_for_retry'::procrastinate_job_event_type
            WHEN OLD.status = 'doing'::procrastinate_job_status
                AND NEW.status = 'failed'::procrastinate_job_status
                THEN 'failed'::procrastinate_job_event_type
            WHEN OLD.status = 'doing'::procrastinate_job_status
                AND NEW.status = 'succeeded'::procrastinate_job_status
                THEN 'succeeded'::procrastinate_job_event_type
            WHEN OLD.status = 'todo'::procrastinate_job_status
                AND (
                    NEW.status = 'failed'::procrastinate_job_status
                    OR NEW.status = 'succeeded'::procrastinate_job_status
                )
                THEN 'cancelled'::procrastinate_job_event_type
            ELSE NULL
        END as event_type
    )
    INSERT INTO procrastinate_events(job_id, type)
        SELECT NEW.id, t.event_type
        FROM t
        WHERE t.event_type IS NOT NULL;
	RETURN NEW;
END;
$$;

CREATE FUNCTION procrastinate_trigger_scheduled_events_procedure() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    INSERT INTO procrastinate_events(job_id, type, at)
        VALUES (NEW.id, 'scheduled'::procrastinate_job_event_type, NEW.scheduled_at);

	RETURN NEW;
END;
$$;

CREATE INDEX ON procrastinate_jobs(queue_name);

CREATE TRIGGER procrastinate_jobs_notify_queue
    AFTER INSERT ON procrastinate_jobs
    FOR EACH ROW WHEN ((new.status = 'todo'::procrastinate_job_status))
    EXECUTE PROCEDURE procrastinate_notify_queue();

CREATE TRIGGER procrastinate_trigger_status_events_update
    AFTER UPDATE OF status ON procrastinate_jobs
    FOR EACH ROW
    EXECUTE PROCEDURE procrastinate_trigger_status_events_procedure_update();

CREATE TRIGGER procrastinate_trigger_status_events_insert
    AFTER INSERT ON procrastinate_jobs
    FOR EACH ROW WHEN ((new.status = 'todo'::procrastinate_job_status))
    EXECUTE PROCEDURE procrastinate_trigger_status_events_procedure_insert();

CREATE TRIGGER procrastinate_trigger_scheduled_events
    AFTER UPDATE OR INSERT ON procrastinate_jobs
    FOR EACH ROW WHEN ((new.scheduled_at IS NOT NULL AND new.status = 'todo'::procrastinate_job_status))
    EXECUTE PROCEDURE procrastinate_trigger_scheduled_events_procedure();

CREATE INDEX procrastinate_events_job_id_fkey ON procrastinate_events(job_id);
