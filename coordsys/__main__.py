"""Allow `python -m coordsys <args>` as an alias for `python -m coordsys.cli`."""
from .cli import main

if __name__ == "__main__":
    main()
