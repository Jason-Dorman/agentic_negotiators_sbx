"""The turn executor: one turn per spec section 9.2.

Arrives in stage 2.4 of docs/build_plan.md; see docs/architecture.md section 5.2. The package exists
from stage 2.1 so that the import contract in `.importlinter` can name it and be enforced from the
first backend code.
"""
