"""Relay nonce management, gas, signing raw transactions with the relay key, the durable outbox
(persist before broadcast), rebroadcast.

Arrives in stage 2.3 of docs/build_plan.md; see docs/architecture.md sections 3.2 and 5.2. The
package exists from stage 2.1 so that the import contract in `.importlinter` can name it and be
enforced from the first backend code.
"""
