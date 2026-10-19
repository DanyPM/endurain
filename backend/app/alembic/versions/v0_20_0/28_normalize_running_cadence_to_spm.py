"""28: normalize stored running cadence to steps per minute (spm)

Running cadence was stored as the source reported it: native Garmin (FIT)
running cadence is a single-leg rate (rpm, around 85) while some treadmill and
footpod files store full spm (around 160). The importers store spm for running
activities, so existing rows are brought to the same unit.

The healing is value-driven, not type-driven: a running activity whose stored
``average_cad`` is below 120 holds rpm and is doubled; one at or above 120
already holds spm and is left untouched. Human running rpm tops out around 110
and running spm starts around 130, so the split is unambiguous.

The per-activity decision keys off ``activities.average_cad`` and is applied
uniformly to the cadence stream (``stream_type = 3``), the per-lap cadence and
the activity scalars. Streams and laps are updated first, so every guard reads
the original ``average_cad``.

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

# The running activity types (1, 2, 3, 34, 40) and the rpm/spm threshold (120)
# are inlined as a snapshot; a migration must not follow later edits to app code.


def upgrade() -> None:
    """Double running cadence still stored as a single-leg rate."""
    op.execute(
        """
        UPDATE activities_streams s
        SET stream_waypoints = (
            SELECT jsonb_agg(
                CASE
                    WHEN elem ? 'cad' AND (elem ->> 'cad') IS NOT NULL
                    THEN jsonb_set(elem, '{cad}', to_jsonb(round((elem ->> 'cad')::numeric * 2)::int))
                    ELSE elem
                END
                ORDER BY ord
            )::json
            FROM jsonb_array_elements(s.stream_waypoints::jsonb) WITH ORDINALITY AS t(elem, ord)
        )
        FROM activities a
        WHERE a.id = s.activity_id
          AND s.stream_type = 3
          AND json_typeof(s.stream_waypoints) = 'array'
          AND json_array_length(s.stream_waypoints) > 0
          AND a.activity_type IN (1, 2, 3, 34, 40)
          AND a.average_cad IS NOT NULL
          AND a.average_cad < 120
        """
    )

    op.execute(
        """
        UPDATE activity_laps l
        SET avg_cadence = l.avg_cadence * 2,
            max_cadence = l.max_cadence * 2
        FROM activities a
        WHERE a.id = l.activity_id
          AND a.activity_type IN (1, 2, 3, 34, 40)
          AND a.average_cad IS NOT NULL
          AND a.average_cad < 120
          AND l.avg_cadence IS NOT NULL
        """
    )

    op.execute(
        """
        UPDATE activities
        SET average_cad = average_cad * 2,
            max_cad = max_cad * 2
        WHERE activity_type IN (1, 2, 3, 34, 40)
          AND average_cad IS NOT NULL
          AND average_cad < 120
        """
    )


def downgrade() -> None:
    """Leave cadence in spm.

    A doubled rpm row (85 to 170) and a natively-spm row (159) are
    indistinguishable afterwards, so the doubling cannot be undone selectively
    without corrupting the rows that were already spm. Re-import the source
    files if the per-leg values are needed.
    """
