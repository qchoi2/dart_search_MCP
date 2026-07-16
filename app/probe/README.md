# Stage 0 measurement probes

This directory contains independent measurement scripts only. It is deliberately
separate from future product code.

## Safety and reproducibility

- HTTP concurrency is always 1.
- DART web requests default to a one-second minimum interval.
- The OpenDART key is read only from `DART_API_KEY`, `OPENDART_API_KEY`,
  `OPEN_DART_API_KEY`, or `CRTFC_KEY`.
- The key is masked in every recorded URL and form body and is never written to a
  fixture.
- Raw responses and masked request metadata are written beneath
  `tests/fixtures/probe/`.

## Commands

```powershell
python -m app.probe.run_stage0 --web-only
[Environment]::SetEnvironmentVariable("DART_API_KEY", "<your key>", "User")
python -m app.probe.run_stage0 --opendart-only
python -m unittest tests.test_probe
[Environment]::SetEnvironmentVariable("DART_API_KEY", $null, "User")
```

The full run is also available with `python -m app.probe.run_stage0`. The probe
generates `PROBE_RESULTS.md`, `DECISIONS.md`, `stage0_findings.json`, and an
approval checklist. It does not implement or start the main program.
