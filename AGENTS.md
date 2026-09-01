# AutoBuild Factory - project context

## Context authority

This file owns runtime-neutral project context. Provider-specific files import it and contain mechanics only.

Read `README.md` and the relevant document under `docs/` before changing behaviour. Keep each fact in one authoritative place and link to it elsewhere.

## Project boundary

This repository is the public `autobuild-factory` package. It is a curated projection from the private `autobuild-src` build home.

AutoBuild is a harness-neutral workflow driver. It coordinates a tracker, an isolated workspace, implementation, fresh review, and acceptance evidence through adapters.

Product decisions, release approval, and deployment remain outside the driver. Do not add project-specific policy to the portable core.

## Change rules

- Preserve adapter boundaries and deterministic workflow state.
- Extend an existing abstraction before creating a parallel path.
- Keep public content free of private paths, private operational state, and unpublished source context.
- Update the closest owning documentation when a public capability changes.
- Use the verification lane documented in `README.md` for code changes. Instruction-only changes need import, scope, and public-safety checks.

## Release relationship

Changes originate in the private build home and reach this repository through a reviewed public projection. Do not create a separate implementation line here.
