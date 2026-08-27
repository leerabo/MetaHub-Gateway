#!/usr/bin/env python3
"""Tenant-neutral entry point included in the signed PRD-124 release."""

from metahub_shared.installer.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
