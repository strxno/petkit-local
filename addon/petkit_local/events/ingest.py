"""One import surface over the three modules that do the event work.

- `events/normalize.py` — transport in: an HTTP form, an MQTT frame or a file
  notification becomes a row.
- `events/sessions.py` — rows out: grouping a visit's several reports into one
  session, and the Timeline's filters.
- `events/migrations.py` — one-shot repair of rows already stored.

It exists because most callers and nearly every test reach these through the
module object (`from petkit_local.events import ingest`, then `ingest.<name>`),
and an alias here is cheaper than rewriting all of them. Nothing new belongs in
this file: add it to the module that owns it.
"""
from __future__ import annotations

from petkit_local.events.migrations import backfill_event_rows
from petkit_local.events.normalize import (CATEGORY_CLOUD_DOUBLE, CATEGORY_HEALTH,
                                           CATEGORY_TO_CAPABILITY, CATEGORY_WASTE_CHECK,
                                           CONTENT_KEYS, EVENT_ID_KEYS,
                                           EVENT_TYPE_KEYS, KIND_TO_ENTITY,
                                           MQTT_ENVELOPE_KEYS, STATE_KEYS, _as_dict,
                                           _extract_pet_ref, _extract_score,
                                           _MODULE_TYPE_TO_CATEGORY, apply_derived_state,
                                           apply_state_snapshot, capability_for_category,
                                           classify_event_kind, cleaning_label,
                                           entity_for_event, event_type_label,
                                           from_event_report, from_file_info, from_mqtt,
                                           is_detail_event, parse_event_report_form,
                                           telemetry_only)
from petkit_local.events.sessions import (CYCLE_TAIL_WINDOW_SEC, SUB_EVENT_WINDOW_SEC,
                                          _duration_of, _recalc_weight_of, _started_at,
                                          _weight_of, content_of_row, filter_counts,
                                          group_sessions, matches_filter, state_of_row)

#: Also what marks the imports above as used, so the lint gate does not read a
#: re-export as dead code. The private names are here because callers outside
#: the package already reached for them: `main.py` for the media category
#: table, the tests for the content decoder and the pet-id extractors, and
#: `events/metrics.py` for the same weight/duration/start-time extraction
#: `_session_from_visit` already does — reusing it there keeps "how a visit's
#: weight and duration are read off the wire" defined in exactly one place.
__all__ = [
    "CATEGORY_CLOUD_DOUBLE",
    "CATEGORY_HEALTH",
    "CATEGORY_TO_CAPABILITY",
    "CATEGORY_WASTE_CHECK",
    "CONTENT_KEYS",
    "CYCLE_TAIL_WINDOW_SEC",
    "EVENT_ID_KEYS",
    "EVENT_TYPE_KEYS",
    "KIND_TO_ENTITY",
    "MQTT_ENVELOPE_KEYS",
    "STATE_KEYS",
    "SUB_EVENT_WINDOW_SEC",
    "_MODULE_TYPE_TO_CATEGORY",
    "_as_dict",
    "_duration_of",
    "_extract_pet_ref",
    "_extract_score",
    "_recalc_weight_of",
    "_started_at",
    "_weight_of",
    "apply_derived_state",
    "apply_state_snapshot",
    "backfill_event_rows",
    "capability_for_category",
    "classify_event_kind",
    "cleaning_label",
    "content_of_row",
    "entity_for_event",
    "event_type_label",
    "filter_counts",
    "from_event_report",
    "from_file_info",
    "from_mqtt",
    "group_sessions",
    "is_detail_event",
    "matches_filter",
    "parse_event_report_form",
    "state_of_row",
    "telemetry_only",
]
