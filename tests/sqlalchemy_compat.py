from sqlalchemy.sql import visitors


def with_bind_values(statement, **values):
    """Return a copy of ``statement`` whose named bind parameters carry ``values``.

    SQLAlchemy 2.1 changed ``Executable.params()`` to record execution
    parameters instead of rewriting the bind values, and ``literal_binds``
    compilation does not read those, so ``statement.params(x=1)`` compiles
    ``x`` as NULL there. Rewriting the binds directly is what ``params()`` did
    through SQLAlchemy 2.0 and works on every supported release.
    """

    def visit_bindparam(bind):
        if bind.key in values:
            bind.value = values[bind.key]
            bind.required = False

    return visitors.cloned_traverse(statement, {}, {"bindparam": visit_bindparam})
