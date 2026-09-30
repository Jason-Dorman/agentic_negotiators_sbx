"""The run lifecycle state machine of architecture.md section 6.1: lease, pause and resume, abort,
clone, recovery.

Arrives in stage 2.4 of docs/build_plan.md; see docs/architecture.md sections 5.4 and 6.1. The
package exists from stage 2.1 so that the import contract in `.importlinter` can name it and be
enforced from the first backend code.
"""
