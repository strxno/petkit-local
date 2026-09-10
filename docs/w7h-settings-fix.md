# W7H panel settings delivery

## Evidence

On September 5, 2026 the W7H authenticated at 12:31:20 UTC and sent an
HTTP heartbeat with `iotStatus=0` 23 seconds later. That cleared the routing
flag despite a connected, subscribed broker session. MQTT traffic continued
for days, while panel settings waited in an unused HTTP queue.

## Implemented

- Fresh traffic on the current authenticated broker session takes precedence
  over an HTTP report of no MQTT connection. Stale sessions still expire.
- Incoming packets restore a cleared routing flag only for the current connected
  session. Disconnect packets and superseded sessions cannot restore it.
- Panel scalar settings and range schedules keep pending wire values in the
  existing atomic device registry. New edits replace older pending values.
- The local MQTT bridge retries pending settings when connected. HTTP heartbeats
  can also deliver them after a restart. One-shot actions are not retried.
- Panel feedback distinguishes saving/queuing from sending, and does not claim
  that a sent setting was applied. Quiet-period descriptions explain muting,
  refill suppression, and alarm-light suppression separately.

## Validation and rollout

Regression coverage includes the late HTTP zero, routing recovery, failed sends,
latest-edit replacement, empty quiet periods, and restart delivery. Apply the
updated container build before testing on hardware; these workspace changes do
not modify a running container.

After updating, verify W7H remains MQTT-connected after HTTP status reports,
save a voice setting, and check pending settings clear after MQTT publication.
For audio all day, enable Voice Prompt and disable Quiet Voice Prompts. Confirm
the actual audio behavior on the device; MQTT publication is not an application
acknowledgment.

Previously saved values are preserved. Commands queued by the old version were
not durable: re-save any affected setting after updating. No cloud proxy changes
are included. Device acknowledgment tracking and physical execution verification
remain outside the delivery guarantees of this fix.
