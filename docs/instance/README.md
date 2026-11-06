# An instance's own files

Everything that belongs to one homelab stays out of this repository: its notes for an assistant (`INSTANCE.md`),
its runbook, its logos, its own golden test. Keep them in a private repository of your own, checked out at
`instance/` in this one (ignored here):

```text
instance/
├── docs/INSTANCE.md      # ./INSTANCE.md links here (CLAUDE.md imports it)
├── docs/RUNBOOK.md       # ./RUNBOOK.md links here, if you keep one
├── branding/<slug>/      # BRANDING_DIR=instance/branding/<slug> in .env (a copy of branding/default/)
└── tests/instance.env    # your .env with dummy secrets, and tests/golden/ its baseline:
                          #   tests/golden.sh --env instance/tests/instance.env --golden instance/tests/golden
```

Start `INSTANCE.md` from [INSTANCE.example.md](INSTANCE.example.md). Without an instance repository, a copy of it
at the top of the checkout (`INSTANCE.md`, ignored too) does the same.
