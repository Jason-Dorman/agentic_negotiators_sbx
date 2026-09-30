"""Receipt polling, log decoding, block-hash tracking, the canonical flag, reorg detection and
rebuild.

Arrives in stage 2.3 of docs/build_plan.md; see docs/architecture.md sections 3.2 and 5.5. The
package exists from stage 2.1 so that the import contract in `.importlinter` can name it and be
enforced from the first backend code.
"""
