# Template companion

This directory is a neutral template for a companion an install names itself.
The rules are ones a session can inhabit, with no personal name, place, person,
or private history.

Copy `SOUL.md` and `CORTEX.md` into the Tessera home, edit the copies, and
point `identity_file` at them:

```toml
identity_file = ["SOUL.md", "CORTEX.md"]
```

Paths are relative to the home. An empty `identity_file` loads nothing. This
template is not applied on its own, and the copies in the home are the files
an install should change.

Keep the copies that hold a real person's life in the home. They are not part
of this tree. The files in this directory stay generic.
