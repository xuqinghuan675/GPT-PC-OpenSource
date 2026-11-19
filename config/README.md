# Local configuration

This directory contains safe templates only.

- machine.example.toml can be copied to machine.local.toml for machine-specific paths and settings.
- example.env lists optional environment variables. It is a template and is not automatically loaded.
- Gmail OAuth client and token files are created locally and must never be committed.

Each computer should create its own browser profile, OAuth consent, local auth keys, runtime state, and machine configuration. Do not copy those files between machines.
