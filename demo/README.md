# Publishing demo evidence

Keep raw live reports and selected credentials outside the checkout. Publish
observations through `demo.evidence.write_json`, which the JudgeKit and swarm
smokes use. The HTTP transcript smoke uses the same text redactor. These helpers
only transform local data; they never discover credentials, resolve hosts, call
providers or manage integrations.

```python
from demo.evidence import write_json

report = {"marker": "<ACCEPTANCE_MARKER>", "status": "<OBSERVED_STATUS>", "count": 0}
write_json("demo/<REPORT_NAME>.json", report)
```

The JSON helper copies the input and retains finite observations, booleans,
lists and nulls. It removes endpoint/model/account/project/credential metadata,
URLs, domain names, IP addresses and absolute paths, including common encoded
or escaped forms. Text redaction may also remove domain-like filenames. For
additional private strings, pass `(private_value, public_marker)` pairs as the
third argument. Select those values explicitly from the caller's existing
configuration or in-memory credential; never print them, persist them in TOML,
pass them as command-line arguments or search other credential stores.

Publication validates the complete report before replacing the destination.
Nonfinite numbers, unsupported JSON values, excessive nesting and keys that
collide after redaction fail with fixed errors. Writes use an owner-only
temporary file, fsync and atomic replacement; a failed replacement preserves
the previous report and removes its temporary file.

Keep report schemas limited to the observations needed for acceptance. The
helper cannot identify an arbitrary private word in unrestricted prose: supply
explicit replacements for private labels or selected secrets that do not look
like locations. Offline regressions also check every tracked demo JSON for
unredacted locations. Run `bash scripts/check_no_hardwires.sh` before committing
public evidence. Sanitizing saved data makes no new live acceptance claim.
