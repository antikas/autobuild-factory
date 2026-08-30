"""Allow `python -m autobuild` to run the production command."""

from autobuild.cli import main


raise SystemExit(main())
