# Module comments guide

Comments should explain contracts and invariants that are easy to break, rather
than repeat the code. Keep module headers short and link to
[`docs/protocol-notes.md`](protocol-notes.md) for public interoperability scope.

## What a useful module header contains

1. **Responsibility:** the service or format implemented, and its boundary.
2. **Ownership:** thread, connection, cipher or UI resources owned by the caller.
3. **Failure semantics:** unsupported data, cancellation and uncertain outcomes.
4. **Evidence scope:** a link to a public summary, with important unknowns retained.

For example:

```python
"""Single-owner codec for one bounded service.

The worker supplies an authenticated channel; this module does not access widgets.
Unsupported records fail explicitly. Cancellation requests stop future work and
does not undo an already dispatched operation. See docs/protocol-notes.md for scope.
"""
```

## Keep these distinctions explicit

- A codec decoding a record does not prove a GUI feature or live interoperability.
- Queued input is not acknowledged input; an acknowledged power request is not an
  observed state transition.
- A local resource limit is not necessarily a protocol limit.
- Clearing a Python reference is not secure erasure.
- A preflight filename check is not atomic create-new.
- Offscreen and synthetic tests do not certify native desktop behavior.

Use function/class docstrings for parameters, return values, raised exceptions
and lifecycle requirements. Put inline comments near non-obvious bounds, byte
order, queue overflow policy and cancellation races. Avoid embedding credentials,
real endpoints, private filesystem locations, proprietary dumps or references to
unpublished research files. A source-level observation can be summarized without
publishing its private capture.

For documentation-only source cleanup, edit only the opening docstring or comments.
Do not reformat or alter executable statements. Verify that the parsed program is
unchanged apart from those documentation nodes. Behavioral improvements belong in
a separate reviewed change with relevant tests.
