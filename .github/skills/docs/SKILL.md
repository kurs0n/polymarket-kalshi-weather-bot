---
name: docs
description: 'Use when working in front of API schemas and endpoints to document them in Swagger/OpenAPI and keep the backend contract aligned with the frontend.'
---

# API schemas documentation

Use this skill when you are changing FastAPI routes, Pydantic response models, request payloads, error contracts, or endpoint documentation in the Swagger UI.

## Project-specific guidance

- The backend API entrypoint is in [backend/api/main.py](backend/api/main.py). Keep route docs consistent with the actual runtime behavior and the data returned by the scheduler/market scanners.
- The dashboard consumes API payloads from [frontend/src/api.ts](frontend/src/api.ts) and expects matching shapes in [frontend/src/types.ts](frontend/src/types.ts). Update both sides together when a field, enum, or response contract changes.
- Preserve business semantics such as YES/NO outcomes, Fahrenheit temperature units, and simulation-only behavior. Swagger docs should clarify what is real data vs simulated output.
- Avoid documenting internal implementation details that are not part of the stable API contract. Prefer user-facing descriptions and explicit field meaning.

## Documentation standards

- Add a clear summary and description to each route so the Swagger page explains the purpose, trigger, and output.
- Use Pydantic models to document request and response bodies instead of raw dicts.
- Add Field descriptions for all important properties, especially units, enums, and edge-case semantics.
- Document response codes explicitly, especially 200, 400, 404, and 500. Explain what each code means to API consumers.
- Keep examples realistic and aligned with the current payloads returned by the service.
- Mark optional fields clearly and avoid implying a field is always present unless it is guaranteed.

## Endpoint documentation checklist

Before finalizing API changes, verify:

1. The path, method, and summary match the actual implementation.
2. Response models reflect the real structure returned by the route.
3. Nullable fields are represented accurately.
4. Enum-like fields (for example platform, direction, metric) are described clearly.
5. Data semantics such as threshold temperature, city_key, target_date, and yes/no pricing are explained in plain language.
6. The frontend types still match the backend contract after the change.

## Swagger/OpenAPI best practices

- Prefer response models over ad-hoc dicts for stable contracts.
- Use descriptive names for schemas so they read naturally in the generated docs.
- Keep field names and types consistent across the whole app to avoid confusion in generated examples.
- If a route is simulation-only, say so in the description and notes to avoid mistaken assumptions about live execution.
- If a field is derived from forecast data, note whether it is model output, market output, or an internally computed edge metric.

## Example conventions for this project

- Weather market endpoints should explain city, target date, threshold temperature, metric, and direction.
- Signal endpoints should describe model probability, market probability, edge, confidence, and suggested size.
- Trade and settlement payloads should document P&L, platform, direction, entry price, and whether the trade is settled.
- Dashboard endpoints should emphasize that they return aggregated stats and recent market/signal snapshots for UI rendering.

## Maintainership rules

- If you change a payload shape, update the route model and the frontend TypeScript contract in the same change.
- If a route changes risk semantics or simulation mode behavior, reflect that in the description and examples.
- If you discover stale docs, fix them in the same patch rather than leaving mismatched Swagger output behind.

## Quick reminder

When writing or updating API docs, the goal is not just Swagger completeness—it is contract clarity. The docs should help both developers and UI consumers understand the exact meaning of every field without needing to inspect the backend implementation details.