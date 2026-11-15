# Compute Factory project instructions

## Living documentation

Documentation is living, breathing project material and must be kept current at all times.

- Treat stale or contradictory documentation as a defect, not as a follow-up task.
- Before editing code, inspect the relevant documentation in this repository and the affected
  sibling repository. Before finishing, update every README, design document, contract, example,
  workflow note, and operational procedure made inaccurate or incomplete by the change.
- Keep behavior, interfaces, configuration, security boundaries, lifecycle commands, UI behavior,
  and verification evidence synchronized with the implementation. Record important limitations and
  unknowns instead of implying that an unverified behavior works.
- Review documentation changes in the same verification pass as code changes. Do not land a code
  change with a known documentation mismatch.

The cross-repository contract is defined in `CONTRACTS.md`; update it when a shared annotation,
workflow input/output, environment variable, or file contract changes.
