"""The web3 adapter: blocking calls behind a thread executor, ABI loading, event and custom-error
decoding. Depends only on the protocol package.

Arrives in stage 2.3 of docs/build_plan.md; see docs/architecture.md section 3.2. The package exists
from stage 2.1 so that the import contract in `.importlinter` can name it and be enforced from the
first backend code.
"""
