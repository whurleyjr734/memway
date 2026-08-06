"""Allow `python -m memway <args>` as an alias for `python -m memway.cli`."""
from .cli import main

if __name__ == "__main__":
    main()
