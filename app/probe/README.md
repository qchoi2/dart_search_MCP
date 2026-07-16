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

## Stage 0.5

Stage 0.5 has a separate runner with an execution-level request budget, deadline,
PID lock, cancellation file, raw directory, and manifest:

```powershell
python -m app.probe.run_stage0_5 --max-requests 120 --deadline-seconds 1200
```

Raw responses are isolated below `tests/fixtures/probe/raw/<run_id>/` and are
not committed by default. Curated, minimized evidence is written to
`tests/fixtures/probe/golden/stage0_5/`. The runner only measures the five v16
stage-0.5 gates and never starts stage 1.

## Stage 0.6

The v18 residual mini-probe is isolated in `app/probe/stage0_6/`:

```powershell
python -m app.probe.stage0_6 --max-requests 60 --deadline-seconds 900
```

It enforces a hard 60-request cap, concurrency 1, a request-start interval of at
least one second, strict TLS verification, atomic manifests, and separate
`tests/fixtures/probe/stage0_6/raw/` and `golden/` trees. It measures only query
switching, page size, date windows, and the two residual `rm` questions. It never
starts stage 1.
