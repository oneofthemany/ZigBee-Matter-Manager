# Learning recipes

One JSON file per kind of device, read by `modules/learning_recipes.py`
(`docs/plans/device-learning.md` §4). A user's own recipes go in
`<data>/learning_recipes/`; one with the same `id` replaces the shipped one.

A recipe orders instructions and names inference operations from
`modules/learning_ops.OPS`; loading rejects unknown operations, malformed
fields and windows outside 5–300 s. Each operation's proposals must fall
inside the step's `yields`.
