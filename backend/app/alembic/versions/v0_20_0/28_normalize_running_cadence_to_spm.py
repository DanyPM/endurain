"""28: normalize stored running cadence to steps per minute (spm)

Running cadence was stored as the source reported it: FIT, Strava, TCX and GPX
running cadence is a single-leg rate (rpm, around 85) while some treadmill and
footpod sources store full spm (around 160). The importers store spm for running
activities, so existing rows are brought to the same unit.

Only the stored numbers are available here, so the per-activity decision is a
best-effort heuristic, not an exact split: a running activity whose
representative cadence is below 100 is taken as rpm and doubled, one at or above
100 is taken as spm and left untouched. Per-leg running cadence is about 70 to
110 and real spm is almost never below 120, so a slow run above 100 rpm stays in
rpm and a walk logged as a run below 100 spm is doubled.

The representative cadence is ``activities.average_cad``, else the mean of the
non-zero cadence stream samples (``stream_type = 3``), else the mean of the
per-lap cadence. The decision is taken once per activity, recorded in
``cadence_spm_migration_rows`` together with the original scalars, and applied
to the stream, the laps and the scalars alike. Activities without an average get
``average_cad``/``max_cad`` from the stream or laps they were decided from.

Only plausible values are touched: cadence is a uint8 in FIT, so activities with
``max_cad`` above 255, lap values above 255 and stream samples above 255 or
non-numeric are left as stored rather than overflowing ``int4``. A lap whose
fraction is folded into ``avg_cadence``/``max_cadence`` has its
``*_fractional_cadence`` cleared.

``downgrade()`` restores the recorded activities' original scalars and halves
their lap values and stream samples with integer division: a folded fraction of
0.75 or more comes back one higher, cleared fractions stay empty, and a lap value
or stream sample that upgrade left above 255 but at most 510 is halved too.

Revision ID: c91ac2dad0f0
Revises: d54bdd779392
Create Date: 2026-10-19 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c91ac2dad0f0"
down_revision: str | None = "d54bdd779392"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The running activity types (1, 2, 3, 34, 40), the rpm/spm cutoff (100) and the
# plausible cadence ceiling (255) are inlined as a snapshot; a migration must not
# follow later edits to app code. A stream ``cad`` is cast to numeric only when
# it matches ``^[0-9]{1,3}([.][0-9]+)?$``, so a non-numeric sample cannot abort it.


def upgrade() -> None:
    """Double running cadence still stored as a single-leg rate."""
    op.execute(
        """
        CREATE TABLE cadence_spm_migration_rows (
            activity_id integer PRIMARY KEY,
            average_cad integer,
            max_cad integer,
            per_leg_avg numeric,
            per_leg_max numeric
        )
        """
    )

    op.execute(
        """
        INSERT INTO cadence_spm_migration_rows (activity_id, average_cad, max_cad, per_leg_avg, per_leg_max)
        WITH stream_cad AS (
            SELECT s.activity_id, avg(v.cad) AS avg_cad, max(v.cad) AS max_cad
            FROM activities_streams s
            JOIN activities a ON a.id = s.activity_id AND a.activity_type IN (1, 2, 3, 34, 40)
            CROSS JOIN LATERAL json_array_elements(
                CASE WHEN json_typeof(s.stream_waypoints) = 'array' THEN s.stream_waypoints ELSE '[]'::json END
            ) AS t(elem)
            CROSS JOIN LATERAL (
                SELECT CASE WHEN (t.elem ->> 'cad') ~ '^[0-9]{1,3}([.][0-9]+)?$' THEN (t.elem ->> 'cad')::numeric END AS cad
            ) AS v
            WHERE s.stream_type = 3
              AND v.cad > 0
              AND v.cad <= 255
            GROUP BY s.activity_id
        ),
        lap_cad AS (
            SELECT l.activity_id,
                   avg(COALESCE(l.avg_cadence, l.max_cadence)) AS avg_cad,
                   max(COALESCE(l.max_cadence, l.avg_cadence)) AS max_cad
            FROM activity_laps l
            JOIN activities a ON a.id = l.activity_id AND a.activity_type IN (1, 2, 3, 34, 40)
            WHERE COALESCE(l.avg_cadence, l.max_cadence) > 0
              AND COALESCE(l.avg_cadence, 0) <= 255
              AND COALESCE(l.max_cadence, 0) <= 255
            GROUP BY l.activity_id
        ),
        decided AS (
            SELECT a.id,
                   a.average_cad,
                   a.max_cad,
                   COALESCE(a.average_cad, sc.avg_cad, lc.avg_cad) AS per_leg_avg,
                   CASE
                       WHEN a.max_cad IS NOT NULL OR a.average_cad IS NOT NULL THEN a.max_cad
                       WHEN sc.avg_cad IS NOT NULL THEN sc.max_cad
                       ELSE lc.max_cad
                   END AS per_leg_max
            FROM activities a
            LEFT JOIN stream_cad sc ON sc.activity_id = a.id
            LEFT JOIN lap_cad lc ON lc.activity_id = a.id
            WHERE a.activity_type IN (1, 2, 3, 34, 40)
              AND COALESCE(a.max_cad, 0) <= 255
        )
        SELECT id, average_cad, max_cad, per_leg_avg, per_leg_max
        FROM decided
        WHERE per_leg_avg < 100
        """
    )

    op.execute(
        """
        UPDATE activities_streams s
        SET stream_waypoints = (
            SELECT jsonb_agg(
                CASE
                    WHEN (elem ->> 'cad') IS NULL OR (elem ->> 'cad') !~ '^[0-9]{1,3}([.][0-9]+)?$' THEN elem
                    WHEN (elem ->> 'cad')::numeric > 255 THEN elem
                    ELSE jsonb_set(elem, '{cad}', to_jsonb(round((elem ->> 'cad')::numeric * 2)::int))
                END
                ORDER BY ord
            )::json
            FROM jsonb_array_elements(s.stream_waypoints::jsonb) WITH ORDINALITY AS t(elem, ord)
        )
        FROM cadence_spm_migration_rows r
        WHERE r.activity_id = s.activity_id
          AND s.stream_type = 3
          AND CASE WHEN json_typeof(s.stream_waypoints) = 'array' THEN json_array_length(s.stream_waypoints) > 0 END
        """
    )

    op.execute(
        """
        UPDATE activity_laps l
        SET avg_cadence = CASE WHEN d.avg_ok THEN round((l.avg_cadence + COALESCE(l.avg_fractional_cadence, 0)) * 2)
                               ELSE l.avg_cadence END,
            max_cadence = CASE WHEN d.max_ok THEN round((l.max_cadence + COALESCE(l.max_fractional_cadence, 0)) * 2)
                               ELSE l.max_cadence END,
            avg_fractional_cadence = CASE WHEN d.avg_ok THEN NULL ELSE l.avg_fractional_cadence END,
            max_fractional_cadence = CASE WHEN d.max_ok THEN NULL ELSE l.max_fractional_cadence END
        FROM (
            SELECT k.id,
                   k.avg_cadence IS NOT NULL AND k.avg_cadence + COALESCE(k.avg_fractional_cadence, 0) <= 255 AS avg_ok,
                   k.max_cadence IS NOT NULL AND k.max_cadence + COALESCE(k.max_fractional_cadence, 0) <= 255 AS max_ok
            FROM activity_laps k
            JOIN cadence_spm_migration_rows r ON r.activity_id = k.activity_id
        ) AS d
        WHERE d.id = l.id
          AND (d.avg_ok OR d.max_ok)
        """
    )

    op.execute(
        """
        UPDATE activities a
        SET average_cad = round(r.per_leg_avg * 2)::int,
            max_cad = round(r.per_leg_max * 2)::int
        FROM cadence_spm_migration_rows r
        WHERE r.activity_id = a.id
        """
    )


def downgrade() -> None:
    """Return the recorded activities' cadence to single-leg rpm.

    The module docstring lists where the inverse is not exact.
    """
    op.execute(
        """
        UPDATE activities_streams s
        SET stream_waypoints = (
            SELECT jsonb_agg(
                CASE
                    WHEN (elem ->> 'cad') IS NULL OR (elem ->> 'cad') !~ '^[0-9]{1,3}([.][0-9]+)?$' THEN elem
                    WHEN (elem ->> 'cad')::numeric > 510 THEN elem
                    ELSE jsonb_set(elem, '{cad}', to_jsonb(floor((elem ->> 'cad')::numeric / 2)::int))
                END
                ORDER BY ord
            )::json
            FROM jsonb_array_elements(s.stream_waypoints::jsonb) WITH ORDINALITY AS t(elem, ord)
        )
        FROM cadence_spm_migration_rows r
        WHERE r.activity_id = s.activity_id
          AND s.stream_type = 3
          AND CASE WHEN json_typeof(s.stream_waypoints) = 'array' THEN json_array_length(s.stream_waypoints) > 0 END
        """
    )

    op.execute(
        """
        UPDATE activity_laps l
        SET avg_cadence = CASE WHEN l.avg_cadence <= 510 THEN l.avg_cadence / 2 ELSE l.avg_cadence END,
            max_cadence = CASE WHEN l.max_cadence <= 510 THEN l.max_cadence / 2 ELSE l.max_cadence END
        FROM cadence_spm_migration_rows r
        WHERE r.activity_id = l.activity_id
        """
    )

    op.execute(
        """
        UPDATE activities a
        SET average_cad = r.average_cad,
            max_cad = r.max_cad
        FROM cadence_spm_migration_rows r
        WHERE r.activity_id = a.id
        """
    )

    op.execute("DROP TABLE cadence_spm_migration_rows")
