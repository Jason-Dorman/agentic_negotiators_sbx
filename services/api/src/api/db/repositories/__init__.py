"""SQL implementations of the repository protocols in `api.db.protocols`.

Nothing outside `api.db` imports these: upper layers depend on the protocols and receive
implementations through a `UnitOfWork`. The one exception the architecture allows is the
composition root, which constructs `api.db.Database`.
"""
