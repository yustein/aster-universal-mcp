# Aster regular-Chat wake relay

This component provides a bounded fallback wake path for regular ChatGPT conversations when native MCP events are unavailable on that surface.

Flow:

1. Amsterdam Coordinator persists normal `coord_events`.
2. Its authenticated `/wake-stream` WebSocket sends metadata-only event records.
3. Lenomila's relay consumes the stream from a durable cursor.
4. A dedicated, pruned Firefox profile is controlled through Firefox WebDriver BiDi on loopback only.
5. The relay navigates only configured conversation bindings, verifies the conversation ID and regular Chat surface, inserts a deterministic wake message, and records a durable delivery receipt.
6. Task/result contents remain in the Coordinator; the wake stream carries only task/event metadata.

Security boundaries:

- Never expose the Firefox BiDi port beyond loopback.
- Use a dedicated ChatGPT/OpenAI-only Firefox profile. Do not use the user's general browsing profile.
- Keep the relay secret mode 0600 and off source control.
- Keep conversation IDs and canonical conversation URLs out of public configuration examples.
- A browser wake is not a native-event claim.
- ChatGPT plugin permission prompts are separate from wake delivery; do not silently choose persistent "Always allow" permissions.

Production acceptance requires evidence for both legs separately: Coordinator -> regular Chat worker, and completed worker result -> main regular Chat conversation.
